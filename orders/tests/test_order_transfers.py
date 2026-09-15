"""Transferring a held order to a warehouse with stock — F45, hold-complete.

The rule under test is page 8 of the 14 August pack:

    Backorder process
    1. Orders held until enough inventory is received to release a picklist
    2. Orders released in a FIFO sequence
    3. Option to transfer an order to another warehouse with Inventory

So an order short of stock is held **whole** — nothing ships, no pick list is
generated — and what moves to another warehouse is the order itself, not a
per-SKU shortfall row. The `Backorder` model covers the other reading, where
the warehouse part-fills; both exist because AsOne have not confirmed which.

The test that matters most here is
`test_the_schools_own_warehouse_is_unchanged`. Decision D2 is two rules —
ordering is fixed to the school's primary warehouse, fulfilment may move —
and that test is the one that fails if somebody collapses them.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from inventory.models import MovementType, StockMovement, StockStatus
from inventory.services import post_movement, stock_level
from orders.models.school_orders import OrderStatus
from orders.services import (
    NoStockToFill,
    OrderNotTransferable,
    orders_awaiting_stock,
    pick_order,
    place_order,
    release_order,
    transfer_order,
    warehouses_that_could_fill_order,
)

IN_FORCE = date(2026, 1, 1)
ORDERED_ON = date(2026, 11, 10)
Role = User.Role


class TransferSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.school = self.sites["school_a"]          # orders from Namayemba
        self.namayemba = self.sites["namayemba"]
        self.serere = self.sites["serere"]

        self.chrisis = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)
        self.julius = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.namayemba)
        self.joan = make_user("joan", Role.WAREHOUSE_STAFF, warehouse=self.serere)
        self.sharon = make_user("sharon", Role.PROGRAM_LEAD)

        self.shirt = self.priced_sku("White Shirt", "25000.00")
        self.socks = self.priced_sku("Socks", "5000.00")

    def priced_sku(self, name, price):
        garment = Garment.objects.create(name=name)
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal(price), active_date=IN_FORCE
        )
        return Sku.objects.create(
            garment=garment,
            size=Size.objects.create(name=f"{name[:6]}-10", sort_order=10),
        )

    def stock(self, sku, quantity, warehouse):
        post_movement(
            warehouse=warehouse, sku=sku, quantity=quantity,
            movement_type=MovementType.RECEIPT, unit_value=Decimal("25000.00"),
            document_number="RC-100001", occurred_on=IN_FORCE, created_by=self.julius,
        )

    def released_order(self, **lines):
        """A paid order sitting with the warehouse, which is when a transfer
        becomes meaningful."""
        order = place_order(
            school=self.school,
            student_name="Miriam Achieng",
            order_date=ORDERED_ON,
            skus=[
                {"sku": getattr(self, name), "quantity": qty}
                for name, qty in lines.items()
            ],
            created_by=self.chrisis,
        )
        return release_order(order, released_by=self.chrisis)

    def held_order(self):
        """Namayemba has nothing; Serere has plenty. The p.8 case."""
        self.stock(self.shirt, 20, self.serere)
        return self.released_order(shirt=5)


class AnOrderShortOfStockIsHeldWhole(TransferSetup):
    """F43 under the pack's rule — nothing part-ships."""

    def test_picking_is_refused_rather_than_partial(self):
        order = self.held_order()

        with self.assertRaises(Exception) as caught:
            pick_order(order, picked_by=self.julius)

        self.assertIn("Not enough stock", str(caught.exception))

    def test_no_stock_moves_at_all(self):
        order = self.held_order()

        try:
            pick_order(order, picked_by=self.julius)
        except Exception:
            pass

        self.assertEqual(
            StockMovement.objects.filter(document_number=order.number).count(), 0
        )

    def test_the_order_stays_released_not_picked(self):
        order = self.held_order()

        try:
            pick_order(order, picked_by=self.julius)
        except Exception:
            pass

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.RELEASED)


class TheWaitingQueue(TransferSetup):
    """F44 — "orders released in a FIFO sequence"."""

    def test_a_held_order_appears_with_what_it_is_waiting_on(self):
        order = self.held_order()

        waiting = orders_awaiting_stock()

        self.assertEqual([entry["order"].pk for entry in waiting], [order.pk])
        self.assertEqual(waiting[0]["shortfalls"][0]["sku"], self.shirt)
        self.assertEqual(waiting[0]["shortfalls"][0]["shortfall"], 5)

    def test_an_order_that_can_be_filled_is_not_waiting(self):
        self.stock(self.shirt, 50, self.namayemba)
        self.released_order(shirt=5)

        self.assertEqual(orders_awaiting_stock(), [])

    def test_the_queue_is_in_the_order_the_schools_placed_them(self):
        self.stock(self.shirt, 20, self.serere)
        first = self.released_order(shirt=5)
        second = self.released_order(shirt=5)
        third = self.released_order(shirt=5)

        waiting = [entry["order"].pk for entry in orders_awaiting_stock()]

        self.assertEqual(waiting, [first.pk, second.pk, third.pk])

    def test_it_narrows_to_one_warehouse(self):
        self.held_order()

        self.assertEqual(len(orders_awaiting_stock(warehouse=self.namayemba)), 1)
        self.assertEqual(orders_awaiting_stock(warehouse=self.serere), [])


class WhoCouldFillIt(TransferSetup):
    """F45's shortlist."""

    def test_only_warehouses_holding_every_line(self):
        self.stock(self.shirt, 20, self.serere)
        self.stock(self.socks, 1, self.serere)          # not enough socks
        order = self.released_order(shirt=5, socks=4)

        self.assertEqual(warehouses_that_could_fill_order(order), [])

    def test_a_warehouse_holding_everything_is_offered(self):
        self.stock(self.shirt, 20, self.serere)
        self.stock(self.socks, 20, self.serere)
        order = self.released_order(shirt=5, socks=4)

        self.assertEqual(warehouses_that_could_fill_order(order), [self.serere])

    def test_the_warehouse_already_responsible_is_never_offered(self):
        self.stock(self.shirt, 50, self.namayemba)
        order = self.released_order(shirt=5)

        self.assertNotIn(self.namayemba, warehouses_that_could_fill_order(order))


class TransferringTheOrder(TransferSetup):
    """F45 proper."""

    def test_fulfilment_moves_to_the_new_warehouse(self):
        order = self.held_order()

        transfer_order(order, warehouse=self.serere, transferred_by=self.julius)

        order.refresh_from_db()
        self.assertEqual(order.warehouse, self.serere)
        self.assertTrue(order.was_transferred)

    def test_the_schools_own_warehouse_is_unchanged(self):
        """D2 is two rules. Ordering does not move because fulfilment did."""
        order = self.held_order()

        transfer_order(order, warehouse=self.serere, transferred_by=self.julius)

        self.school.refresh_from_db()
        self.assertEqual(self.school.primary_warehouse, self.namayemba)

    def test_nothing_moves_in_the_ledger(self):
        order = self.held_order()
        before = StockMovement.objects.count()

        transfer_order(order, warehouse=self.serere, transferred_by=self.julius)

        self.assertEqual(StockMovement.objects.count(), before)
        self.assertEqual(stock_level(self.shirt, self.serere), 20)

    def test_who_moved_it_and_why_are_recorded(self):
        order = self.held_order()

        transfer_order(
            order,
            warehouse=self.serere,
            transferred_by=self.julius,
            reason="Namayemba out of shirts until the next TC delivery",
        )

        order.refresh_from_db()
        self.assertEqual(order.transferred_by, self.julius)
        self.assertIsNotNone(order.transferred_at)
        self.assertIn("Namayemba out of shirts", order.transfer_reason)

    def test_the_new_warehouse_can_then_pick_it(self):
        order = self.held_order()
        transfer_order(order, warehouse=self.serere, transferred_by=self.julius)
        order.refresh_from_db()

        pick_order(order, picked_by=self.joan)

        self.assertEqual(
            stock_level(self.shirt, self.serere, stock_status=StockStatus.PICK), 5
        )
        self.assertEqual(stock_level(self.shirt, self.namayemba), 0)

    def test_it_leaves_the_waiting_queue(self):
        order = self.held_order()

        transfer_order(order, warehouse=self.serere, transferred_by=self.julius)

        self.assertEqual(orders_awaiting_stock(), [])


class TransfersThatMustBeRefused(TransferSetup):
    def test_a_warehouse_that_cannot_fill_it_either(self):
        self.stock(self.shirt, 2, self.serere)
        order = self.released_order(shirt=5)

        with self.assertRaises(NoStockToFill) as caught:
            transfer_order(order, warehouse=self.serere, transferred_by=self.julius)

        self.assertIn("needs 5", str(caught.exception))
        self.assertIn("has 2", str(caught.exception))

    def test_the_warehouse_already_filling_it(self):
        self.stock(self.shirt, 50, self.namayemba)
        order = self.released_order(shirt=5)

        with self.assertRaises(OrderNotTransferable):
            transfer_order(order, warehouse=self.namayemba, transferred_by=self.julius)

    def test_an_order_already_picked(self):
        """Stock is reserved at the old warehouse; re-pointing the order
        would strand that reservation."""
        self.stock(self.shirt, 50, self.namayemba)
        self.stock(self.shirt, 50, self.serere)
        order = self.released_order(shirt=5)
        pick_order(order, picked_by=self.julius)

        with self.assertRaises(OrderNotTransferable) as caught:
            transfer_order(order, warehouse=self.serere, transferred_by=self.julius)

        self.assertIn("already been picked", str(caught.exception))

    def test_an_unpaid_order(self):
        self.stock(self.shirt, 20, self.serere)
        order = place_order(
            school=self.school,
            student_name="Miriam Achieng",
            order_date=ORDERED_ON,
            skus=[{"sku": self.shirt, "quantity": 5}],
            created_by=self.chrisis,
        )

        with self.assertRaises(OrderNotTransferable) as caught:
            transfer_order(order, warehouse=self.serere, transferred_by=self.julius)

        self.assertIn("not been paid for", str(caught.exception))


class WhoSeesATransferredOrder(TransferSetup):
    """D2: the warehouse doing the filling must see what it is filling,
    including for a school that is not "theirs"."""

    def url(self, order):
        """Availability, not the order detail.

        Warehouse Staff never retrieve a school order directly — that is the
        School Orders Entry column and the matrix does not give it to them.
        They reach an order through the warehouse actions, so that is where
        scoping has to be right, and where it is tested.
        """
        return reverse("orders:school-order-availability", args=[order.pk])

    def test_the_receiving_warehouse_can_see_it(self):
        order = self.held_order()
        transfer_order(order, warehouse=self.serere, transferred_by=self.julius)

        self.client.force_authenticate(self.joan)          # Serere
        response = self.client.get(self.url(order))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_warehouse_that_gave_it_away_no_longer_sees_it(self):
        order = self.held_order()
        transfer_order(order, warehouse=self.serere, transferred_by=self.julius)

        self.client.force_authenticate(self.julius)        # Namayemba
        response = self.client.get(self.url(order))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_before_a_transfer_it_is_the_other_way_round(self):
        """The same two assertions, inverted, so a scoping rule that ignored
        the transfer entirely could not pass both classes."""
        order = self.held_order()

        self.client.force_authenticate(self.julius)
        self.assertEqual(self.client.get(self.url(order)).status_code, status.HTTP_200_OK)

        self.client.force_authenticate(self.joan)
        self.assertEqual(
            self.client.get(self.url(order)).status_code, status.HTTP_404_NOT_FOUND
        )

    def test_the_school_still_sees_its_own_order(self):
        order = self.held_order()
        transfer_order(order, warehouse=self.serere, transferred_by=self.julius)

        self.client.force_authenticate(self.chrisis)
        response = self.client.get(
            reverse("orders:school-order-detail", args=[order.pk])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)


class TheTransferEndpoints(TransferSetup):
    def test_candidates_lists_a_warehouse_that_could_fill_it(self):
        order = self.held_order()

        self.client.force_authenticate(self.julius)
        response = self.client.get(
            reverse("orders:school-order-transfer-candidates", args=[order.pk])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([w["name"] for w in response.data], [self.serere.name])

    def test_transfer_moves_the_order(self):
        order = self.held_order()

        self.client.force_authenticate(self.julius)
        response = self.client.post(
            reverse("orders:school-order-transfer", args=[order.pk]),
            {"warehouse": self.serere.pk, "reason": "No shirts here"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.warehouse, self.serere)

    def test_a_school_user_cannot_transfer(self):
        order = self.held_order()

        self.client.force_authenticate(self.chrisis)
        response = self.client.post(
            reverse("orders:school-order-transfer", args=[order.pk]),
            {"warehouse": self.serere.pk},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_the_refusal_says_what_is_short(self):
        self.stock(self.shirt, 2, self.serere)
        order = self.released_order(shirt=5)

        self.client.force_authenticate(self.julius)
        response = self.client.post(
            reverse("orders:school-order-transfer", args=[order.pk]),
            {"warehouse": self.serere.pk},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("needs 5", response.data["detail"])


class TheAwaitingStockEndpoint(TransferSetup):
    """F43, F44 over HTTP."""

    def url(self):
        return reverse("orders:orders-awaiting-stock")

    def test_a_clerk_sees_their_held_orders_and_what_they_wait_on(self):
        order = self.held_order()

        self.client.force_authenticate(self.julius)
        response = self.client.get(self.url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["order"]["number"], order.number)
        self.assertEqual(response.data[0]["waiting_on"][0]["shortfall"], 5)

    def test_another_warehouse_sees_nothing(self):
        self.held_order()

        self.client.force_authenticate(self.joan)
        response = self.client.get(self.url())

        self.assertEqual(response.data, [])

    def test_a_transferred_order_moves_to_the_new_warehouses_queue(self):
        """It should leave Namayemba's queue entirely — Serere can fill it,
        so it is nobody's backlog any more."""
        order = self.held_order()
        transfer_order(order, warehouse=self.serere, transferred_by=self.julius)

        self.client.force_authenticate(self.julius)
        self.assertEqual(self.client.get(self.url()).data, [])

        self.client.force_authenticate(self.joan)
        self.assertEqual(self.client.get(self.url()).data, [])

    def test_a_school_user_is_refused(self):
        self.held_order()

        self.client.force_authenticate(self.chrisis)
        response = self.client.get(self.url())

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

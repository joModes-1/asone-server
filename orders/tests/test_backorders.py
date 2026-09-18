"""Backorders — F43, F44, F45, F46.

The rule under test is decision D2, Jim on 24 August:

    ordering    a school orders on its primary warehouse and no other
    fulfilment  a backorder may be filled by any warehouse with stock,
                shipping **direct to the school**

Those two must not collapse into each other. The test that matters most is
`test_the_stock_never_touches_the_schools_own_warehouse` — it is the one that
would fail if somebody "simplified" fulfilment into a transfer.
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
from orders.models import Backorder
from orders.models.backorders import BackorderStatus
from orders.models.school_orders import OrderStatus
from orders.services import (
    CannotAssign,
    NoStockToFill,
    NothingToPick,
    assign_backorder,
    fill_backorder,
    open_backorders,
    pick_available,
    pick_order,
    place_order,
    release_order,
    warehouses_that_could_fill,
)

IN_FORCE = date(2026, 1, 1)
ORDERED_ON = date(2026, 11, 10)
Role = User.Role


class BackorderSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.school = self.sites["school_a"]          # orders from Namayemba
        self.namayemba = self.sites["namayemba"]
        self.serere = self.sites["serere"]

        self.clerk = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)
        self.julius = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.namayemba)
        self.joan = make_user("joan", Role.WAREHOUSE_STAFF, warehouse=self.serere)
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.finance = make_user("musana", Role.FINANCE)

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

    def order_for(self, **lines):
        """`shirt=5, socks=2` -> an order for those quantities, paid for.

        Released here because everything in this file happens *after* the
        warehouse starts work on an order, and neither door into the
        warehouse — `pick_order` or `pick_available` — opens for an unpaid
        one. The payment gate itself is tested in `test_release.py`.
        """
        order = place_order(
            school=self.school,
            student_name="Miriam Achieng",
            order_date=ORDERED_ON,
            skus=[
                {"sku": getattr(self, name), "quantity": qty}
                for name, qty in lines.items()
            ],
            created_by=self.clerk,
        )
        return release_order(order, released_by=self.finance)

    def short_pick(self):
        """Namayemba has 3 shirts; the school ordered 5. Two are owed."""
        self.stock(self.shirt, 3, self.namayemba)
        order = self.order_for(shirt=5)
        return pick_available(order, picked_by=self.julius)


class PickingWhatIsThereRaisesTheRest(BackorderSetup):
    """F43."""

    def test_the_available_units_are_reserved(self):
        order, _ = self.short_pick()

        self.assertEqual(
            stock_level(self.shirt, self.namayemba, stock_status=StockStatus.PICK), 3
        )
        self.assertEqual(stock_level(self.shirt, self.namayemba), 0)

    def test_the_shortfall_becomes_a_backorder(self):
        _, backorders = self.short_pick()

        self.assertEqual(len(backorders), 1)
        self.assertEqual(backorders[0].sku, self.shirt)
        self.assertEqual(backorders[0].quantity, 2)
        self.assertEqual(backorders[0].status, BackorderStatus.OPEN)

    def test_the_order_is_picked(self):
        order, _ = self.short_pick()

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.PICKED)

    def test_a_fully_available_order_raises_nothing(self):
        self.stock(self.shirt, 10, self.namayemba)
        order = self.order_for(shirt=4)

        _, backorders = pick_available(order, picked_by=self.julius)

        self.assertEqual(backorders, [])

    def test_only_the_short_line_is_backordered(self):
        self.stock(self.shirt, 1, self.namayemba)
        self.stock(self.socks, 50, self.namayemba)
        order = self.order_for(shirt=4, socks=2)

        _, backorders = pick_available(order, picked_by=self.julius)

        self.assertEqual([b.sku for b in backorders], [self.shirt])
        self.assertEqual(
            stock_level(self.socks, self.namayemba, stock_status=StockStatus.PICK), 2
        )

    def test_an_order_with_nothing_available_is_refused(self):
        """Not a partial pick. Marking it Picked would be untrue."""
        order = self.order_for(shirt=5)

        with self.assertRaises(NothingToPick):
            pick_available(order, picked_by=self.julius)

    def test_pick_order_still_refuses_a_short_order(self):
        """`pick_available` is a new door, not a change to the old one —
        Denis's F39 behaviour is untouched."""
        from orders.services import OrderNotFillable

        self.stock(self.shirt, 3, self.namayemba)
        order = self.order_for(shirt=5)

        with self.assertRaises(OrderNotFillable):
            pick_order(order, picked_by=self.julius)


class FindingSomewhereToFillIt(BackorderSetup):
    """F45's shortlist — a clerk cannot see another site's shelves."""

    def test_a_warehouse_with_enough_is_offered(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 20, self.serere)

        self.assertIn(self.serere, warehouses_that_could_fill(backorders[0]))

    def test_a_warehouse_without_enough_is_not(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 1, self.serere)  # backorder needs 2

        self.assertEqual(warehouses_that_could_fill(backorders[0]), [])

    def test_the_warehouse_that_ran_short_is_never_offered(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 99, self.namayemba)

        self.assertNotIn(self.namayemba, warehouses_that_could_fill(backorders[0]))


class AssigningABackorder(BackorderSetup):
    """F45. Nothing moves in the ledger — what changes is who owes the school."""

    def test_assigning_records_the_warehouse(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 20, self.serere)

        assigned = assign_backorder(
            backorders[0], warehouse=self.serere, assigned_by=self.joan
        )

        self.assertEqual(assigned.status, BackorderStatus.ASSIGNED)
        self.assertEqual(assigned.filled_by_warehouse, self.serere)
        self.assertIsNotNone(assigned.assigned_at)

    def test_assigning_moves_no_stock(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 20, self.serere)
        before = stock_level(self.shirt, self.serere)

        assign_backorder(backorders[0], warehouse=self.serere, assigned_by=self.joan)

        self.assertEqual(stock_level(self.shirt, self.serere), before)

    def test_a_warehouse_without_the_stock_is_refused(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 1, self.serere)

        with self.assertRaises(NoStockToFill):
            assign_backorder(backorders[0], warehouse=self.serere, assigned_by=self.joan)

    def test_the_originating_warehouse_is_refused(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 99, self.namayemba)

        with self.assertRaises(CannotAssign):
            assign_backorder(
                backorders[0], warehouse=self.namayemba, assigned_by=self.julius
            )

    def test_it_cannot_be_assigned_twice(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 20, self.serere)
        assign_backorder(backorders[0], warehouse=self.serere, assigned_by=self.joan)

        with self.assertRaises(CannotAssign):
            assign_backorder(backorders[0], warehouse=self.serere, assigned_by=self.joan)


class FillingItShipsDirect(BackorderSetup):
    """F46, and the half of D2 that overrides the definitions page."""

    def assigned_backorder(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 20, self.serere)
        return assign_backorder(
            backorders[0], warehouse=self.serere, assigned_by=self.joan
        )

    def test_filling_produces_a_shipment_from_the_filling_warehouse(self):
        backorder = self.assigned_backorder()

        shipment = fill_backorder(backorder, filled_by=self.joan)

        self.assertEqual(shipment.from_warehouse, self.serere)
        # F42: the order is on the line now, not the shipment — a van can
        # carry several. A direct backorder fill is a one-order van.
        self.assertEqual(shipment.school, backorder.order.school)
        self.assertEqual(list(shipment.orders), [backorder.order])
        self.assertEqual(shipment.lines.get().quantity, 2)

    def test_the_stock_never_touches_the_schools_own_warehouse(self):
        """The rule this whole design exists for. If somebody ever
        'simplifies' fulfilment into a warehouse transfer, this fails."""
        backorder = self.assigned_backorder()
        namayemba_rows_before = StockMovement.objects.filter(
            warehouse=self.namayemba
        ).count()

        fill_backorder(backorder, filled_by=self.joan)

        self.assertEqual(
            StockMovement.objects.filter(warehouse=self.namayemba).count(),
            namayemba_rows_before,
        )

    def test_stock_leaves_the_filling_warehouse(self):
        backorder = self.assigned_backorder()
        before = stock_level(self.shirt, self.serere)

        fill_backorder(backorder, filled_by=self.joan)

        self.assertEqual(stock_level(self.shirt, self.serere), before - 2)
        self.assertEqual(
            stock_level(self.shirt, self.serere, stock_status=StockStatus.SHIPPED), 2
        )

    def test_the_backorder_is_marked_filled(self):
        backorder = self.assigned_backorder()

        fill_backorder(backorder, filled_by=self.joan)

        backorder.refresh_from_db()
        self.assertEqual(backorder.status, BackorderStatus.FILLED)

    def test_an_unassigned_backorder_cannot_be_filled(self):
        _, backorders = self.short_pick()

        with self.assertRaises(CannotAssign):
            fill_backorder(backorders[0], filled_by=self.joan)

    def test_stock_taken_since_assignment_is_caught_at_fill_time(self):
        """Time passes between accepting a backorder and shipping it."""
        backorder = self.assigned_backorder()
        # Something else empties Serere.
        post_movement(
            warehouse=self.serere, sku=self.shirt, quantity=-20,
            movement_type=MovementType.ADJUSTMENT, unit_value=Decimal("25000.00"),
            document_number="ADJ-9999", occurred_on=ORDERED_ON, created_by=self.joan,
        )

        with self.assertRaises(NoStockToFill):
            fill_backorder(backorder, filled_by=self.joan)


class SeeingWhatIsOutstanding(BackorderSetup):
    """F44."""

    def test_open_backorders_are_listed(self):
        self.short_pick()

        self.assertEqual(open_backorders().count(), 1)

    def test_an_assigned_backorder_is_no_longer_open(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 20, self.serere)
        assign_backorder(backorders[0], warehouse=self.serere, assigned_by=self.joan)

        self.assertEqual(open_backorders().count(), 0)

    def test_it_can_be_narrowed_to_the_warehouse_that_ran_short(self):
        self.short_pick()

        self.assertEqual(open_backorders(warehouse=self.namayemba).count(), 1)
        self.assertEqual(open_backorders(warehouse=self.serere).count(), 0)


class BackordersOverHttp(BackorderSetup):
    def test_a_warehouse_clerk_sees_backorders_their_site_raised(self):
        self.short_pick()
        self.client.force_authenticate(self.julius)

        response = self.client.get(reverse("orders:backorder-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_another_warehouse_does_not_see_it_until_it_is_assigned(self):
        """D5 widens a clerk's view to what they must act on, and no further."""
        _, backorders = self.short_pick()
        self.client.force_authenticate(self.joan)

        self.assertEqual(
            self.client.get(reverse("orders:backorder-list")).data["count"], 0
        )

        self.stock(self.shirt, 20, self.serere)
        assign_backorder(backorders[0], warehouse=self.serere, assigned_by=self.joan)

        self.assertEqual(
            self.client.get(reverse("orders:backorder-list")).data["count"], 1
        )

    def test_a_school_clerk_may_not_reach_backorders(self):
        self.short_pick()
        self.client.force_authenticate(self.clerk)

        self.assertEqual(
            self.client.get(reverse("orders:backorder-list")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_assigning_over_http(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 20, self.serere)
        self.client.force_authenticate(self.julius)

        response = self.client.post(
            reverse("orders:backorder-assign", args=[backorders[0].pk]),
            {"warehouse": self.serere.pk},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["filled_by_warehouse_name"], "Serere")

    def test_assigning_to_an_empty_warehouse_is_a_400(self):
        _, backorders = self.short_pick()
        self.client.force_authenticate(self.julius)

        response = self.client.post(
            reverse("orders:backorder-assign", args=[backorders[0].pk]),
            {"warehouse": self.serere.pk},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_filling_over_http(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 20, self.serere)
        assign_backorder(backorders[0], warehouse=self.serere, assigned_by=self.joan)
        self.client.force_authenticate(self.joan)

        response = self.client.post(
            reverse("orders:backorder-fill", args=[backorders[0].pk]),
            {"waybill_number": "WB-55"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["from_warehouse_name"], "Serere")

    def test_the_candidates_endpoint_lists_somewhere_to_send_it(self):
        _, backorders = self.short_pick()
        self.stock(self.shirt, 20, self.serere)
        self.client.force_authenticate(self.julius)

        rows = self.client.get(
            reverse("orders:backorder-candidates", args=[backorders[0].pk])
        ).data

        self.assertEqual([row["name"] for row in rows], ["Serere"])

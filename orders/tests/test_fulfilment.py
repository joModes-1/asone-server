"""Warehouse fulfilment of a school order — F37, F38, F39.

Three checklist items, one underlying question: can the warehouse fill this
order, and if so, reserve the stock for it.

    F37  can it be filled — a read-only check
    F38  the pick list — the same demand data, for a warehouse audience
    F39  actually reserve the stock — Available -> Pick

F39 refuses using the exact same comparison F37 reports, so the two can
never disagree — see services.check_availability().
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from inventory.models import MovementType, StockStatus
from inventory.services import post_movement, stock_level
from orders.models.school_orders import OrderStatus
from orders.services import (
    OrderCannotBePicked,
    OrderNotFillable,
    check_availability,
    pick_order,
    place_order,
    release_order,
)

Role = User.Role
IN_FORCE = date(2026, 1, 1)
ORDERED_ON = date(2026, 11, 10)


class FulfilmentSetup(TestCase):
    def setUp(self):
        self.sites = build_sites()
        self.school = self.sites["school_a"]  # Namayemba PS
        self.warehouse = self.school.primary_warehouse  # Namayemba
        self.other_warehouse = self.sites["serere"]

        self.clerk = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)
        self.finance = make_user("musana", Role.FINANCE)

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=IN_FORCE
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="10", sort_order=10)
        )

        self.order = place_order(
            school=self.school,
            student_name="Miriam Achieng",
            order_date=ORDERED_ON,
            skus=[{"sku": self.sku, "quantity": 2}],
            created_by=self.clerk,
        )

        # Paid for. Picking requires it — `REQUIRE_RELEASE_BEFORE_PICK` — and
        # these tests are about fulfilment rather than the payment gate,
        # which has its own file. An order reaching a real picking screen has
        # already been released, so that is the order these start from.
        release_order(self.order, released_by=self.finance)
        self.order.refresh_from_db()

    def stock_in(self, quantity, warehouse=None, value="20000.00", on=date(2026, 10, 1)):
        return post_movement(
            warehouse=warehouse or self.warehouse,
            sku=self.sku,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal(value),
            document_number="RC-SETUP",
            occurred_on=on,
            created_by=self.finance,
        )


class AvailabilityReportsWhatIsShort(FulfilmentSetup):
    """F37 — read-only, and never confused with actually reserving anything."""

    def test_with_no_stock_the_whole_line_is_short(self):
        rows = check_availability(self.order)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["needed"], 2)
        self.assertEqual(rows[0]["available"], 0)
        self.assertEqual(rows[0]["shortfall"], 2)

    def test_enough_stock_reports_no_shortfall(self):
        self.stock_in(5)

        rows = check_availability(self.order)
        self.assertEqual(rows[0]["available"], 5)
        self.assertEqual(rows[0]["shortfall"], 0)

    def test_checking_availability_does_not_change_stock(self):
        self.stock_in(5)
        check_availability(self.order)

        self.assertEqual(stock_level(self.sku, self.warehouse), 5)


class PickingReservesStock(FulfilmentSetup):
    """F39 — Available -> Pick. Total stock is unchanged; its composition is."""

    def test_picking_moves_stock_from_available_to_pick(self):
        self.stock_in(5)

        pick_order(self.order, picked_by=self.finance)

        self.assertEqual(stock_level(self.sku, self.warehouse), 3)
        self.assertEqual(
            stock_level(self.sku, self.warehouse, stock_status=StockStatus.PICK), 2
        )

    def test_total_physical_stock_is_unchanged_by_picking(self):
        """Recategorised, not destroyed — the same "no money moves" shape a
        transfer uses, applied between statuses instead of warehouses."""
        self.stock_in(5)

        pick_order(self.order, picked_by=self.finance)

        available = stock_level(self.sku, self.warehouse)
        picked = stock_level(self.sku, self.warehouse, stock_status=StockStatus.PICK)
        self.assertEqual(available + picked, 5)

    def test_picking_marks_the_order_picked(self):
        self.stock_in(5)

        pick_order(self.order, picked_by=self.finance)

        self.order.refresh_from_db()
        self.assertEqual(self.order.status, OrderStatus.PICKED)

    def test_picking_is_valued_at_the_warehouse_average_not_the_catalog_price(self):
        """Received at 20000, priced on the order at 25000 — picking must
        carry the warehouse's own value, not the school's price."""
        self.stock_in(5, value="20000.00")

        pick_order(self.order, picked_by=self.finance)

        picked_row = self.warehouse.stock_movements.get(stock_status=StockStatus.PICK)
        self.assertEqual(picked_row.unit_value, Decimal("20000.00"))

    def test_picking_short_stock_is_refused_and_reserves_nothing(self):
        self.stock_in(1)  # order needs 2

        with self.assertRaises(OrderNotFillable):
            pick_order(self.order, picked_by=self.finance)

        self.assertEqual(stock_level(self.sku, self.warehouse), 1)
        self.order.refresh_from_db()
        # Still RELEASED, not PICKED: the refusal left the order exactly as
        # it found it. (It reads RELEASED rather than HOLD because these
        # orders are paid for in setUp — picking requires that now.)
        self.assertEqual(self.order.status, OrderStatus.RELEASED)

    def test_picking_twice_is_refused(self):
        self.stock_in(5)
        pick_order(self.order, picked_by=self.finance)

        with self.assertRaises(OrderCannotBePicked):
            pick_order(self.order, picked_by=self.finance)

    def test_a_cancelled_order_cannot_be_picked(self):
        self.stock_in(5)
        self.order.status = OrderStatus.CANCELLED
        self.order.save(update_fields=["status"])

        with self.assertRaises(OrderCannotBePicked):
            pick_order(self.order, picked_by=self.finance)


class FulfilmentApi(APITestCase):
    """CanReceiveAndShip, not CanEnterSchoolOrders — a different matrix
    column from the rest of this viewset. See orders/views.py."""

    def setUp(self):
        self.sites = build_sites()
        self.school = self.sites["school_a"]  # Namayemba PS
        self.warehouse = self.school.primary_warehouse  # Namayemba
        self.serere = self.sites["serere"]

        self.clerk = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)
        self.finance = make_user("musana", Role.FINANCE)
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.namayemba_clerk = make_user(
            "julius", Role.WAREHOUSE_STAFF, warehouse=self.warehouse
        )
        self.serere_clerk = make_user(
            "joan", Role.WAREHOUSE_STAFF, warehouse=self.serere
        )

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=IN_FORCE
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="10", sort_order=10)
        )
        self.order = place_order(
            school=self.school,
            student_name="Miriam Achieng",
            order_date=ORDERED_ON,
            skus=[{"sku": self.sku, "quantity": 2}],
            created_by=self.clerk,
        )

        # Paid for. Picking requires it — `REQUIRE_RELEASE_BEFORE_PICK` — and
        # these tests are about fulfilment rather than the payment gate,
        # which has its own file. An order reaching a real picking screen has
        # already been released, so that is the order these start from.
        release_order(self.order, released_by=self.finance)
        self.order.refresh_from_db()
        post_movement(
            warehouse=self.warehouse,
            sku=self.sku,
            quantity=5,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("20000.00"),
            document_number="RC-SETUP",
            occurred_on=date(2026, 10, 1),
            created_by=self.finance,
        )

    def test_the_order_s_own_warehouse_clerk_can_check_availability(self):
        self.client.force_authenticate(self.namayemba_clerk)
        response = self.client.get(
            reverse("orders:school-order-availability", args=[self.order.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["shortfall"], 0)

    def test_a_different_warehouse_s_clerk_cannot_even_see_the_order(self):
        """Scoped by warehouse: Serere has nothing to do with a Namayemba
        school's order."""
        self.client.force_authenticate(self.serere_clerk)
        response = self.client.get(
            reverse("orders:school-order-availability", args=[self.order.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_school_that_placed_it_cannot_use_the_warehouse_actions(self):
        """CanEnterSchoolOrders is not CanReceiveAndShip — different columns."""
        self.client.force_authenticate(self.clerk)
        response = self.client.get(
            reverse("orders:school-order-availability", args=[self.order.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_finance_cannot_use_the_warehouse_actions_either(self):
        self.client.force_authenticate(self.finance)
        response = self.client.get(
            reverse("orders:school-order-pick-list", args=[self.order.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_lead_can_pick_the_order(self):
        self.client.force_authenticate(self.lead)
        response = self.client.post(
            reverse("orders:school-order-pick", args=[self.order.pk])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], OrderStatus.PICKED)

    def test_pick_list_returns_the_same_shape_as_demand(self):
        self.client.force_authenticate(self.namayemba_clerk)
        response = self.client.get(
            reverse("orders:school-order-pick-list", args=[self.order.pk])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["sku_number"], self.sku.number)
        self.assertEqual(response.data[0]["quantity"], 2)

    def test_picking_short_stock_is_a_400_not_a_500(self):
        # A second order for more than the 3 units left after the first pick.
        second_order = place_order(
            school=self.school,
            student_name="Second Student",
            order_date=ORDERED_ON,
            skus=[{"sku": self.sku, "quantity": 999}],
            created_by=self.clerk,
        )
        self.client.force_authenticate(self.namayemba_clerk)

        response = self.client.post(
            reverse("orders:school-order-pick", args=[second_order.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_picking_twice_over_the_api_is_a_400_not_a_500(self):
        self.client.force_authenticate(self.namayemba_clerk)
        self.client.post(reverse("orders:school-order-pick", args=[self.order.pk]))

        response = self.client.post(
            reverse("orders:school-order-pick", args=[self.order.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

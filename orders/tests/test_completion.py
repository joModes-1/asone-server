"""Confirming a parcel arrived, and completing the order.

Shipped and completed are different facts. Shipped means it left the
warehouse; completed means the school says it got there. **The gap between
them is the point** — a parcel that left Namayemba three weeks ago and never
arrived is invisible without it, and that gap is where losses live.

Two rules this file protects:

    confirming does not touch stock — it left at ship and stays gone
    an order completes only when EVERY shipment on it is confirmed
"""

from datetime import date, timedelta
from decimal import Decimal

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
    CannotConfirmReceipt,
    cancel_order,
    confirm_receipt,
    pick_order,
    place_order,
    release_order,
    ship_order,
    shipments_awaiting_confirmation,
)

IN_FORCE = date(2026, 1, 1)
ORDERED_ON = date(2026, 11, 10)
SHIPPED_ON = date(2026, 11, 12)
Role = User.Role


class CompletionSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.school = self.sites["school_a"]
        self.namayemba = self.sites["namayemba"]
        self.serere = self.sites["serere"]

        self.clerk = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)
        self.julius = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.namayemba)
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.finance = make_user("musana", Role.FINANCE)

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=IN_FORCE
        )
        self.shirt = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="10", sort_order=10)
        )

    def stock(self, quantity, warehouse=None):
        post_movement(
            warehouse=warehouse or self.namayemba, sku=self.shirt, quantity=quantity,
            movement_type=MovementType.RECEIPT, unit_value=Decimal("25000.00"),
            document_number="RC-100001", occurred_on=IN_FORCE, created_by=self.julius,
        )

    def shipped_order(self, quantity=2):
        """An order that has been all the way out of the door.

        Released on the way past: a shipped order has necessarily been
        picked, and the warehouse cannot pick one nobody has paid for
        (`REQUIRE_RELEASE_BEFORE_PICK`).
        """
        self.stock(20)
        order = place_order(
            school=self.school, student_name="Miriam Achieng", order_date=ORDERED_ON,
            skus=[{"sku": self.shirt, "quantity": quantity}], created_by=self.clerk,
        )
        order = release_order(order, released_by=self.finance)
        shipment = ship_order(
            pick_order(order, picked_by=self.julius),
            shipped_by=self.julius, shipped_on=SHIPPED_ON,
        )
        return order, shipment


class ConfirmingArrivalCompletesTheOrder(CompletionSetup):
    def test_a_shipped_order_is_not_yet_completed(self):
        order, _ = self.shipped_order()

        self.assertEqual(order.status, OrderStatus.SHIPPED)

    def test_confirming_the_only_shipment_completes_it(self):
        order, shipment = self.shipped_order()

        confirm_receipt(shipment, confirmed_by=self.clerk)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)

    def test_who_confirmed_and_when_are_recorded(self):
        _, shipment = self.shipped_order()

        confirm_receipt(shipment, confirmed_by=self.clerk, notes="One shirt torn.")

        shipment.refresh_from_db()
        self.assertTrue(shipment.is_received)
        self.assertEqual(shipment.received_by, self.clerk)
        self.assertEqual(shipment.receipt_notes, "One shirt torn.")

    def test_confirming_twice_is_refused(self):
        """It would overwrite who confirmed it and when."""
        _, shipment = self.shipped_order()
        confirm_receipt(shipment, confirmed_by=self.clerk)

        with self.assertRaises(CannotConfirmReceipt):
            confirm_receipt(shipment, confirmed_by=self.clerk)

    def test_a_cancelled_order_cannot_be_confirmed(self):
        order, shipment = self.shipped_order()
        order.status = OrderStatus.CANCELLED
        order.save(update_fields=["status"])

        with self.assertRaises(CannotConfirmReceipt):
            confirm_receipt(shipment, confirmed_by=self.clerk)


class ConfirmingDoesNotTouchStock(CompletionSetup):
    """Stock left at ship and stays gone. Recording it here instead would
    mean stock the warehouse has handed to a driver still counting as
    theirs — long enough for two warehouses to promise the same shirts."""

    def test_shipped_stock_is_unchanged_by_confirmation(self):
        _, shipment = self.shipped_order(quantity=2)
        before = stock_level(self.shirt, self.namayemba, stock_status=StockStatus.SHIPPED)

        confirm_receipt(shipment, confirmed_by=self.clerk)

        self.assertEqual(
            stock_level(self.shirt, self.namayemba, stock_status=StockStatus.SHIPPED),
            before,
        )

    def test_available_stock_is_unchanged_too(self):
        _, shipment = self.shipped_order(quantity=2)
        before = stock_level(self.shirt, self.namayemba)

        confirm_receipt(shipment, confirmed_by=self.clerk)

        self.assertEqual(stock_level(self.shirt, self.namayemba), before)


class AnOrderWithTwoParcels(CompletionSetup):
    """D2 lets a backorder ship direct from a warehouse that is not the
    school's own, so an order can arrive in two pieces."""

    def second_shipment_for(self, order):
        """A second parcel from Serere, the way a filled backorder arrives."""
        self.stock(10, warehouse=self.serere)
        for status_, sign in ((StockStatus.AVAILABLE, -1), (StockStatus.PICK, 1)):
            post_movement(
                warehouse=self.serere, sku=self.shirt, quantity=sign * 1,
                movement_type=MovementType.PICK, stock_status=status_,
                unit_value=Decimal("25000.00"), document_number=order.number,
                occurred_on=ORDERED_ON, created_by=self.julius,
            )
        order.status = OrderStatus.PICKED
        order.save(update_fields=["status"])
        return ship_order(
            order, shipped_by=self.julius, from_warehouse=self.serere,
            shipped_on=SHIPPED_ON,
        )

    def test_confirming_one_does_not_complete_the_order(self):
        """The bug this design avoids: closing an order still waiting on a
        parcel."""
        order, first = self.shipped_order()
        self.second_shipment_for(order)

        confirm_receipt(first, confirmed_by=self.clerk)

        order.refresh_from_db()
        self.assertNotEqual(order.status, OrderStatus.COMPLETED)

    def test_confirming_both_completes_it(self):
        order, first = self.shipped_order()
        second = self.second_shipment_for(order)

        confirm_receipt(first, confirmed_by=self.clerk)
        confirm_receipt(second, confirmed_by=self.clerk)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)


class WhatLeftAndNobodyConfirmed(CompletionSetup):
    """The report the whole completion step exists to make possible."""

    def test_an_unconfirmed_shipment_appears(self):
        self.shipped_order()

        self.assertEqual(shipments_awaiting_confirmation().count(), 1)

    def test_a_confirmed_one_drops_off(self):
        _, shipment = self.shipped_order()
        confirm_receipt(shipment, confirmed_by=self.clerk)

        self.assertEqual(shipments_awaiting_confirmation().count(), 0)

    def test_it_can_be_narrowed_to_the_ones_worth_chasing(self):
        """Everything shipped this morning is unconfirmed and none of it is
        a problem yet."""
        self.shipped_order()

        self.assertEqual(
            shipments_awaiting_confirmation(older_than_days=3650).count(), 0
        )


class ConfirmingOverHttp(CompletionSetup):
    def url(self, order):
        return reverse("orders:school-order-confirm-receipt", args=[order.pk])

    def test_the_school_may_confirm(self):
        order, _ = self.shipped_order()
        self.client.force_authenticate(self.clerk)

        response = self.client.post(self.url(order), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.COMPLETED)

    def test_the_shipment_may_be_omitted_when_there_is_only_one(self):
        order, _ = self.shipped_order()
        self.client.force_authenticate(self.clerk)

        self.assertEqual(
            self.client.post(self.url(order), {}, format="json").status_code,
            status.HTTP_200_OK,
        )

    def test_it_must_be_named_when_there_are_two(self):
        """Guessing would silently complete the wrong parcel."""
        order, _ = self.shipped_order()
        AnOrderWithTwoParcels.second_shipment_for(self, order)
        self.client.force_authenticate(self.clerk)

        response = self.client.post(self.url(order), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("shipment", response.data)

    def test_another_schools_shipment_cannot_be_named(self):
        order, _ = self.shipped_order()
        self.client.force_authenticate(self.clerk)

        response = self.client.post(
            self.url(order), {"shipment": 99999}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_confirming_an_unshipped_order_is_a_400(self):
        self.stock(20)
        order = place_order(
            school=self.school, student_name="Daniel Kato", order_date=ORDERED_ON,
            skus=[{"sku": self.shirt, "quantity": 1}], created_by=self.clerk,
        )
        self.client.force_authenticate(self.clerk)

        response = self.client.post(self.url(order), {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_warehouse_clerk_may_not_confirm_for_the_school(self):
        """The school received it. The warehouse saying so would defeat the
        point of asking."""
        order, _ = self.shipped_order()
        self.client.force_authenticate(self.julius)

        self.assertEqual(
            self.client.post(self.url(order), {}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

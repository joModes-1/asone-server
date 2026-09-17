"""Sending a picked order out — F41.

The rule with teeth: **what ships is what is reserved, not what was
ordered.** Those differ whenever a pick was short, and reading the order's
lines instead of the ledger would despatch stock the warehouse does not
have.

The second rule is decision D2: a shipment carries its own origin
warehouse, because a backorder may be filled by a warehouse that is not the
school's own, shipping direct.
"""

from datetime import date
from decimal import Decimal

from django.db.models import Sum
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from inventory.models import MovementType, StockMovement, StockStatus
from inventory.services import post_movement, stock_level
from orders.models import Shipment
from orders.models.school_orders import OrderStatus
from orders.services import (
    NothingToShip,
    OrderCannotBeShipped,
    cancel_order,
    pick_order,
    place_order,
    release_order,
    ship_order,
)

IN_FORCE = date(2026, 1, 1)
ORDERED_ON = date(2026, 11, 10)
SHIPPED_ON = date(2026, 11, 12)
Role = User.Role


class ShippingSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.school = self.sites["school_a"]
        self.warehouse = self.sites["namayemba"]
        self.other_warehouse = self.sites["serere"]

        self.clerk = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)
        self.julius = make_user(
            "julius", Role.WAREHOUSE_STAFF, warehouse=self.warehouse
        )
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

    def stock(self, sku, quantity, warehouse=None, value="25000.00"):
        post_movement(
            warehouse=warehouse or self.warehouse,
            sku=sku,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal(value),
            document_number="RC-100001",
            occurred_on=ORDERED_ON,
            created_by=self.julius,
        )

    def picked_order(self, quantity=2):
        self.stock(self.shirt, 20)
        order = place_order(
            school=self.school,
            student_name="Miriam Achieng",
            order_date=ORDERED_ON,
            skus=[{"sku": self.shirt, "quantity": quantity}],
            created_by=self.clerk,
        )
        # Paid for before the shelf is touched — picking requires it now.
        order = release_order(order, released_by=self.finance)
        return pick_order(order, picked_by=self.julius)


class StockLeavesAtShip(ShippingSetup):
    def test_shipping_moves_stock_out_of_pick(self):
        order = self.picked_order(quantity=2)
        self.assertEqual(
            stock_level(self.shirt, self.warehouse, stock_status=StockStatus.PICK), 2
        )

        ship_order(order, shipped_by=self.julius, shipped_on=SHIPPED_ON)

        self.assertEqual(
            stock_level(self.shirt, self.warehouse, stock_status=StockStatus.PICK), 0
        )
        self.assertEqual(
            stock_level(self.shirt, self.warehouse, stock_status=StockStatus.SHIPPED), 2
        )

    def test_available_stock_is_untouched_by_shipping(self):
        """It left AVAILABLE at pick, not here. Decrementing it again would
        take the same two shirts off the shelf twice."""
        order = self.picked_order(quantity=2)
        before = stock_level(self.shirt, self.warehouse)

        ship_order(order, shipped_by=self.julius, shipped_on=SHIPPED_ON)

        self.assertEqual(stock_level(self.shirt, self.warehouse), before)

    def test_the_order_becomes_shipped(self):
        order = self.picked_order()

        ship_order(order, shipped_by=self.julius, shipped_on=SHIPPED_ON)

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.SHIPPED)

    def test_a_shipment_records_what_left_and_from_where(self):
        order = self.picked_order(quantity=3)

        shipment = ship_order(
            order, shipped_by=self.julius, shipped_on=SHIPPED_ON, waybill_number="WB-12"
        )

        self.assertTrue(shipment.number.startswith("SH-"))
        self.assertEqual(shipment.from_warehouse, self.warehouse)
        self.assertEqual(shipment.shipped_by, self.julius)
        self.assertEqual(shipment.waybill_number, "WB-12")
        self.assertEqual(shipment.lines.get().quantity, 3)

    def test_shipment_numbers_do_not_repeat(self):
        first = ship_order(self.picked_order(), shipped_by=self.julius)
        self.stock(self.socks, 10)
        second_order = place_order(
            school=self.school,
            student_name="Daniel Kato",
            order_date=ORDERED_ON,
            skus=[{"sku": self.socks, "quantity": 1}],
            created_by=self.clerk,
        )
        second_order = release_order(second_order, released_by=self.finance)
        second = ship_order(
            pick_order(second_order, picked_by=self.julius), shipped_by=self.julius
        )

        self.assertNotEqual(first.number, second.number)

    def test_the_ledger_keeps_its_value(self):
        """Shipping moves stock, it does not revalue it."""
        order = self.picked_order(quantity=2)

        shipment = ship_order(order, shipped_by=self.julius, shipped_on=SHIPPED_ON)

        rows = StockMovement.objects.filter(document_number=shipment.number)
        self.assertEqual(rows.count(), 2)
        self.assertEqual({row.unit_value for row in rows}, {Decimal("25000.00")})


class WhatShipsIsWhatWasReserved(ShippingSetup):
    def test_a_warehouse_that_picked_its_whole_shelf_can_still_ship(self):
        """The regression this exists for: `average_unit_value` used to look
        only at AVAILABLE, so a warehouse with nothing left available had no
        value to ship with."""
        self.stock(self.socks, 4)
        order = place_order(
            school=self.school,
            student_name="Sarah Auma",
            order_date=ORDERED_ON,
            skus=[{"sku": self.socks, "quantity": 4}],
            created_by=self.clerk,
        )
        order = release_order(order, released_by=self.finance)
        picked = pick_order(order, picked_by=self.julius)
        self.assertEqual(stock_level(self.socks, self.warehouse), 0)

        shipment = ship_order(picked, shipped_by=self.julius, shipped_on=SHIPPED_ON)

        self.assertEqual(shipment.lines.get().quantity, 4)


class ShippingIsRefusedWhenItShouldBe(ShippingSetup):
    def test_an_order_that_was_never_picked_cannot_ship(self):
        self.stock(self.shirt, 10)
        order = place_order(
            school=self.school,
            student_name="Moses Wandera",
            order_date=ORDERED_ON,
            skus=[{"sku": self.shirt, "quantity": 1}],
            created_by=self.clerk,
        )

        with self.assertRaises(OrderCannotBeShipped):
            ship_order(order, shipped_by=self.julius)

    def test_a_cancelled_order_cannot_ship(self):
        self.stock(self.shirt, 10)
        order = cancel_order(
            place_order(
                school=self.school,
                student_name="Brian Ochieng",
                order_date=ORDERED_ON,
                skus=[{"sku": self.shirt, "quantity": 1}],
                created_by=self.clerk,
            ),
            cancelled_by=self.clerk,
            reason="No funds.",
        )

        with self.assertRaises(OrderCannotBeShipped):
            ship_order(order, shipped_by=self.julius)

    def test_an_order_cannot_ship_twice(self):
        order = self.picked_order()
        ship_order(order, shipped_by=self.julius, shipped_on=SHIPPED_ON)

        with self.assertRaises(OrderCannotBeShipped):
            ship_order(order, shipped_by=self.julius, shipped_on=SHIPPED_ON)

    def test_shipping_from_a_warehouse_holding_nothing_is_refused(self):
        """Rather than writing an empty shipment nobody can act on."""
        order = self.picked_order()

        with self.assertRaises(NothingToShip):
            ship_order(
                order, shipped_by=self.julius, from_warehouse=self.other_warehouse
            )


class ABackorderShipsFromWhereverFilledIt(ShippingSetup):
    """Decision D2, Jim on 24 August: the fulfilling warehouse ships direct
    to the school, so a shipment's origin is never derived from the
    school's primary warehouse."""

    def test_a_shipment_can_leave_a_warehouse_that_is_not_the_schools(self):
        # Serere holds the stock and does the picking, even though
        # Namayemba PS orders from Namayemba.
        self.stock(self.shirt, 10, warehouse=self.other_warehouse)
        order = place_order(
            school=self.school,
            student_name="Ruth Naigaga",
            order_date=ORDERED_ON,
            skus=[{"sku": self.shirt, "quantity": 2}],
            created_by=self.clerk,
        )
        # Reserve it at Serere by hand — the backorder transfer that would
        # normally do this is F43-F46.
        for status_, sign in ((StockStatus.AVAILABLE, -1), (StockStatus.PICK, 1)):
            post_movement(
                warehouse=self.other_warehouse,
                sku=self.shirt,
                quantity=sign * 2,
                movement_type=MovementType.PICK,
                stock_status=status_,
                unit_value=Decimal("25000.00"),
                document_number=order.number,
                occurred_on=ORDERED_ON,
                created_by=self.julius,
            )
        order.status = OrderStatus.PICKED
        order.save(update_fields=["status"])

        shipment = ship_order(
            order,
            shipped_by=self.julius,
            from_warehouse=self.other_warehouse,
            shipped_on=SHIPPED_ON,
        )

        self.assertEqual(shipment.from_warehouse, self.other_warehouse)
        self.assertNotEqual(shipment.from_warehouse, self.school.primary_warehouse)


class ShippingOverHttp(ShippingSetup):
    def url(self, order):
        return reverse("orders:school-order-ship", args=[order.pk])

    def test_a_warehouse_clerk_may_ship(self):
        order = self.picked_order()
        self.client.force_authenticate(self.julius)

        response = self.client.post(self.url(order), {"waybill_number": "WB-9"}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["number"].startswith("SH-"))

    def test_a_school_clerk_may_not_ship(self):
        order = self.picked_order()
        self.client.force_authenticate(self.clerk)

        self.assertEqual(
            self.client.post(self.url(order), {}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_shipping_an_unpicked_order_is_a_400_not_a_500(self):
        self.stock(self.shirt, 5)
        order = place_order(
            school=self.school,
            student_name="Esther Amongin",
            order_date=ORDERED_ON,
            skus=[{"sku": self.shirt, "quantity": 1}],
            created_by=self.clerk,
        )
        self.client.force_authenticate(self.julius)

        response = self.client.post(self.url(order), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_the_school_can_see_its_own_shipments(self):
        order = self.picked_order()
        ship_order(order, shipped_by=self.julius, shipped_on=SHIPPED_ON)

        self.client.force_authenticate(self.julius)
        rows = self.client.get(
            reverse("orders:school-order-shipments", args=[order.pk])
        ).data

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["from_warehouse_name"], "Namayemba")

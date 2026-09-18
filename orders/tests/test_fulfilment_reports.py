"""Fulfilment documents and reports — F40, F49, F52, F54, F57.

The one worth reading first is `TheCostedReportDoesNotDoubleCount`. Valuing
a shipment means joining what left against what the school was charged, and
an order can carry the same SKU twice — once inside a kit, once loose. Done
as a join rather than a subquery, every shipment line multiplies by every
matching order line and the report silently doubles. That is the bug this
file exists to keep out.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Kit, KitItem, Size, Sku
from inventory.models import MovementType
from inventory.services import post_movement
from orders.reports import (
    outstanding_backorders,
    part_processed_orders,
    shipments_costed,
)
from orders.services import (
    assign_backorder,
    fill_backorder,
    packing_list_for,
    pick_available,
    pick_order,
    place_order,
    release_order,
    ship_order,
)

IN_FORCE = date(2026, 1, 1)
ORDERED_ON = date(2026, 11, 10)
SHIPPED_ON = date(2026, 11, 12)
Role = User.Role


class ReportSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.school = self.sites["school_a"]
        self.namayemba = self.sites["namayemba"]
        self.serere = self.sites["serere"]

        self.clerk = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)
        self.julius = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.namayemba)
        self.joan = make_user("joan", Role.WAREHOUSE_STAFF, warehouse=self.serere)
        self.finance = make_user("musana", Role.FINANCE)
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)

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

    def stock(self, sku, quantity, warehouse=None):
        post_movement(
            warehouse=warehouse or self.namayemba, sku=sku, quantity=quantity,
            movement_type=MovementType.RECEIPT, unit_value=Decimal("25000.00"),
            document_number="RC-100001", occurred_on=IN_FORCE, created_by=self.julius,
        )

    def order(self, student="Miriam Achieng", **lines):
        """A placed order, paid for.

        Released here rather than in each test: picking requires payment
        (`REQUIRE_RELEASE_BEFORE_PICK`), and every report in this file is
        drawn from orders that have been through the warehouse — so an order
        that never reached RELEASED could not appear in one anyway.
        """
        order = place_order(
            school=self.school, student_name=student, order_date=ORDERED_ON,
            skus=[{"sku": getattr(self, n), "quantity": q} for n, q in lines.items()],
            created_by=self.clerk,
        )
        return release_order(order, released_by=self.finance)


class ThePackingList(ReportSetup):
    """F40 — the document that travels with the goods."""

    def shipped(self, student="Miriam Achieng"):
        self.stock(self.shirt, 20)
        order = self.order(student=student, shirt=2)
        return ship_order(
            pick_order(order, picked_by=self.julius),
            shipped_by=self.julius, shipped_on=SHIPPED_ON, waybill_number="WB-3",
        )

    def test_it_carries_the_invoice_number_and_the_student_together(self):
        """AsOne's definitions page: both are needed to hand the parcel to
        the right child. Either alone is not enough.

        Since F42 the pairing is **per line**, because a consolidated van
        carries several students — which is exactly when the pairing starts
        to matter.
        """
        sheet = packing_list_for(self.shipped(student="Grace Nabirye"))

        for line in sheet["lines"]:
            self.assertEqual(line["student_name"], "Grace Nabirye")
            self.assertTrue(line["invoice_number"].startswith("SO-"))

        self.assertEqual(len(sheet["order_numbers"]), 1)

    def test_it_lists_what_is_in_the_parcel(self):
        sheet = packing_list_for(self.shipped())

        self.assertEqual(sheet["total_units"], 2)
        self.assertEqual(sheet["lines"][0]["sku_number"], self.shirt.number)

    def test_a_normal_shipment_is_not_flagged_as_direct(self):
        sheet = packing_list_for(self.shipped())

        self.assertFalse(sheet["is_direct_from_another_warehouse"])

    def test_a_backorder_filled_elsewhere_is_flagged(self):
        """A school receiving a parcel from Serere when it orders from
        Namayemba needs to see why."""
        self.stock(self.shirt, 1)
        self.stock(self.shirt, 20, warehouse=self.serere)
        order = self.order(shirt=3)
        _, backorders = pick_available(order, picked_by=self.julius)
        assign_backorder(backorders[0], warehouse=self.serere, assigned_by=self.joan)
        shipment = fill_backorder(backorders[0], filled_by=self.joan)

        sheet = packing_list_for(shipment)

        self.assertTrue(sheet["is_direct_from_another_warehouse"])
        self.assertEqual(sheet["from_warehouse"], "Serere")

    def test_the_warehouse_that_packed_it_may_read_it(self):
        shipment = self.shipped()
        self.client.force_authenticate(self.julius)

        rows = self.client.get(
            reverse("orders:school-order-packing-lists", args=[shipment.orders.first().pk])
        ).data

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["shipment_number"], shipment.number)

    def test_the_school_may_not_read_it_over_http(self):
        """The checklist leaves F40's School Staff cell blank. Coded as
        written — but see CanReadPackingList: their definitions page says
        the school uses this document, so it is worth querying. The
        likeliest reading is that the school gets the printed sheet in the
        box, not a screen."""
        shipment = self.shipped()
        self.client.force_authenticate(self.clerk)

        self.assertEqual(
            self.client.get(
                reverse("orders:school-order-packing-lists", args=[shipment.orders.first().pk])
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class OutstandingBackordersReport(ReportSetup):
    """F49."""

    def a_backorder(self):
        self.stock(self.shirt, 1)
        _, backorders = pick_available(self.order(shirt=3), picked_by=self.julius)
        return backorders[0]

    def test_an_open_backorder_is_outstanding(self):
        self.a_backorder()

        self.assertEqual(outstanding_backorders().count(), 1)

    def test_an_assigned_backorder_is_still_outstanding(self):
        """Assigned is not delivered — somebody still has to put it on a van."""
        backorder = self.a_backorder()
        self.stock(self.shirt, 20, warehouse=self.serere)
        assign_backorder(backorder, warehouse=self.serere, assigned_by=self.joan)

        self.assertEqual(outstanding_backorders().count(), 1)

    def test_a_filled_backorder_is_not(self):
        backorder = self.a_backorder()
        self.stock(self.shirt, 20, warehouse=self.serere)
        assign_backorder(backorder, warehouse=self.serere, assigned_by=self.joan)
        fill_backorder(backorder, filled_by=self.joan)

        self.assertEqual(outstanding_backorders().count(), 0)

    def test_a_school_sees_only_its_own(self):
        self.a_backorder()
        self.client.force_authenticate(self.clerk)

        response = self.client.get(reverse("orders:backorders-outstanding"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_finance_sees_it(self):
        """F49 is the one fulfilment report Finance gets — they carry an
        unfilled backorder as an invoice nobody can ship against."""
        self.a_backorder()
        self.client.force_authenticate(self.finance)

        self.assertEqual(
            self.client.get(reverse("orders:backorders-outstanding")).data["count"], 1
        )

    def test_the_warehouse_chasing_it_sees_it(self):
        self.a_backorder()
        self.client.force_authenticate(self.julius)

        self.assertEqual(
            self.client.get(reverse("orders:backorders-outstanding")).data["count"], 1
        )


class PickedButNotDespatched(ReportSetup):
    """F52 and F54 — one query, two checklist items, different audiences."""

    def test_a_picked_order_appears(self):
        self.stock(self.shirt, 20)
        pick_order(self.order(shirt=2), picked_by=self.julius)

        self.assertEqual(part_processed_orders().count(), 1)

    def test_an_order_still_on_hold_does_not(self):
        self.stock(self.shirt, 20)
        self.order(shirt=2)

        self.assertEqual(part_processed_orders().count(), 0)

    def test_a_shipped_order_drops_off(self):
        """The whole point: this measures the gap, so closing it empties it."""
        self.stock(self.shirt, 20)
        order = pick_order(self.order(shirt=2), picked_by=self.julius)
        self.assertEqual(part_processed_orders().count(), 1)

        ship_order(order, shipped_by=self.julius, shipped_on=SHIPPED_ON)

        self.assertEqual(part_processed_orders().count(), 0)

    def test_a_warehouse_clerk_sees_their_own_site(self):
        self.stock(self.shirt, 20)
        pick_order(self.order(shirt=2), picked_by=self.julius)
        self.client.force_authenticate(self.julius)

        response = self.client.get(reverse("orders:orders-part-processed"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["count"], 1)

    def test_finance_is_excluded(self):
        """Deliberate on AsOne's part — Finance gets the costed reports, not
        the operational backlog. Worth knowing before somebody 'fixes' it."""
        self.stock(self.shirt, 20)
        pick_order(self.order(shirt=2), picked_by=self.julius)
        self.client.force_authenticate(self.finance)

        self.assertEqual(
            self.client.get(reverse("orders:orders-part-processed")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_another_warehouse_does_not(self):
        self.stock(self.shirt, 20)
        pick_order(self.order(shirt=2), picked_by=self.julius)
        self.client.force_authenticate(self.joan)

        self.assertEqual(
            self.client.get(reverse("orders:orders-part-processed")).data["count"], 0
        )


class TheCostedReportDoesNotDoubleCount(ReportSetup):
    """F57, and the bug this file exists to keep out.

    An order can carry the same SKU on two lines — once inside a kit, once
    ordered loose. Valuing shipments by joining shipment lines to order
    lines multiplies one by the other and silently doubles the report.
    """

    def test_a_simple_shipment_is_valued_at_what_the_school_was_charged(self):
        self.stock(self.shirt, 20)
        order = self.order(shirt=2)
        ship_order(pick_order(order, picked_by=self.julius),
                   shipped_by=self.julius, shipped_on=SHIPPED_ON)

        row = shipments_costed()[0]

        self.assertEqual(row["units"], 2)
        self.assertEqual(row["value"], Decimal("50000.00"))

    def test_a_sku_appearing_twice_on_the_order_is_not_double_counted(self):
        """Two shirts in a kit plus one loose is three shirts at 25,000 —
        75,000, not 150,000."""
        kit = Kit.objects.create(
            kit_number="PS-STARTER", name="Starter", school_level=Kit.SchoolLevel.PRIMARY
        )
        KitItem.objects.create(kit=kit, sku=self.shirt, quantity=2)
        self.stock(self.shirt, 20)

        order = place_order(
            school=self.school, student_name="Daniel Kato", order_date=ORDERED_ON,
            kits=[{"kit": kit, "quantity": 1}],
            skus=[{"sku": self.shirt, "quantity": 1}],
            created_by=self.clerk,
        )
        self.assertEqual(order.lines.filter(sku=self.shirt).count(), 2)
        order = release_order(order, released_by=self.finance)

        ship_order(pick_order(order, picked_by=self.julius),
                   shipped_by=self.julius, shipped_on=SHIPPED_ON)

        row = shipments_costed()[0]

        self.assertEqual(row["units"], 3)
        self.assertEqual(row["value"], Decimal("75000.00"))

    def test_it_can_be_narrowed_to_a_period(self):
        self.stock(self.shirt, 20)
        order = self.order(shirt=2)
        ship_order(pick_order(order, picked_by=self.julius),
                   shipped_by=self.julius, shipped_on=date(2026, 11, 12))

        self.assertEqual(len(shipments_costed(date_from=date(2026, 12, 1))), 0)
        self.assertEqual(len(shipments_costed(date_to=date(2026, 11, 30))), 1)

    def test_finance_may_read_it_and_a_school_may_not(self):
        self.stock(self.shirt, 20)
        ship_order(pick_order(self.order(shirt=2), picked_by=self.julius),
                   shipped_by=self.julius, shipped_on=SHIPPED_ON)

        self.client.force_authenticate(self.finance)
        self.assertEqual(
            self.client.get(reverse("orders:shipments-costed")).status_code,
            status.HTTP_200_OK,
        )

        self.client.force_authenticate(self.clerk)
        self.assertEqual(
            self.client.get(reverse("orders:shipments-costed")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

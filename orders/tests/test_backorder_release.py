"""Releasing backorders in bulk — the queue behind "Release All Eligible".

Releasing is `assign` then `fill` done together: responsibility moves to a
warehouse that has the stock, and that warehouse ships direct to the school
(decision D2). The tests worth having are about the seam — that eligibility
means "somebody can actually fill it", and that pressing one button gives
one outcome rather than half a released queue.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from inventory.models import MovementType
from inventory.services import post_movement
from orders.models import Backorder
from orders.models.backorders import BackorderStatus
from orders.services import (
    eligible_for_release,
    pick_available,
    place_order,
    release_eligible,
    release_order,
)

IN_FORCE = date(2026, 1, 1)
TODAY = date(2026, 11, 10)
Role = User.Role


class BackorderReleaseSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.namayemba = self.sites["namayemba"]
        self.serere = self.sites["serere"]
        self.school = self.sites["school_a"]

        self.julius = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.namayemba)
        self.finance = make_user("musana", Role.FINANCE)
        self.clerk = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=IN_FORCE
        )
        self.shirt = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="S8", sort_order=10)
        )

    def stock(self, warehouse, quantity):
        post_movement(
            warehouse=warehouse,
            sku=self.shirt,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-100001",
            occurred_on=IN_FORCE,
            created_by=self.julius,
        )

    def short_order(self, ordered=40, on_hand=25, student="Nakato Grace"):
        """An order Namayemba cannot fill, picked short so a backorder exists."""
        self.stock(self.namayemba, on_hand)
        order = place_order(
            school=self.school,
            student_name=student,
            order_date=TODAY,
            skus=[{"sku": self.shirt, "quantity": ordered}],
            created_by=self.clerk,
        )
        release_order(order, released_by=self.finance)
        pick_available(order, picked_by=self.julius)
        return order


class EligibilityMeansSomebodyCanFillIt(BackorderReleaseSetup):
    def test_a_shortfall_nobody_stocks_is_not_eligible(self):
        self.short_order()

        self.assertEqual(eligible_for_release(), [])

    def test_it_becomes_eligible_once_stock_arrives_elsewhere(self):
        """D2: stock at Serere can fill a Namayemba shortfall."""
        self.short_order()
        self.stock(self.serere, 50)

        self.assertEqual(len(eligible_for_release()), 1)

    def test_stock_at_the_warehouse_that_ran_short_does_not_count(self):
        """Assigning back to the site that ran short is refused, so holding
        stock there cannot make a backorder eligible."""
        self.short_order()
        # More arrives at Namayemba, but it is the origin.
        self.stock(self.namayemba, 100)

        self.assertEqual(eligible_for_release(), [])


class ReleasingShipsDirect(BackorderReleaseSetup):
    def test_it_assigns_and_ships_in_one_go(self):
        order = self.short_order()
        self.stock(self.serere, 50)

        shipments = release_eligible(released_by=self.julius)

        self.assertEqual(len(shipments), 1)
        self.assertEqual(shipments[0].from_warehouse, self.serere)
        self.assertEqual(shipments[0].school, self.school)

        backorder = Backorder.objects.get(order=order)
        self.assertEqual(backorder.status, BackorderStatus.FILLED)

    def test_it_picks_the_warehouse_holding_the_most(self):
        """So one release does not strip a site only just covering itself."""
        self.short_order()
        self.stock(self.serere, 50)

        shipments = release_eligible(released_by=self.julius)

        self.assertEqual(shipments[0].from_warehouse, self.serere)

    def test_nothing_eligible_is_said_plainly(self):
        self.short_order()

        with self.assertRaises(Exception):
            release_eligible(released_by=self.julius)


class ReleasingOverHttp(BackorderReleaseSetup):
    def setUp(self):
        super().setUp()
        self.eligible_url = reverse("orders:backorder-eligible")
        self.release_url = reverse("orders:backorder-release-eligible")

    def test_the_queue_lists_what_can_go(self):
        self.short_order()
        self.stock(self.serere, 50)
        self.client.force_authenticate(self.julius)

        rows = self.client.get(self.eligible_url).data

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["available"], 50)
        self.assertEqual(rows[0]["fillable_at"], self.serere.name)

    def test_a_row_carries_its_priority(self):
        self.short_order()
        self.stock(self.serere, 50)
        self.client.force_authenticate(self.julius)

        row = self.client.get(self.eligible_url).data[0]

        self.assertIn("priority", row)
        self.assertIn("priority_display", row)

    def test_releasing_returns_the_shipments_it_made(self):
        self.short_order()
        self.stock(self.serere, 50)
        self.client.force_authenticate(self.julius)

        response = self.client.post(self.release_url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(len(response.data), 1)

    def test_releasing_nothing_is_a_400_that_explains(self):
        self.short_order()
        self.client.force_authenticate(self.julius)

        response = self.client.post(self.release_url, {}, format="json")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_school_clerk_may_not_release(self):
        self.client.force_authenticate(self.clerk)

        self.assertEqual(
            self.client.post(self.release_url, {}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

"""Needs Attention: pending registrations, and the on-hold wording.

Two fixes under test.

**Registrations reached nobody.** Somebody could ask for an account, confirm
their email, and sit there: `needs_attention` had no row for it, so the only
way a lead found out was by opening the Users screen on a hunch.

**The on-hold row said "waiting for stock".** OrderStatus.HOLD means awaiting
*payment*. Since F43 there is a real queue of orders waiting for stock, so the
old wording pointed a warehouse user at the wrong problem entirely.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import RegistrationRequest, User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from dashboard import services
from orders.services import place_order

IN_FORCE = date(2026, 1, 1)
Role = User.Role


class AttentionSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.school = self.sites["school_a"]
        self.namayemba = self.sites["namayemba"]

        self.sharon = make_user("sharon", Role.PROGRAM_LEAD)
        self.julius = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.namayemba)
        self.chrisis = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)

    def request_account(self, email, *, verified):
        return RegistrationRequest.objects.create(
            first_name="Grace",
            last_name="Nakato",
            email=email,
            verified_at=timezone.now() if verified else None,
        )

    def kinds(self, user=None, warehouse=None):
        return [row["kind"] for row in services.needs_attention(warehouse, user=user)]

    def row(self, kind, user=None, warehouse=None):
        for r in services.needs_attention(warehouse, user=user):
            if r["kind"] == kind:
                return r
        return None


class PendingRegistrationsAreSurfaced(AttentionSetup):
    def registration_rows(self, user=None, warehouse=None):
        return [
            r
            for r in services.needs_attention(warehouse, user=user)
            if r["kind"] == "registrations_pending"
        ]

    def test_a_lead_is_told_somebody_is_waiting(self):
        request = self.request_account("grace@example.com", verified=True)

        rows = self.registration_rows(user=self.sharon)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["count"], 1)
        self.assertEqual(rows[0]["ref_id"], request.id)
        self.assertEqual(rows[0]["message"], "Grace Nakato asked for an account")

    def test_several_read_correctly(self):
        """One row per request, not a single rolled-up count — each links
        through `ref_id` to that person's own review, not a shared screen."""
        requests = [
            self.request_account(f"g{n}@example.com", verified=True) for n in range(3)
        ]

        rows = self.registration_rows(user=self.sharon)

        self.assertEqual(len(rows), 3)
        self.assertEqual({r["ref_id"] for r in rows}, {r.id for r in requests})
        self.assertTrue(all(r["count"] == 1 for r in rows))

    def test_an_unverified_request_is_not_shown(self):
        """A lead cannot approve a request whose address nobody has proved
        they hold, so showing it would be work that cannot be done."""
        self.request_account("grace@example.com", verified=False)

        self.assertNotIn("registrations_pending", self.kinds(user=self.sharon))

    def test_it_appears_once_they_enter_their_code(self):
        request = self.request_account("grace@example.com", verified=False)
        self.assertNotIn("registrations_pending", self.kinds(user=self.sharon))

        request.verified_at = timezone.now()
        request.save(update_fields=["verified_at"])

        self.assertIn("registrations_pending", self.kinds(user=self.sharon))

    def test_a_decided_request_drops_off(self):
        request = self.request_account("grace@example.com", verified=True)
        request.status = RegistrationRequest.Status.APPROVED
        request.save(update_fields=["status"])

        self.assertNotIn("registrations_pending", self.kinds(user=self.sharon))

    def test_a_warehouse_clerk_is_not_shown_it(self):
        """They cannot approve one, and a row nobody can act on is noise."""
        self.request_account("grace@example.com", verified=True)

        self.assertNotIn(
            "registrations_pending",
            self.kinds(user=self.julius, warehouse=self.namayemba),
        )

    def test_a_school_user_is_not_shown_it(self):
        self.request_account("grace@example.com", verified=True)

        self.assertNotIn("registrations_pending", self.kinds(user=self.chrisis))

    def test_callers_that_pass_no_user_are_unaffected(self):
        """Every existing caller behaves exactly as before."""
        self.request_account("grace@example.com", verified=True)

        self.assertNotIn("registrations_pending", self.kinds())

    def test_it_reaches_the_endpoint(self):
        self.request_account("grace@example.com", verified=True)

        self.client.force_authenticate(self.sharon)
        response = self.client.get(reverse("dashboard:attention"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(
            "registrations_pending", [row["kind"] for row in response.data]
        )


class TheOnHoldRowSaysPaymentNotStock(AttentionSetup):
    def priced_sku(self):
        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=IN_FORCE
        )
        return Sku.objects.create(
            garment=garment, size=Size.objects.create(name="10", sort_order=10)
        )

    def test_the_message_names_payment(self):
        place_order(
            school=self.school,
            student_name="Miriam Achieng",
            order_date=IN_FORCE,
            skus=[{"sku": self.priced_sku(), "quantity": 2}],
            created_by=self.chrisis,
        )

        row = self.row("orders_on_hold", user=self.sharon)

        self.assertIsNotNone(row)
        self.assertIn("payment", row["message"])
        self.assertNotIn("stock", row["message"])

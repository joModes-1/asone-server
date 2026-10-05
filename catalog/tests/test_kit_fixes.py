"""Regressions from the Uniform Kit BOM review.

Three findings, each of which was real:

    1. Listing kits cost one query per component, per kit.
    2. A Primary kit accepted a High-School-only garment.
    3. A retired SKU could be added to a kit.
"""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Kit, KitItem, Size, Sku
from catalog.services import compute_kit_price, kit_prices

IN_FORCE = date(2026, 1, 1)


class KitFixSetup(APITestCase):
    def setUp(self):
        build_sites()
        self.client.force_authenticate(make_user("sharon", User.Role.PROGRAM_LEAD))
        self.size = Size.objects.create(name="S", sort_order=20)

    def sku_for(self, name, level=Garment.SchoolLevel.BOTH, amount="10000.00"):
        garment = Garment.objects.create(name=name, school_level=level)
        if amount is not None:
            GarmentPrice.objects.create(
                garment=garment, unit_price=Decimal(amount), active_date=IN_FORCE
            )
        return Sku.objects.create(garment=garment, size=self.size)

    def make_kit(self, number="PS-01", level=Kit.SchoolLevel.PRIMARY):
        return Kit.objects.create(kit_number=number, name=f"Kit {number}", school_level=level)


class ListingKitsDoesNotScaleWithComponents(KitFixSetup):
    """Finding 1. Measured at 52 queries for ten kits of four items."""

    def setUp(self):
        super().setUp()
        for k in range(10):
            kit = self.make_kit(f"PS-{k:02}")
            for i in range(4):
                KitItem.objects.create(kit=kit, sku=self.sku_for(f"G{k}-{i}"), quantity=2)

    def test_the_kit_list_is_a_fixed_number_of_queries(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse("catalog:kit-list"))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertLessEqual(
            len(queries),
            6,
            msg=f"ran {len(queries)} queries for 10 kits of 4 items — component "
            "prices are being looked up per kit again.",
        )

    def test_the_batched_total_matches_the_direct_one(self):
        """The fast path and the slow path must agree, or a kit shows two prices."""
        listed = {
            row["kit_number"]: row["current_price"]
            for row in self.client.get(reverse("catalog:kit-list")).data["results"]
        }
        for kit in Kit.objects.all():
            with self.subTest(kit=kit.kit_number):
                self.assertEqual(listed[kit.kit_number], str(compute_kit_price(kit)))

    def test_one_unpriceable_kit_does_not_hide_the_others(self):
        broken = self.make_kit("PS-BROKEN")
        KitItem.objects.create(
            kit=broken, sku=self.sku_for("Unpriced", amount=None), quantity=1
        )

        rows = {
            row["kit_number"]: row["current_price"]
            for row in self.client.get(reverse("catalog:kit-list")).data["results"]
        }
        self.assertIsNone(rows["PS-BROKEN"])
        self.assertIsNotNone(rows["PS-00"])

    def test_a_partially_priced_kit_is_null_not_a_short_total(self):
        """Why this is summed in Python: SQL SUM() would skip the NULL."""
        mixed = self.make_kit("PS-MIXED")
        KitItem.objects.create(kit=mixed, sku=self.sku_for("Priced", amount="5000.00"), quantity=1)
        KitItem.objects.create(kit=mixed, sku=self.sku_for("NoPrice", amount=None), quantity=1)

        self.assertIsNone(kit_prices([mixed])[mixed.pk])

    def test_an_empty_kit_is_null(self):
        empty = self.make_kit("PS-EMPTY")
        self.assertIsNone(kit_prices([empty])[empty.pk])


class ComponentsMustSuitTheKit(KitFixSetup):
    """Finding 2. A PS kit must not contain an HS-only garment."""

    def setUp(self):
        super().setUp()
        self.ps_kit = self.make_kit("PS-01", Kit.SchoolLevel.PRIMARY)

    def add(self, sku, kit=None):
        return self.client.post(
            reverse("catalog:kit-item-list"),
            {"kit": (kit or self.ps_kit).pk, "sku": sku.pk, "quantity": 1},
            format="json",
        )

    def test_a_matching_garment_is_accepted(self):
        sku = self.sku_for("PS Tunic", Garment.SchoolLevel.PRIMARY)
        self.assertEqual(self.add(sku).status_code, status.HTTP_201_CREATED)

    def test_a_garment_marked_both_is_accepted(self):
        sku = self.sku_for("Socks", Garment.SchoolLevel.BOTH)
        self.assertEqual(self.add(sku).status_code, status.HTTP_201_CREATED)

    def test_an_opposite_level_garment_is_refused(self):
        sku = self.sku_for("HS Blazer", Garment.SchoolLevel.HIGH)

        response = self.add(sku)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("sku", response.data)

    def test_the_rule_holds_in_the_admin_too(self):
        """Central Office builds kits there, so it is enforced on the model."""
        sku = self.sku_for("HS Blazer", Garment.SchoolLevel.HIGH)
        item = KitItem(kit=self.ps_kit, sku=sku, quantity=1)

        with self.assertRaises(ValidationError):
            item.full_clean()


class RetiredSkusCannotBeAddedToKits(KitFixSetup):
    """Finding 3. A kit containing a retired SKU cannot be fulfilled."""

    def setUp(self):
        super().setUp()
        self.kit = self.make_kit()

    def test_a_retired_sku_is_refused(self):
        sku = self.sku_for("Retired", Garment.SchoolLevel.PRIMARY)
        sku.is_active = False
        sku.save(update_fields=["is_active"])

        response = self.client.post(
            reverse("catalog:kit-item-list"),
            {"kit": self.kit.pk, "sku": sku.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_active_sku_is_accepted(self):
        sku = self.sku_for("Active", Garment.SchoolLevel.PRIMARY)

        response = self.client.post(
            reverse("catalog:kit-item-list"),
            {"kit": self.kit.pk, "sku": sku.pk, "quantity": 1},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_editing_an_existing_line_still_works(self):
        """The copy-not-rebuild pattern: an edit must not read as a new row."""
        sku = self.sku_for("Active", Garment.SchoolLevel.PRIMARY)
        item = KitItem.objects.create(kit=self.kit, sku=sku, quantity=1)

        response = self.client.patch(
            reverse("catalog:kit-item-detail", args=[item.pk]),
            {"quantity": 3},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

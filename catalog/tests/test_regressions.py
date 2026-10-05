"""Regression tests for bugs found by probing untested paths.

Each test here names a bug that was real. They are grouped by what went
wrong rather than by feature, because the failure modes are the point.
"""

from datetime import date
from decimal import Decimal

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, MinimumStockLevel, Size, Sku

IN_FORCE = date(2026, 1, 1)


class RegressionSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.client.force_authenticate(make_user("sharon", User.Role.PROGRAM_LEAD))

        self.shirt = Garment.objects.create(name="White Shirt")
        self.price = GarmentPrice.objects.create(
            garment=self.shirt, unit_price=Decimal("25000.00"), active_date=IN_FORCE
        )
        self.size = Size.objects.create(name="S", sort_order=20)
        self.sku = Sku.objects.create(garment=self.shirt, size=self.size)


class BadInputIsFourHundredNotFiveHundred(RegressionSetup):
    """A caller's mistake must never look like a server fault.

    A 500 sends someone hunting a bug in the server; a 400 tells them what
    they sent was wrong. Getting this backwards wastes an afternoon.
    """

    def test_a_malformed_date_is_rejected_not_crashed_on(self):
        response = self.client.get(
            reverse("catalog:price-list"), {"level": "PS", "on": "not-a-date"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("on", response.data)

    def test_a_malformed_date_on_the_gap_report_too(self):
        response = self.client.get(reverse("catalog:price-gaps"), {"on": "garbage"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_unknown_school_level_is_rejected(self):
        """It used to return 200 with a short list.

        A wrong answer is worse than an error: an error gets reported, and a
        price list quietly missing half its garments does not.
        """
        response = self.client.get(reverse("catalog:price-list"), {"level": "NONSENSE"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("level", response.data)

    def test_an_overlapping_reprice_is_rejected_not_crashed_on(self):
        """`reprice()` raises Django's ValidationError, which DRF did not know."""
        response = self.client.post(
            reverse("catalog:garment-reprice", args=[self.shirt.pk]),
            {"unit_price": "30000.00", "active_from": IN_FORCE.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_duplicate_minimum_stock_level_is_rejected(self):
        MinimumStockLevel.objects.create(
            sku=self.sku, warehouse=self.sites["namayemba"], minimum_quantity=10
        )
        response = self.client.post(
            reverse("catalog:minimum-stock-level-list"),
            {
                "sku": self.sku.pk,
                "warehouse": self.sites["namayemba"].pk,
                "minimum_quantity": 20,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CorrectingAPriceInPlace(RegressionSetup):
    """Repricing adds history. Correcting a typo edits the row.

    The serializer used to rebuild the instance and assign its `pk`, leaving
    `_state.adding` True — so Django treated a correction as a new row with a
    duplicate primary key and refused every update with "already exists".
    """

    def detail_url(self):
        return reverse("catalog:garment-price-detail", args=[self.price.pk])

    def test_the_amount_can_be_corrected(self):
        response = self.client.patch(
            self.detail_url(), {"unit_price": "26000.00"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.price.refresh_from_db()
        self.assertEqual(self.price.unit_price, Decimal("26000.00"))

    def test_an_expiration_date_can_be_set(self):
        response = self.client.patch(
            self.detail_url(), {"expiration_date": "2027-01-01"}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.price.refresh_from_db()
        self.assertEqual(self.price.expiration_date, date(2027, 1, 1))

    def test_a_full_replacement_still_works(self):
        response = self.client.put(
            self.detail_url(),
            {
                "garment": self.shirt.pk,
                "unit_price": "27000.00",
                "active_date": IN_FORCE.isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_correction_still_cannot_create_an_overlap(self):
        """Excluding the row from its own check must not disable the check."""
        # Close the open-ended price first, then open the next one. Doing it
        # the other way round is itself an overlap.
        self.price.expiration_date = date(2027, 1, 1)
        self.price.save(update_fields=["expiration_date"])
        GarmentPrice.objects.create(
            garment=self.shirt,
            unit_price=Decimal("30000.00"),
            active_date=date(2027, 1, 1),
        )

        # Push the earlier price's end past the later price's start.
        response = self.client.patch(
            self.detail_url(), {"expiration_date": "2027-06-01"}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ListingsDoNotScaleWithRowCount(RegressionSetup):
    """Price used to be looked up once per row.

    45 garments meant 46 queries and 200 SKUs meant 201 — on warehouse
    connections that drop, that is the difference between a page loading and
    a page timing out.
    """

    def setUp(self):
        super().setUp()
        for index in range(15):
            garment = Garment.objects.create(name=f"Item {index}")
            GarmentPrice.objects.create(
                garment=garment, unit_price=Decimal("1000.00"), active_date=IN_FORCE
            )
            Sku.objects.create(garment=garment, size=self.size)

    def assert_constant_queries(self, route):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get(reverse(route))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertLessEqual(
            len(queries),
            4,
            msg=f"{route} ran {len(queries)} queries for 16 rows — price is being "
            "looked up per row again.",
        )

    def test_listing_garments_is_a_fixed_number_of_queries(self):
        self.assert_constant_queries("catalog:garment-list")

    def test_listing_skus_is_a_fixed_number_of_queries(self):
        self.assert_constant_queries("catalog:sku-list")

    def test_an_annotated_price_matches_a_looked_up_one(self):
        """The fast path and the slow path must agree.

        The list annotates; a freshly created object falls back to a direct
        lookup. If those ever disagree, the same garment shows two prices.
        """
        listed = {
            row["name"]: row["current_price"]
            for row in self.client.get(reverse("catalog:garment-list")).data["results"]
        }
        detail = self.client.get(
            reverse("catalog:garment-detail", args=[self.shirt.pk])
        ).data

        self.assertEqual(listed[self.shirt.name], detail["current_price"])

    def test_an_unpriced_row_reads_as_null_on_both_paths(self):
        socks = Garment.objects.create(name="Socks")
        listed = {
            row["name"]: row["current_price"]
            for row in self.client.get(reverse("catalog:garment-list")).data["results"]
        }
        detail = self.client.get(
            reverse("catalog:garment-detail", args=[socks.pk])
        ).data

        self.assertIsNone(listed["Socks"])
        self.assertIsNone(detail["current_price"])

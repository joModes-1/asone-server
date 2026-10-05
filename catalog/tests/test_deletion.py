"""What may and may not be deleted.

Master data divides into two kinds, and the line between them is not "how
important is this" but **does the row carry meaning that outlives it**.

    Carries meaning  ->  deactivate, never delete
    Configuration    ->  delete freely; PROTECT stops the dangerous cases

Everything here is about unreferenced rows. A referenced row is already
refused by PROTECT, whatever the HTTP verb says.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, MinimumStockLevel, Size, Sku

IN_FORCE = date(2026, 1, 1)


class DeletionSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.client.force_authenticate(make_user("sharon", User.Role.PROGRAM_LEAD))

        self.shirt = Garment.objects.create(name="White Shirt")
        self.price = GarmentPrice.objects.create(
            garment=self.shirt, unit_price=Decimal("25000.00"), active_date=IN_FORCE
        )
        self.size = Size.objects.create(name="S", sort_order=20)
        self.sku = Sku.objects.create(garment=self.shirt, size=self.size)


class RowsThatCarryMeaningCannotBeDeleted(DeletionSetup):
    def test_a_sku_cannot_be_deleted(self):
        """Its control number is printed on documents and never reissued."""
        response = self.client.delete(reverse("catalog:sku-detail", args=[self.sku.pk]))

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertTrue(Sku.objects.filter(pk=self.sku.pk).exists())

    def test_a_sku_is_retired_by_deactivating_it(self):
        """The path that replaces deletion, so the refusal is not a dead end."""
        response = self.client.patch(
            reverse("catalog:sku-detail", args=[self.sku.pk]),
            {"is_active": False},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.sku.refresh_from_db()
        self.assertFalse(self.sku.is_active)

    def test_a_price_cannot_be_deleted(self):
        """Reprinting a March invoice at March's price needs the March row."""
        response = self.client.delete(
            reverse("catalog:garment-price-detail", args=[self.price.pk])
        )

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertTrue(GarmentPrice.objects.filter(pk=self.price.pk).exists())

    def test_a_mistaken_price_is_corrected_instead(self):
        response = self.client.patch(
            reverse("catalog:garment-price-detail", args=[self.price.pk]),
            {"unit_price": "26000.00"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class ConfigurationRowsMayBeDeleted(DeletionSetup):
    """Deleting these is how a typo gets fixed.

    Blocking them would be strictness for its own sake: Size has no
    `is_active`, so a mistyped "XLL" would clutter every dropdown forever.
    """

    def test_an_unused_size_can_be_deleted(self):
        typo = Size.objects.create(name="XLL", sort_order=99)

        response = self.client.delete(reverse("catalog:size-detail", args=[typo.pk]))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_an_unused_garment_can_be_deleted(self):
        mistake = Garment.objects.create(name="Typo Garment")

        response = self.client.delete(
            reverse("catalog:garment-detail", args=[mistake.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_a_minimum_stock_level_can_be_deleted(self):
        """A reorder floor is configuration, not history."""
        level = MinimumStockLevel.objects.create(
            sku=self.sku, warehouse=self.sites["namayemba"], minimum_quantity=100
        )

        response = self.client.delete(
            reverse("catalog:minimum-stock-level-detail", args=[level.pk])
        )
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)


class ProtectStopsTheDangerousCases(DeletionSetup):
    """Where DELETE is still allowed, the database is the backstop.

    A refused delete is a 409, not a 400: the request was well formed and
    nothing the caller changes about it would help — something else has to
    stop referencing the row first. It used to be a 500.
    """

    def test_a_size_in_use_by_a_sku_cannot_be_deleted(self):
        response = self.client.delete(reverse("catalog:size-detail", args=[self.size.pk]))

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(Size.objects.filter(pk=self.size.pk).exists())

    def test_a_garment_with_skus_cannot_be_deleted(self):
        response = self.client.delete(
            reverse("catalog:garment-detail", args=[self.shirt.pk])
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertTrue(Garment.objects.filter(pk=self.shirt.pk).exists())

    def test_a_warehouse_with_staff_cannot_be_deleted(self):
        response = self.client.delete(
            reverse("catalog:warehouse-detail", args=[self.sites["namayemba"].pk])
        )
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_the_refusal_names_what_is_still_using_the_row(self):
        """"Cannot delete Namayemba" is far less useful than naming the school."""
        response = self.client.delete(
            reverse("catalog:warehouse-detail", args=[self.sites["namayemba"].pk])
        )

        self.assertIn("in_use_by", response.data)
        self.assertTrue(response.data["in_use_by"], "should name the blocking rows")

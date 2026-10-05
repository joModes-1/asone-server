"""Uniform Kits — the bill of materials, and its derived price.

Two things are being protected here, mirroring pricing.py's own framing:

    1. A kit's price is never stored — it is the live sum of its
       components' current prices, so it can never go stale.
    2. Deleting a kit takes its line items with it (CASCADE), but deleting
       a SKU that a kit still depends on is refused (PROTECT) — see
       KitItem's model docstring for why these are opposite defaults.
"""

from datetime import timedelta
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.db.models.deletion import ProtectedError
from django.test import TestCase

from catalog.models import KitItem, Size, Sku
from catalog.services import EmptyKit, PriceNotSet, compute_kit_price, reprice

from .factories import SEASON_START, make_garment, make_kit, make_kit_item, make_price


def make_sku(garment, size_name="S", sort_order=10):
    size = Size.objects.create(name=size_name, sort_order=sort_order)
    return Sku.objects.create(garment=garment, size=size)


class KitIdentityTests(TestCase):
    def test_kit_number_must_be_unique(self):
        make_kit(kit_number="PS-STARTER-01")

        with self.assertRaises(IntegrityError), transaction.atomic():
            make_kit(kit_number="PS-STARTER-01", name="A different kit")


class KitItemUniquenessTests(TestCase):
    def setUp(self):
        self.kit = make_kit()
        self.sku = make_sku(make_garment())

    def test_a_sku_can_only_appear_once_per_kit(self):
        """Two rows for the same SKU would split its quantity across two lines."""
        make_kit_item(self.kit, self.sku, quantity=1)

        with self.assertRaises(IntegrityError), transaction.atomic():
            KitItem.objects.create(kit=self.kit, sku=self.sku, quantity=2)

    def test_the_same_sku_may_appear_in_two_different_kits(self):
        other_kit = make_kit(kit_number="PS-STARTER-02", name="Another kit")

        make_kit_item(self.kit, self.sku, quantity=1)
        make_kit_item(other_kit, self.sku, quantity=3)

        self.assertEqual(KitItem.objects.filter(sku=self.sku).count(), 2)


class KitDeletionTests(TestCase):
    """Decision: KitItem.kit is CASCADE, KitItem.sku stays PROTECT."""

    def setUp(self):
        self.kit = make_kit()
        self.sku = make_sku(make_garment())
        self.item = make_kit_item(self.kit, self.sku, quantity=2)

    def test_deleting_a_kit_deletes_its_line_items(self):
        """A line item has no meaning apart from the kit it belongs to."""
        self.kit.delete()

        self.assertFalse(KitItem.objects.filter(pk=self.item.pk).exists())

    def test_deleting_a_sku_used_in_a_kit_is_refused(self):
        """A SKU is real master data independent of any one kit — PROTECT
        stays the default here, unlike the kit side of the same model."""
        with self.assertRaises(ProtectedError):
            self.sku.delete()


class KitPricingTests(TestCase):
    """Kit price is the live sum of its components — see compute_kit_price()."""

    def setUp(self):
        self.shirt = make_garment("White Shirt")
        self.trousers = make_garment("Grey Trousers")
        self.shirt_sku = make_sku(self.shirt, "S", 20)
        self.trousers_sku = make_sku(self.trousers, "12", 12)
        self.kit = make_kit()

    def test_kit_price_is_the_sum_of_its_components(self):
        make_price(self.shirt, "25000.00", SEASON_START)
        make_price(self.trousers, "35000.00", SEASON_START)
        make_kit_item(self.kit, self.shirt_sku, quantity=2)
        make_kit_item(self.kit, self.trousers_sku, quantity=1)

        # 2 shirts @ 25000.00 + 1 trousers @ 35000.00
        self.assertEqual(compute_kit_price(self.kit, SEASON_START), Decimal("85000.00"))

    def test_kit_price_raises_when_component_has_no_price(self):
        """A kit missing one component's price must refuse to price itself
        entirely, rather than quietly returning a total that is short."""
        make_price(self.shirt, "25000.00", SEASON_START)
        # self.trousers is deliberately left unpriced.
        make_kit_item(self.kit, self.shirt_sku, quantity=1)
        make_kit_item(self.kit, self.trousers_sku, quantity=1)

        with self.assertRaises(PriceNotSet):
            compute_kit_price(self.kit, SEASON_START)

    def test_an_empty_kit_raises_rather_than_pricing_at_zero(self):
        """No components at all is far more likely to be an unfinished bill
        of materials than a genuine free kit."""
        with self.assertRaises(EmptyKit):
            compute_kit_price(self.kit, SEASON_START)

    def test_kit_price_is_dated_like_garment_price(self):
        """An invoice built from a kit must still reprint at the price on the
        day it was raised — the whole reason compute_kit_price() takes a
        date instead of only ever answering for today."""
        make_price(self.shirt, "25000.00", SEASON_START)
        make_kit_item(self.kit, self.shirt_sku, quantity=1)

        changeover = SEASON_START + timedelta(days=180)
        reprice(self.shirt, Decimal("30000.00"), changeover)

        self.assertEqual(
            compute_kit_price(self.kit, changeover - timedelta(days=1)), Decimal("25000.00")
        )
        self.assertEqual(compute_kit_price(self.kit, changeover), Decimal("30000.00"))

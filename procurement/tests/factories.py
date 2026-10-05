"""Shared fixtures for the procurement tests."""

from datetime import date
from decimal import Decimal

from catalog.models import Garment, GarmentPrice, Size, Sku

#: Prices in force well before any order date used in these tests.
PRICED_FROM = date(2026, 1, 1)
ORDER_DATE = date(2026, 9, 1)


def make_priced_sku(name="White Shirt", size_name="S", amount="25000.00"):
    """A SKU whose garment has a price — the normal case for ordering."""
    garment, _ = Garment.objects.get_or_create(name=name)
    GarmentPrice.objects.get_or_create(
        garment=garment,
        active_date=PRICED_FROM,
        defaults={"unit_price": Decimal(amount)},
    )
    size, _ = Size.objects.get_or_create(name=size_name, defaults={"sort_order": 10})
    return Sku.objects.create(garment=garment, size=size)


def make_unpriced_sku(name="Blazer", size_name="M"):
    """A SKU whose garment has no price — an order for it cannot be costed."""
    garment, _ = Garment.objects.get_or_create(name=name)
    size, _ = Size.objects.get_or_create(name=size_name, defaults={"sort_order": 12})
    return Sku.objects.create(garment=garment, size=size)

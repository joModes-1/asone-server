"""SKUs and minimum stock levels (F06, F07)."""

from datetime import date
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from catalog.models import Garment, MinimumStockLevel, Size, Sku, Warehouse
from catalog.services import PriceNotSet, price_for_sku

from .factories import SEASON_START, make_garment, make_price


class SkuNumberTests(TestCase):
    """AsOne's control number: system assigned, unique, and stable.

    It used to be a bare sequence value — `100015` — and now reads `WSH-10`:
    the garment's code, then the size. The guarantee changed shape with it,
    and the change is worth stating because one of these tests used to assert
    the opposite.

    **Before.** Uniqueness came from a Postgres sequence, which never goes
    backwards. Deleting a SKU and creating the same product again gave a
    *different* number, and that was the point: the number was an arbitrary
    token, so reusing it would have let one token mean two things.

    **Now.** The code *is* the product — this garment, this size — so
    recreating White Shirt size 10 gives WSH-10 again, and that is correct
    rather than a regression. The property AsOne actually needs is that a
    code printed on a 2027 packing list still means the same product in
    2035, and a derived code gives that by construction instead of by
    bookkeeping.

    What has to hold for that to be true is tested here: a garment's code is
    frozen once assigned, and a SKU's number never changes on save.
    """

    def setUp(self):
        self.shirt = make_garment()
        self.size_10 = Size.objects.create(name="10", sort_order=10)
        self.size_12 = Size.objects.create(name="12", sort_order=12)

    def test_a_number_is_assigned_automatically(self):
        sku = Sku.objects.create(garment=self.shirt, size=self.size_10)

        self.assertEqual(sku.number, f"{self.shirt.code}-10")

    def test_numbers_do_not_repeat(self):
        first = Sku.objects.create(garment=self.shirt, size=self.size_10)
        second = Sku.objects.create(garment=self.shirt, size=self.size_12)

        self.assertNotEqual(first.number, second.number)

    def test_the_same_product_gets_the_same_code_again(self):
        """The code is the product, so recreating it gives the same code.

        The inverse of what a sequence did, and deliberately — see the class
        docstring. Nothing else in the system can reach this state anyway:
        SKUs are deactivated rather than deleted, because the ledger points
        at them and PROTECT would refuse.
        """
        sku = Sku.objects.create(garment=self.shirt, size=self.size_10)
        retired = sku.number
        sku.delete()

        replacement = Sku.objects.create(garment=self.shirt, size=self.size_10)
        self.assertEqual(replacement.number, retired)

    def test_an_existing_number_never_changes_on_save(self):
        """It is printed on pick lists — it must mean the same thing forever."""
        sku = Sku.objects.create(garment=self.shirt, size=self.size_10)
        original = sku.number

        sku.is_active = False
        sku.save()

        sku.refresh_from_db()
        self.assertEqual(sku.number, original)

    def test_a_garment_code_survives_a_rename(self):
        """Shelf labels are already printed. A tidied name must not move them."""
        original = self.shirt.code

        self.shirt.name = "White Shirt (long sleeve)"
        self.shirt.save()

        self.shirt.refresh_from_db()
        self.assertEqual(self.shirt.code, original)

    def test_a_clashing_stem_takes_a_suffix(self):
        """One name on both price lists is two garments wanting one code.

        "White Shirt" for Primary and "White Shirt" for High School are
        separate rows — they can carry different prices — and both derive
        WSH. The first keeps it; the second takes a digit.
        """
        primary = make_garment("Blue Tunic", Garment.SchoolLevel.PRIMARY)
        high = make_garment("Blue Tunic", Garment.SchoolLevel.HIGH)

        self.assertEqual(primary.code, "BTU")
        self.assertEqual(high.code, "BTU2")

    def test_a_size_with_punctuation_does_not_split_the_code(self):
        """"E2E-12" must not put a second hyphen in E2E Tunic's code."""
        tunic = make_garment("E2E Tunic", Garment.SchoolLevel.PRIMARY)
        odd_size = Size.objects.create(name="E2E-12", sort_order=12)

        sku = Sku.objects.create(garment=tunic, size=odd_size)

        self.assertEqual(sku.number, "ETU-E2E12")


class SkuIdentityTests(TestCase):
    def setUp(self):
        self.shirt = make_garment("White Shirt", Garment.SchoolLevel.PRIMARY, colour="White")
        self.size_10 = Size.objects.create(name="10", sort_order=10)

    def test_a_garment_and_size_pair_is_unique(self):
        """Two rows for the same product would split its stock in two."""
        Sku.objects.create(garment=self.shirt, size=self.size_10)

        with self.assertRaises(IntegrityError), transaction.atomic():
            Sku.objects.create(garment=self.shirt, size=self.size_10)

    def test_the_description_is_built_from_the_garment_and_size(self):
        sku = Sku.objects.create(garment=self.shirt, size=self.size_10)

        self.assertEqual(sku.description, "White Shirt White size 10 (PS)")

    def test_a_supplied_description_is_kept(self):
        sku = Sku.objects.create(
            garment=self.shirt, size=self.size_10, description="Custom label"
        )
        self.assertEqual(sku.description, "Custom label")

    def test_skus_sort_in_description_order(self):
        """Pick lists print "in Description sequence" (p.2)."""
        size_8 = Size.objects.create(name="8", sort_order=8)
        Sku.objects.create(garment=self.shirt, size=self.size_10)
        Sku.objects.create(garment=self.shirt, size=size_8)

        descriptions = list(Sku.objects.values_list("description", flat=True))
        self.assertEqual(descriptions, sorted(descriptions))


class SkuPricingTests(TestCase):
    """Price does not vary by size — every size reads through to the garment."""

    def setUp(self):
        self.shirt = make_garment()
        self.sizes = [
            Size.objects.create(name=str(n), sort_order=n) for n in (8, 10, 12, 14)
        ]
        self.skus = [
            Sku.objects.create(garment=self.shirt, size=size) for size in self.sizes
        ]

    def test_every_size_of_a_garment_costs_the_same(self):
        make_price(self.shirt, "25000.00", SEASON_START)

        prices = {price_for_sku(sku, SEASON_START) for sku in self.skus}
        self.assertEqual(prices, {Decimal("25000.00")})

    def test_repricing_the_garment_moves_every_size_at_once(self):
        make_price(self.shirt, "25000.00", SEASON_START)
        from catalog.services import reprice

        reprice(self.shirt, Decimal("30000.00"), date(2027, 6, 1))

        for sku in self.skus:
            self.assertEqual(price_for_sku(sku, date(2027, 7, 1)), Decimal("30000.00"))

    def test_an_unpriced_garment_leaves_its_skus_unpriced(self):
        with self.assertRaises(PriceNotSet):
            price_for_sku(self.skus[0], SEASON_START)


class MinimumStockLevelTests(TestCase):
    """F07 — the level that triggers a replenishment order, per warehouse."""

    def setUp(self):
        self.shirt = make_garment()
        self.size_10 = Size.objects.create(name="10", sort_order=10)
        self.sku = Sku.objects.create(garment=self.shirt, size=self.size_10)
        self.namayemba = Warehouse.objects.create(name="Namayemba")
        self.serere = Warehouse.objects.create(name="Serere")

    def test_a_sku_can_have_a_different_floor_at_each_warehouse(self):
        """The two warehouses serve different numbers of schools."""
        MinimumStockLevel.objects.create(
            sku=self.sku, warehouse=self.namayemba, minimum_quantity=100
        )
        MinimumStockLevel.objects.create(
            sku=self.sku, warehouse=self.serere, minimum_quantity=40
        )

        self.assertEqual(self.sku.minimum_levels.count(), 2)

    def test_only_one_floor_per_sku_per_warehouse(self):
        MinimumStockLevel.objects.create(
            sku=self.sku, warehouse=self.namayemba, minimum_quantity=100
        )

        with self.assertRaises(IntegrityError), transaction.atomic():
            MinimumStockLevel.objects.create(
                sku=self.sku, warehouse=self.namayemba, minimum_quantity=50
            )

    def test_a_floor_of_zero_is_allowed(self):
        """A SKU that should never be reordered automatically."""
        level = MinimumStockLevel.objects.create(
            sku=self.sku, warehouse=self.namayemba, minimum_quantity=0
        )
        self.assertEqual(level.minimum_quantity, 0)

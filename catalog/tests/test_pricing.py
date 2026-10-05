"""Garment pricing (F08).

The rules being protected, in the order they matter:

    1. A garment has at most one price on any given day.
    2. Price does not vary by size.
    3. Repricing adds history; it never rewrites it.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from catalog.models import Garment, GarmentPrice
from catalog.services import (
    PriceNotSet,
    garments_without_a_price,
    price_for,
    price_list,
    reprice,
)

from .factories import SEASON_START, make_garment, make_price


class PriceIsNeverAmbiguousTests(TestCase):
    """Rule 1, enforced by the database rather than by application code."""

    def setUp(self):
        self.shirt = make_garment()

    def test_two_prices_covering_the_same_day_are_refused(self):
        make_price(self.shirt, "25000.00", SEASON_START)

        with self.assertRaises(IntegrityError), transaction.atomic():
            GarmentPrice.objects.create(
                garment=self.shirt,
                unit_price=Decimal("30000.00"),
                active_date=SEASON_START + timedelta(days=30),
            )

    def test_an_overlap_surfaces_as_a_validation_error_on_full_clean(self):
        """So the admin shows a message instead of a 500."""
        make_price(self.shirt, "25000.00", SEASON_START)

        clash = GarmentPrice(
            garment=self.shirt,
            unit_price=Decimal("30000.00"),
            active_date=SEASON_START + timedelta(days=30),
        )
        with self.assertRaises(ValidationError):
            clash.full_clean()

    def test_consecutive_prices_may_meet_exactly(self):
        """The changeover day belongs to the new price, not both.

        This is why expiration_date is exclusive — an inclusive end would make
        these two overlap on 1 June and the second insert would be refused.
        """
        changeover = date(2027, 6, 1)
        make_price(self.shirt, "25000.00", SEASON_START, expires=changeover)
        make_price(self.shirt, "30000.00", changeover)

        self.assertEqual(price_for(self.shirt, changeover - timedelta(days=1)),
                         Decimal("25000.00"))
        self.assertEqual(price_for(self.shirt, changeover), Decimal("30000.00"))

    def test_different_garments_may_share_a_period(self):
        trousers = make_garment("Grey Trousers")
        make_price(self.shirt, "25000.00", SEASON_START)
        make_price(trousers, "35000.00", SEASON_START)

        self.assertEqual(GarmentPrice.objects.count(), 2)

    def test_a_price_must_be_positive(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            GarmentPrice.objects.create(
                garment=self.shirt, unit_price=Decimal("0.00"), active_date=SEASON_START
            )

    def test_a_price_cannot_expire_before_it_starts(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            GarmentPrice.objects.create(
                garment=self.shirt,
                unit_price=Decimal("25000.00"),
                active_date=SEASON_START,
                expiration_date=SEASON_START - timedelta(days=1),
            )


class PriceLookupTests(TestCase):
    def setUp(self):
        self.shirt = make_garment()

    def test_a_price_applies_from_its_active_date(self):
        make_price(self.shirt, "25000.00", SEASON_START)

        self.assertEqual(price_for(self.shirt, SEASON_START), Decimal("25000.00"))

    def test_a_price_does_not_apply_the_day_before(self):
        make_price(self.shirt, "25000.00", SEASON_START)

        with self.assertRaises(PriceNotSet):
            price_for(self.shirt, SEASON_START - timedelta(days=1))

    def test_a_price_does_not_apply_on_its_expiration_date(self):
        """Exclusive end. The expiration date is the first day it is gone."""
        ends = date(2027, 6, 1)
        make_price(self.shirt, "25000.00", SEASON_START, expires=ends)

        self.assertEqual(price_for(self.shirt, ends - timedelta(days=1)),
                         Decimal("25000.00"))
        with self.assertRaises(PriceNotSet):
            price_for(self.shirt, ends)

    def test_an_unpriced_garment_raises_rather_than_returning_zero(self):
        """A free uniform on an invoice is worse than a loud failure."""
        with self.assertRaises(PriceNotSet):
            price_for(self.shirt, SEASON_START)

    def test_an_open_ended_price_applies_indefinitely(self):
        make_price(self.shirt, "25000.00", SEASON_START)

        self.assertEqual(price_for(self.shirt, date(2035, 1, 1)), Decimal("25000.00"))


class PriceListTests(TestCase):
    """AsOne asked for separate PS and HS price lists (p.7)."""

    def setUp(self):
        self.ps_shirt = make_garment("PS White Shirt", Garment.SchoolLevel.PRIMARY)
        self.hs_shirt = make_garment("HS White Shirt", Garment.SchoolLevel.HIGH)
        self.socks = make_garment("Socks", Garment.SchoolLevel.BOTH)

        for garment, amount in (
            (self.ps_shirt, "20000.00"),
            (self.hs_shirt, "25000.00"),
            (self.socks, "5000.00"),
        ):
            make_price(garment, amount, SEASON_START)

    def names_on(self, level):
        return [row["garment"].name for row in price_list(level, SEASON_START)]

    def test_a_primary_list_excludes_high_school_garments(self):
        self.assertEqual(self.names_on(Garment.SchoolLevel.PRIMARY),
                         ["PS White Shirt", "Socks"])

    def test_a_high_school_list_excludes_primary_garments(self):
        self.assertEqual(self.names_on(Garment.SchoolLevel.HIGH),
                         ["HS White Shirt", "Socks"])

    def test_a_garment_marked_both_appears_on_each_list(self):
        self.assertIn("Socks", self.names_on(Garment.SchoolLevel.PRIMARY))
        self.assertIn("Socks", self.names_on(Garment.SchoolLevel.HIGH))

    def test_inactive_garments_are_left_off(self):
        self.socks.is_active = False
        self.socks.save(update_fields=["is_active"])

        self.assertNotIn("Socks", self.names_on(Garment.SchoolLevel.PRIMARY))

    def test_an_unpriced_garment_is_omitted_not_shown_at_zero(self):
        make_garment("Blazer", Garment.SchoolLevel.PRIMARY)

        self.assertNotIn("Blazer", self.names_on(Garment.SchoolLevel.PRIMARY))

    def test_the_list_costs_two_queries_regardless_of_size(self):
        """Guards against an N+1 as the catalogue grows towards 45 garments."""
        for index in range(20):
            garment = make_garment(f"Item {index}", Garment.SchoolLevel.PRIMARY)
            make_price(garment, "1000.00", SEASON_START)

        with self.assertNumQueries(2):
            price_list(Garment.SchoolLevel.PRIMARY, SEASON_START)

    def test_the_gap_report_finds_unpriced_garments(self):
        blazer = make_garment("Blazer", Garment.SchoolLevel.PRIMARY)

        gaps = garments_without_a_price(SEASON_START)
        self.assertEqual([g.name for g in gaps], [blazer.name])


class RepricingTests(TestCase):
    """Rule 3. Repricing is additive — an old invoice must still reprint."""

    def setUp(self):
        self.shirt = make_garment()
        self.original = make_price(self.shirt, "25000.00", SEASON_START)

    def test_repricing_closes_the_previous_price(self):
        changeover = date(2027, 6, 1)
        reprice(self.shirt, Decimal("30000.00"), changeover)

        self.original.refresh_from_db()
        self.assertEqual(self.original.expiration_date, changeover)

    def test_repricing_leaves_the_old_amount_intact(self):
        reprice(self.shirt, Decimal("30000.00"), date(2027, 6, 1))

        self.original.refresh_from_db()
        self.assertEqual(self.original.unit_price, Decimal("25000.00"))

    def test_an_invoice_raised_in_march_still_costs_out_at_march_prices(self):
        """The whole reason prices are dated."""
        reprice(self.shirt, Decimal("30000.00"), date(2027, 6, 1))

        self.assertEqual(price_for(self.shirt, date(2027, 3, 15)), Decimal("25000.00"))
        self.assertEqual(price_for(self.shirt, date(2027, 9, 15)), Decimal("30000.00"))

    def test_repricing_twice_builds_a_history(self):
        reprice(self.shirt, Decimal("30000.00"), date(2027, 6, 1))
        reprice(self.shirt, Decimal("35000.00"), date(2027, 9, 1))

        self.assertEqual(self.shirt.prices.count(), 3)
        self.assertEqual(price_for(self.shirt, date(2027, 7, 1)), Decimal("30000.00"))


class PriceDoesNotVaryBySizeTests(TestCase):
    """Rule 2, and the reason price hangs off Garment rather than SKU.

    There is no test that two sizes cost the same, because there is nowhere to
    express a per-size price. That is the design working.
    """

    def test_price_is_reachable_from_the_garment_alone(self):
        shirt = make_garment()
        make_price(shirt, "25000.00", SEASON_START)

        self.assertEqual(price_for(shirt, SEASON_START), Decimal("25000.00"))

    def test_a_garment_price_has_no_size_field(self):
        self.assertNotIn(
            "size", [field.name for field in GarmentPrice._meta.get_fields()]
        )


class KitPriceListTests(TestCase):
    """F15 and F51 at kit level — the half the checklist asks for and the
    garment list does not cover.

    A kit is what a school actually buys, so a price list without kits on it
    is only half a price list.
    """

    def setUp(self):
        from catalog.models import Kit, KitItem, Size, Sku

        self.shirt = make_garment("White Shirt", Garment.SchoolLevel.PRIMARY)
        self.socks = make_garment("Socks", Garment.SchoolLevel.BOTH)
        make_price(self.shirt, "25000.00", SEASON_START)
        make_price(self.socks, "5000.00", SEASON_START)

        size = Size.objects.create(name="S", sort_order=20)
        self.shirt_sku = Sku.objects.create(garment=self.shirt, size=size)
        self.socks_sku = Sku.objects.create(garment=self.socks, size=size)

        self.kit = Kit.objects.create(
            kit_number="PS-STARTER", name="PS Starter Kit",
            school_level=Kit.SchoolLevel.PRIMARY,
        )
        KitItem.objects.create(kit=self.kit, sku=self.shirt_sku, quantity=2)
        KitItem.objects.create(kit=self.kit, sku=self.socks_sku, quantity=3)

    def rows(self, level=None):
        from catalog.models import Kit
        from catalog.services import kit_price_list

        return kit_price_list(level or Kit.SchoolLevel.PRIMARY, SEASON_START)

    def test_a_kit_is_priced_as_the_sum_of_its_components(self):
        """Two shirts at 25,000 plus three socks at 5,000 — 65,000."""
        row = self.rows()[0]

        self.assertEqual(row["kit"], self.kit)
        self.assertEqual(row["unit_price"], Decimal("65000.00"))

    def test_the_item_count_counts_quantities_not_lines(self):
        """Two shirts and three socks is five garments, on two lines."""
        self.assertEqual(self.rows()[0]["item_count"], 5)

    def test_a_kit_appears_on_one_list_only(self):
        from catalog.models import Kit

        self.assertEqual(len(self.rows(Kit.SchoolLevel.PRIMARY)), 1)
        self.assertEqual(self.rows(Kit.SchoolLevel.HIGH), [])

    def test_an_inactive_kit_is_left_off(self):
        self.kit.is_active = False
        self.kit.save()

        self.assertEqual(self.rows(), [])

    def test_a_kit_with_an_unpriced_component_is_omitted_entirely(self):
        """Not shown short by that component — the whole line goes.

        A price list is a document a school orders from, and a kit priced at
        less than its contents is worse than a kit that is not offered.
        """
        from catalog.models import Kit, KitItem, Size, Sku

        pe = make_garment("PE Shorts", Garment.SchoolLevel.PRIMARY)
        KitItem.objects.create(
            kit=self.kit,
            sku=Sku.objects.create(
                garment=pe, size=Size.objects.create(name="M", sort_order=30)
            ),
            quantity=1,
        )

        self.assertEqual(self.rows(), [])
        self.assertEqual(Kit.objects.filter(is_active=True).count(), 1)

    def test_a_kit_with_no_components_is_omitted(self):
        from catalog.models import Kit

        Kit.objects.create(
            kit_number="EMPTY", name="Empty Kit",
            school_level=Kit.SchoolLevel.PRIMARY,
        )

        self.assertEqual([row["kit"] for row in self.rows()], [self.kit])


class KitPriceGapTests(TestCase):
    """The gap report behind the kit list.

    Its job is to name the *component* at fault. A kit going missing looks
    like a problem with the kit, and it almost never is.
    """

    def setUp(self):
        from catalog.models import Kit, KitItem, Size, Sku

        self.shirt = make_garment("White Shirt", Garment.SchoolLevel.PRIMARY)
        self.pe = make_garment("PE Shorts", Garment.SchoolLevel.PRIMARY)
        make_price(self.shirt, "25000.00", SEASON_START)  # PE Shorts: unpriced

        size = Size.objects.create(name="S", sort_order=20)
        self.kit = Kit.objects.create(
            kit_number="PS-STARTER", name="PS Starter Kit",
            school_level=Kit.SchoolLevel.PRIMARY,
        )
        for garment in (self.shirt, self.pe):
            KitItem.objects.create(
                kit=self.kit,
                sku=Sku.objects.create(garment=garment, size=size),
                quantity=1,
            )

    def gaps(self):
        from catalog.services import kits_without_a_price

        return kits_without_a_price(SEASON_START)

    def test_the_unpriceable_kit_is_reported(self):
        self.assertEqual([row["kit"] for row in self.gaps()], [self.kit])

    def test_it_names_the_component_at_fault(self):
        """The whole point — "PS Starter Kit is unpriced" sends somebody to
        the wrong record."""
        self.assertEqual(self.gaps()[0]["unpriced_components"], ["PE Shorts"])

    def test_a_priced_component_is_not_blamed(self):
        self.assertNotIn("White Shirt", self.gaps()[0]["unpriced_components"])

    def test_an_empty_kit_is_told_apart_from_a_missing_price(self):
        """Same symptom, different problem."""
        from catalog.models import Kit

        empty = Kit.objects.create(
            kit_number="EMPTY", name="Empty Kit",
            school_level=Kit.SchoolLevel.PRIMARY,
        )

        row = next(r for r in self.gaps() if r["kit"] == empty)

        self.assertTrue(row["has_no_items"])
        self.assertEqual(row["unpriced_components"], [])

    def test_pricing_the_component_clears_the_gap(self):
        make_price(self.pe, "12000.00", SEASON_START)

        self.assertEqual(self.gaps(), [])

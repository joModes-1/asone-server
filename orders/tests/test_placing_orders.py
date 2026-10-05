"""Placing a school order — F30, F31, F32, F33.

Four checklist items, one document. The rules being protected:

    1. A kit is exploded into SKUs when the order is placed, and stored.
    2. The price is the price on the day, and never moves afterwards.
    3. An order lands on Hold and nothing here can move it off.
    4. A school orders from its own level, and nothing retired.

Rule 1 is the one with teeth. If explosion were computed on read instead of
stored, editing a kit's bill of materials next term would silently change
what a parent already paid for.
"""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import TestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Kit, KitItem, Size, Sku
from catalog.services import PriceNotSet, reprice
from orders.models import SchoolOrder, SchoolOrderLine
from orders.models.school_orders import OrderStatus
from orders.services import (
    EmptyOrder,
    InactiveItem,
    WrongSchoolLevel,
    order_demand,
    place_order,
)

IN_FORCE = date(2026, 1, 1)
ORDERED_ON = date(2026, 11, 10)
Level = Garment.SchoolLevel


class OrderSetup(TestCase):
    def setUp(self):
        self.sites = build_sites()
        self.school = self.sites["school_a"]           # Namayemba PS, Primary
        self.high_school = self.sites["school_b"]      # Serere HS, High
        self.clerk = make_user("chrisis", User.Role.SCHOOL_STAFF, school=self.school)

        self.size = Size.objects.create(name="S", sort_order=20)
        self.shirt = self.priced_sku("White Shirt", Level.BOTH, "25000.00")
        self.tunic = self.priced_sku("Blue Tunic", Level.PRIMARY, "30000.00")
        self.socks = self.priced_sku("Socks", Level.BOTH, "5000.00")
        self.blazer = self.priced_sku("HS Blazer", Level.HIGH, "60000.00")

        # A starter kit: two shirts, one tunic.
        self.kit = Kit.objects.create(
            kit_number="PS-STARTER", name="PS Starter Kit", school_level=Kit.SchoolLevel.PRIMARY
        )
        KitItem.objects.create(kit=self.kit, sku=self.shirt, quantity=2)
        KitItem.objects.create(kit=self.kit, sku=self.tunic, quantity=1)

    def priced_sku(self, name, level, price):
        garment = Garment.objects.create(name=name, school_level=level)
        if price is not None:
            GarmentPrice.objects.create(
                garment=garment, unit_price=Decimal(price), active_date=IN_FORCE
            )
        size = Size.objects.create(name=f"{name[:6]}-10", sort_order=10)
        return Sku.objects.create(garment=garment, size=size)

    def place(self, kits=(), skus=(), school=None, name="Miriam Achieng"):
        return place_order(
            school=school or self.school,
            student_name=name,
            order_date=ORDERED_ON,
            kits=kits,
            skus=skus,
            created_by=self.clerk,
        )


class AKitBecomesItsComponents(OrderSetup):
    """F33. The warehouse picks garments, never "a kit"."""

    def test_a_kit_order_produces_a_line_per_component(self):
        order = self.place(kits=[{"kit": self.kit, "quantity": 1}])

        self.assertEqual(order.lines.count(), 2)
        self.assertEqual(
            sorted(line.sku.id for line in order.lines.all()),
            sorted([self.shirt.id, self.tunic.id]),
        )

    def test_component_quantities_multiply_by_the_number_of_kits(self):
        """Two starter kits is four shirts, not two."""
        order = self.place(kits=[{"kit": self.kit, "quantity": 2}])

        shirt_line = order.lines.get(sku=self.shirt)
        self.assertEqual(shirt_line.quantity, 4)

    def test_each_line_remembers_the_kit_it_came_from(self):
        """So the invoice can show the school the kit it actually chose."""
        order = self.place(kits=[{"kit": self.kit, "quantity": 1}])

        for line in order.lines.all():
            self.assertEqual(line.from_kit, self.kit)

    def test_editing_the_kit_afterwards_does_not_change_the_order(self):
        """The rule this whole design exists for.

        A parent paid for two shirts and a tunic. Central Office adding socks
        to the kit next term must not retrospectively change what they bought.
        """
        order = self.place(kits=[{"kit": self.kit, "quantity": 1}])
        KitItem.objects.create(kit=self.kit, sku=self.socks, quantity=3)

        order.refresh_from_db()
        self.assertEqual(order.lines.count(), 2)
        self.assertNotIn(self.socks.id, [line.sku_id for line in order.lines.all()])

    def test_a_kit_and_a_loose_item_of_the_same_sku_stay_separate(self):
        """Two shirts in a kit plus one loose is three shirts — but the
        invoice has to show which two came from the kit."""
        order = self.place(
            kits=[{"kit": self.kit, "quantity": 1}],
            skus=[{"sku": self.shirt, "quantity": 1}],
        )

        shirt_lines = order.lines.filter(sku=self.shirt)
        self.assertEqual(shirt_lines.count(), 2)
        self.assertEqual({line.from_kit for line in shirt_lines}, {self.kit, None})

    def test_the_pick_view_adds_them_back_together(self):
        """A pick list does not care where a shirt came from."""
        order = self.place(
            kits=[{"kit": self.kit, "quantity": 1}],
            skus=[{"sku": self.shirt, "quantity": 1}],
        )

        demand = dict(order_demand(order))
        self.assertEqual(demand[self.shirt], 3)
        self.assertEqual(demand[self.tunic], 1)


class ThePriceIsTheOneOnTheDay(OrderSetup):
    """F30. An invoice reprinted next term must still add up."""

    def test_lines_carry_the_price_at_order_time(self):
        order = self.place(skus=[{"sku": self.shirt, "quantity": 2}])

        self.assertEqual(order.lines.get().unit_price, Decimal("25000.00"))

    def test_repricing_afterwards_does_not_restate_the_order(self):
        order = self.place(skus=[{"sku": self.shirt, "quantity": 2}])
        reprice(self.shirt.garment, Decimal("99000.00"), date(2026, 12, 1))

        order.refresh_from_db()
        self.assertEqual(order.lines.get().unit_price, Decimal("25000.00"))

    def test_the_total_is_summed_from_the_lines(self):
        order = self.place(
            kits=[{"kit": self.kit, "quantity": 1}],
            skus=[{"sku": self.socks, "quantity": 3}],
        )

        # 2 shirts @ 25000 + 1 tunic @ 30000 + 3 socks @ 5000
        self.assertEqual(order.total, Decimal("95000.00"))

    def test_an_unpriced_item_cannot_be_ordered(self):
        """Better refused at the counter than sold for nothing."""
        unpriced = self.priced_sku("Blazer", Level.PRIMARY, None)

        with self.assertRaises(PriceNotSet):
            self.place(skus=[{"sku": unpriced, "quantity": 1}])


class AnOrderLandsOnHold(OrderSetup):
    """F32. Releasing it needs payment confirmed, and what confirms payment
    is open question Q2 — so nothing here can move it on."""

    def test_a_new_order_is_on_hold(self):
        order = self.place(skus=[{"sku": self.shirt, "quantity": 1}])

        self.assertEqual(order.status, OrderStatus.HOLD)

    def test_the_number_is_assigned_and_is_also_the_invoice_number(self):
        order = self.place(skus=[{"sku": self.shirt, "quantity": 1}])

        self.assertTrue(order.number.startswith("SO-"))

    def test_numbers_do_not_repeat(self):
        first = self.place(skus=[{"sku": self.shirt, "quantity": 1}])
        second = self.place(skus=[{"sku": self.shirt, "quantity": 1}])

        self.assertNotEqual(first.number, second.number)

    def test_an_order_is_filled_by_the_schools_own_warehouse(self):
        order = self.place(skus=[{"sku": self.shirt, "quantity": 1}])

        self.assertEqual(order.warehouse, self.school.primary_warehouse)


class TheStudentsName(OrderSetup):
    """F31. Free text — students have no accounts."""

    def test_the_name_is_stored(self):
        order = self.place(skus=[{"sku": self.shirt, "quantity": 1}], name="Miriam Achieng")

        self.assertEqual(order.student_name, "Miriam Achieng")

    def test_surrounding_space_is_trimmed(self):
        """" Miriam " and "Miriam" are the same child, and a stray space is
        enough to lose an order in a search."""
        order = self.place(skus=[{"sku": self.shirt, "quantity": 1}], name="  Miriam  ")

        self.assertEqual(order.student_name, "Miriam")

    def test_a_blank_name_is_refused(self):
        with self.assertRaises(ValidationError):
            self.place(skus=[{"sku": self.shirt, "quantity": 1}], name="   ")


class WhatASchoolMayNotOrder(OrderSetup):
    def test_an_order_with_nothing_on_it_is_refused(self):
        with self.assertRaises(EmptyOrder):
            self.place()

    def test_a_retired_sku_cannot_be_ordered(self):
        self.shirt.is_active = False
        self.shirt.save(update_fields=["is_active"])

        with self.assertRaises(InactiveItem):
            self.place(skus=[{"sku": self.shirt, "quantity": 1}])

    def test_a_retired_kit_cannot_be_ordered(self):
        self.kit.is_active = False
        self.kit.save(update_fields=["is_active"])

        with self.assertRaises(InactiveItem):
            self.place(kits=[{"kit": self.kit, "quantity": 1}])

    def test_an_active_kit_containing_a_retired_sku_is_refused(self):
        """Just as unfillable as a retired kit, and easier to miss."""
        self.tunic.is_active = False
        self.tunic.save(update_fields=["is_active"])

        with self.assertRaises(InactiveItem):
            self.place(kits=[{"kit": self.kit, "quantity": 1}])

    def test_a_primary_school_cannot_order_a_high_school_garment(self):
        """The same rule as the price list — it never appears on theirs."""
        with self.assertRaises(WrongSchoolLevel):
            self.place(skus=[{"sku": self.blazer, "quantity": 1}])

    def test_a_garment_for_both_levels_can_be_ordered_by_either(self):
        order = self.place(skus=[{"sku": self.socks, "quantity": 2}])

        self.assertEqual(order.lines.count(), 1)

    def test_a_high_school_cannot_order_a_primary_kit(self):
        high_clerk = make_user("peter", User.Role.SCHOOL_STAFF, school=self.high_school)

        with self.assertRaises(WrongSchoolLevel):
            place_order(
                school=self.high_school,
                student_name="Joan",
                order_date=ORDERED_ON,
                kits=[{"kit": self.kit, "quantity": 1}],
                created_by=high_clerk,
            )

    def test_a_refused_order_leaves_nothing_behind(self):
        """Atomic. A half-written order is worse than none."""
        before = SchoolOrder.objects.count()
        try:
            self.place(skus=[{"sku": self.blazer, "quantity": 1}])
        except WrongSchoolLevel:
            pass

        self.assertEqual(SchoolOrder.objects.count(), before)
        self.assertEqual(SchoolOrderLine.objects.count(), 0)


class AnOrderMustHaveSomethingOnIt(OrderSetup):
    """A kit with no components used to slip through.

    The check tested the *inputs* — a kit was named — not the result after
    explosion. An empty kit passed it and produced an order with no lines and
    a total of nothing: a parent handed an invoice number for an empty
    parcel, and a warehouse with nothing to pick.

    Empty kits are not corrupt data. They are created in the admin and filled
    in afterwards, so this is a state the system will genuinely see.
    """

    def an_empty_kit(self):
        return Kit.objects.create(
            kit_number="PS-EMPTY",
            name="Not filled in yet",
            school_level=Kit.SchoolLevel.PRIMARY,
        )

    def test_ordering_a_kit_with_no_components_is_refused(self):
        with self.assertRaises(EmptyOrder):
            self.place(kits=[{"kit": self.an_empty_kit(), "quantity": 1}])

    def test_the_refusal_names_the_kit(self):
        """"Your order came to nothing" would send someone hunting."""
        with self.assertRaises(EmptyOrder) as caught:
            self.place(kits=[{"kit": self.an_empty_kit(), "quantity": 1}])

        self.assertIn("PS-EMPTY", str(caught.exception))

    def test_an_empty_kit_alongside_a_real_item_is_still_refused(self):
        """The school asked for that kit. Quietly dropping it and charging
        for the rest would be worse than refusing."""
        with self.assertRaises(EmptyOrder):
            self.place(
                kits=[{"kit": self.an_empty_kit(), "quantity": 1}],
                skus=[{"sku": self.socks, "quantity": 2}],
            )

    def test_nothing_is_left_behind_when_it_is_refused(self):
        before = SchoolOrder.objects.count()

        try:
            self.place(kits=[{"kit": self.an_empty_kit(), "quantity": 1}])
        except EmptyOrder:
            pass

        self.assertEqual(SchoolOrder.objects.count(), before)

    def test_a_kit_with_components_is_still_fine(self):
        order = self.place(kits=[{"kit": self.kit, "quantity": 1}])

        self.assertEqual(order.lines.count(), 2)

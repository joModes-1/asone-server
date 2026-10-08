"""Group and production orders (F16, F17, F22).

The rules being protected:

    1. An order line's price is fixed when the order is placed.
    2. An order without lines is not an order.
    3. Orders are cancelled, never deleted.
    4. A warehouse sees its own production orders and no others.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import TailoringCenter
from catalog.services import PriceNotSet, reprice
from procurement.models import GroupOrder
from procurement.models.base import OrderStatus
from procurement.services import (
    OrderHasNoLines,
    create_group_order,
    create_production_order,
    open_production_orders,
    reconcile,
)

from .factories import ORDER_DATE, make_priced_sku, make_unpriced_sku


class OrderNumberTests(TestCase):
    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.sku = make_priced_sku()

    def line(self, quantity=100):
        return [{"sku": self.sku, "quantity": quantity}]

    def test_group_and_production_orders_number_independently(self):
        """Two series, so a gap in one does not appear in the other."""
        group = create_group_order(
            created_by=self.lead, lines=self.line(), order_date=ORDER_DATE
        )
        production = create_production_order(
            created_by=self.lead,
            lines=self.line(),
            order_date=ORDER_DATE,
            tailoring_center=TailoringCenter.objects.create(name="Serere TC"),
            warehouse=self.sites["namayemba"],
        )

        self.assertTrue(group.number.startswith("GO-"))
        self.assertTrue(production.number.startswith("PO-"))

    def test_numbers_do_not_repeat(self):
        first = create_group_order(
            created_by=self.lead, lines=self.line(), order_date=ORDER_DATE
        )
        second = create_group_order(
            created_by=self.lead, lines=self.line(), order_date=ORDER_DATE
        )
        self.assertNotEqual(first.number, second.number)

    def test_a_number_never_changes_once_assigned(self):
        """It is quoted on the TC's handwritten packing list."""
        order = create_group_order(
            created_by=self.lead, lines=self.line(), order_date=ORDER_DATE
        )
        original = order.number

        order.status = OrderStatus.CLOSED
        order.save()

        order.refresh_from_db()
        self.assertEqual(order.number, original)


class PriceIsFixedAtOrderTime(TestCase):
    """Rule 1. An order is a commitment at the price agreed that day."""

    def setUp(self):
        build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.sku = make_priced_sku(amount="25000.00")

    def test_the_line_takes_the_price_on_the_order_date(self):
        order = create_group_order(
            created_by=self.lead,
            lines=[{"sku": self.sku, "quantity": 100}],
            order_date=ORDER_DATE,
        )
        self.assertEqual(order.lines.get().unit_price, Decimal("25000.00"))

    def test_repricing_the_garment_does_not_restate_an_existing_order(self):
        """The whole reason the price is copied rather than looked up."""
        order = create_group_order(
            created_by=self.lead,
            lines=[{"sku": self.sku, "quantity": 100}],
            order_date=ORDER_DATE,
        )

        reprice(self.sku.garment, Decimal("40000.00"), date(2027, 1, 1))

        order.refresh_from_db()
        self.assertEqual(order.lines.get().unit_price, Decimal("25000.00"))
        self.assertEqual(order.total_value, Decimal("2500000.00"))

    def test_an_agreed_price_overrides_the_price_list(self):
        """AsOne negotiates with the TCs; the system records what was agreed."""
        order = create_group_order(
            created_by=self.lead,
            lines=[{"sku": self.sku, "quantity": 100, "unit_price": Decimal("22000.00")}],
            order_date=ORDER_DATE,
        )
        self.assertEqual(order.lines.get().unit_price, Decimal("22000.00"))

    def test_an_unpriced_sku_cannot_be_ordered_without_an_agreed_price(self):
        """A group order funds the TCs — a line worth nothing under-funds them."""
        unpriced = make_unpriced_sku()

        with self.assertRaises(PriceNotSet):
            create_group_order(
                created_by=self.lead,
                lines=[{"sku": unpriced, "quantity": 10}],
                order_date=ORDER_DATE,
            )


class AnOrderNeedsLines(TestCase):
    """Rule 2. A header alone would sit in the open-orders view forever."""

    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)

    def test_a_group_order_with_no_lines_is_refused(self):
        with self.assertRaises(OrderHasNoLines):
            create_group_order(created_by=self.lead, lines=[], order_date=ORDER_DATE)

    def test_a_production_order_with_no_lines_is_refused(self):
        with self.assertRaises(OrderHasNoLines):
            create_production_order(
                created_by=self.lead,
                lines=[],
                order_date=ORDER_DATE,
                tailoring_center=TailoringCenter.objects.create(name="Idudi TC"),
                warehouse=self.sites["namayemba"],
            )

    def test_nothing_is_left_behind_when_creation_fails(self):
        """Atomic: a failed line must not leave a header stranded."""
        unpriced = make_unpriced_sku()
        before = GroupOrder.objects.count()

        with self.assertRaises(PriceNotSet):
            create_group_order(
                created_by=self.lead,
                lines=[{"sku": unpriced, "quantity": 10}],
                order_date=ORDER_DATE,
            )

        self.assertEqual(GroupOrder.objects.count(), before)


class ReconciliationTests(TestCase):
    """"Production orders should initially sum up to the Group Order" (p.2)."""

    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.tc = TailoringCenter.objects.create(name="Idudi TC")
        self.shirt = make_priced_sku("White Shirt", "10")

        self.group = create_group_order(
            created_by=self.lead,
            lines=[{"sku": self.shirt, "quantity": 500}],
            order_date=ORDER_DATE,
        )

    def place(self, quantity, status=OrderStatus.OPEN, tc=None, warehouse=None):
        order = create_production_order(
            created_by=self.lead,
            lines=[{"sku": self.shirt, "quantity": quantity}],
            order_date=ORDER_DATE,
            tailoring_center=tc or self.tc,
            warehouse=warehouse or self.sites["namayemba"],
            group_order=self.group,
        )
        if status != OrderStatus.OPEN:
            order.status = status
            order.save(update_fields=["status"])
        return order

    def test_a_shortfall_shows_as_a_negative_difference(self):
        self.place(480)

        row = reconcile(self.group)[0]
        self.assertEqual(row["requested"], 500)
        self.assertEqual(row["ordered"], 480)
        self.assertEqual(row["difference"], -20)

    def test_orders_summing_exactly_show_no_difference(self):
        self.place(300)
        self.place(200)

        self.assertEqual(reconcile(self.group)[0]["difference"], 0)

    def test_ordering_extra_is_reported_not_blocked(self):
        """Safety stock is a judgement call for a person, not an error."""
        self.place(600)

        self.assertEqual(reconcile(self.group)[0]["difference"], 100)

    def test_orders_on_different_tcs_sum_to_the_one_group_order(self):
        """The whole point of a group order, confirmed by AsOne.

        It is the order the warehouses place across **all** the Tailoring
        Centers, entered once, and the production orders — one per TC, and
        there are three — sum up to it. So the reconciliation counts every
        production order under the group order whatever TC it went to and
        whatever warehouse it is bound for. A per-TC total would make the
        requirement look permanently unmet.
        """
        rwanyabihuka = TailoringCenter.objects.create(name="Rwanyabihuka")
        serere_tc = TailoringCenter.objects.create(name="Serere TC")

        self.place(200)
        self.place(200, tc=rwanyabihuka, warehouse=self.sites["serere"])
        self.place(100, tc=serere_tc, warehouse=self.sites["serere"])

        row = reconcile(self.group)[0]
        self.assertEqual(row["requested"], 500)
        self.assertEqual(row["ordered"], 500)
        self.assertEqual(row["difference"], 0)

    def test_cancelled_orders_do_not_count_towards_the_requirement(self):
        """Otherwise the requirement looks covered by goods nobody is making."""
        self.place(500, status=OrderStatus.CANCELLED)

        row = reconcile(self.group)[0]
        self.assertEqual(row["ordered"], 0)
        self.assertEqual(row["difference"], -500)


class OpenOrdersTests(TestCase):
    """F22 — placed on the TCs but not yet closed."""

    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.tc = TailoringCenter.objects.create(name="Idudi TC")
        self.sku = make_priced_sku()

    def place(self, status=OrderStatus.OPEN, warehouse=None):
        order = create_production_order(
            created_by=self.lead,
            lines=[{"sku": self.sku, "quantity": 100}],
            order_date=ORDER_DATE,
            tailoring_center=self.tc,
            warehouse=warehouse or self.sites["namayemba"],
        )
        if status != OrderStatus.OPEN:
            order.status = status
            order.save(update_fields=["status"])
        return order

    def test_open_orders_are_listed(self):
        order = self.place()
        self.assertIn(order, open_production_orders())

    def test_closed_orders_are_not(self):
        order = self.place(status=OrderStatus.CLOSED)
        self.assertNotIn(order, open_production_orders())

    def test_cancelled_orders_are_not(self):
        order = self.place(status=OrderStatus.CANCELLED)
        self.assertNotIn(order, open_production_orders())


class OrdersAreNeverDeleted(TestCase):
    """Rule 3. An order funds a Tailoring Center — the document must survive."""

    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.sku = make_priced_sku()

    def test_a_group_order_is_cancelled_rather_than_removed(self):
        order = create_group_order(
            created_by=self.lead,
            lines=[{"sku": self.sku, "quantity": 100}],
            order_date=ORDER_DATE,
        )
        order.status = OrderStatus.CANCELLED
        order.save(update_fields=["status"])

        order.refresh_from_db()
        self.assertEqual(order.status, OrderStatus.CANCELLED)
        self.assertTrue(GroupOrder.objects.filter(pk=order.pk).exists())

    def test_the_person_who_raised_an_order_cannot_be_deleted(self):
        """PROTECT on created_by — the audit trail has to hold."""
        from django.db.models import ProtectedError

        create_group_order(
            created_by=self.lead,
            lines=[{"sku": self.sku, "quantity": 100}],
            order_date=ORDER_DATE,
        )
        with self.assertRaises(ProtectedError):
            self.lead.delete()

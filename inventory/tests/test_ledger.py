"""The stock ledger (F21, F47, F48, F50).

The rules being protected:

    1. A ledger row, once written, is never changed or deleted.
    2. A stock level is derived, never stored.
    3. Every movement records the user who caused it.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, MinimumStockLevel, Size, Sku
from inventory.models import MovementType, StockMovement
from inventory.services import (
    below_minimum,
    movements_for_sku,
    post_movement,
    stock_level,
    stock_levels,
)

TODAY = date(2026, 9, 1)


class LedgerSetup(TestCase):
    def setUp(self):
        self.sites = build_sites()
        self.clerk = make_user(
            "julius", User.Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.size = Size.objects.create(name="S", sort_order=20)
        self.sku = Sku.objects.create(garment=garment, size=self.size)

    def move(self, quantity, warehouse=None, on=TODAY, sku=None):
        return post_movement(
            warehouse=warehouse or self.sites["namayemba"],
            sku=sku or self.sku,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-100001",
            occurred_on=on,
            created_by=self.clerk,
        )


class TheLedgerIsAppendOnly(LedgerSetup):
    """Rule 1. A ledger you can edit is a ledger nobody can trust."""

    def test_a_movement_cannot_be_changed(self):
        movement = self.move(500)
        movement.quantity = 999

        with self.assertRaises(ValidationError):
            movement.save()

    def test_a_movement_cannot_be_deleted(self):
        movement = self.move(500)

        with self.assertRaises(ValidationError):
            movement.delete()

    def test_the_original_survives_an_attempted_edit(self):
        movement = self.move(500)
        movement.quantity = 999
        try:
            movement.save()
        except ValidationError:
            pass

        movement.refresh_from_db()
        self.assertEqual(movement.quantity, 500)

    def test_a_correction_is_a_new_row_not_an_edit(self):
        """How a miscount is actually fixed: post the difference."""
        self.move(500)
        self.move(-20)

        self.assertEqual(stock_level(self.sku, self.sites["namayemba"]), 480)
        self.assertEqual(StockMovement.objects.count(), 2)

    def test_a_movement_of_zero_is_refused(self):
        """It would imply something happened when nothing did."""
        with self.assertRaises(IntegrityError), transaction.atomic():
            StockMovement.objects.create(
                warehouse=self.sites["namayemba"],
                sku=self.sku,
                quantity=0,
                movement_type=MovementType.RECEIPT,
                unit_value=Decimal("25000.00"),
                document_number="RC-1",
                occurred_on=TODAY,
                created_by=self.clerk,
            )


class TransactionNumbers(LedgerSetup):
    def test_every_movement_gets_one(self):
        self.assertTrue(self.move(100).number.startswith("TX-"))

    def test_they_do_not_repeat(self):
        first, second = self.move(100), self.move(100)
        self.assertNotEqual(first.number, second.number)


class StockLevelsAreDerived(LedgerSetup):
    """Rule 2. There is no quantity column anywhere."""

    def test_a_sku_that_never_moved_is_zero_not_missing(self):
        self.assertEqual(stock_level(self.sku, self.sites["namayemba"]), 0)

    def test_movements_sum(self):
        self.move(500)
        self.move(300)

        self.assertEqual(stock_level(self.sku, self.sites["namayemba"]), 800)

    def test_outbound_movements_subtract(self):
        self.move(500)
        self.move(-120)

        self.assertEqual(stock_level(self.sku, self.sites["namayemba"]), 380)

    def test_each_warehouse_is_counted_separately(self):
        self.move(500, warehouse=self.sites["namayemba"])
        self.move(200, warehouse=self.sites["serere"])

        self.assertEqual(stock_level(self.sku, self.sites["namayemba"]), 500)
        self.assertEqual(stock_level(self.sku, self.sites["serere"]), 200)

    def test_a_level_can_be_asked_for_as_at_a_past_date(self):
        """A count sheet from last Friday is checked against last Friday."""
        self.move(500, on=TODAY - timedelta(days=7))
        self.move(300, on=TODAY)

        self.assertEqual(
            stock_level(self.sku, self.sites["namayemba"], as_of=TODAY - timedelta(days=1)),
            500,
        )
        self.assertEqual(stock_level(self.sku, self.sites["namayemba"]), 800)

    def test_the_levels_report_carries_value(self):
        self.move(10)

        row = list(stock_levels(warehouse=self.sites["namayemba"]))[0]
        self.assertEqual(row["level"], 10)
        self.assertEqual(row["value"], Decimal("250000.00"))

    def test_zero_rows_are_left_out_unless_asked_for(self):
        self.move(100)
        self.move(-100)

        self.assertEqual(len(list(stock_levels())), 0)
        self.assertEqual(len(list(stock_levels(include_zero=True))), 1)

    def test_the_report_is_one_query_whatever_the_sku_count(self):
        garment = Garment.objects.create(name="Trousers")
        for index in range(20):
            size = Size.objects.create(name=f"S{index}", sort_order=index)
            self.move(10, sku=Sku.objects.create(garment=garment, size=size))

        with self.assertNumQueries(1):
            list(stock_levels())


class EveryMovementRecordsTheUser(LedgerSetup):
    """Rule 3. AsOne asked for this explicitly (p.9)."""

    def test_the_user_is_stored(self):
        self.assertEqual(self.move(100).created_by, self.clerk)

    def test_a_user_who_posted_a_movement_cannot_be_deleted(self):
        from django.db.models import ProtectedError

        self.move(100)
        with self.assertRaises(ProtectedError):
            self.clerk.delete()

    def test_the_audit_trail_for_a_sku_shows_who_did_what(self):
        """F48 — the question a ledger answers and a counter cannot."""
        self.move(500)
        self.move(-20)

        trail = movements_for_sku(self.sku)
        self.assertEqual(trail.count(), 2)
        self.assertEqual({m.created_by for m in trail}, {self.clerk})


class ReorderAlerts(LedgerSetup):
    """F50 — at or below the floor set for that warehouse."""

    def setUp(self):
        super().setUp()
        MinimumStockLevel.objects.create(
            sku=self.sku, warehouse=self.sites["namayemba"], minimum_quantity=100
        )

    def test_stock_above_the_floor_raises_nothing(self):
        self.move(150)
        self.assertEqual(below_minimum(), [])

    def test_stock_below_the_floor_raises_an_alert(self):
        self.move(80)

        alert = below_minimum()[0]
        self.assertEqual(alert["level"], 80)
        self.assertEqual(alert["minimum"], 100)
        self.assertEqual(alert["shortfall"], 20)

    def test_stock_exactly_at_the_floor_raises_an_alert(self):
        """AsOne: the level "that should trigger a replenishment order".

        At the level is the moment to reorder, not one below it.
        """
        self.move(100)

        self.assertEqual(len(below_minimum()), 1)

    def test_a_sku_with_a_floor_and_no_stock_at_all_is_alerted(self):
        """Zero is below any positive minimum, and it is the worst case."""
        alert = below_minimum()[0]

        self.assertEqual(alert["level"], 0)
        self.assertEqual(alert["shortfall"], 100)

    def test_a_floor_at_one_warehouse_does_not_alert_for_another(self):
        self.move(500, warehouse=self.sites["serere"])

        self.assertEqual(len(below_minimum(warehouse=self.sites["serere"])), 0)

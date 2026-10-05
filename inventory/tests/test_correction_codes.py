"""A physical count can land either side of the system figure.

F23 gave every reason code a fixed direction, which is right for the codes
AsOne named — a return always adds, damage always removes. A correction is
the exception: the shelf can hold more than the system thinks as easily as
less.

A single `CORR` fixed to DECREASE would let F24 post shortfalls and nothing
else, which is why migration 0006 splits it into `CORR_UP` and `CORR_DOWN`.
These tests exist so nobody merges them back together.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from inventory.models import MovementType, ReasonCode
from inventory.services import (
    create_adjustment,
    post_adjustment,
    post_movement,
    stock_level,
)

COUNTED_ON = date(2026, 11, 1)
Direction = ReasonCode.AdjustmentDirection


class CorrectingACountEitherWay(TestCase):
    """The reason the split exists — F24 has to work in both directions."""

    def setUp(self):
        self.sites = build_sites()
        self.warehouse = self.sites["namayemba"]
        self.finance = make_user("musana", User.Role.FINANCE)
        self.clerk = make_user(
            "julius", User.Role.WAREHOUSE_STAFF, warehouse=self.warehouse
        )

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="S", sort_order=20)
        )

        # Created here rather than relied on from a migration: master data
        # comes from seed_demo, and a test that leans on migration-seeded
        # rows is depending on a fixture nobody declared.
        self.codes = {
            "CORR_UP": ReasonCode.objects.create(
                code="CORR_UP",
                name="Inventory correction — count higher",
                direction=Direction.INCREASE,
            ),
            "CORR_DOWN": ReasonCode.objects.create(
                code="CORR_DOWN",
                name="Inventory correction — count lower",
                direction=Direction.DECREASE,
            ),
        }

        # The system thinks there are 500 on the shelf.
        post_movement(
            warehouse=self.warehouse,
            sku=self.sku,
            quantity=500,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-1",
            occurred_on=date(2026, 10, 1),
            created_by=self.clerk,
        )

    def correct_by(self, quantity, code):
        adjustment = create_adjustment(
            warehouse=self.warehouse,
            sku=self.sku,
            quantity=quantity,
            reason_code=self.codes[code],
            created_by=self.finance,
            adjustment_date=COUNTED_ON,
        )
        post_adjustment(adjustment, posted_by=self.finance)
        return stock_level(self.sku, self.warehouse)

    def test_a_count_lower_than_the_system_brings_it_down(self):
        """System says 500, the shelf holds 480."""
        self.assertEqual(self.correct_by(20, "CORR_DOWN"), 480)

    def test_a_count_higher_than_the_system_brings_it_up(self):
        """System says 500, the shelf holds 520 — the case the old single
        code could not express at all."""
        self.assertEqual(self.correct_by(20, "CORR_UP"), 520)

    def test_an_upward_correction_is_not_limited_by_stock_on_hand(self):
        """Finding more than expected must never be refused for lack of stock.

        The negative-stock check only applies to decreases; if it ran on
        increases, the whole point of CORR_UP would be blocked.
        """
        self.assertEqual(self.correct_by(5000, "CORR_UP"), 5500)

    def test_a_downward_correction_still_cannot_go_below_zero(self):
        """Splitting the code must not have loosened the decrease check."""
        from inventory.services import NotEnoughStock

        with self.assertRaises(NotEnoughStock):
            self.correct_by(501, "CORR_DOWN")

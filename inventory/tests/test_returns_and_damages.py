"""Returns (F26) and damages (F27) — proof they need no new code.

Bashir's own framing for Wave 2: "All the same document with different
reason codes and directions." F23 already built the document
(InventoryAdjustment) and the mechanism (create_adjustment() /
post_adjustment()); F26 and F27 are that same mechanism used with the RET
and DMG reason codes. Nothing here adds a new model, serializer or
endpoint — these tests exist to prove the claim rather than just assert it.

F24 (physical count correction) is proven at the service layer already in
test_correction_codes.py. The API-level tests at the bottom of this file
close the one gap that file doesn't cover — CORR_UP/CORR_DOWN posted over
HTTP, not just through the service functions directly.
"""

from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from inventory.models import MovementType, ReasonCode
from inventory.services import (
    NotEnoughStock,
    create_adjustment,
    post_adjustment,
    post_movement,
    stock_level,
)

Role = User.Role
Direction = ReasonCode.AdjustmentDirection
ON = date(2026, 11, 1)


class ReturnsAndDamagesSetup(TestCase):
    def setUp(self):
        self.sites = build_sites()
        self.warehouse = self.sites["namayemba"]
        self.finance = make_user("musana", Role.FINANCE)

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="S", sort_order=20)
        )

        # Same codes and wording seed_demo ships, not stand-ins — these
        # tests assert F26/F27 work with the reason codes Finance will
        # actually see, not a look-alike.
        self.returned = ReasonCode.objects.create(
            code="RET", name="Return", direction=Direction.INCREASE
        )
        self.damaged = ReasonCode.objects.create(
            code="DMG", name="Damaged", direction=Direction.DECREASE
        )

    def stock_in(self, quantity, on=date(2026, 10, 1)):
        return post_movement(
            warehouse=self.warehouse,
            sku=self.sku,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-SETUP",
            occurred_on=on,
            created_by=self.finance,
        )

    def adjust(self, reason_code, quantity):
        adjustment = create_adjustment(
            warehouse=self.warehouse,
            sku=self.sku,
            quantity=quantity,
            reason_code=reason_code,
            adjustment_date=ON,
            created_by=self.finance,
        )
        post_adjustment(adjustment, posted_by=self.finance)
        return adjustment


class ReturnsIncreaseSellableStock(ReturnsAndDamagesSetup):
    """F26 — a school hands a uniform back, and it goes back into stock."""

    def test_a_return_raises_stock(self):
        self.adjust(self.returned, 3)

        self.assertEqual(stock_level(self.sku, self.warehouse), 3)

    def test_a_return_has_no_stock_ceiling(self):
        """Increasing stock is never refused for lack of stock — that check
        only exists for decreases."""
        self.adjust(self.returned, 10_000)

        self.assertEqual(stock_level(self.sku, self.warehouse), 10_000)

    def test_a_return_is_valued_at_the_catalog_price(self):
        adjustment = self.adjust(self.returned, 1)

        adjustment.refresh_from_db()
        self.assertEqual(adjustment.unit_value, Decimal("25000.00"))


class DamagesDecreaseSellableStock(ReturnsAndDamagesSetup):
    """F27 — damaged stock can no longer be sold."""

    def test_damage_lowers_stock(self):
        self.stock_in(20)
        self.adjust(self.damaged, 5)

        self.assertEqual(stock_level(self.sku, self.warehouse), 15)

    def test_damage_cannot_exceed_what_is_on_hand(self):
        """The same floor F23 enforces everywhere — you cannot damage more
        than exists."""
        self.stock_in(5)

        with self.assertRaises(NotEnoughStock):
            self.adjust(self.damaged, 6)


class ReturnsAndDamagesApi(APITestCase):
    """F26 and F27 over HTTP — same endpoint as F23, a different reason code."""

    def setUp(self):
        self.sites = build_sites()
        self.warehouse = self.sites["namayemba"]
        self.finance = make_user("musana", Role.FINANCE)
        self.client.force_authenticate(self.finance)

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="S", sort_order=20)
        )
        self.returned = ReasonCode.objects.create(
            code="RET", name="Return", direction=Direction.INCREASE
        )
        self.damaged = ReasonCode.objects.create(
            code="DMG", name="Damaged", direction=Direction.DECREASE
        )

    def post_and_ledger(self, reason_code, quantity):
        created = self.client.post(
            reverse("inventory:adjustment-list"),
            {
                "warehouse": self.warehouse.pk,
                "sku": self.sku.pk,
                "quantity": quantity,
                "reason_code": reason_code.pk,
                "adjustment_date": ON.isoformat(),
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)

        posted = self.client.post(
            reverse("inventory:adjustment-post-to-ledger", args=[created.data["id"]])
        )
        self.assertEqual(posted.status_code, status.HTTP_200_OK)
        return posted

    def test_a_return_over_the_api_raises_stock(self):
        self.post_and_ledger(self.returned, 4)

        self.assertEqual(stock_level(self.sku, self.warehouse), 4)

    def test_damage_over_the_api_lowers_stock(self):
        post_movement(
            warehouse=self.warehouse,
            sku=self.sku,
            quantity=10,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-SETUP",
            occurred_on=date(2026, 10, 1),
            created_by=self.finance,
        )
        self.post_and_ledger(self.damaged, 4)

        self.assertEqual(stock_level(self.sku, self.warehouse), 6)


class PhysicalCountCorrectionApi(APITestCase):
    """F24 over HTTP. Proven at the service layer already in
    test_correction_codes.py; this closes the API-level gap that file
    doesn't cover."""

    def setUp(self):
        self.sites = build_sites()
        self.warehouse = self.sites["namayemba"]
        self.finance = make_user("musana", Role.FINANCE)
        self.client.force_authenticate(self.finance)

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="S", sort_order=20)
        )
        self.corr_up = ReasonCode.objects.create(
            code="CORR_UP",
            name="Inventory correction — count higher",
            direction=Direction.INCREASE,
        )
        self.corr_down = ReasonCode.objects.create(
            code="CORR_DOWN",
            name="Inventory correction — count lower",
            direction=Direction.DECREASE,
        )
        post_movement(
            warehouse=self.warehouse,
            sku=self.sku,
            quantity=500,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-1",
            occurred_on=date(2026, 10, 1),
            created_by=self.finance,
        )

    def correct(self, reason_code, quantity):
        created = self.client.post(
            reverse("inventory:adjustment-list"),
            {
                "warehouse": self.warehouse.pk,
                "sku": self.sku.pk,
                "quantity": quantity,
                "reason_code": reason_code.pk,
                "adjustment_date": ON.isoformat(),
            },
            format="json",
        )
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)

        posted = self.client.post(
            reverse("inventory:adjustment-post-to-ledger", args=[created.data["id"]])
        )
        self.assertEqual(posted.status_code, status.HTTP_200_OK)

    def test_a_count_found_higher_over_the_api(self):
        self.correct(self.corr_up, 20)

        self.assertEqual(stock_level(self.sku, self.warehouse), 520)

    def test_a_count_found_lower_over_the_api(self):
        self.correct(self.corr_down, 20)

        self.assertEqual(stock_level(self.sku, self.warehouse), 480)

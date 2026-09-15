"""Query filters on adjustments and transfers — inventory/filters.py.

The two questions the screens ask that `filterset_fields` cannot answer:
a date *range*, and whether a document has been posted at all rather than
whether it was posted at one exact timestamp.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from inventory.models import MovementType, ReasonCode
from inventory.services import create_adjustment, post_adjustment, post_movement

Role = User.Role
Direction = ReasonCode.AdjustmentDirection


class AdjustmentFilters(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.namayemba = self.sites["namayemba"]
        self.finance = make_user("musana", Role.FINANCE)

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="10", sort_order=10)
        )
        self.damage = ReasonCode.objects.create(
            code="DMG", name="Damaged", direction=Direction.DECREASE
        )

        post_movement(
            warehouse=self.namayemba,
            sku=self.sku,
            quantity=500,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-SETUP",
            occurred_on=date(2026, 1, 2),
            created_by=self.finance,
        )

        # One in March, one in June. The March one is posted; the June one is
        # still a draft, so the two filters can be told apart.
        self.march = create_adjustment(
            created_by=self.finance,
            warehouse=self.namayemba,
            sku=self.sku,
            quantity=5,
            reason_code=self.damage,
            adjustment_date=date(2026, 3, 10),
        )
        post_adjustment(self.march, posted_by=self.finance)

        self.june = create_adjustment(
            created_by=self.finance,
            warehouse=self.namayemba,
            sku=self.sku,
            quantity=7,
            reason_code=self.damage,
            adjustment_date=date(2026, 6, 20),
        )

        self.client.force_authenticate(self.finance)
        self.url = reverse("inventory:adjustment-list")

    def numbers(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return {row["number"] for row in response.data["results"]}

    def test_date_from_is_inclusive(self):
        self.assertEqual(
            self.numbers(date_from="2026-06-20"), {self.june.number}
        )

    def test_date_to_is_inclusive(self):
        self.assertEqual(
            self.numbers(date_to="2026-03-10"), {self.march.number}
        )

    def test_a_range_takes_both_bounds(self):
        self.assertEqual(
            self.numbers(date_from="2026-01-01", date_to="2026-12-31"),
            {self.march.number, self.june.number},
        )

    def test_a_range_that_spans_nothing_is_empty(self):
        self.assertEqual(self.numbers(date_from="2026-04-01", date_to="2026-05-01"), set())

    def test_posted_true_excludes_drafts(self):
        self.assertEqual(self.numbers(posted="true"), {self.march.number})

    def test_posted_false_is_the_drafts(self):
        """The one that matters operationally: a draft has moved no stock."""
        self.assertEqual(self.numbers(posted="false"), {self.june.number})

    def test_no_filter_returns_both(self):
        self.assertEqual(self.numbers(), {self.march.number, self.june.number})


class TransferFilters(APITestCase):
    def setUp(self):
        from inventory.services import create_transfer, post_transfer

        self.sites = build_sites()
        self.namayemba = self.sites["namayemba"]
        self.serere = self.sites["serere"]
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="10", sort_order=10)
        )

        post_movement(
            warehouse=self.namayemba,
            sku=self.sku,
            quantity=500,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-SETUP",
            occurred_on=date(2026, 1, 2),
            created_by=self.lead,
        )

        self.posted = create_transfer(
            created_by=self.lead,
            from_warehouse=self.namayemba,
            to_warehouse=self.serere,
            transfer_date=date(2026, 3, 1),
            lines=[{"sku": self.sku, "quantity": 10}],
        )
        post_transfer(self.posted, posted_by=self.lead)

        self.draft = create_transfer(
            created_by=self.lead,
            from_warehouse=self.namayemba,
            to_warehouse=self.serere,
            transfer_date=date(2026, 6, 1),
            lines=[{"sku": self.sku, "quantity": 20}],
        )

        self.client.force_authenticate(self.lead)
        self.url = reverse("inventory:transfer-list")

    def numbers(self, **params):
        response = self.client.get(self.url, params)
        self.assertEqual(response.status_code, 200)
        return {row["number"] for row in response.data["results"]}

    def test_posted_false_is_the_drafts(self):
        self.assertEqual(self.numbers(posted="false"), {self.draft.number})

    def test_posted_true_excludes_drafts(self):
        self.assertEqual(self.numbers(posted="true"), {self.posted.number})

    def test_date_range_applies_to_the_transfer_date(self):
        self.assertEqual(
            self.numbers(date_from="2026-05-01"), {self.draft.number}
        )

"""Warehouse transfers (F25).

Three rules, each with a way of going wrong that would be hard to spot later:

    1. Stock moves. Total inventory value does not.
    2. A transfer is two ledger rows, or it is none.
    3. You cannot move what is not there.

Not to be confused with a backorder transfer (Phase 3), where a warehouse
ships direct to a school and the goods never reach the school's own
warehouse — decision D2.
"""

from datetime import date
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku
from inventory.models import (
    MovementType,
    StockMovement,
    WarehouseTransfer,
    WarehouseTransferLine,
)
from inventory.services import (
    NotEnoughStock,
    TransferAlreadyPosted,
    average_unit_value,
    create_transfer,
    post_movement,
    post_transfer,
    stock_level,
)

MOVED_ON = date(2026, 11, 1)
Role = User.Role


class TransferSetup(TestCase):
    def setUp(self):
        self.sites = build_sites()
        self.namayemba = self.sites["namayemba"]
        self.serere = self.sites["serere"]

        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.finance = make_user("musana", Role.FINANCE)
        self.clerk = make_user(
            "julius", Role.WAREHOUSE_STAFF, warehouse=self.namayemba
        )

        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.size = Size.objects.create(name="S", sort_order=20)
        self.sku = Sku.objects.create(garment=garment, size=self.size)

    def stock_in(self, warehouse, quantity, value="25000.00", on=date(2026, 10, 1)):
        return post_movement(
            warehouse=warehouse,
            sku=self.sku,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal(value),
            document_number="RC-SETUP",
            occurred_on=on,
            created_by=self.clerk,
        )

    def transfer(self, quantity=100, **fields):
        return create_transfer(
            from_warehouse=self.namayemba,
            to_warehouse=self.serere,
            lines=[{"sku": self.sku, "quantity": quantity}],
            created_by=self.lead,
            transfer_date=MOVED_ON,
            **fields,
        )


class StockMovesButValueDoesNot(TransferSetup):
    """Rule 1. "No money moves" (p.6) — AsOne owns the stock either side."""

    def setUp(self):
        super().setUp()
        self.stock_in(self.namayemba, 500)

    def test_stock_leaves_the_source(self):
        post_transfer(self.transfer(100), posted_by=self.lead)

        self.assertEqual(stock_level(self.sku, self.namayemba), 400)

    def test_stock_arrives_at_the_destination(self):
        post_transfer(self.transfer(100), posted_by=self.lead)

        self.assertEqual(stock_level(self.sku, self.serere), 100)

    def test_the_total_across_both_warehouses_is_unchanged(self):
        before = stock_level(self.sku, self.namayemba) + stock_level(self.sku, self.serere)
        post_transfer(self.transfer(100), posted_by=self.lead)
        after = stock_level(self.sku, self.namayemba) + stock_level(self.sku, self.serere)

        self.assertEqual(before, after)

    def test_the_value_arriving_equals_the_value_leaving(self):
        """The point of the rule. Both rows carry the same unit value."""
        post_transfer(self.transfer(100), posted_by=self.lead)

        out = StockMovement.objects.get(movement_type=MovementType.TRANSFER_OUT)
        into = StockMovement.objects.get(movement_type=MovementType.TRANSFER_IN)

        self.assertEqual(out.unit_value, into.unit_value)
        self.assertEqual(out.total_value, into.total_value)

    def test_it_is_valued_at_what_the_stock_was_carried_at(self):
        """Not today's price list — the stock is worth what was paid for it."""
        post_transfer(self.transfer(100), posted_by=self.lead)

        into = StockMovement.objects.get(movement_type=MovementType.TRANSFER_IN)
        self.assertEqual(into.unit_value, Decimal("25000.00"))

    def test_mixed_cost_stock_transfers_at_its_average(self):
        """Two deliveries at different prices leave one blended value."""
        self.stock_in(self.namayemba, 500, value="35000.00")
        # 500 @ 25000 + 500 @ 35000 = 30000 average
        self.assertEqual(
            average_unit_value(self.sku, self.namayemba), Decimal("30000.00")
        )

        post_transfer(self.transfer(100), posted_by=self.lead)
        into = StockMovement.objects.get(movement_type=MovementType.TRANSFER_IN)
        self.assertEqual(into.unit_value, Decimal("30000.00"))


class ATransferIsTwoRowsOrNone(TransferSetup):
    """Rule 2. A half-posted transfer would make the goods cease to exist."""

    def setUp(self):
        super().setUp()
        self.stock_in(self.namayemba, 500)

    def test_posting_writes_exactly_two_rows_per_line(self):
        post_transfer(self.transfer(100), posted_by=self.lead)

        rows = StockMovement.objects.filter(document_number__startswith="WT-")
        self.assertEqual(rows.count(), 2)
        self.assertEqual(
            {r.movement_type for r in rows},
            {MovementType.TRANSFER_OUT, MovementType.TRANSFER_IN},
        )

    def test_both_rows_name_the_same_document(self):
        transfer = self.transfer(100)
        post_transfer(transfer, posted_by=self.lead)

        for row in StockMovement.objects.filter(document_number=transfer.number):
            self.assertEqual(row.source, self.namayemba.name)
            self.assertEqual(row.destination, self.serere.name)

    def test_entering_a_transfer_moves_nothing(self):
        """Preparing and committing are separate steps, as with receipts."""
        self.transfer(100)

        self.assertEqual(stock_level(self.sku, self.namayemba), 500)
        self.assertEqual(stock_level(self.sku, self.serere), 0)

    def test_posting_twice_is_refused(self):
        transfer = self.transfer(100)
        post_transfer(transfer, posted_by=self.lead)

        with self.assertRaises(TransferAlreadyPosted):
            post_transfer(transfer, posted_by=self.lead)

    def test_a_refused_second_posting_does_not_move_stock_again(self):
        transfer = self.transfer(100)
        post_transfer(transfer, posted_by=self.lead)
        try:
            post_transfer(transfer, posted_by=self.lead)
        except TransferAlreadyPosted:
            pass

        self.assertEqual(stock_level(self.sku, self.serere), 100)

    def test_every_row_records_who_posted_it(self):
        post_transfer(self.transfer(100), posted_by=self.finance)

        for row in StockMovement.objects.filter(document_number__startswith="WT-"):
            self.assertEqual(row.created_by, self.finance)


class YouCannotMoveWhatIsNotThere(TransferSetup):
    """Rule 3. Negative stock is not a number anyone can act on."""

    def test_transferring_more_than_is_held_is_refused(self):
        self.stock_in(self.namayemba, 50)

        with self.assertRaises(NotEnoughStock):
            self.transfer(100)

    def test_transferring_from_an_empty_warehouse_is_refused(self):
        with self.assertRaises(NotEnoughStock):
            self.transfer(1)

    def test_the_error_says_what_is_short_and_by_how_much(self):
        self.stock_in(self.namayemba, 50)

        with self.assertRaises(NotEnoughStock) as caught:
            self.transfer(100)

        shortfall = caught.exception.shortfalls[0]
        self.assertEqual(shortfall["requested"], 100)
        self.assertEqual(shortfall["available"], 50)

    def test_stock_is_rechecked_at_posting(self):
        """Time passes between writing a transfer down and committing it."""
        self.stock_in(self.namayemba, 500)
        transfer = self.transfer(400)

        # Something else moves the stock in the meantime.
        post_movement(
            warehouse=self.namayemba,
            sku=self.sku,
            quantity=-300,
            movement_type=MovementType.ADJUSTMENT,
            unit_value=Decimal("25000.00"),
            document_number="ADJ-1",
            occurred_on=date(2026, 10, 15),
            created_by=self.clerk,
        )

        with self.assertRaises(NotEnoughStock):
            post_transfer(transfer, posted_by=self.lead)

    def test_a_failed_posting_leaves_no_ledger_rows_behind(self):
        """Atomic. Half a transfer is worse than none."""
        self.stock_in(self.namayemba, 500)
        transfer = self.transfer(400)
        post_movement(
            warehouse=self.namayemba,
            sku=self.sku,
            quantity=-300,
            movement_type=MovementType.ADJUSTMENT,
            unit_value=Decimal("25000.00"),
            document_number="ADJ-1",
            occurred_on=date(2026, 10, 15),
            created_by=self.clerk,
        )
        try:
            post_transfer(transfer, posted_by=self.lead)
        except NotEnoughStock:
            pass

        self.assertEqual(
            StockMovement.objects.filter(document_number=transfer.number).count(), 0
        )
        transfer.refresh_from_db()
        self.assertFalse(transfer.is_posted)


class ATransferNeedsTwoDifferentWarehouses(TransferSetup):
    def test_the_database_refuses_a_self_transfer(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            WarehouseTransfer.objects.create(
                from_warehouse=self.namayemba,
                to_warehouse=self.namayemba,
                transfer_date=MOVED_ON,
                created_by=self.lead,
            )

    def test_the_model_refuses_it_with_a_readable_message(self):
        transfer = WarehouseTransfer(
            from_warehouse=self.namayemba,
            to_warehouse=self.namayemba,
            transfer_date=MOVED_ON,
            created_by=self.lead,
        )
        with self.assertRaises(ValidationError) as caught:
            transfer.full_clean(exclude=["number", "created_by"])

        self.assertIn("to_warehouse", caught.exception.error_dict)

    def test_a_sku_appears_once_per_transfer(self):
        self.stock_in(self.namayemba, 500)
        transfer = self.transfer(100)

        with self.assertRaises(IntegrityError), transaction.atomic():
            WarehouseTransferLine.objects.create(
                transfer=transfer, sku=self.sku, quantity=50
            )


class WhoCanTransfer(APITestCase):
    """F25 gives this to both leads and Finance — unlike its neighbours,
    which the matrix reserves for Finance alone."""

    def setUp(self):
        self.sites = build_sites()
        self.users = {
            Role.PROGRAM_LEAD: make_user("sharon", Role.PROGRAM_LEAD),
            Role.OPERATIONS_MANAGER: make_user("andrew", Role.OPERATIONS_MANAGER),
            Role.FINANCE: make_user("musana", Role.FINANCE),
            Role.WAREHOUSE_STAFF: make_user(
                "julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
            ),
            Role.SCHOOL_STAFF: make_user(
                "chrisis", Role.SCHOOL_STAFF, school=self.sites["school_a"]
            ),
        }

    def read_as(self, role):
        self.client.force_authenticate(self.users[role])
        return self.client.get(reverse("inventory:transfer-list")).status_code

    def test_leads_and_finance_may_use_it(self):
        for role in (Role.PROGRAM_LEAD, Role.OPERATIONS_MANAGER, Role.FINANCE):
            with self.subTest(role=role):
                self.assertEqual(self.read_as(role), status.HTTP_200_OK)

    def test_warehouse_staff_may_not(self):
        """A transfer commits two sites; a clerk can only see one."""
        self.assertEqual(
            self.read_as(Role.WAREHOUSE_STAFF), status.HTTP_403_FORBIDDEN
        )

    def test_school_staff_may_not(self):
        self.assertEqual(self.read_as(Role.SCHOOL_STAFF), status.HTTP_403_FORBIDDEN)


class TransferApi(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.clerk = make_user(
            "julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        garment = Garment.objects.create(name="White Shirt")
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=date(2026, 1, 1)
        )
        self.sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="S", sort_order=20)
        )
        post_movement(
            warehouse=self.sites["namayemba"],
            sku=self.sku,
            quantity=500,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-1",
            occurred_on=date(2026, 10, 1),
            created_by=self.clerk,
        )
        self.client.force_authenticate(self.lead)

    def create(self, **overrides):
        payload = {
            "from_warehouse": self.sites["namayemba"].pk,
            "to_warehouse": self.sites["serere"].pk,
            "transfer_date": MOVED_ON.isoformat(),
            "lines": [{"sku": self.sku.pk, "quantity": 100}],
        }
        payload.update(overrides)
        return self.client.post(reverse("inventory:transfer-list"), payload, format="json")

    def test_a_transfer_can_be_prepared_and_posted(self):
        created = self.create()
        self.assertEqual(created.status_code, status.HTTP_201_CREATED)

        posted = self.client.post(
            reverse("inventory:transfer-post-to-ledger", args=[created.data["id"]])
        )
        self.assertEqual(posted.status_code, status.HTTP_200_OK)
        self.assertTrue(posted.data["is_posted"])
        self.assertEqual(stock_level(self.sku, self.sites["serere"]), 100)

    def test_moving_more_than_is_held_is_a_400(self):
        response = self.create(lines=[{"sku": self.sku.pk, "quantity": 9999}])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("lines", response.data)

    def test_the_same_warehouse_twice_is_a_400(self):
        response = self.create(to_warehouse=self.sites["namayemba"].pk)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("to_warehouse", response.data)

    def test_a_repeated_sku_is_a_400(self):
        response = self.create(
            lines=[
                {"sku": self.sku.pk, "quantity": 10},
                {"sku": self.sku.pk, "quantity": 20},
            ]
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_posting_twice_is_a_400(self):
        created = self.create()
        url = reverse("inventory:transfer-post-to-ledger", args=[created.data["id"]])
        self.client.post(url)

        self.assertEqual(self.client.post(url).status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_transfer_cannot_be_deleted(self):
        created = self.create()

        response = self.client.delete(
            reverse("inventory:transfer-detail", args=[created.data["id"]])
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

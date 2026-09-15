"""Seed a handful of warehouse transfers — F25.

So the transfer screens are built and reviewed against rows rather than an
empty table. Everything here is real: each transfer moves SKUs the source
warehouse actually holds, at quantities it can actually cover, and posting
writes the same two ledger rows the application would.

Idempotent by count — running it twice does not double the history.
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from catalog.models import Sku, Warehouse
from inventory.models import ReasonCode, WarehouseTransfer
from inventory.services import create_transfer, post_transfer, stock_levels

# Why a warehouse would rebalance. Written as a dispatcher would write them,
# not as lorem — these end up on screen.
NOTES = [
    "Serere short of shirts before the term one intake.",
    "Weekly replenishment dispatch for the Serere packing cluster.",
    "Rebalancing after the Idudi delivery landed at the wrong site.",
    "Namayemba covering Serere until the next TC delivery arrives.",
    "Consolidating slow-moving sizes at the site that ships them.",
]


class Command(BaseCommand):
    help = "Create a few warehouse transfers between the seeded warehouses."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=6)

    def handle(self, *args, **options):
        wanted = options["count"]

        existing = WarehouseTransfer.objects.count()
        if existing >= wanted:
            self.stdout.write(f"{existing} transfers already exist — nothing to do.")
            return

        warehouses = list(Warehouse.objects.all()[:2])
        if len(warehouses) < 2:
            self.stderr.write("Needs two warehouses. Run seed_reference_data first.")
            return

        user = User.objects.filter(role=User.Role.PROGRAM_LEAD).first()
        if user is None:
            self.stderr.write("Needs a Program Lead to raise them.")
            return

        reason = ReasonCode.objects.filter(code__startswith="XFER", is_active=True).first()
        today = timezone.localdate()
        rng = random.Random(42)

        made = 0
        # Continues the sequence rather than restarting it, so topping the
        # history up keeps alternating direction and still leaves a prepared one.
        for index in range(existing, wanted):
            # Alternate the direction, so the list is not one arrow repeated.
            source, destination = (
                (warehouses[0], warehouses[1])
                if index % 2 == 0
                else (warehouses[1], warehouses[0])
            )

            on = today - timedelta(days=(index % 5) + 1)

            # Creating checks stock as it is **now**; posting checks it as it
            # was on the transfer date. Both have to pass, so take whichever
            # figure is smaller and only use SKUs that clear the bar on both.
            back_then = {
                row["sku_id"]: row["level"]
                for row in stock_levels(warehouse=source, as_of=on)
            }
            holdings = [
                (row["sku_id"], min(row["level"], back_then.get(row["sku_id"], 0)))
                for row in stock_levels(warehouse=source)
            ]
            holdings = [(sku_id, level) for sku_id, level in holdings if level >= 20]
            if not holdings:
                break

            picked = rng.sample(holdings, min(3, len(holdings)))
            skus = Sku.objects.in_bulk([sku_id for sku_id, _ in picked])
            lines = [
                {"sku": skus[sku_id], "quantity": rng.randint(5, max(5, on_hand // 4))}
                for sku_id, on_hand in picked
            ]

            transfer = create_transfer(
                created_by=user,
                from_warehouse=source,
                to_warehouse=destination,
                transfer_date=on,
                reason_code=reason,
                notes=NOTES[index % len(NOTES)],
                lines=lines,
            )

            # One in three is left prepared, so the screen shows both states
            # and the "nothing has moved yet" warning has something to warn
            # about.
            if index % 3 != 2:
                post_transfer(transfer, posted_by=user)

            made += 1

        self.stdout.write(self.style.SUCCESS(f"Created {made} transfers."))

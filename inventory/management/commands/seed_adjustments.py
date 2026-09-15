"""Seed a realistic spread of inventory adjustments — F23, F26, F27.

The opening-stock seed writes 66 corrections that all say the same thing, so
the adjustments screen renders one badge, one sign and one reason sixty-six
times over. That is true history and worth keeping, but it shows nothing
about how the screen reads when a return, a write-off and a loss sit next to
each other.

Everything here is a real posting: each decrease is checked against stock the
warehouse actually holds, and each writes the same ledger row the application
would. Nothing is fabricated into the table.

Idempotent by count on the non-opening reason codes.
"""

import random
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from catalog.models import Sku, Warehouse
from inventory.models import InventoryAdjustment, ReasonCode, StockMovement
from inventory.services import create_adjustment, post_adjustment, stock_levels

# What somebody would actually write in the notes box, per reason family.
# These end up on screen, so they are written as a storekeeper writes them.
NOTES = {
    "RET": [
        "School surplus returned after the term one issue.",
        "Wrong size sent to Bugiri High; came back unworn.",
        "Student size exchange — original returned to stock.",
    ],
    "DMG": [
        "Water damage during the rain on the loading bay.",
        "Moth damage found in the back rack.",
        "Torn in handling; unsellable.",
    ],
    "LOSS": [
        "Not found at the shelf during the weekly verify cycle.",
        "Short on the van manifest; never traced.",
        "Missing after the stock move to the new rack.",
    ],
}


class Command(BaseCommand):
    help = "Post a spread of returns, damages and losses against real stock."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=12)

    def handle(self, *args, **options):
        wanted = options["count"]

        codes = {
            prefix: ReasonCode.objects.filter(
                code__startswith=prefix, is_active=True
            ).first()
            for prefix in NOTES
        }
        missing = [prefix for prefix, code in codes.items() if code is None]
        if missing:
            self.stderr.write(f"Missing reason codes: {', '.join(missing)}.")
            return

        existing = InventoryAdjustment.objects.filter(
            reason_code__in=[code for code in codes.values()]
        ).count()
        if existing >= wanted:
            self.stdout.write(f"{existing} already posted — nothing to do.")
            return

        user = User.objects.filter(role=User.Role.FINANCE).first()
        if user is None:
            self.stderr.write("Needs a Finance user to post them.")
            return

        today = timezone.localdate()
        rng = random.Random(7)

        # The first day anything was on a shelf anywhere.
        opening = (
            StockMovement.objects.order_by("occurred_on")
            .values_list("occurred_on", flat=True)
            .first()
            or today
        )
        prefixes = list(NOTES)

        made = 0
        for index in range(existing, wanted):
            prefix = prefixes[index % len(prefixes)]
            code = codes[prefix]
            # Dated on or after the day opening stock landed — an adjustment
            # cannot take units off a shelf that was empty, and posting checks
            # the figure as at this date, not today's.
            on = max(opening, today - timedelta(days=rng.randint(0, 6)))

            # Creating checks stock now; posting checks it as at the
            # adjustment date. Take the smaller figure so both pass.
            back_then = {
                (row["sku_id"], row["warehouse_id"]): row["level"]
                for row in stock_levels(as_of=on)
            }
            candidates = [
                (
                    row["sku_id"],
                    row["warehouse_id"],
                    min(row["level"], back_then.get((row["sku_id"], row["warehouse_id"]), 0)),
                )
                for row in stock_levels()
            ]
            candidates = [entry for entry in candidates if entry[2] >= 30]
            if not candidates:
                break

            sku_id, warehouse_id, on_hand = rng.choice(candidates)

            adjustment = create_adjustment(
                created_by=user,
                warehouse=Warehouse.objects.get(pk=warehouse_id),
                sku=Sku.objects.get(pk=sku_id),
                quantity=rng.randint(2, min(25, on_hand // 4)),
                reason_code=code,
                adjustment_date=on,
                notes=rng.choice(NOTES[prefix]),
            )
            post_adjustment(adjustment, posted_by=user)
            made += 1

        self.stdout.write(self.style.SUCCESS(f"Posted {made} adjustments."))

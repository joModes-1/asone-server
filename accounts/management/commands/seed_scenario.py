"""Simulate one full AsOne season — by calling the real service functions,
never by writing rows directly.

    .venv/bin/python manage.py seed_scenario

seed_demo gives the system its master data and one user per role, but almost
nothing happens in it. This builds a season on top of that: a Group Order
broken into Production Orders on all three Tailoring Centers, receipts posted
against them — one of them short, so a discrepancy is visible — a stock
adjustment, a warehouse transfer, and school orders left in every status the
system can currently reach.

Every transaction here is posted through the same function the API uses
(`place_order`, `post_receipt`, `pick_order`, and so on), so this data obeys
exactly the rules real data would: prices are snapshotted the same way,
stock can't go negative, a kit explodes into the same components, and a
picked order reserves stock the same way a warehouse clerk's click would.

Calls seed_demo first, so this works against a freshly migrated database.

Not idempotent like seed_demo — every call here raises a new numbered
document, so running it twice would place a second season on top of the
first. A Group Order carrying SCENARIO_MARKER on its notes is used to detect
a previous run and refuse rather than duplicate it.
"""

from collections import Counter
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from accounts.models import User
from catalog.models import Kit, School, Sku, TailoringCenter, Warehouse
from catalog.services import reprice
from inventory.models import ReasonCode
from inventory.services import (
    below_minimum,
    correct_count,
    create_adjustment,
    create_transfer,
    post_adjustment,
    post_transfer,
)
from orders.services import cancel_order, pick_order, place_order
from procurement.models import GroupOrder
from procurement.services import (
    create_group_order,
    create_production_order,
    create_receipt,
    post_receipt,
)

#: Written onto the Group Order's notes so a second run can be detected and
#: refused, rather than silently placing a second season on top of the first.
SCENARIO_MARKER = "seed_scenario: 2026/27 season"


class Command(BaseCommand):
    help = (
        "Simulate a full AsOne season on top of seed_demo's master data: "
        "group/production orders on all three Tailoring Centers, a short "
        "delivery, a stock adjustment, a warehouse transfer, and school "
        "orders in every status the system can reach today. Everything is "
        "posted through the real service functions. Development only."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run even with DEBUG off. You almost certainly do not want this.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "seed_scenario posts a season's worth of transactions against "
                "demo accounts and is refusing to run with DEBUG off. Pass "
                "--force only if you are certain this is not a real database."
            )

        if GroupOrder.objects.filter(notes=SCENARIO_MARKER).exists():
            raise CommandError(
                "The season scenario has already been seeded — a Group Order "
                f"carries the marker {SCENARIO_MARKER!r}. Running this again "
                "would place a second season on top of the first. Reset the "
                "database if you want to generate it again from scratch."
            )

        call_command("seed_demo", force=options["force"])

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Season scenario"))

        people = self._people()
        sites = self._sites()
        catalog = self._catalog()

        group_order, production_orders = self._place_orders(people, sites, catalog)
        receipts = self._receive_deliveries(production_orders, people)
        adjustment = self._adjust_stock(people, sites, catalog)
        transfer = self._transfer_stock(people, sites, catalog)
        school_orders = self._place_school_orders(people, sites, catalog)
        corrections = self._correct_counts(people, sites, catalog)
        return_and_loss = self._return_and_loss(people, sites, catalog)
        new_price = self._reprice_midseason(catalog)

        self._report(
            group_order, production_orders, receipts, adjustment, transfer,
            school_orders, corrections, return_and_loss, new_price, sites, catalog,
        )

    # -- lookups -------------------------------------------------------------

    def _people(self):
        """The demo users who play each part in the season, by role."""
        emails = {
            "leads": "andrew@asone.test",  # raises the Group and Production Orders
            "finance": "musana@asone.test",  # posts the adjustment and the transfer
            "namayemba_staff": "julius@asone.test",  # receives and picks at Namayemba
            "serere_staff": "joan@asone.test",  # receives and picks at Serere
            "namayemba_school": "chrisis@asone.test",  # places orders for Namayemba PS
            "serere_school": "peter@asone.test",  # places orders for Serere HS
        }
        try:
            return {key: User.objects.get(email=email) for key, email in emails.items()}
        except User.DoesNotExist as exc:
            raise CommandError(
                "seed_demo did not create a user this scenario expects — has "
                "the demo user list changed?"
            ) from exc

    def _sites(self):
        return {
            "idudi": TailoringCenter.objects.get(name="Idudi"),
            "serere_tc": TailoringCenter.objects.get(name="Serere"),
            "rwanyabihuka": TailoringCenter.objects.get(name="Rwanyabihuka"),
            "namayemba_wh": Warehouse.objects.get(name="Namayemba"),
            "serere_wh": Warehouse.objects.get(name="Serere"),
            "namayemba_ps": School.objects.get(name="Namayemba Primary School"),
            "serere_hs": School.objects.get(name="Serere High School"),
        }

    def _catalog(self):
        """The SKUs and kits this season moves. Each SKU is the one size
        seed_demo actually uses as a kit component (the smallest sort_order
        for that garment), so kit and item-level lines can share stock."""

        def sku(garment, size):
            return Sku.objects.get(garment__name=garment, size__name=size)

        return {
            "white_shirt": sku("White Shirt", "8"),
            "blue_tunic": sku("Blue Tunic", "8"),
            "grey_trousers": sku("Grey Trousers", "8"),
            "navy_skirt": sku("Navy Skirt", "8"),
            "grey_shorts": sku("Grey Shorts", "8"),
            "jumper": sku("Jumper", "8"),
            "socks": sku("Socks", "10"),
            "ps_kit": Kit.objects.get(kit_number="PS-STARTER-01"),
            "hs_kit": Kit.objects.get(kit_number="HS-STARTER-01"),
        }

    # -- procurement: F16, F17 -------------------------------------------------

    def _place_orders(self, people, sites, catalog):
        """One Group Order, broken into a Production Order on each of the
        three Tailoring Centers — two of them shipping into Namayemba and
        Serere respectively, the third showing a warehouse ordering on a TC
        that isn't its primary one (p.4: "can order on any TC")."""
        order_date = date(2026, 9, 1)
        due = date(2026, 10, 15)

        # Namayemba serves a Primary and a High School; Serere the same. Both
        # warehouses stock close to the full catalogue for that reason, not
        # only the SKUs this scenario later orders and picks — a warehouse
        # carries stock ahead of demand, not just-in-time against known orders.
        idudi_lines = {
            "white_shirt": 250,
            "blue_tunic": 250,
            "grey_shorts": 150,
            "jumper": 150,
            "socks": 200,
        }
        serere_tc_lines = {
            "white_shirt": 150,
            "grey_trousers": 150,
            "jumper": 100,
            "socks": 120,
        }
        rwanyabihuka_lines = {
            "navy_skirt": 60,  # the line that comes up short on receipt
            "grey_shorts": 80,
            "socks": 40,
        }

        def totals(*line_sets):
            summed = {}
            for lines in line_sets:
                for key, quantity in lines.items():
                    summed[key] = summed.get(key, 0) + quantity
            return summed

        group_order = create_group_order(
            created_by=people["leads"],
            order_date=order_date,
            due_in_warehouse_date=due,
            notes=SCENARIO_MARKER,
            lines=[
                {"sku": catalog[key], "quantity": quantity}
                for key, quantity in totals(idudi_lines, serere_tc_lines, rwanyabihuka_lines).items()
            ],
        )

        po_idudi = create_production_order(
            created_by=people["leads"],
            order_date=order_date,
            due_in_warehouse_date=due,
            tailoring_center=sites["idudi"],
            warehouse=sites["namayemba_wh"],
            group_order=group_order,
            notes="The bulk of Namayemba's stock for the season.",
            lines=[
                {"sku": catalog[key], "quantity": quantity}
                for key, quantity in idudi_lines.items()
            ],
        )

        po_serere = create_production_order(
            created_by=people["leads"],
            order_date=order_date,
            due_in_warehouse_date=due,
            tailoring_center=sites["serere_tc"],
            warehouse=sites["serere_wh"],
            group_order=group_order,
            notes="Serere's own Tailoring Center supplying its own warehouse.",
            lines=[
                {"sku": catalog[key], "quantity": quantity}
                for key, quantity in serere_tc_lines.items()
            ],
        )

        po_rwanyabihuka = create_production_order(
            created_by=people["leads"],
            order_date=order_date,
            due_in_warehouse_date=due,
            tailoring_center=sites["rwanyabihuka"],
            warehouse=sites["serere_wh"],
            group_order=group_order,
            notes="Serere drawing on a second TC, not just its primary one.",
            lines=[
                {"sku": catalog[key], "quantity": quantity}
                for key, quantity in rwanyabihuka_lines.items()
            ],
        )

        return group_order, {
            "idudi": po_idudi,
            "serere": po_serere,
            "rwanyabihuka": po_rwanyabihuka,
        }

    # -- receipts: F19, F20, F21 -----------------------------------------------

    def _receive_deliveries(self, production_orders, people):
        """Receipts against each order. Idudi and Serere arrive exactly as
        ordered; Rwanyabihuka comes up short on the Navy Skirt line, which
        is what leaves that SKU below its reorder floor at Serere."""
        receipts = {}

        receipts["idudi"] = self._receive_in_full(
            production_orders["idudi"],
            posted_by=people["namayemba_staff"],
            packing_list_number="IDUDI-2026-041",
            date_received=date(2026, 10, 10),
        )
        receipts["serere"] = self._receive_in_full(
            production_orders["serere"],
            posted_by=people["serere_staff"],
            packing_list_number="SERERE-2026-018",
            date_received=date(2026, 10, 10),
        )

        # Every line on the order is received in full except Navy Skirt,
        # which comes up short. Built from all of the order's lines rather
        # than naming each one, so a line added to rwanyabihuka_lines later
        # cannot be forgotten here the way Grey Shorts was the first time.
        short_delivered = {"Navy Skirt": 25}

        def rwanyabihuka_line(line):
            garment_name = line.sku.garment.name
            if garment_name not in short_delivered:
                return {
                    "sku": line.sku,
                    "quantity_received": line.quantity,
                    "quantity_on_packing_list": line.quantity,
                }
            received = short_delivered[garment_name]
            return {
                "sku": line.sku,
                "quantity_received": received,
                "quantity_on_packing_list": line.quantity,
                "discrepancy_note": (
                    f"Only {received} of {line.quantity} completed — raw material "
                    "shortage at Rwanyabihuka. Balance to follow on a later delivery."
                ),
            }

        receipt = create_receipt(
            production_order=production_orders["rwanyabihuka"],
            created_by=people["serere_staff"],
            packing_list_number="RWY-2026-007",
            date_received=date(2026, 10, 12),
            lines=[
                rwanyabihuka_line(line)
                for line in production_orders["rwanyabihuka"].lines.select_related("sku__garment")
            ],
        )
        post_receipt(receipt, posted_by=people["serere_staff"])
        receipts["rwanyabihuka"] = receipt

        return receipts

    def _receive_in_full(self, production_order, *, posted_by, packing_list_number, date_received):
        """A delivery that matches its production order exactly, line for line."""
        receipt = create_receipt(
            production_order=production_order,
            created_by=posted_by,
            packing_list_number=packing_list_number,
            date_received=date_received,
            lines=[
                {
                    "sku": line.sku,
                    "quantity_received": line.quantity,
                    "quantity_on_packing_list": line.quantity,
                }
                for line in production_order.lines.all()
            ],
        )
        post_receipt(receipt, posted_by=posted_by)
        return receipt

    # -- inventory: F23/F27, F25 -----------------------------------------------

    def _adjust_stock(self, people, sites, catalog):
        """A handful of White Shirts written off as damaged at Namayemba —
        the same F23 endpoint F27 (Damages) reuses, DMG reason code."""
        dmg = ReasonCode.objects.get(code="DMG")
        adjustment = create_adjustment(
            warehouse=sites["namayemba_wh"],
            sku=catalog["white_shirt"],
            quantity=5,
            reason_code=dmg,
            adjustment_date=date(2026, 10, 25),
            created_by=people["finance"],
            notes="Water damage discovered during the October shelf check.",
        )
        post_adjustment(adjustment, posted_by=people["finance"])
        return adjustment

    def _transfer_stock(self, people, sites, catalog):
        """Namayemba over-received on Blue Tunics; Serere had none. No money
        moves — both ledger rows carry the value already on Namayemba's shelf."""
        transfer = create_transfer(
            from_warehouse=sites["namayemba_wh"],
            to_warehouse=sites["serere_wh"],
            created_by=people["finance"],
            transfer_date=date(2026, 10, 26),
            notes="Rebalancing Namayemba's Blue Tunic surplus to Serere.",
            lines=[{"sku": catalog["blue_tunic"], "quantity": 70}],
        )
        post_transfer(transfer, posted_by=people["finance"])
        return transfer

    # -- school orders: F30-F39 -------------------------------------------------

    #: (student_name, order_date, kits, skus, final_status).
    #: Only Namayemba Primary and Serere High have a School Staff demo
    #: account in seed_demo — Bugiri High and Serere Primary do not, and
    #: place_order is something only School Staff actually do in the real
    #: system, so orders are not fabricated for schools nobody can place one
    #: for. This is where the volume lives instead: fourteen orders across
    #: the two schools that can genuinely place them, not one apiece.
    NAMAYEMBA_ORDERS = [
        ("Grace Nabirye", date(2026, 11, 2), [("ps_kit", 1)], [], "hold"),
        ("Isaac Mukasa", date(2026, 11, 2), [("ps_kit", 1)], [], "cancel"),
        ("Faith Namutebi", date(2026, 11, 3), [], [("blue_tunic", 2), ("socks", 1)], "pick"),
        ("Daniel Kato", date(2026, 11, 6), [("ps_kit", 2)], [], "pick"),
        ("Ruth Naigaga", date(2026, 11, 7), [], [("white_shirt", 3)], "pick"),
        ("Moses Wandera", date(2026, 11, 10), [("ps_kit", 1)], [], "hold"),
        ("Sarah Auma", date(2026, 11, 10), [], [("socks", 4)], "pick"),
        ("Brian Ochieng", date(2026, 11, 12), [("ps_kit", 1)], [], "cancel"),
    ]
    SERERE_ORDERS = [
        ("Peter Okurut", date(2026, 11, 4), [("hs_kit", 1)], [], "hold"),
        ("Mary Achen", date(2026, 11, 5), [("hs_kit", 1)], [], "pick"),
        ("James Ejang", date(2026, 11, 8), [], [("grey_trousers", 2)], "pick"),
        ("Esther Amongin", date(2026, 11, 9), [("hs_kit", 1)], [], "hold"),
        ("Simon Okello", date(2026, 11, 11), [], [("white_shirt", 2), ("socks", 2)], "pick"),
        ("Grace Adong", date(2026, 11, 13), [("hs_kit", 1)], [], "cancel"),
    ]

    def _place_school_orders(self, people, sites, catalog):
        """A season's worth of orders at the two schools that can actually
        place one, left in every status reachable today: Hold, Cancelled and
        Picked. Released is not reachable yet — see open question Q2."""
        orders = []
        orders += self._run_order_batch(
            self.NAMAYEMBA_ORDERS,
            school=sites["namayemba_ps"],
            placed_by=people["namayemba_school"],
            picked_by=people["namayemba_staff"],
            catalog=catalog,
        )
        orders += self._run_order_batch(
            self.SERERE_ORDERS,
            school=sites["serere_hs"],
            placed_by=people["serere_school"],
            picked_by=people["serere_staff"],
            catalog=catalog,
        )
        return orders

    def _run_order_batch(self, specs, *, school, placed_by, picked_by, catalog):
        orders = []
        for student_name, order_date, kit_lines, sku_lines, final_status in specs:
            order = place_order(
                school=school,
                student_name=student_name,
                order_date=order_date,
                kits=[{"kit": catalog[key], "quantity": qty} for key, qty in kit_lines],
                skus=[{"sku": catalog[key], "quantity": qty} for key, qty in sku_lines],
                created_by=placed_by,
            )
            if final_status == "cancel":
                order = cancel_order(order, cancelled_by=placed_by, reason="Family could not pay before term start.")
            elif final_status == "pick":
                order = pick_order(order, picked_by=picked_by)
            orders.append(order)
        return orders

    # -- physical count correction: F24 -----------------------------------------

    def _correct_counts(self, people, sites, catalog):
        """A term-end stock take at each warehouse — the actual F24, distinct
        from the generic F23 adjustment `_adjust_stock` posts above. Unlike
        that one, `correct_count()` does the comparing itself: the caller
        supplies only what was physically counted, on one SKU that comes in
        over the system's figure (CORR_UP) and one that comes in under
        (CORR_DOWN). Posted immediately — a count correction has no
        create/post split."""
        count_date = date(2026, 11, 16)

        over = correct_count(
            warehouse=sites["namayemba_wh"],
            sku=catalog["grey_shorts"],
            counted_quantity=155,  # the ledger says 150 at this point
            adjustment_date=count_date,
            created_by=people["finance"],
            notes="Term-end stock take found five more on the shelf than the system expected.",
        )
        under = correct_count(
            warehouse=sites["serere_wh"],
            sku=catalog["jumper"],
            counted_quantity=96,  # the ledger says 100 at this point
            adjustment_date=count_date,
            created_by=people["finance"],
            notes="Term-end stock take found four fewer than the system expected.",
        )
        return [correction for correction in (over, under) if correction is not None]

    # -- returns and losses: F26, F27 --------------------------------------------

    def _return_and_loss(self, people, sites, catalog):
        """The other two F23-family reason codes the season hasn't touched
        yet — RET and LOSS — proven the same way F26/F27 were proven against
        DMG: no new code, the same create_adjustment/post_adjustment pair,
        a different reason code."""
        ret = ReasonCode.objects.get(code="RET")
        loss = ReasonCode.objects.get(code="LOSS")
        event_date = date(2026, 11, 14)

        returned = create_adjustment(
            warehouse=sites["namayemba_wh"],
            sku=catalog["white_shirt"],
            quantity=2,
            reason_code=ret,
            adjustment_date=event_date,
            created_by=people["finance"],
            notes="Namayemba Primary returned two unused White Shirts, wrong size.",
        )
        post_adjustment(returned, posted_by=people["finance"])

        lost = create_adjustment(
            warehouse=sites["serere_wh"],
            sku=catalog["socks"],
            quantity=3,
            reason_code=loss,
            adjustment_date=event_date,
            created_by=people["finance"],
            notes="Three pairs unaccounted for at Serere — picked up without a matching order.",
        )
        post_adjustment(lost, posted_by=people["finance"])

        return {"return": returned, "loss": lost}

    # -- mid-season reprice: F08 --------------------------------------------------

    def _reprice_midseason(self, catalog):
        """A garment repriced partway through the season — F08's whole point.
        Effective after every order this scenario places, so none of their
        already-snapshotted line prices change: an order placed in November
        still costs out at November's price even after this."""
        return reprice(catalog["white_shirt"].garment, Decimal("27000.00"), date(2026, 12, 1))

    # -- summary ---------------------------------------------------------------

    def _report(
        self, group_order, production_orders, receipts, adjustment, transfer,
        school_orders, corrections, return_and_loss, new_price, sites, catalog,
    ):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Procurement"))
        self.stdout.write(
            f"  {group_order.number}  Group Order, "
            f"{group_order.total_quantity} garments across all three TCs"
        )
        for po in production_orders.values():
            self.stdout.write(
                f"    {po.number}  {po.tailoring_center.name} -> {po.warehouse.name}"
            )

        short_line = receipts["rwanyabihuka"].lines.get(sku__garment__name="Navy Skirt")
        self.stdout.write(
            f"  {receipts['rwanyabihuka'].number} arrived short: "
            f"{short_line.quantity_received} of {short_line.quantity_on_packing_list} "
            "Navy Skirts (size 8)."
        )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Inventory"))
        self.stdout.write(
            f"  {adjustment.number}  {adjustment.reason_code.name}: "
            f"{adjustment.quantity} x {adjustment.sku.number}"
        )
        self.stdout.write(
            f"  {transfer.number}  {transfer.from_warehouse.name} -> {transfer.to_warehouse.name}"
        )

        alerts = below_minimum()
        self.stdout.write(
            f"  {len(alerts)} SKU/warehouse pairs now below their reorder floor "
            "(most are simply untouched by this scenario) — the one worth "
            "looking at:"
        )
        for alert in alerts:
            if alert["sku"] == catalog["navy_skirt"] and alert["warehouse"] == sites["serere_wh"]:
                self.stdout.write(
                    f"    {alert['sku'].number}  {alert['sku'].description} "
                    f"at {alert['warehouse'].name}: {alert['level']} on hand, "
                    f"minimum {alert['minimum']} — caused by the short delivery above."
                )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("School orders"))
        tally = Counter(order.get_status_display() for order in school_orders)
        self.stdout.write(
            f"  {len(school_orders)} orders: "
            + ", ".join(f"{count} {status}" for status, count in tally.items())
        )
        for order in school_orders:
            self.stdout.write(
                f"    {order.number}  {order.school.name} — {order.student_name} "
                f"— {order.get_status_display()}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Term-end stock take (F24)"))
        for correction in corrections:
            direction = "found more" if correction.reason_code.code == "CORR_UP" else "found fewer"
            self.stdout.write(
                f"  {correction.number}  {correction.warehouse.name}, "
                f"{correction.sku.number}: {direction} — {correction.reason_code.code} "
                f"{correction.quantity}"
            )

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Returns and losses (F26, F27)"))
        returned, lost = return_and_loss["return"], return_and_loss["loss"]
        self.stdout.write(f"  {returned.number}  RET  +{returned.quantity} x {returned.sku.number} at {returned.warehouse.name}")
        self.stdout.write(f"  {lost.number}  LOSS  -{lost.quantity} x {lost.sku.number} at {lost.warehouse.name}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Mid-season reprice (F08)"))
        self.stdout.write(
            f"  {new_price.garment}: UGX {new_price.unit_price} from {new_price.active_date} — "
            "every order already placed this season keeps its original price."
        )

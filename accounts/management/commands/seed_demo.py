"""Populate a development database with AsOne's real sites and one user per role.

    .venv/bin/python manage.py seed_demo

Idempotent — run it as often as you like. It creates nothing that does not
already exist and never overwrites a password you have changed.

It lives in `accounts` because its purpose is the users; the sites exist so
those users have somewhere to belong.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from datetime import date
from decimal import Decimal

from accounts.models import User
from catalog.models import Garment, GarmentPrice, Kit, KitItem, MinimumStockLevel, School, Size, Sku, TailoringCenter, Warehouse
from inventory.models import ReasonCode

#: Known password, printed to the terminal. This is exactly why the command
#: refuses to run outside DEBUG.
DEMO_PASSWORD = "asone-demo-2027"

TAILORING_CENTERS = ["Idudi", "Serere", "Rwanyabihuka"]

# Namayemba also hosts AsOne's central office.
WAREHOUSES = [
    ("Namayemba", "Idudi"),
    ("Serere", "Serere"),
]

SCHOOLS = [
    ("Namayemba Primary School", School.Level.PRIMARY, "Namayemba"),
    ("Bugiri High School", School.Level.HIGH, "Namayemba"),
    ("Serere Primary School", School.Level.PRIMARY, "Serere"),
    ("Serere High School", School.Level.HIGH, "Serere"),
]

#: AsOne sizes garments XS through XXL, not by number — confirmed by the
#: 2027 SKU workbook, where all 308 SKUs carry one of these seven. The demo
#: used 8/10/12/14/16 before, which made every screenshot and every
#: conversation about the demo disagree with the real catalogue.
#:
#: "OS" is for the things that come in one size only — socks and neckties.
#: It sorts last because it is not a point on the scale.
SIZES = [
    ("XS", 10),
    ("S", 20),
    ("M", 30),
    ("L", 40),
    ("XL", 50),
    ("XXL", 60),
    ("OS", 99),
]

#: Garments that come in one size rather than the full run.
ONE_SIZE_GARMENTS = {"Socks"}

# name, school level, colour, unit price in UGX
#: name, fit group, colour, colour code, item code, price
#:
#: Every garment carries a fit group and a colour code now, so the demo
#: produces the five-part SKU numbers AsOne's catalogue uses rather than the
#: two-part fallback. A demo whose numbers do not look like the real ones is
#: a demo nobody can check anything against.
#:
#: The item codes are stated rather than derived for the same reason AsOne's
#: are: "Jumper" derives to JUM and "Socks" to SOC, but Grey Trousers and
#: Grey Shorts would both give GSH, and picking which one wins is a human
#: call, not a rule.
GARMENTS = [
    ("White Shirt", Garment.FitGroup.ALL, "White", "WHT", "WSH", "25000.00"),
    ("Grey Trousers", Garment.FitGroup.S1_S4, "Grey", "GRY", "GTR", "35000.00"),
    ("Blue Tunic", Garment.FitGroup.P1_P4, "Blue", "BLU", "BTU", "30000.00"),
    ("Navy Skirt", Garment.FitGroup.S5_S6, "Navy", "NVY", "NSK", "32000.00"),
    ("Grey Shorts", Garment.FitGroup.P5_P7, "Grey", "GRY", "GYSH", "22000.00"),
    ("Jumper", Garment.FitGroup.NURSERY, "Maroon", "MAR", "JUM", "45000.00"),
    ("Socks", Garment.FitGroup.ALL, "White", "WHT", "SOC", "5000.00"),
]

#: Prices run from the start of the 2026 school year so that "price today"
#: resolves. The 2027 season is what AsOne is actually planning for.
PRICES_ACTIVE_FROM = date(2026, 1, 1)

#: A starter kit per school level — what a new student needs, and AsOne's
#: own example of why kits exist. Without one, nothing outside the tests
#: exercises kit explosion at the point of sale.
KITS = [
    ("PS-STARTER-01", "PS Starter Kit", "PS",
     [("Blue Tunic", 2), ("White Shirt", 2), ("Socks", 3)]),
    ("HS-STARTER-01", "HS Starter Kit", "HS",
     [("Grey Trousers", 2), ("White Shirt", 2), ("Socks", 3)]),
]

#: The four AsOne named on p.3, followed by "May be more…" — so this is a
#: starting set for Central Office to extend, not the definitive list.
#: code, name, description, direction (F23 — see ReasonCode.AdjustmentDirection)
REASON_CODES = [
    ("RET", "Return", "Uniform returned by a school, back into sellable stock.", "INCREASE"),
    ("XFER", "Warehouse transfer", "Stock moved between Namayemba and Serere.", "DECREASE"),
    ("LOSS", "Pick up or loss", "Stock gone missing, or collected without an order.", "DECREASE"),
    ("DMG", "Damaged", "Damaged in transit or in the warehouse, no longer sellable.", "DECREASE"),
    # A physical count can land either side of the system figure, so a
    # correction needs a code each way — see inventory migration 0006.
    ("CORR_UP", "Inventory correction — count higher",
     "A physical count found more than the system expected.", "INCREASE"),
    ("CORR_DOWN", "Inventory correction — count lower",
     "A physical count found less than the system expected.", "DECREASE"),
]

# email, first name, last name, role, warehouse, school
DEMO_USERS = [
    ("sharon@asone.test", "Sharon", "Nakato", User.Role.PROGRAM_LEAD, None, None),
    ("andrew@asone.test", "Andrew", "Mugisha", User.Role.OPERATIONS_MANAGER, None, None),
    ("musana@asone.test", "Musana", "Kato", User.Role.FINANCE, None, None),
    ("julius@asone.test", "Julius", "Okello", User.Role.WAREHOUSE_STAFF, "Namayemba", None),
    ("joan@asone.test", "Joan", "Adeke", User.Role.WAREHOUSE_STAFF, "Serere", None),
    ("chrisis@asone.test", "Chrisis", "Nabirye", User.Role.SCHOOL_STAFF, None, "Namayemba Primary School"),
    ("peter@asone.test", "Peter", "Owor", User.Role.SCHOOL_STAFF, None, "Serere High School"),
]


class Command(BaseCommand):
    help = "Create AsOne's sites and one demo user per role. Development only."

    def add_arguments(self, parser):
        parser.add_argument(
            "--force",
            action="store_true",
            help="Run even with DEBUG off. You almost certainly do not want this.",
        )
        parser.add_argument(
            "--prune-legacy-sizes",
            action="store_true",
            help=(
                "Retire demo sizes that are no longer in SIZES, and the SKUs "
                "beneath them. Use after the size scheme changes. Anything a "
                "stock movement, order or kit still points at is kept and "
                "reported \u2014 history is never deleted to tidy a list."
            ),
        )
        parser.add_argument(
            "--reset-passwords",
            action="store_true",
            help=(
                "Put every demo account back on the shared demo password. "
                "Use after testing a password change has left you locked out."
            ),
        )

    @transaction.atomic
    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "seed_demo creates accounts with a published password and is "
                "refusing to run with DEBUG off. Pass --force only if you are "
                "certain this is not a real database."
            )

        if options["prune_legacy_sizes"]:
            self._prune_legacy_sizes()

        centers = self._seed(
            "Tailoring Centers",
            TAILORING_CENTERS,
            lambda name: TailoringCenter.objects.get_or_create(name=name),
        )

        warehouses = self._seed(
            "Warehouses",
            WAREHOUSES,
            lambda row: Warehouse.objects.get_or_create(
                name=row[0],
                defaults={"primary_tailoring_center": centers[row[1]]},
            ),
            key=lambda row: row[0],
        )

        self._seed(
            "Schools",
            SCHOOLS,
            lambda row: School.objects.get_or_create(
                name=row[0],
                defaults={"level": row[1], "primary_warehouse": warehouses[row[2]]},
            ),
            key=lambda row: row[0],
        )

        self._seed_catalog(warehouses)
        self._seed_kits()
        self._seed_reason_codes()
        self._seed_users(warehouses)

        if options["reset_passwords"]:
            self._reset_passwords()

        self._report()

    # -- helpers ---------------------------------------------------------

    def _seed(self, label, rows, create, key=lambda row: row):
        """Run `create` over `rows`, reporting what was new. Returns a name map."""
        self.stdout.write(self.style.MIGRATE_HEADING(label))
        made = {}
        for row in rows:
            obj, created = create(row)
            made[key(row)] = obj
            verb = self.style.SUCCESS("created") if created else "exists "
            self.stdout.write(f"  {verb}  {obj}")
        return made

    def _seed_catalog(self, warehouses):
        """Garments, sizes, SKUs, prices and reorder floors.

        Roughly a tenth of the real catalogue — enough to exercise the price
        lists, both school levels, and per-warehouse minimums.
        """
        self.stdout.write(self.style.MIGRATE_HEADING("Sizes"))
        sizes = {}
        for name, order in SIZES:
            size, created = Size.objects.get_or_create(
                name=name,
                defaults={"sort_order": order, "is_one_size": name == "OS"},
            )
            # `defaults` only applies on creation, so a size that already
            # existed from an earlier run keeps whatever flag it had. That
            # left "OS" un-flagged after the size scheme changed, and the
            # kit size rule silently stopped exempting socks. Re-asserted
            # every run so re-seeding converges rather than drifting.
            wants_one_size = name == "OS"
            if not created and size.is_one_size != wants_one_size:
                size.is_one_size = wants_one_size
                size.save(update_fields=["is_one_size"])
            sizes[name] = size
            self.stdout.write(f"  {self.style.SUCCESS('created') if created else 'exists '}  size {name}")

        self.stdout.write(self.style.MIGRATE_HEADING("Garments and prices"))
        garments = []
        for name, fit, colour, colour_code, item_code, price in GARMENTS:
            # Looked up on name alone.
            #
            # The key used to be (name, school_level) and became (name,
            # fit_group) when the fit group arrived — which meant a demo
            # database seeded before that change had every garment at
            # fit_group="" and re-seeding quietly created a second copy of
            # each one. The names are unique within this list, so the name
            # is the honest key, and the fit group is something to bring up
            # to date rather than to match on.
            garment = Garment.objects.filter(name=name).first()
            created = garment is None
            if created:
                garment = Garment(name=name)
            # Re-asserted every run, not just on creation, so re-seeding
            # converges rather than drifting — the same trap "OS" fell into.
            # A garment seeded before these fields existed kept a blank
            # colour code and went on producing two-part SKU numbers.
            garment.fit_group = fit
            garment.colour = colour
            garment.colour_code = colour_code
            if not garment.code:
                garment.code = item_code
            garment.full_clean()
            garment.save()
            garments.append(garment)

            # get_or_create on the price too: the exclusion constraint would
            # refuse a second overlapping row, so re-running must not try.
            GarmentPrice.objects.get_or_create(
                garment=garment,
                active_date=PRICES_ACTIVE_FROM,
                defaults={"unit_price": Decimal(price)},
            )
            verb = self.style.SUCCESS("created") if created else "exists "
            self.stdout.write(f"  {verb}  {garment} @ {price}")

        self.stdout.write(self.style.MIGRATE_HEADING("SKUs"))
        made = 0
        for garment in garments:
            # Socks come in one size, like the real catalogue.
            applicable = (
                ["OS"]
                if garment.name in ONE_SIZE_GARMENTS
                else [name for name, _ in SIZES if name != "OS"]
            )
            for size_name in applicable:
                sku, created = Sku.objects.get_or_create(
                    garment=garment, size=sizes[size_name]
                )
                made += created
        self.stdout.write(f"  {Sku.objects.count()} SKUs ({made} new this run)")

        self.stdout.write(self.style.MIGRATE_HEADING("Minimum stock levels"))
        floors = 0
        for sku in Sku.objects.all():
            for warehouse_name, quantity in (("Namayemba", 120), ("Serere", 60)):
                _, created = MinimumStockLevel.objects.get_or_create(
                    sku=sku,
                    warehouse=warehouses[warehouse_name],
                    defaults={"minimum_quantity": quantity},
                )
                floors += created
        self.stdout.write(f"  {MinimumStockLevel.objects.count()} floors ({floors} new this run)")

    def _seed_kits(self):
        """A starter kit per school level — F09."""
        self.stdout.write(self.style.MIGRATE_HEADING("Uniform kits"))

        for number, name, level, components in KITS:
            kit, created = Kit.objects.get_or_create(
                kit_number=number, defaults={"name": name, "school_level": level}
            )
            verb = self.style.SUCCESS("created") if created else "exists "
            self.stdout.write(f"  {verb}  {number}  {name}")

            for garment_name, quantity in components:
                sku = (
                    Sku.objects.filter(garment__name=garment_name)
                    .order_by("size__sort_order")
                    .first()
                )
                if sku is None:
                    self.stdout.write(f"           (no SKU for {garment_name}, skipped)")
                    continue
                KitItem.objects.get_or_create(
                    kit=kit, sku=sku, defaults={"quantity": quantity}
                )

    def _seed_reason_codes(self):
        """Why an inventory adjustment was made — F13.

        Seeded because Phase 2 adjustments will need them, and Central Office
        can start from AsOne's four rather than a blank table.
        """
        self.stdout.write(self.style.MIGRATE_HEADING("Inventory adjustment reason codes"))
        for code, name, description, direction in REASON_CODES:
            _, created = ReasonCode.objects.get_or_create(
                code=code,
                defaults={"name": name, "description": description, "direction": direction},
            )
            verb = self.style.SUCCESS("created") if created else "exists "
            self.stdout.write(f"  {verb}  {code:5} {name}")

    def _seed_users(self, warehouses):
        self.stdout.write(self.style.MIGRATE_HEADING("Users"))
        schools = {s.name: s for s in School.objects.all()}

        for email, first_name, last_name, role, warehouse_name, school_name in DEMO_USERS:
            if User.objects.filter(email__iexact=email).exists():
                self.stdout.write(f"  exists   {email}")
                continue

            user = User(
                email=email,
                first_name=first_name,
                last_name=last_name,
                role=role,
                warehouse=warehouses.get(warehouse_name) if warehouse_name else None,
                school=schools.get(school_name) if school_name else None,
                # Demo accounts skip email confirmation. Nobody can read
                # @asone.test mail, so leaving it unconfirmed would create
                # accounts that exist and can never sign in.
                email_verified_at=timezone.now(),
            )
            user.set_password(DEMO_PASSWORD)
            # Run the role/site invariant rather than trusting the table above.
            user.full_clean(exclude=["password"])
            user.save()
            self.stdout.write(f"  {self.style.SUCCESS('created')}  {user}")

    def _reset_passwords(self):
        """Put the demo accounts back on the shared password.

        Testing a password change leaves the account on whatever it was
        changed to, which is correct behaviour and inconvenient the next time
        you want to sign in. This undoes it.

        Only touches the accounts this command creates — a real account that
        happens to share the database is left alone.
        """
        self.stdout.write(self.style.MIGRATE_HEADING("Resetting demo passwords"))

        demo_emails = [row[0] for row in DEMO_USERS]
        for user in User.objects.filter(email__in=demo_emails).order_by("email"):
            user.set_password(DEMO_PASSWORD)
            user.must_change_password = False
            user.save(update_fields=["password", "must_change_password"])
            self.stdout.write(f"  {self.style.SUCCESS('reset')}    {user.email}")

    def _report(self):
        self.stdout.write("")
        self.stdout.write(self.style.WARNING(f"All demo users share the password: {DEMO_PASSWORD}"))
        self.stdout.write("")
        self.stdout.write("Sign in with the email address, not a username.")
        self.stdout.write("")
        self.stdout.write("Try these against each other:")
        self.stdout.write("  julius@  (Namayemba) vs joan@ (Serere)    — must not see each other's site")
        self.stdout.write("  chrisis@ (school)    vs musana@ (Finance) — different matrix columns entirely")

    def _prune_legacy_sizes(self):
        """Retire demo sizes no longer in SIZES, and the SKUs beneath them.

        The seed is otherwise additive — everything is `get_or_create`, which
        is what makes it safe to re-run. That is the wrong behaviour when the
        size scheme itself changes: adding XS–XXL beside 8/10/12/14/16 leaves
        a demo carrying two conventions at once, which is exactly the
        confusion the change was meant to end.

        **History is never deleted to tidy a list.** A SKU that a stock
        movement, order line, receipt or kit still points at is protected by
        the same PROTECT this codebase uses everywhere, and is reported
        rather than forced. If anything is held back, the honest fix is a
        fresh database, not a harder delete.
        """
        from django.db.models import ProtectedError

        keep = {name for name, _ in SIZES}
        legacy = Size.objects.exclude(name__in=keep)

        self.stdout.write(self.style.MIGRATE_HEADING("Retiring legacy sizes"))
        if not legacy.exists():
            self.stdout.write("  nothing to retire")
            return

        held = []
        for size in legacy:
            for sku in list(size.skus.all()):
                try:
                    with transaction.atomic():
                        sku.delete()
                except ProtectedError:
                    held.append(sku.number)

            if size.skus.exists():
                continue
            try:
                with transaction.atomic():
                    name = size.name
                    size.delete()
                    self.stdout.write(f"  {self.style.WARNING('retired')}  size {name}")
            except ProtectedError:
                held.append(f"size {size.name}")

        if held:
            self.stdout.write(
                self.style.WARNING(
                    f"  kept {len(held)} still referenced by real records: "
                    + ", ".join(held[:8])
                    + ("..." if len(held) > 8 else "")
                )
            )
            self.stdout.write(
                "  a demo with transactions on the old sizes needs a fresh "
                "database, not a harder delete."
            )

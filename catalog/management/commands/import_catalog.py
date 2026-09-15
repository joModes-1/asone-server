"""Load AsOne's product catalogue from a spreadsheet — F05, F06, F08.

    .venv/bin/python manage.py import_catalog catalogue.xlsx --dry-run
    .venv/bin/python manage.py import_catalog catalogue.xlsx

Reads **Excel** (`.xlsx`), which is what AsOne will actually send, and CSV
for anyone who prefers it. The first sheet is used unless `--sheet` names
another.

About 45 garments in four or five sizes each, and a price for every one.
Typing 200 SKUs into a form is a day's work and a dozen typos, so AsOne
sends a spreadsheet and this reads it.

## One row per garment, not per SKU

A garment, its sizes, its SKUs and its price arrive together because they
are not separable: a garment with no sizes has no SKUs, and a SKU with no
price cannot be ordered. Asking for three files that have to agree with each
other invites them not to.

    name,colour,school_level,sizes,unit_price,active_date
    White Shirt,White,BOTH,6;8;10;12;14,25000,2026-01-01
    Blue Tunic,Blue,PS,6;8;10;12,30000,2026-01-01

`sizes` is semicolon-separated, in the order they should appear on a price
list. `school_level` is PS, HS or BOTH. `active_date` is the first day the
price applies, and may be left blank for today — Excel may hand it over as a
real date, which is read as such rather than as text.

Column names are matched case-insensitively and ignore surrounding spaces,
because a spreadsheet somebody has been editing rarely has them exactly
right.

That one file makes 45 garments, the sizes they share, ~200 SKUs and 45
prices.

## What it guarantees

**Nothing is half-imported.** The whole file is one transaction: a bad row on
line 40 means lines 1-39 are rolled back too. A catalogue missing the last
five garments, with nobody sure which, is worse than no catalogue.

**Every error is reported, not just the first.** Fixing a spreadsheet one
error per run is miserable when there are thirty.

**Running it twice changes nothing.** Garments are matched on name and
school level, sizes on name, SKUs on the pair. Re-importing a corrected file
updates rather than duplicates.

**It writes through the models**, so every constraint the application
enforces applies here too — no back door that admits data the API would
refuse.
"""

import csv
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from catalog.models import Garment, GarmentPrice, Size, Sku

REQUIRED_COLUMNS = {"name", "school_level", "sizes", "unit_price"}
LEVELS = {choice for choice, _ in Garment.SchoolLevel.choices}

#: Sizes sort by this where the name is not a number — "S" before "M" before
#: "L". Anything else falls back to the order it appeared in the row, which
#: is what a person filling in the spreadsheet would expect.
LETTER_ORDER = {"XS": 1, "S": 2, "M": 3, "L": 4, "XL": 5, "XXL": 6}


class RowError(Exception):
    """One bad row. Collected rather than raised, so the whole file reports."""


class Command(BaseCommand):
    help = "Import garments, sizes, SKUs and prices from a CSV. Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument("path", help="The .xlsx or .csv file to read.")
        parser.add_argument(
            "--sheet",
            help="Which worksheet to read. Defaults to the first one.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Read and check the file, report what would change, write nothing.",
        )

    def handle(self, *args, **options):
        path = Path(options["path"])
        if not path.exists():
            raise CommandError(f"No such file: {path}")

        rows = self._read(path, options.get("sheet"))
        if not rows:
            raise CommandError("That file has no data rows.")

        parsed, errors = [], []
        for line_no, row in rows:
            try:
                parsed.append((line_no, self._parse(row)))
            except RowError as exc:
                errors.append(f"  line {line_no}: {exc}")

        if errors:
            self.stdout.write(self.style.ERROR(f"{len(errors)} rows could not be read:"))
            for error in errors:
                self.stdout.write(self.style.ERROR(error))
            raise CommandError("Nothing was imported. Fix the file and run it again.")

        self._apply(parsed, dry_run=options["dry_run"])

    # -- reading ------------------------------------------------------------

    def _read(self, path, sheet_name=None):
        """Rows paired with their line number, so an error can name the line.

        Excel and CSV both end up as dicts keyed by lower-cased column name.
        A spreadsheet that has been passed around tends to have "Unit Price"
        or " name " rather than the exact header, so the keys are normalised
        rather than demanded.
        """
        if path.suffix.lower() in {".xlsx", ".xlsm"}:
            rows = self._read_excel(path, sheet_name)
        elif path.suffix.lower() == ".csv":
            rows = self._read_csv(path)
        else:
            raise CommandError(
                f"Cannot read {path.suffix or 'a file with no extension'}. "
                "Send the catalogue as .xlsx or .csv."
            )

        if not rows:
            return []

        header = set(rows[0][1])
        missing = REQUIRED_COLUMNS - header
        if missing:
            raise CommandError(
                "That file is missing columns: "
                + ", ".join(sorted(missing))
                + ".\nExpected: name, colour, school_level, sizes, "
                "unit_price, active_date"
            )
        return rows

    def _read_csv(self, path):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                return []
            # enumerate from 2: line 1 is the header, and a person counting
            # rows in a spreadsheet counts the header as line 1.
            return [
                (i, {self._key(k): v for k, v in row.items()})
                for i, row in enumerate(reader, start=2)
                if any((v or "").strip() for v in row.values())
            ]

    def _read_excel(self, path, sheet_name=None):
        """The first sheet, or the one named.

        `data_only=True` reads what a formula evaluated to rather than the
        formula itself — a price worked out with `=B2*1.1` has to import as
        the number somebody saw, not as text beginning with an equals sign.
        """
        from openpyxl import load_workbook

        try:
            book = load_workbook(path, read_only=True, data_only=True)
        except Exception as exc:
            raise CommandError(
                f"That file could not be opened as a spreadsheet: {exc}"
            ) from None

        if sheet_name:
            if sheet_name not in book.sheetnames:
                raise CommandError(
                    f"No sheet called {sheet_name!r}. This file has: "
                    + ", ".join(book.sheetnames)
                )
            sheet = book[sheet_name]
        else:
            sheet = book[book.sheetnames[0]]

        rows_iter = sheet.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            return []

        header = [self._key(cell) for cell in header_row]

        rows = []
        for i, values in enumerate(rows_iter, start=2):
            if not any(v not in (None, "") for v in values):
                continue  # a blank row left behind by editing
            rows.append((i, {header[j]: values[j] for j in range(min(len(header), len(values)))}))

        book.close()
        return rows

    @staticmethod
    def _key(name):
        """Column names as they are meant, not as they were typed."""
        return str(name or "").strip().lower().replace(" ", "_")

    def _parse(self, row):
        """One row into the values a garment needs.

        Every cell arrives via `_text` or a type check, because Excel does
        not hand over strings: a price is a float, a date is a datetime, and
        a size column reading just "8" is a number. CSV gives strings for all
        three. Both have to land in the same place.
        """
        name = self._text(row.get("name"))
        if not name:
            raise RowError("no garment name")

        level = self._text(row.get("school_level")).upper()
        if level not in LEVELS:
            raise RowError(
                f"school_level {level or '(blank)'!r} is not one of {', '.join(sorted(LEVELS))}"
            )

        sizes = [s.strip() for s in self._text(row.get("sizes")).split(";") if s.strip()]
        if not sizes:
            raise RowError("no sizes — a garment with no sizes produces no SKUs")

        price = self._price(row.get("unit_price"))
        active_date = self._date(row.get("active_date"))

        return {
            "name": name,
            "colour": self._text(row.get("colour")),
            "school_level": level,
            "sizes": sizes,
            "unit_price": price,
            "active_date": active_date,
        }

    @staticmethod
    def _text(value):
        """A cell as text, whatever Excel decided it was.

        A size column of `8` comes back as the float 8.0, and "8.0" is not a
        size anybody recognises — so a whole number is rendered without its
        decimal part.
        """
        if value is None:
            return ""
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        if isinstance(value, datetime):
            return value.date().isoformat()
        return str(value).strip()

    def _price(self, value):
        if isinstance(value, (int, float, Decimal)):
            price = Decimal(str(value))
        else:
            raw = self._text(value).replace(",", "")
            try:
                price = Decimal(raw)
            except (InvalidOperation, ValueError):
                raise RowError(
                    f"unit_price {raw or '(blank)'!r} is not a number"
                ) from None

        if price <= 0:
            raise RowError("unit_price must be greater than zero")
        return price

    def _date(self, value):
        """Blank means today. Excel usually gives a real date; CSV gives text."""
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, date):
            return value

        raw = self._text(value)
        if not raw:
            return date.today()
        try:
            return date.fromisoformat(raw)
        except ValueError:
            raise RowError(
                f"active_date {raw!r} is not a date. Use YYYY-MM-DD."
            ) from None

    # -- writing ------------------------------------------------------------

    def _apply(self, parsed, *, dry_run):
        """One transaction for the whole file. See the module docstring."""
        tally = {"garments": 0, "sizes": 0, "skus": 0, "prices": 0, "unchanged": 0}

        try:
            with transaction.atomic():
                for line_no, entry in parsed:
                    try:
                        self._import_garment(entry, tally)
                    except ValidationError as exc:
                        raise CommandError(
                            f"line {line_no} ({entry['name']}): "
                            + "; ".join(
                                f"{field}: {' '.join(messages)}"
                                for field, messages in exc.message_dict.items()
                            )
                            + "\nNothing was imported."
                        ) from None

                if dry_run:
                    # Roll the whole thing back. The counts above are still
                    # true — they say what *would* have happened.
                    raise _DryRun
        except _DryRun:
            pass

        verb = "would create" if dry_run else "created"
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Catalogue import"))
        self.stdout.write(f"  {verb}  {tally['garments']:4} garments")
        self.stdout.write(f"  {verb}  {tally['sizes']:4} sizes")
        self.stdout.write(f"  {verb}  {tally['skus']:4} SKUs")
        self.stdout.write(f"  {verb}  {tally['prices']:4} prices")
        if tally["unchanged"]:
            self.stdout.write(f"  already there: {tally['unchanged']} garments")

        if dry_run:
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING("Dry run — nothing was written. Drop --dry-run to import.")
            )

    def _import_garment(self, entry, tally):
        garment, created = Garment.objects.get_or_create(
            name__iexact=entry["name"],
            school_level=entry["school_level"],
            defaults={
                "name": entry["name"],
                "colour": entry["colour"],
                "school_level": entry["school_level"],
            },
        )
        tally["garments" if created else "unchanged"] += 1

        for position, size_name in enumerate(entry["sizes"], start=1):
            size, size_created = Size.objects.get_or_create(
                name__iexact=size_name,
                defaults={"name": size_name, "sort_order": self._sort_order(size_name, position)},
            )
            if size_created:
                tally["sizes"] += 1

            _, sku_created = Sku.objects.get_or_create(garment=garment, size=size)
            if sku_created:
                tally["skus"] += 1

        # A second price for the same garment on the same day is refused by
        # the database, so an unchanged re-import must not try to add one.
        price, price_created = GarmentPrice.objects.get_or_create(
            garment=garment,
            active_date=entry["active_date"],
            defaults={"unit_price": entry["unit_price"]},
        )
        if price_created:
            tally["prices"] += 1
        elif price.unit_price != entry["unit_price"]:
            price.unit_price = entry["unit_price"]
            price.full_clean()
            price.save(update_fields=["unit_price"])

    def _sort_order(self, size_name, position):
        """Numbers sort by value, letters by the usual clothing order.

        Falls back to the position in the row, so a size nobody anticipated
        still lands where the spreadsheet put it rather than at the front.
        """
        if size_name.isdigit():
            return int(size_name)
        return LETTER_ORDER.get(size_name.upper(), 100 + position)


class _DryRun(Exception):
    """Rolls the transaction back after a successful dry run."""

"""Loading AsOne's product catalogue from a spreadsheet — F05, F06, F08.

The logic behind both ways of running an import: the `import_catalog`
management command, and `POST /api/catalog/import/` for the Garments tab.
Neither owns it, because the rules about what makes a file acceptable must
not depend on which door it came through.

Nothing here prints or returns an HTTP response. It reads rows, says which
ones it cannot read and why, and either writes them all or writes none.

## One row per garment, not per SKU

A garment, its sizes, its SKUs and its price arrive together because they
are not separable: a garment with no sizes has no SKUs, and a SKU with no
price cannot be ordered. Asking for three files that have to agree with each
other invites them not to.

    name,colour,school_level,sizes,unit_price,active_date
    White Shirt,White,BOTH,XS;S;M;L;XL;XXL,25000,2026-01-01

`sizes` is semicolon-separated, in the order they should appear on a price
list. `school_level` is PS, HS or BOTH. `active_date` is the first day the
price applies, and may be left blank for today.

Three optional columns decide the shape of the SKU numbers this produces:
`code`, `colour_code` and `fit_group` compose AsOne's five-part number
(`UNI-NUR-CHS-XS-CHK`). Leave them out and the two-part fallback is used.

## What it guarantees

**Nothing is half-imported.** The whole file is one transaction: a bad row
on line 40 means lines 1-39 are rolled back too. A catalogue missing the
last five garments, with nobody sure which, is worse than no catalogue.

**Every error is reported, not just the first.** Fixing a spreadsheet one
error per run is miserable when there are thirty.

**Running it twice changes nothing.** Garments are matched on name, school
level and fit group, sizes on name, SKUs on the pair. Re-importing a
corrected file updates rather than duplicates.

**It writes through the models**, so every constraint the application
enforces applies here too — no back door that admits data the API refuses.
"""

import csv
import io
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from catalog.models import Garment, GarmentPrice, Size, Sku

#: Without these there is no garment to make.
REQUIRED_COLUMNS = {"name", "school_level", "sizes", "unit_price"}

#: Everything a file may carry, for the message when something is missing.
KNOWN_COLUMNS = (
    "name, description, code, colour, colour_code, school_level, fit_group, "
    "sizes, unit_price, active_date"
)

#: Read off the models so the accepted values cannot drift from them.
FIT_GROUPS = {choice.value for choice in Garment.FitGroup}
LEVELS = {choice for choice, _ in Garment.SchoolLevel.choices}

#: Clothing sizes in the order clothes come in, which is not alphabetical.
LETTER_ORDER = {"XS": 10, "S": 20, "M": 30, "L": 40, "XL": 50, "XXL": 60, "OS": 99}

READABLE_SUFFIXES = {".xlsx", ".xlsm", ".csv"}


class CatalogueFileError(Exception):
    """Something about the file as a whole, before any row is considered —
    the wrong kind of file, no data, a missing column, an unknown sheet."""


class RowError(Exception):
    """One row cannot be read. Carries the reason, not the line number —
    the caller knows which line it handed over."""


class RowsRejected(Exception):
    """One or more rows could not be read, so nothing was imported.

    Carries every failure rather than the first, because fixing a
    spreadsheet one error per run is miserable when there are thirty.
    """

    def __init__(self, errors):
        #: ``[{"line": 4, "message": "no sizes — ..."}, ...]``
        self.errors = errors
        super().__init__(f"{len(errors)} rows could not be read.")


class _DryRun(Exception):
    """Rolls the transaction back after a successful dry run."""


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


#: Column names that mean the same thing, mapped to the one this reads.
#:
#: American spelling first, because AsOne's own workbook uses "Color" and a
#: column spelled a way this did not recognise was not refused — it was
#: silently ignored, so every colour imported blank and nothing said why. A
#: quietly dropped column is worse than a rejected file.
COLUMN_ALIASES = {
    "color": "colour",
    "color_code": "colour_code",
    "garment": "name",
    "garment_name": "name",
    "price": "unit_price",
    "unit_cost": "unit_price",
    "class_band": "fit_group",
    "band": "fit_group",
    "item_code": "code",
    "sku_code": "code",
}


def _key(name):
    """Column names as they are meant, not as they were typed.

    A spreadsheet that has been passed around tends to have "Unit Price" or
    " name " rather than the exact header, so the keys are normalised rather
    than demanded — and a few well-known synonyms are accepted outright.
    """
    key = str(name or "").strip().lower().replace(" ", "_")
    return COLUMN_ALIASES.get(key, key)


def read_rows(source, *, filename, sheet=None):
    """Rows paired with their line number, so an error can name the line.

    ``source`` is a path or an open binary file — an upload never touches
    the disk, which is why the suffix is taken from ``filename`` rather than
    from the object.

    Excel and CSV both end up as dicts keyed by lower-cased column name.
    """
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if suffix in {".xlsx", ".xlsm"}:
        rows = _read_excel(source, sheet)
    elif suffix == ".csv":
        rows = _read_csv(source)
    else:
        raise CatalogueFileError(
            f"Cannot read {suffix or 'a file with no extension'}. "
            "Send the catalogue as .xlsx or .csv."
        )

    if not rows:
        raise CatalogueFileError("That file has no data rows.")

    missing = REQUIRED_COLUMNS - set(rows[0][1])
    if missing:
        raise CatalogueFileError(
            "That file is missing columns: "
            + ", ".join(sorted(missing))
            + f".\nExpected: {KNOWN_COLUMNS}"
        )
    return rows


def _open_text(source):
    """A text handle over a path or an uploaded binary file.

    `utf-8-sig` because Excel writes a byte-order mark at the front of a CSV
    and the first column name would otherwise come back as "﻿name".
    """
    if hasattr(source, "read"):
        data = source.read()
        if isinstance(data, bytes):
            try:
                return io.StringIO(data.decode("utf-8-sig"))
            except UnicodeDecodeError:
                # Excel still writes CSVs in the machine's own code page on
                # a Windows laptop, and a pound sign or an accented name is
                # enough. A traceback told the lead nothing they could act
                # on; this at least names the fix.
                raise CatalogueFileError(
                    "That file is not UTF-8 text, so some characters cannot "
                    "be read. In Excel, Save As \u2192 CSV UTF-8, or send "
                    "the .xlsx instead."
                ) from None
        return io.StringIO(data)
    return source.open(newline="", encoding="utf-8-sig")


def _read_csv(source):
    handle = _open_text(source)
    try:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            return []
        # enumerate from 2: line 1 is the header, and a person counting rows
        # in a spreadsheet counts the header as line 1.
        return [
            (i, {_key(k): v for k, v in row.items()})
            for i, row in enumerate(reader, start=2)
            if any((v or "").strip() for v in row.values())
        ]
    finally:
        handle.close()


def _read_excel(source, sheet_name=None):
    """The first sheet, or the one named.

    `data_only=True` reads what a formula evaluated to rather than the
    formula itself — a price worked out with `=B2*1.1` has to import as the
    number somebody saw, not as text beginning with an equals sign.
    """
    from openpyxl import load_workbook

    try:
        book = load_workbook(source, read_only=True, data_only=True)
    except Exception as exc:
        raise CatalogueFileError(
            f"That file could not be opened as a spreadsheet: {exc}"
        ) from None

    if sheet_name:
        if sheet_name not in book.sheetnames:
            raise CatalogueFileError(
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

    header = [_key(cell) for cell in header_row]

    rows = []
    for i, values in enumerate(rows_iter, start=2):
        if not any(v not in (None, "") for v in values):
            continue  # a blank row left behind by editing
        rows.append(
            (i, {header[j]: values[j] for j in range(min(len(header), len(values)))})
        )

    book.close()
    return rows


# ---------------------------------------------------------------------------
# Parsing one row
# ---------------------------------------------------------------------------


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


def _price(value):
    if isinstance(value, (int, float, Decimal)):
        price = Decimal(str(value))
    else:
        raw = _text(value).replace(",", "")
        try:
            price = Decimal(raw)
        except (InvalidOperation, ValueError):
            raise RowError(f"unit_price {raw or '(blank)'!r} is not a number") from None

    if price <= 0:
        raise RowError("unit_price must be greater than zero")
    return price


def _date(value):
    """Blank means today. Excel usually gives a real date; CSV gives text."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    raw = _text(value)
    if not raw:
        return date.today()
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise RowError(f"active_date {raw!r} is not a date. Use YYYY-MM-DD.") from None


def _max_length(field_name):
    """Read off the model, so a widened column does not leave this behind."""
    return Garment._meta.get_field(field_name).max_length


def _within_length(value, field_name, label):
    """Refuse an over-long cell here, before the database sees it.

    `full_clean()` on the built garment is too late: `get_or_create` sends
    the value in its own lookup first, and Postgres answers `DataError:
    value too long for type character varying(8)` — a message about a column
    for somebody looking at a spreadsheet. Caught here it carries the line
    number and the limit.
    """
    limit = _max_length(field_name)
    if limit is not None and len(value) > limit:
        raise RowError(
            f"{label} {value!r} is too long — {len(value)} characters, "
            f"and the most it can be is {limit}"
        )
    return value


def parse_row(row):
    """One spreadsheet row as the fields a garment is made from.

    Raises RowError, which the caller pairs with a line number.
    """
    name = _text(row.get("name"))
    if not name:
        raise RowError("no garment name")
    _within_length(name, "name", "name")

    level = _text(row.get("school_level")).upper()
    if level not in LEVELS:
        raise RowError(
            f"school_level {level or '(blank)'!r} is not one of "
            f"{', '.join(sorted(LEVELS))}"
        )

    # Optional, and the three that decide whether this garment's SKU numbers
    # come out in AsOne's five-part shape or the two-part fallback. A file
    # without them still imports exactly as before.
    fit_group = _text(row.get("fit_group")).upper()
    if fit_group and fit_group not in FIT_GROUPS:
        raise RowError(
            f"fit_group {fit_group!r} is not one of {', '.join(sorted(FIT_GROUPS))}"
        )

    # Stated together or not at all. One without the other silently produces
    # the two-part fallback, and somebody who filled in a fit group plainly
    # expected the full number — better to say so than to hand back SKUs in
    # the wrong shape.
    colour_code = _text(row.get("colour_code")).upper()
    if bool(fit_group) != bool(colour_code):
        missing = "colour_code" if fit_group else "fit_group"
        raise RowError(
            f"{missing} is needed too — a five-part SKU number uses both the "
            "fit group and the colour code"
        )

    # AsOne's own wording for the garment. Optional: a file without it still
    # imports, and the garment simply carries none.
    description = _text(row.get("description"))
    _within_length(description, "description", "description")

    item_code = _text(row.get("code")).upper()
    if item_code and not item_code.isalnum():
        raise RowError(f"code {item_code!r} must be letters and digits only")
    _within_length(item_code, "code", "code")
    _within_length(colour_code, "colour_code", "colour_code")
    _within_length(_text(row.get("colour")), "colour", "colour")

    sizes = [s.strip() for s in _text(row.get("sizes")).split(";") if s.strip()]
    if not sizes:
        raise RowError("no sizes — a garment with no sizes produces no SKUs")

    return {
        "name": name,
        "description": description,
        "colour": _text(row.get("colour")),
        "colour_code": colour_code,
        "school_level": level,
        "fit_group": fit_group,
        "code": item_code,
        "sizes": sizes,
        "unit_price": _price(row.get("unit_price")),
        "active_date": _date(row.get("active_date")),
    }


def parse_rows(rows):
    """Every row, or every reason one could not be read.

    Raises RowsRejected carrying all of them, so a spreadsheet can be fixed
    in one pass rather than one error per run.
    """
    parsed, errors = [], []
    for line_no, row in rows:
        try:
            parsed.append((line_no, parse_row(row)))
        except RowError as exc:
            errors.append({"line": line_no, "message": str(exc)})

    if errors:
        raise RowsRejected(errors)
    return parsed


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _sort_order(size_name, position):
    """Numbers sort by value, letters by the usual clothing order.

    Falls back to the position in the row, so a size nobody anticipated
    still lands where the spreadsheet put it rather than at the front.
    """
    if size_name.isdigit():
        return int(size_name)
    return LETTER_ORDER.get(size_name.upper(), 100 + position)


def _stored_school_level(entry):
    """The level this row will actually be saved with.

    `Garment.save()` derives the level from the fit group, so looking a
    garment up by the level written in the *file* found nothing on a second
    run and tried to create a duplicate — which the uniqueness constraint
    refused, as an IntegrityError, halfway through an import that had worked
    perfectly the first time. Ask the same question the save will answer.
    """
    if entry["fit_group"]:
        return Garment.PRICE_LIST_BY_FIT_GROUP[Garment.FitGroup(entry["fit_group"])]
    return entry["school_level"]


def _import_garment(entry, tally):
    # Matched on fit group as well, because that is what the uniqueness rule
    # is: AsOne has "Sweater" at five fit groups and they are five garments,
    # not one re-imported five times.
    level = _stored_school_level(entry)

    garment, created = Garment.objects.get_or_create(
        name__iexact=entry["name"],
        school_level=level,
        fit_group=entry["fit_group"],
        defaults={
            "name": entry["name"],
            "description": entry["description"],
            "colour": entry["colour"],
            "colour_code": entry["colour_code"],
            "school_level": level,
            "fit_group": entry["fit_group"],
            # Blank falls through to the derivation in Garment.save().
            "code": entry["code"],
        },
    )

    # finding: an over-long cell used to surface as `DataError: value too
    # long for type character varying(8)` — a database message about a
    # column, for a lead looking at a spreadsheet. The models already know
    # how to say it properly, so ask them.
    if created:
        garment.full_clean()
        garment.save()

    tally["garments" if created else "unchanged"] += 1

    for position, size_name in enumerate(entry["sizes"], start=1):
        size, size_created = Size.objects.get_or_create(
            name__iexact=size_name,
            defaults={
                "name": size_name,
                "sort_order": _sort_order(size_name, position),
            },
        )
        if size_created:
            tally["sizes"] += 1

        _, sku_created = Sku.objects.get_or_create(garment=garment, size=size)
        if sku_created:
            tally["skus"] += 1

    # A second price for the same garment on the same day is refused by the
    # database, so an unchanged re-import must not try to add one.
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


def _constraint_hint(exc):
    """The constraint's name out of a database error, for the message.

    Postgres puts it in the text and nowhere a program can reach, so this is
    a string search — kept to one place, and only ever used to make a
    message more helpful than it would otherwise be.
    """
    text = str(exc)
    for name in ("unique_sku_stem", "unique_garment_per_level_and_fit"):
        if name in text:
            return name.replace("_", " ")
    return "a uniqueness rule"


def apply_rows(parsed, *, dry_run=False):
    """Write every row, or none. Returns the tally.

    A model that refuses a row is reported the same way a malformed one is —
    with its line number — rather than as a traceback. By the time we are
    here the file read cleanly, so a failure means a rule the spreadsheet
    broke rather than a cell somebody mistyped.
    """
    tally = {"garments": 0, "sizes": 0, "skus": 0, "prices": 0, "unchanged": 0}

    try:
        with transaction.atomic():
            for line_no, entry in parsed:
                try:
                    # Its own savepoint. A constraint violation poisons the
                    # transaction, so without this the handler below could
                    # not even read the row it was reporting on.
                    with transaction.atomic():
                        _import_garment(entry, tally)
                except IntegrityError as exc:
                    # Two rows in one file claiming the same SKU stem, most
                    # often. The constraint's own comment promises the
                    # clashing line gets named rather than a traceback.
                    raise RowsRejected(
                        [
                            {
                                "line": line_no,
                                "message": (
                                    f"{entry['name']}: clashes with another "
                                    "garment already imported — check the "
                                    "item code and colour code are not "
                                    f"reused. ({_constraint_hint(exc)})"
                                ),
                            }
                        ]
                    ) from None
                except ValidationError as exc:
                    raise RowsRejected(
                        [
                            {
                                "line": line_no,
                                "message": f"{entry['name']}: "
                                + "; ".join(
                                    f"{field}: {' '.join(messages)}"
                                    for field, messages in exc.message_dict.items()
                                ),
                            }
                        ]
                    ) from None

            if dry_run:
                # Roll the whole thing back. The counts are still true — they
                # say what *would* have happened.
                raise _DryRun
    except _DryRun:
        pass

    return tally


def import_catalogue(source, *, filename, sheet=None, dry_run=False):
    """Read, check and write a catalogue file. Returns the tally.

    Raises `CatalogueFileError` for a file that cannot be read at all, and
    `RowsRejected` — carrying every bad row — for one that can.
    """
    return apply_rows(parse_rows(read_rows(source, filename=filename, sheet=sheet)), dry_run=dry_run)

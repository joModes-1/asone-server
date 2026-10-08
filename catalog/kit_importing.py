"""Loading AsOne's uniform kits and their bills of materials from a sheet.

A sibling of `catalog/importing.py`, which loads the garment catalogue, and
deliberately a separate module: the two read different spreadsheets, and the
only thing they share is how a file is opened. That part is imported rather
than copied.

## Why this exists at all

AsOne's 2027 catalogue has **120 kits** — five class bands, two genders, day
or boarder, six sizes — carrying 828 component lines between them. Typing
them into a form is a hundred and twenty forms, and a kit is the one piece
of master data a school actually orders by. The sheet already exists, in
`assets/templates/2027 Uniform SKU's.xlsm - Kit BOM.csv`; this reads it.

## One row per component, grouped into kits

    Kit Number,Kit Name,Fit Group,Gender,Residence,Size,Component SKU,Qty
    KIT-NUR-B-DAY-XS,"Nursery Boy, Day — XS",Nursery,Boy,Day,XS,UNI-NUR-CHS-XS-CHK,1

Each row names its kit, so the kit's own details repeat down the file — that
is how the sheet is written and reformatting it by hand is one more thing to
get wrong. Rows are grouped by cohort and the repeated details are checked
for agreement rather than taken from whichever row came last.

## The catalogue comes first

A component is matched to an existing SKU by its number. Nothing is created
here: a kit naming a SKU that does not exist is an error that names it and
says to import the catalogue first. Inventing a garment from a kit sheet
would produce an unpriced SKU that nothing can order.

## The kit number in the file is checked, not used

`Kit.save()` composes the number from the cohort — `KIT-NUR-B-DAY-XS`. The
file carries one too, and a disagreement means the sheet and the system
understand the cohort differently, which is worth stopping for. All 120 of
AsOne's agree today; this is the thing that says so if one stops agreeing.

## What it guarantees

The same three promises as the catalogue import, for the same reasons:
**nothing is half-imported** (one transaction), **every error is reported**
(not just the first), and **running it twice changes nothing**.

One difference, and it is deliberate. For a kit named in the file, the
file's components **replace** what the kit holds — a line removed from the
sheet is removed from the kit. A bill of materials is a recipe rather than a
record of something that happened (see `removeKitItem` in the client's
`api/kits.ts`), so a component left behind from an earlier import is simply
wrong. Kits *not* named in the file are untouched.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction

from catalog.importing import (
    CatalogueFileError,
    RowError,
    RowsRejected,
    _DryRun,
    _text,
    read_rows,
)
from catalog.models import Kit, KitItem, Size, Sku
from catalog.services import kit_number

#: Without these there is no kit, and no line to put in it.
REQUIRED_COLUMNS = {"fit_group", "gender", "residence", "size", "component_sku"}

#: Everything a file may carry, for the message when something is missing.
KNOWN_COLUMNS = (
    "kit_number, kit_name, fit_group, gender, residence, size, "
    "component_sku, qty"
)


def _codes(choices, *extra):
    """A lookup from anything somebody might write to the stored code.

    Both the code itself and the label off the model, plus whatever else the
    sheet uses — AsOne's Kit BOM writes the band as "P1–P4" where the model
    calls it "Lower Primary (P1–P4)". Keys are squashed to letters and
    digits so an en dash, a hyphen, a space or a capital makes no
    difference: "P1–P4", "p1-p4" and "P1 P4" are one value.
    """
    table = {}
    for value, label in choices:
        table[_squash(value)] = value
        table[_squash(label)] = value
    for written, value in extra:
        table[_squash(written)] = value
    return table


def _squash(text):
    return "".join(c for c in str(text) if c.isalnum()).upper()


#: The sheet's own spellings, alongside the model's codes and labels.
FIT_GROUPS = _codes(
    Kit.FitGroup.choices,
    ("P1-P4", Kit.FitGroup.P1_P4),
    ("P5-P7", Kit.FitGroup.P5_P7),
    ("S1-S4", Kit.FitGroup.S1_S4),
    ("S5-S6", Kit.FitGroup.S5_S6),
)
GENDERS = _codes(
    Kit.Gender.choices,
    ("Boys", Kit.Gender.BOY),
    ("Girls", Kit.Gender.GIRL),
)
RESIDENCES = _codes(
    Kit.Residence.choices,
    ("Boarding", Kit.Residence.BOARDER),
    ("Day Pupil", Kit.Residence.DAY),
)


# ---------------------------------------------------------------------------
# Parsing one row
# ---------------------------------------------------------------------------


def _choice(value, table, label, field):
    """One of a fixed set, however it was written."""
    written = _text(value)
    code = table.get(_squash(written))
    if code is None:
        # The codes rather than every accepted spelling: a list of twelve
        # synonyms is not a thing anybody reads.
        allowed = ", ".join(sorted({v for v in table.values()}))
        raise RowError(f"{label} {written or '(blank)'!r} is not one of {allowed}")
    return str(code)


def parse_row(row):
    """One spreadsheet row as a kit's cohort plus one component.

    Raises RowError, which the caller pairs with a line number.
    """
    entry = {
        "fit_group": _choice(row.get("fit_group"), FIT_GROUPS, "fit_group", "fit_group"),
        "gender": _choice(row.get("gender"), GENDERS, "gender", "gender"),
        "residence": _choice(
            row.get("residence"), RESIDENCES, "residence", "residence"
        ),
        "size": _text(row.get("size")),
        "sku": _text(row.get("component_sku")),
        "kit_number": _text(row.get("kit_number")),
        # The band as the sheet wrote it — "Nursery", "S5–S6" — kept
        # alongside the code so `_tidy_name` can spot it repeated in the
        # kit's name.
        "band_written": _text(row.get("fit_group")),
        # The sheet's own name for the kit. Optional — a cohort already
        # describes the kit well enough to build one, and a name can be
        # composed from it.
        "name": _text(row.get("kit_name")) or _text(row.get("name")),
        "quantity": _quantity(row.get("qty") if "qty" in row else row.get("quantity")),
    }

    if not entry["size"]:
        raise RowError("no size — a kit is built in one size")
    if not entry["sku"]:
        raise RowError("no component SKU — a row with no component is not a line")

    return entry


def _quantity(value):
    """How many of this SKU the kit holds. Blank means one."""
    raw = _text(value)
    if not raw:
        return 1
    try:
        quantity = int(float(raw))
    except (TypeError, ValueError):
        raise RowError(f"qty {raw!r} is not a whole number") from None
    if quantity < 1:
        raise RowError("qty must be at least 1")
    return quantity


def parse_rows(rows):
    """Every row, or every reason one could not be read."""
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
# Grouping
# ---------------------------------------------------------------------------


def group_kits(parsed):
    """Component rows gathered into the kits they belong to.

    Keyed by cohort rather than by the file's kit number, because the cohort
    is what the system identifies a kit by — `unique_kit_per_cohort_and_size`
    — and it is what the number is composed from. Two rows claiming the same
    cohort under different numbers are therefore one kit with a problem,
    which is reported, rather than two kits.
    """
    kits, errors = {}, []

    for line_no, entry in parsed:
        key = (entry["fit_group"], entry["gender"], entry["residence"], entry["size"])
        kit = kits.get(key)

        if kit is None:
            kits[key] = {
                "line": line_no,
                "cohort": key,
                "kit_number": entry["kit_number"],
                "name": _tidy_name(entry["name"], entry["band_written"]),
                "lines": [(line_no, entry["sku"], entry["quantity"])],
            }
            continue

        # The kit's own details repeat on every one of its rows. Checked for
        # agreement rather than overwritten, because the last row winning is
        # how a typo three hundred lines down renames a kit silently.
        if entry["kit_number"] and entry["kit_number"] != kit["kit_number"]:
            errors.append(
                {
                    "line": line_no,
                    "message": (
                        f"this kit is called {kit['kit_number'] or '(blank)'!r} on "
                        f"line {kit['line']} and {entry['kit_number']!r} here — "
                        "the same cohort cannot be two kits"
                    ),
                }
            )

        if any(sku == entry["sku"] for _, sku, _ in kit["lines"]):
            errors.append(
                {
                    "line": line_no,
                    "message": (
                        f"{entry['sku']} is already a component of this kit — "
                        "one line per SKU, with the quantity on it"
                    ),
                }
            )
            continue

        kit["lines"].append((line_no, entry["sku"], entry["quantity"]))

    if errors:
        raise RowsRejected(errors)
    return list(kits.values())


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _resolve(kits):
    """Sizes and SKUs by name, in two queries rather than two per row.

    Both must already exist. A kit sheet that names an unknown SKU is a kit
    sheet imported before the catalogue it depends on, and saying so is more
    use than creating a garment nobody priced.
    """
    wanted_sizes = {kit["cohort"][3] for kit in kits}
    sizes = {
        size.name.upper(): size
        for size in Size.objects.filter(name__in=wanted_sizes)
    }
    # Case-insensitively, because a size is typed in both sheets.
    for size in Size.objects.all():
        sizes.setdefault(size.name.upper(), size)

    wanted_skus = {sku for kit in kits for _, sku, _ in kit["lines"]}
    skus = {
        sku.number.upper(): sku
        for sku in Sku.objects.select_related("garment").filter(
            number__in=wanted_skus
        )
    }

    # Grouped by what is missing, not listed per row.
    #
    # AsOne's socks are a component of all 120 kits, so one SKU missing from
    # the catalogue produced 120 identical messages — a wall of text saying
    # one thing, with the one thing that varied (the line number) the only
    # part nobody needed. Each distinct problem is reported once, with how
    # many lines it affects and the first of them.
    missing_sizes, missing_skus = {}, {}

    for kit in kits:
        size_name = kit["cohort"][3]
        if size_name.upper() not in sizes:
            missing_sizes.setdefault(size_name, []).append(kit["line"])
        for line_no, number, _ in kit["lines"]:
            if number.upper() not in skus:
                missing_skus.setdefault(number, []).append(line_no)

    errors = [
        {
            "line": min(lines),
            "message": (
                f"there is no size called {name!r}{_on_lines(lines)}. "
                "Sizes come from the garment catalogue — import that first."
            ),
        }
        for name, lines in missing_sizes.items()
    ] + [
        {
            "line": min(lines),
            "message": (
                f"there is no SKU called {number!r}{_on_lines(lines)}. "
                "Components are matched to the catalogue by number — import "
                "the garments first, and check the number is spelled the "
                "same in both sheets."
            ),
        }
        for number, lines in missing_skus.items()
    ]

    if errors:
        # In file order, so the list reads the way the spreadsheet does.
        errors.sort(key=lambda error: error["line"])
        raise RowsRejected(errors, rows_affected=sum(
            len(lines) for lines in (*missing_sizes.values(), *missing_skus.values())
        ))
    return sizes, skus


def _on_lines(lines):
    """" — used on 120 lines, first on line 6", or nothing for a single one.

    Silent when a problem affects one line, because the line number is
    already on the error and saying it twice is noise.
    """
    if len(lines) == 1:
        return ""
    return f" — used on {len(lines)} lines, first on line {min(lines)}"


def _import_kit(kit, sizes, skus, tally):
    fit_group, gender, residence, size_name = kit["cohort"]
    size = sizes[size_name.upper()]

    record, created = Kit.objects.get_or_create(
        fit_group=fit_group,
        gender=gender,
        residence=residence,
        size=size,
        defaults={"name": kit["name"] or _composed_name(kit, size)},
    )

    # The file's number is a check on ours, not a source for it. They agree
    # for all 120 of AsOne's 2027 kits; this is what says so if one stops.
    expected = kit_number(record)
    if kit["kit_number"] and kit["kit_number"].upper() != expected.upper():
        raise RowError(
            f"this kit is numbered {kit['kit_number']!r} in the file, but its "
            f"class band, gender, residence and size compose {expected!r}. "
            "One of the two is wrong about what this kit is."
        )

    if not created and kit["name"] and record.name != kit["name"]:
        record.name = kit["name"]
        record.full_clean()
        record.save()

    tally["kits" if created else "unchanged"] += 1

    wanted = {skus[number.upper()].id: quantity for _, number, quantity in kit["lines"]}

    # Removed first, so the kit never holds more than the file says even
    # momentarily — and see the module docstring on why a line missing from
    # the file is a line removed.
    for item in record.items.all():
        if item.sku_id not in wanted:
            item.delete()
            tally["components_removed"] += 1

    for sku_id, quantity in wanted.items():
        item, item_created = KitItem.objects.get_or_create(
            kit=record, sku_id=sku_id, defaults={"quantity": quantity}
        )
        if item_created:
            item.full_clean()
            tally["components"] += 1
        elif item.quantity != quantity:
            item.quantity = quantity
            item.full_clean()
            item.save(update_fields=["quantity"])
            tally["components_changed"] += 1


def _tidy_name(name, band_written):
    """The kit's name with the class band dropped where it repeats.

    AsOne's sheet names every kit twice over: "Nursery Boy, Day — Nursery —
    XS" says Nursery at the front and again in the middle. All 120 do it,
    and the middle one is the redundant one — so the name runs to 37
    characters and wraps to two lines on a card that already shows the band
    in the kit number (`KIT-NUR-B-DAY-XS`) and again on its cohort line.

    Dropped rather than the whole name rebuilt, because the name is theirs:
    this removes a repetition and invents nothing. Only a *middle* segment
    goes, so a kit genuinely called "Nursery" keeps its name.

    "Nursery Boy, Day — Nursery — XS" -> "Nursery Boy, Day — XS"
    "A-Level Boy, Day — S5–S6 — L"    -> "A-Level Boy, Day — L"
    """
    if not name or not band_written:
        return name

    parts = name.split(" \u2014 ")
    if len(parts) < 3:
        return name

    wanted = _squash(band_written)
    kept = [parts[0]] + [p for p in parts[1:-1] if _squash(p) != wanted] + [parts[-1]]
    return " \u2014 ".join(kept)


def _composed_name(kit, size):
    """A name for a kit whose sheet gave none — "Nursery Boy, Day — XS"."""
    fit_group, gender, residence, _ = kit["cohort"]
    return (
        f"{Kit.FitGroup(fit_group).label} "
        f"{Kit.Gender(gender).label}, "
        f"{Kit.Residence(residence).label} — {size.name}"
    )


def apply_kits(kits, *, dry_run=False):
    """Write every kit, or none. Returns the tally."""
    sizes, skus = _resolve(kits)

    tally = {
        "kits": 0,
        "unchanged": 0,
        "components": 0,
        "components_changed": 0,
        "components_removed": 0,
    }

    try:
        with transaction.atomic():
            for kit in kits:
                try:
                    # Its own savepoint, for the same reason the catalogue
                    # import uses one: a constraint violation poisons the
                    # transaction, and the handler below has to be able to
                    # read the row it is reporting on.
                    with transaction.atomic():
                        _import_kit(kit, sizes, skus, tally)
                except RowError as exc:
                    raise RowsRejected(
                        [{"line": kit["line"], "message": str(exc)}]
                    ) from None
                except ValidationError as exc:
                    raise RowsRejected(
                        [
                            {
                                "line": kit["line"],
                                "message": "; ".join(
                                    f"{field}: {' '.join(messages)}"
                                    for field, messages in exc.message_dict.items()
                                ),
                            }
                        ]
                    ) from None
                except IntegrityError:
                    raise RowsRejected(
                        [
                            {
                                "line": kit["line"],
                                "message": (
                                    "this kit clashes with one already in the "
                                    "system — two kits cannot share a cohort "
                                    "and size, or a kit number."
                                ),
                            }
                        ]
                    ) from None

            if dry_run:
                # Rolled back. The counts still say what would have happened.
                raise _DryRun
    except _DryRun:
        pass

    return tally


def import_kits(source, *, filename, sheet=None, dry_run=False):
    """Read, check and write a kit bill of materials. Returns the tally.

    Raises `CatalogueFileError` for a file that cannot be read at all, and
    `RowsRejected` — carrying every bad row — for one that can.
    """
    rows = read_rows(
        source,
        filename=filename,
        sheet=sheet,
        required=REQUIRED_COLUMNS,
        known=KNOWN_COLUMNS,
    )
    return apply_kits(group_kits(parse_rows(rows)), dry_run=dry_run)


__all__ = [
    "CatalogueFileError",
    "RowsRejected",
    "import_kits",
    "KNOWN_COLUMNS",
]

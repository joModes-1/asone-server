"""Business logic for master data.

Thin views, fat services. Everything here is callable from the API, the
admin, a management command or a test without going near HTTP.

Pricing is the substance of this module. Two rules drive all of it:

    A price applies over a period, not forever.
    A garment has at most one price on any given day.

The second is guaranteed by a database constraint, not by these functions —
see catalog/models/pricing.py. That is what lets `price_for()` return a single
value instead of a list and a judgement call.
"""

from datetime import date
from decimal import Decimal

from django.db import connection
from django.db.models import OuterRef, Q, Subquery

from .models import Garment, GarmentPrice


class PriceNotSet(Exception):
    """No price covers the requested date.

    Raised rather than returning None or zero. A garment with no price is a
    master-data gap someone must fix, and quietly costing it at nothing would
    put a free uniform on an invoice.
    """

    def __init__(self, garment, on_date):
        self.garment = garment
        self.on_date = on_date
        super().__init__(f"{garment} has no price effective on {on_date:%Y-%m-%d}.")


# ---------------------------------------------------------------------------
# Reading a price
# ---------------------------------------------------------------------------


def effective_on(queryset, on_date):
    """Narrow a GarmentPrice queryset to rows in force on ``on_date``.

    The boundary matches the exclusion constraint: the active date counts,
    the expiration date does not.
    """
    return queryset.filter(
        Q(active_date__lte=on_date)
        & (Q(expiration_date__isnull=True) | Q(expiration_date__gt=on_date))
    )


#: Name of the annotation the helpers below attach. Deliberately not
#: `unit_price`: Sku has a property of that name, and a property is a data
#: descriptor that an annotation cannot overwrite.
CURRENT_PRICE_ANNOTATION = "current_price_amount"


def _price_subquery(on_date, garment_field="pk"):
    """A correlated subquery yielding the price in force on ``on_date``.

    Limited to one row, which the exclusion constraint already guarantees —
    the slice is there to satisfy the SQL, not to choose between candidates.
    """
    return Subquery(
        effective_on(
            GarmentPrice.objects.filter(garment=OuterRef(garment_field)), on_date
        ).values("unit_price")[:1]
    )


def with_current_price(queryset, on_date=None, garment_field="pk"):
    """Annotate a Garment or Sku queryset with its price on ``on_date``.

    Without this, serialising a list calls `price_for()` once per row: 45
    garments become 46 queries, and 200 SKUs become 201. One subquery does
    the same work in a single round trip.

    Pass ``garment_field="garment_id"`` for a Sku queryset — a SKU's price is
    its garment's price.
    """
    return queryset.annotate(
        **{CURRENT_PRICE_ANNOTATION: _price_subquery(on_date or date.today(), garment_field)}
    )


def price_for(garment, on_date=None):
    """The unit price of ``garment`` on ``on_date`` (default today).

    Returns a Decimal. Raises PriceNotSet if nothing covers that date.

    Safe to call with `.get()` because the database guarantees at most one
    price per garment per day.
    """
    on_date = on_date or date.today()

    try:
        return effective_on(garment.prices, on_date).get().unit_price
    except GarmentPrice.DoesNotExist:
        raise PriceNotSet(garment, on_date) from None


def price_list(school_level, on_date=None):
    """The price list for Primary or High School on ``on_date``.

    Returns ``[{"garment": Garment, "unit_price": Decimal}, ...]``, ordered by
    garment name. Garments carrying BOTH appear on each list.

    Garments with no price on that date are **omitted rather than shown at
    zero**. A price list is a document a school orders from; a line with no
    price on it is worse than a line that is not there. Use
    `garments_without_a_price()` to find them before publishing.

    Resolved in two queries regardless of how many garments there are.
    """
    on_date = on_date or date.today()

    garments = Garment.objects.filter(
        is_active=True,
        school_level__in=[school_level, Garment.SchoolLevel.BOTH],
    ).order_by("name")

    prices = {
        price.garment_id: price.unit_price
        for price in effective_on(
            GarmentPrice.objects.filter(garment__in=garments), on_date
        )
    }

    return [
        {"garment": garment, "unit_price": prices[garment.pk]}
        for garment in garments
        if garment.pk in prices
    ]


def kit_price_list(school_level, on_date=None):
    """The **kit** half of a price list — F15 and F51.

    AsOne's checklist asks for printable price lists "at SKU and Uniform Kit
    level". `price_list()` above is the garment half; this is the other, and
    a school ordering a starter kit needs it more than the component list —
    the kit is what they actually buy.

    Returns ``[{"kit": Kit, "unit_price": Decimal, "item_count": int}, ...]``,
    ordered by name.

    **A kit that cannot be priced is omitted**, exactly as an unpriced garment
    is, and for the same reason: a line with no price on a document a school
    buys from is worse than no line. A kit is unpriceable when any component
    has no price on the date, or when it has no components at all — see
    `kit_prices()`, which refuses to sum around a missing price rather than
    quietly returning a total short by one component.

    Which means a *priced* garment can still leave a kit off this list, and
    nothing about the kit itself will look wrong. `kits_without_a_price()`
    below is how that is found before publishing.

    Unlike garments there is no BOTH: `Kit.SchoolLevel` has two values, so a
    kit appears on exactly one list.
    """
    from .models import Kit

    on_date = on_date or date.today()

    kits = list(Kit.objects.filter(is_active=True, school_level=school_level).order_by("name"))
    if not kits:
        return []

    totals = kit_prices(kits, on_date)
    counts = {
        kit.pk: sum(item.quantity for item in kit.items.all())
        for kit in Kit.objects.filter(pk__in=[k.pk for k in kits]).prefetch_related("items")
    }

    return [
        {"kit": kit, "unit_price": totals[kit.pk], "item_count": counts.get(kit.pk, 0)}
        for kit in kits
        if totals.get(kit.pk) is not None
    ]


def kits_without_a_price(on_date=None, school_level=None):
    """Active kits that cannot be priced on ``on_date`` — the gap report.

    The kit-level twin of `garments_without_a_price()`. Run it before
    publishing a price list, or a kit silently disappears from what the
    schools can order.

    The cause is usually not the kit: one component garment has no price, and
    the kit inherits that. So each row says which components are the problem,
    because "PS Starter Kit is unpriced" sends somebody looking at the kit
    when the fix is on a garment.
    """
    from .models import Kit

    on_date = on_date or date.today()

    kits = Kit.objects.filter(is_active=True)
    if school_level:
        kits = kits.filter(school_level=school_level)
    kits = list(kits.prefetch_related("items__sku__garment").order_by("name"))
    if not kits:
        return []

    totals = kit_prices(kits, on_date)

    rows = []
    for kit in kits:
        if totals.get(kit.pk) is not None:
            continue

        priced = {
            garment.pk
            for garment in with_current_price(
                Garment.objects.filter(
                    pk__in=[item.sku.garment_id for item in kit.items.all()]
                ),
                on_date,
            )
            if getattr(garment, CURRENT_PRICE_ANNOTATION) is not None
        }
        missing = sorted(
            {
                item.sku.garment.name
                for item in kit.items.all()
                if item.sku.garment_id not in priced
            }
        )

        rows.append(
            {
                "kit": kit,
                # Empty when the kit simply has no components — a different
                # problem with the same symptom, and worth telling apart.
                "unpriced_components": missing,
                "has_no_items": not kit.items.all(),
            }
        )

    return rows


def garments_without_a_price(on_date=None, school_level=None):
    """Active garments with no price on ``on_date``.

    The gap report behind a price list. Run it before publishing one, or a
    garment silently disappears from what the schools can order.
    """
    on_date = on_date or date.today()

    garments = Garment.objects.filter(is_active=True)
    if school_level:
        garments = garments.filter(
            school_level__in=[school_level, Garment.SchoolLevel.BOTH]
        )

    priced = effective_on(GarmentPrice.objects.all(), on_date).values_list(
        "garment_id", flat=True
    )
    return garments.exclude(pk__in=priced).order_by("name")


# ---------------------------------------------------------------------------
# Changing a price
# ---------------------------------------------------------------------------


def reprice(garment, unit_price, active_from, *, closed_by=None):
    """Give ``garment`` a new price from ``active_from``.

    Closes whichever price is currently open-ended by expiring it on that
    date, then opens a new one. This is the only sanctioned way to change a
    price: editing a GarmentPrice row in place would rewrite history, and an
    invoice reprinted next term would no longer match the original.

    Returns the new GarmentPrice.

    Callers must wrap this in a transaction — it is two writes, and a failure
    between them would leave the garment unpriced from ``active_from``.
    """
    open_ended = garment.prices.filter(
        expiration_date__isnull=True, active_date__lt=active_from
    )
    open_ended.update(expiration_date=active_from)

    price = GarmentPrice(
        garment=garment, unit_price=unit_price, active_date=active_from
    )
    # Runs the check and exclusion constraints, so an overlap surfaces as a
    # ValidationError here rather than an IntegrityError three frames later.
    price.full_clean()
    price.save()

    return price


# ---------------------------------------------------------------------------
# SKU control numbers
# ---------------------------------------------------------------------------

#: Created by migration 0003. Starts at 100001, so every SKU number is six
#: digits — matching the example in AsOne's own definitions ("123456 = White
#: Shirt size 10").
SKU_NUMBER_SEQUENCE = "catalog_sku_number_seq"


def next_sku_number() -> str:
    """Draw the next SKU control number.

    A Postgres sequence rather than `max(number) + 1` for two reasons AsOne
    cares about:

      * **Never reused.** A sequence never goes backwards, so retiring a SKU
        does not free its number for something else. The number on a packing
        list printed in 2027 still means the same product in 2035.
      * **Safe under concurrency.** `nextval` is atomic. Two people creating
        SKUs at the same instant get different numbers; a max-plus-one would
        hand them both the same one.

    Sequences are also exempt from transaction rollback — a rolled-back
    creation burns a number rather than reusing it. That is the correct
    trade: gaps in the numbering are harmless, collisions are not.
    """
    with connection.cursor() as cursor:
        cursor.execute("SELECT nextval(%s)", [SKU_NUMBER_SEQUENCE])
        return str(cursor.fetchone()[0])


# ---------------------------------------------------------------------------
# Readable codes
# ---------------------------------------------------------------------------

#: How long a garment's stem may be. Three letters keeps the whole SKU code
#: short enough to sit in a table column and be read off a shelf label.
GARMENT_CODE_LENGTH = 3


def _stem(name: str) -> str:
    """The letters a garment code is built from.

    One initial per word, padded from the last word until it reaches three:

        "Blue Tunic"     -> B, T   -> BTU
        "Grey Trousers"  -> G, T   -> GTR
        "Jumper"         -> J      -> JUM

    Digits and punctuation are dropped, so "E2E Tunic" gives ETU rather than
    E2T — a code is read aloud across a warehouse, and letters survive that
    better than a name's own punctuation does.
    """
    words = [
        "".join(character for character in word if character.isalpha())
        for word in name.split()
    ]
    words = [word for word in words if word]
    if not words:
        return "SKU"

    stem = "".join(word[0] for word in words)
    # Short of three, borrow the rest from the last word: "BT" + "unic".
    tail = words[-1][1:]
    while len(stem) < GARMENT_CODE_LENGTH and tail:
        stem, tail = stem + tail[0], tail[1:]

    return stem[:GARMENT_CODE_LENGTH].upper().ljust(GARMENT_CODE_LENGTH, "X")


def garment_code(garment, *, taken=None) -> str:
    """A short, unique, permanent code for a garment.

    Two garments can legitimately produce the same stem — "White Shirt" on
    the Primary list and "White Shirt" on the High School list are two
    garments, and "Grey Shorts" and "Grey Skirt" are two more. The first to
    be created keeps the bare stem and the next takes a digit: GSH, GSH2,
    GSH3. Deterministic, and short enough to stay readable.

    `taken` lets a migration pass the codes it has assigned so far without
    each one needing its own query.
    """
    from catalog.models import Garment

    stem = _stem(garment.name)

    if taken is None:
        # Scoped to the garments this one could actually collide with: the
        # same fit group and colour code. A SKU number carries all three,
        # so "Sweater" at P1–P4 and "Sweater" at P5–P7 may both be SWT and
        # a global search would have handed the second one SWT2 for no
        # reason — and made its SKU numbers disagree with AsOne's.
        taken = set(
            Garment.objects.exclude(pk=garment.pk)
            .exclude(code="")
            .filter(
                fit_group=garment.fit_group,
                colour_code=garment.colour_code,
            )
            .values_list("code", flat=True)
        )

    if stem not in taken:
        return stem

    suffix = 2
    while f"{stem}{suffix}" in taken:
        suffix += 1
    return f"{stem}{suffix}"


#: The first segment of every SKU number.
#:
#: Constant, because AsOne's 2027 workbook uses "UNI" on all 308 of theirs —
#: including the sport and casual items, whose category it does not encode.
#: Named here rather than inlined so that if they ever do want SPT- and CAS-
#: prefixes, there is one place that decides.
SKU_PREFIX = "UNI"


def sku_code(garment, size) -> str:
    """The code printed on shelf labels, pick lists and packing lists.

    Five parts, which is the shape AsOne gave us::

        UNI - NUR - CHS - XS - CHK
         |     |     |     |     colour code      (garment)
         |     |     |     size                   (size)
         |     |     item code                    (garment)
         |     fit group                          (garment)
         constant

    Everything except the item code is a lookup or an attribute already on
    the row, which is what makes the number generated rather than typed.
    The item code is the one human choice — a third of AsOne's cannot be
    derived from the garment's name (SWT for Sweater, TIE for Necktie) — and
    it is chosen once per garment, not once per SKU. Twenty-seven item codes
    produce their three hundred SKUs.

    ## Garments that predate the fit group and colour code

    They fall back to the two-part `GTR-L`, which is what every existing SKU
    already carries. Mixing two shapes is not lovely, but the alternative is
    worse: SKU numbers are frozen once assigned, because they are printed on
    shelf labels and pick lists, so there is no version of this where old
    rows quietly become five-part. Fill in a garment's fit group and colour
    code and its *new* SKUs take the full shape.

    Non-alphanumerics are stripped from the size, so a size named "XL-TALL"
    does not put an extra hyphen in the code and make it look like six
    parts instead of five.
    """
    size_token = "".join(
        character for character in size.name if character.isalnum()
    ).upper()

    if garment.fit_group and garment.colour_code:
        return "-".join(
            (
                SKU_PREFIX,
                garment.fit_group,
                garment.code,
                size_token,
                garment.colour_code.upper(),
            )
        )

    return f"{garment.code}-{size_token}"


def price_for_sku(sku, on_date=None):
    """The price of a SKU, which is the price of its garment.

    Exists so callers do not have to know that price hangs off the garment.
    Raises PriceNotSet if the garment is unpriced on that date.
    """
    return price_for(sku.garment, on_date)


# ---------------------------------------------------------------------------
# Kit pricing
# ---------------------------------------------------------------------------


class EmptyKit(Exception):
    """A kit has no component SKUs, so it cannot be priced.

    Raised rather than treating an empty kit as free. A kit with no line
    items yet is far more likely to be a bill of materials nobody finished
    setting up than a genuine zero-cost bundle, and pricing it at 0 would
    hide that gap instead of surfacing it — the same reasoning PriceNotSet
    already applies to a single unpriced garment.
    """

    def __init__(self, kit):
        self.kit = kit
        super().__init__(f"{kit} has no component SKUs and cannot be priced.")


def compute_kit_price(kit, on_date=None):
    """The kit's price on ``on_date`` (default today).

    Always the live sum of what each component SKU costs on that date,
    multiplied by how many of it the kit contains — never a stored figure.
    See the Kit model's docstring for why: a stored total would go stale the
    moment any component's garment was repriced, silently, with nobody the
    wiser until an invoice was wrong.

    Dated rather than "current price only", for the same reason price_for()
    takes a date: the system must be able to answer "what did this kit cost
    as of a given date", because an invoice raised in March must still cost
    out at March's prices if it is reprinted in September.

    Raises PriceNotSet, propagated unchanged from price_for_sku(), the
    moment any single component has no price covering ``on_date``. A kit
    missing one component's price refuses to price itself entirely, rather
    than quietly returning a total that is short by that component's value.

    Raises EmptyKit if the kit has no component SKUs at all.
    """
    on_date = on_date or date.today()

    items = list(kit.items.select_related("sku__garment"))
    if not items:
        raise EmptyKit(kit)

    total = Decimal("0.00")
    for item in items:
        total += price_for_sku(item.sku, on_date) * item.quantity
    return total


def kit_prices(kits, on_date=None) -> dict:
    """Price a whole list of kits in one query. Returns ``{kit_id: total}``.

    `compute_kit_price()` is correct but costs one query per component, so a
    list of kits costs kits x components. This does the same arithmetic from
    a single annotated pass over their line items.

    **Deliberately not a database SUM.** `Sum()` skips NULLs, so a kit with
    one unpriced component would come back with a total quietly short by that
    component's value — exactly the failure the whole design exists to
    prevent. Summed in Python instead, where a missing price makes the whole
    kit unpriceable.

    A kit that cannot be priced maps to ``None``: either a component has no
    price on that date, or the kit has no components at all. Callers that
    need to know *which* should use `compute_kit_price()`, which says so.
    """
    from decimal import Decimal

    from .models import KitItem

    kits = list(kits)
    if not kits:
        return {}

    on_date = on_date or date.today()
    items = with_current_price(
        KitItem.objects.filter(kit_id__in=[kit.pk for kit in kits]),
        on_date,
        garment_field="sku__garment_id",
    )

    totals, unpriceable = {}, set()
    for item in items:
        amount = getattr(item, CURRENT_PRICE_ANNOTATION)
        if amount is None:
            unpriceable.add(item.kit_id)
            continue
        totals[item.kit_id] = totals.get(item.kit_id, Decimal("0.00")) + amount * item.quantity

    return {
        kit.pk: None if kit.pk in unpriceable else totals.get(kit.pk)
        for kit in kits
    }

"""Fulfilment reports — F49, F52, F54, F57.

Read-only views over documents that already exist. Nothing here writes.

## The one interpretation worth stating

Three of these turn on the phrase "packing list", which AsOne uses on p.8
and in the checklist:

    F52  "Pick lists generated but packing lists not yet created"
    F54  "School orders with a pick list but no packing list"

A packing list is the document that travels with the goods (F40), so it
comes into existence when a shipment does. That makes both of those the same
underlying question in this data model:

    an order that has been **picked** and has **no shipment yet**

Stated here rather than buried, because if AsOne means something else by
"packing list" — a separate step between picking and despatch — these two
reports change and F40 changes with them.

F52 and F54 are also very nearly the same report. They differ only in
audience: the checklist gives F54 to school staff for their own schools and
F52 not at all. Kept separate for that reason alone, and sharing one query
so they cannot drift apart.
"""

from django.db.models import (
    Case,
    Q,
    Count,
    DecimalField,
    F,
    IntegerField,
    Sum,
    Value,
    When,
)
from django.db.models.functions import Coalesce

from .models import Backorder, Shipment
from .models.backorders import BackorderStatus
from .models.school_orders import (
    OrderPriority,
    OrderStatus,
    SchoolOrder,
    SchoolOrderLine,
)

MONEY = DecimalField(max_digits=18, decimal_places=2)


def _within(queryset, field, date_from=None, date_to=None):
    """Narrow to a period, both ends inclusive."""
    if date_from:
        queryset = queryset.filter(**{f"{field}__gte": date_from})
    if date_to:
        queryset = queryset.filter(**{f"{field}__lte": date_to})
    return queryset


# ---------------------------------------------------------------------------
# F49 — Backorders outstanding
# ---------------------------------------------------------------------------


def outstanding_backorders(warehouse=None, school=None):
    """What schools are still owed, and what each one is waiting on — F49.

    "What they are waiting on" is the status: OPEN means nobody has taken it
    on yet, ASSIGNED means a warehouse with stock has and has not shipped.
    Those are different problems — an OPEN backorder needs somebody to find
    stock; an ASSIGNED one needs somebody to put it on a van.

    Filled and cancelled backorders are excluded: they are not outstanding.

    ``warehouse`` narrows to backorders **raised by** that warehouse, which
    is what its own staff are chasing.
    """
    queryset = Backorder.objects.filter(
        status__in=(BackorderStatus.OPEN, BackorderStatus.ASSIGNED)
    ).select_related(
        "order",
        "order__school",
        "order__school__primary_warehouse",
        "sku",
        "sku__garment",
        "filled_by_warehouse",
    )

    if warehouse is not None:
        queryset = queryset.filter(order__school__primary_warehouse=warehouse)
    if school is not None:
        queryset = queryset.filter(order__school=school)

    return queryset


# ---------------------------------------------------------------------------
# F52 and F54 — picked, but nothing has left
# ---------------------------------------------------------------------------


def _responsible_for(queryset, warehouse):
    """Narrow to orders ``warehouse`` actually has to fill.

    Not ``school__primary_warehouse``. After an F45 transfer the two are
    different warehouses, and filtering on the school's own left a
    transferred order on the sending warehouse's backlog — a job they can no
    longer do, since the stock is not theirs — while keeping it off the
    receiving warehouse's, where somebody was waiting to pick it.

    The model expresses this as the `warehouse` property, which a queryset
    cannot filter on, so the same rule is written out here: whoever it was
    transferred to, or the school's own where it was never transferred.
    """
    return queryset.filter(
        Q(fulfilled_by_warehouse=warehouse)
        | Q(fulfilled_by_warehouse__isnull=True, school__primary_warehouse=warehouse)
    )


def picking_queue(warehouse=None):
    """Orders waiting for the warehouse to pull them off the shelves — F38.

    The picking screen's backlog. Three buckets, and the middle one of the
    design's is missing on purpose:

        Ready       released, paid for, nothing picked yet
        Picked      off the shelf and reserved, waiting for a van
        (In progress)  does not exist — `pick_order` is atomic. An order is
                    picked or it is not; there is no half-picked state,
                    because a partial reservation would let the ledger say
                    stock is committed to an order nobody finished.

    Whether picking *should* be resumable is open question Q4 ("can an order
    be part-shipped?"), which AsOne has not answered. Until they do, a
    half-picked state would be a status nothing can reach.
    """
    queryset = (
        SchoolOrder.objects.filter(
            status__in=(OrderStatus.RELEASED, OrderStatus.PICKED)
        )
        .select_related(
            "school", "school__primary_warehouse", "fulfilled_by_warehouse", "created_by"
        )
        .prefetch_related("lines__sku")
    )

    if warehouse is not None:
        queryset = _responsible_for(queryset, warehouse)

    # Most urgent first, then oldest — a warehouse works the top of this list
    # down, and the priority is the hint it sets for itself.
    return queryset.annotate(
        urgency=Case(
            When(priority=OrderPriority.URGENT, then=Value(0)),
            When(priority=OrderPriority.HIGH, then=Value(1)),
            default=Value(2),
            output_field=IntegerField(),
        )
    ).order_by("urgency", "order_date", "number")


def picking_summary(warehouse=None, today=None):
    """The three tiles above the backlog.

    `completed_today` counts orders **picked** today, not shipped: the
    warehouse is measuring its own day's work off the shelves, and a van may
    not go until Friday.
    """
    from django.utils import timezone

    today = today or timezone.localdate()
    queue = picking_queue(warehouse)

    return {
        "ready_to_pick": queue.filter(status=OrderStatus.RELEASED).count(),
        "picked": queue.filter(status=OrderStatus.PICKED).count(),
        # Picking does not stamp a date of its own, so this reads the ledger
        # rows picking writes — the only record of when it happened.
        "completed_today": _picked_today(warehouse, today),
    }


def _picked_today(warehouse, today):
    from inventory.models import MovementType, StockMovement

    rows = StockMovement.objects.filter(
        movement_type=MovementType.PICK, occurred_on=today
    )
    if warehouse is not None:
        rows = rows.filter(warehouse=warehouse)

    return rows.values("document_number").distinct().count()


def part_processed_orders(warehouse=None, school=None):
    """Orders picked but not yet despatched — F52, and F54.

    One query behind two checklist items. See the module docstring: they ask
    the same question of the data and differ only in who may read the
    answer.

    "Picked but no shipment" is the gap a warehouse manager cares about:
    stock is off the shelf, committed to a named student, and still in the
    building. It is also where stock quietly sits if somebody picks an order
    and forgets it.
    """
    queryset = (
        SchoolOrder.objects.filter(status=OrderStatus.PICKED)
        # F42: an order reaches a van through its lines now, so "not yet
        # despatched" is "no shipment line points at it".
        .filter(shipment_lines__isnull=True)
        .select_related(
            "school", "school__primary_warehouse", "fulfilled_by_warehouse", "created_by"
        )
    )

    if warehouse is not None:
        queryset = _responsible_for(queryset, warehouse)
    if school is not None:
        queryset = queryset.filter(school=school)

    return queryset


# ---------------------------------------------------------------------------
# F57 — Shipments to schools, costed
# ---------------------------------------------------------------------------


def shipments_costed(date_from=None, date_to=None, school=None, warehouse=None):
    """What went to each school and what it was worth — F57.

    Valued from the **order lines**, not from the ledger: this is a report
    about what a school received against what it was invoiced, so the
    figure that matters is the price the school was charged, snapshotted
    when the order was placed.

    That is deliberately a different number from the costed adjustments
    report, which values stock at what the warehouse carries it at. A
    shipment's value to Finance is what the school owes; a write-off's
    value is what the stock cost. Conflating them would misstate both.

    One row per school. Grouped in the database.
    """
    from django.db.models import OuterRef, Subquery

    from .models import ShipmentLine

    lines = ShipmentLine.objects.select_related("shipment")
    lines = _within(lines, "shipment__shipped_on", date_from, date_to)
    if school is not None:
        lines = lines.filter(shipment__school=school)
    if warehouse is not None:
        lines = lines.filter(shipment__from_warehouse=warehouse)

    # The price the school was charged for this SKU on this order. A
    # subquery, not a join: an order can carry the same SKU on two lines —
    # once inside a kit, once loose — and joining would multiply every
    # shipment line by however many order lines matched it.
    charged = Subquery(
        SchoolOrderLine.objects.filter(
            order=OuterRef("order"), sku=OuterRef("sku")
        ).values("unit_price")[:1],
        output_field=MONEY,
    )

    return (
        lines.annotate(unit_price=charged)
        .values("shipment__school_id", "shipment__school__name")
        .annotate(
            shipments=Count("shipment_id", distinct=True),
            units=Coalesce(Sum("quantity"), Value(0)),
            value=Coalesce(
                Sum(F("quantity") * F("unit_price"), output_field=MONEY),
                Value(0, output_field=MONEY),
                output_field=MONEY,
            ),
        )
        .order_by("shipment__school__name")
    )

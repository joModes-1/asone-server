"""The numbers behind the dashboard — F62.

Read-only. Nothing here writes, and nothing here owns data: every figure is
recomputed from the app that does own it, so a tile can never disagree with
the screen it links to.

## The one rule worth stating

**Every figure is scoped to one warehouse.** The design has a warehouse
selector at the top and a heading naming the site, so "16,482 items" is
always "at Namayemba", never "in the country". Passing `warehouse=None`
returns the all-sites figure, which is what the two leads and Finance see.

Row-level scoping by role is the view's job, through
`scope_to_user_site()`. This layer takes a warehouse and answers for it.
"""

from datetime import date, timedelta
from decimal import Decimal

from django.db.models import Count, DecimalField, F, IntegerField, Sum, Value
from django.db.models.functions import Coalesce
from django.utils import timezone

from accounts.models import RegistrationRequest
from accounts.permissions import ALL_SITE_ROLES, has_role
from catalog.models import Sku
from inventory.models import MovementType, StockMovement, StockStatus
from inventory.services import below_minimum, stock_levels
from orders.models import Backorder, Shipment
from orders.models.backorders import BackorderStatus
from orders.models.school_orders import OrderStatus, SchoolOrder, SchoolOrderLine
from procurement.models import ProductionOrder, Receipt

MONEY = DecimalField(max_digits=18, decimal_places=2)

#: Statuses where an order is still waiting for the warehouse to act.
#: Deliberately not just RELEASED: picking is not gated on payment yet — see
#: `REQUIRE_RELEASE_BEFORE_PICK` — so an order on Hold is still work sitting
#: in the queue, and a warehouse tile that hid it would understate the day.
AWAITING_PICK = (OrderStatus.HOLD, OrderStatus.RELEASED)


def _orders_for(warehouse=None):
    """Orders belonging to a warehouse, through the school that ordered."""
    orders = SchoolOrder.objects.all()
    if warehouse is not None:
        orders = orders.filter(school__primary_warehouse=warehouse)
    return orders


def available_stock(warehouse=None):
    """Units on hand and what they are worth — the first and last tiles.

    Both come out of one pass over `stock_levels()` rather than two queries,
    because they are the same rows read twice.
    """
    units = 0
    value = Decimal("0.00")

    for row in stock_levels(warehouse=warehouse):
        units += row["level"]
        value += row["value"]

    return {"units": units, "value": value}


def units_awaiting_pick(warehouse=None):
    """Units on orders the warehouse has not picked yet — "items to pick".

    Counted from order lines rather than from orders, because the tile is a
    count of garments to take off shelves, not of documents.
    """
    lines = SchoolOrderLine.objects.filter(order__status__in=AWAITING_PICK)
    if warehouse is not None:
        lines = lines.filter(order__school__primary_warehouse=warehouse)

    return lines.aggregate(
        units=Coalesce(Sum("quantity"), Value(0), output_field=IntegerField())
    )["units"]


def orders_awaiting_dispatch(warehouse=None):
    """Orders picked but not yet shipped — "pending shipments".

    The same question `part_processed_orders()` answers for F52/F54, counted
    here rather than listed. Stock is off the shelf, committed to a named
    student, and still in the building.
    """
    return (
        _orders_for(warehouse)
        # F42: an order reaches a van through its lines now, so "not yet
        # despatched" is "no shipment line points at it".
        .filter(status=OrderStatus.PICKED, shipment_lines__isnull=True)
        .count()
    )


def units_shipped_today(warehouse=None):
    """Garments that left the building today.

    Counted from the shipment lines rather than the ledger: a van is the
    document, and "what went out today" is a question about vans. Summed
    over lines so a consolidated despatch carrying four orders counts its
    garments once each, not its orders.
    """
    from django.db.models import Sum
    from django.utils import timezone

    from orders.models import ShipmentLine

    lines = ShipmentLine.objects.filter(shipment__shipped_on=timezone.localdate())
    if warehouse is not None:
        lines = lines.filter(shipment__from_warehouse=warehouse)

    return lines.aggregate(total=Sum("quantity"))["total"] or 0


def outstanding_backorders(warehouse=None):
    """Backorders nobody has filled yet — open or assigned but not shipped."""
    backorders = Backorder.objects.filter(
        status__in=(BackorderStatus.OPEN, BackorderStatus.ASSIGNED)
    )
    if warehouse is not None:
        backorders = backorders.filter(order__school__primary_warehouse=warehouse)
    return backorders.count()


def skus_below_minimum(warehouse=None):
    """How many SKUs are at or under their reorder floor — "low stock"."""
    return len(below_minimum(warehouse=warehouse))


def summary(warehouse=None):
    """Everything the six tiles need, in one call.

    One call rather than six, because the dashboard is the first screen
    anybody sees and six round trips over a rural connection is the
    difference between a screen that loads and one that does not.
    """
    stock = available_stock(warehouse)

    return {
        "available_units": stock["units"],
        "inventory_value": stock["value"],
        "units_awaiting_pick": units_awaiting_pick(warehouse),
        "orders_awaiting_dispatch": orders_awaiting_dispatch(warehouse),
        "outstanding_backorders": outstanding_backorders(warehouse),
        "skus_below_minimum": skus_below_minimum(warehouse),
        "units_shipped_today": units_shipped_today(warehouse),
    }


# ---------------------------------------------------------------------------
# Needs attention
# ---------------------------------------------------------------------------

#: How urgent an alert is. The design shows these as coloured chips, so the
#: wording is the client's rather than ours — "CRITICAL", "HOLD",
#: "INSPECTION", "READY".
CRITICAL = "CRITICAL"
HOLD = "HOLD"
INSPECTION = "INSPECTION"
READY = "READY"


def receipts_needing_reconciliation(warehouse=None):
    """Deliveries that did not match their packing list and are still open.

    "Still open" means unposted. A posted receipt has been accepted into
    stock and its discrepancy is history; an unposted one with a discrepancy
    is somebody standing at a shelf with a clipboard, and that is the thing
    the dashboard should be nagging about.

    Counted per receipt, not per line: three wrong lines on one delivery is
    one conversation with one tailoring center.
    """
    receipts = Receipt.objects.filter(posted_at__isnull=True)
    if warehouse is not None:
        receipts = receipts.filter(production_order__warehouse=warehouse)

    return [
        receipt
        for receipt in receipts.prefetch_related("lines")
        if any(line.discrepancy != 0 for line in receipt.lines.all())
    ]


def backorders_ready_to_fill(warehouse=None):
    """Open backorders that some warehouse could now fill.

    The useful half of the backorder queue. An OPEN backorder nobody can
    fill is a waiting game; one where stock has since arrived somewhere is a
    job somebody could do this afternoon and has not noticed.

    Deliberately checks **every** warehouse, not just this one — decision D2
    lets another warehouse ship direct to the school, so stock at Serere
    makes a Namayemba backorder fillable.
    """
    from orders.services import warehouses_that_could_fill

    backorders = Backorder.objects.filter(status=BackorderStatus.OPEN)
    if warehouse is not None:
        backorders = backorders.filter(order__school__primary_warehouse=warehouse)

    return [
        backorder
        for backorder in backorders.select_related("sku", "order", "order__school")
        if warehouses_that_could_fill(backorder)
    ]


def needs_attention(warehouse=None, user=None):
    """The alert list — the things somebody should look at.

    Each row is a count and a sentence, not a list: the design shows one line
    per kind with a chip, and the row links through to the screen that has
    the detail. Returning the detail here would be a second copy of four
    other endpoints.

    Rows with a count of zero are omitted. An empty list means there is
    genuinely nothing to do, which is worth being able to say.

    ``user`` gates `registrations_pending`, the one row that is not about a
    site at all — a pending registration belongs to whoever administers
    accounts, not to a warehouse, so it cannot be selected by `warehouse`
    the way the rest are, and not everyone who can see this list is allowed
    to act on it (see below). Omitted when no user is passed, which keeps
    every existing caller — and every test — behaving exactly as before.
    """
    alerts = []

    low_stock = skus_below_minimum(warehouse)
    if low_stock:
        alerts.append(
            {
                "kind": "low_stock",
                "level": CRITICAL,
                "count": low_stock,
                "message": f"{low_stock} SKUs below minimum inventory",
            }
        )

    # HOLD means "awaiting payment" — see OrderStatus. This row said
    # "waiting for stock", which is a different queue entirely and now a real
    # one (F43, orders_awaiting_stock). A warehouse user reading the old
    # wording went looking for goods to receive when what was actually
    # waiting was a parent paying an invoice.
    on_hold = _orders_for(warehouse).filter(status=OrderStatus.HOLD).count()
    if on_hold:
        alerts.append(
            {
                "kind": "orders_on_hold",
                "level": HOLD,
                "count": on_hold,
                "message": (
                    f"{on_hold} school orders waiting for payment"
                    if on_hold != 1
                    else "1 school order waiting for payment"
                ),
            }
        )

    unreconciled = len(receipts_needing_reconciliation(warehouse))
    if unreconciled:
        alerts.append(
            {
                "kind": "receipts_unreconciled",
                "level": INSPECTION,
                "count": unreconciled,
                "message": f"{unreconciled} receipts require reconciliation",
            }
        )

    fillable = len(backorders_ready_to_fill(warehouse))
    if fillable:
        alerts.append(
            {
                "kind": "backorders_fillable",
                "level": READY,
                "count": fillable,
                "message": f"{fillable} backorders eligible for release",
            }
        )

    # Parcels that left and nobody ever said arrived.
    #
    # **This is the alert the Shipped/Completed split exists for.** Keeping
    # the two apart makes a lost delivery visible — but only if somebody who
    # can chase it is told. Without this the gap was recorded and shown to
    # nobody: the school sees its own parcels, and the school is not who
    # rings the warehouse.
    #
    # Fourteen days, because everything shipped this morning is unconfirmed
    # and none of it is a problem yet.
    from orders.services.shipping import shipments_awaiting_confirmation

    stale = len(
        list(shipments_awaiting_confirmation(warehouse=warehouse, older_than_days=14))
    )
    if stale:
        alerts.append(
            {
                "kind": "deliveries_unconfirmed",
                "level": CRITICAL,
                "count": stale,
                "message": (
                    f"{stale} deliver{'y' if stale == 1 else 'ies'} shipped over "
                    "14 days ago and never confirmed"
                ),
            }
        )

    # Registration requests, not stock — gated on the same roles as the
    # approve/decline endpoints (`CanUpdateTables`: Program Lead and
    # Operations Manager), not on warehouse scope. Finance and Warehouse
    # Staff can see the rest of this list but cannot act on a registration,
    # so showing them this row would point at a 403.
    #
    # One alert per request, not a single rolled-up count. Every other kind
    # here is a count over many rows because there is nowhere for one row to
    # link to — but a registration is a specific person a lead reviews one
    # at a time, so each gets its own row and its own `ref_id` to open
    # straight into that person's review, the same way an order or a receipt
    # would if this list linked to records instead of screens.
    #
    # Unverified requests are excluded: a lead cannot approve a request whose
    # email address nobody has proved they hold, so surfacing one would
    # present work that cannot be done. It appears the moment they enter
    # their code.
    if has_role(user, *ALL_SITE_ROLES):
        pending = RegistrationRequest.objects.filter(
            status=RegistrationRequest.Status.PENDING,
            verified_at__isnull=False,
        ).order_by("-created_at")
        for registration in pending:
            alerts.append(
                {
                    "kind": "registrations_pending",
                    "level": READY,
                    "count": 1,
                    "ref_id": registration.id,
                    "message": (
                        f"{registration.first_name} {registration.last_name} "
                        "asked for an account"
                    ),
                }
            )

    return alerts


# ---------------------------------------------------------------------------
# Recent activity
# ---------------------------------------------------------------------------

#: How far back the timeline looks. A dashboard is about today, not history —
#: the movement ledger is where you go for that.
ACTIVITY_WINDOW_DAYS = 7


def recent_activity(warehouse=None, limit=10):
    """What has happened here lately, newest first.

    Five kinds of event from four apps, merged into one list: receipts
    confirmed, orders shipped, stock adjusted, production orders raised, and
    backorders filled.

    **Merged in Python, deliberately.** These are five unrelated tables with
    no shared parent, and a database-level union would need them to agree on
    a column shape they have no reason to share. At ten rows over seven days
    the cost is nothing, and the alternative is a view somebody has to
    maintain every time an event type is added.

    Each row is `{at, kind, reference, description}` — enough to render a
    line and link to the document, and nothing more.
    """
    # Two cut-offs, because these five tables do not agree on what they
    # timestamp. `since` compares against date fields; `since_at` against
    # datetime ones. Using the date for both makes Django warn about a naive
    # datetime and quietly compares against midnight in the wrong zone.
    since = timezone.now().date() - timedelta(days=ACTIVITY_WINDOW_DAYS)
    since_at = timezone.now() - timedelta(days=ACTIVITY_WINDOW_DAYS)
    events = []

    receipts = Receipt.objects.filter(
        posted_at__isnull=False, date_received__gte=since
    ).select_related("production_order", "production_order__tailoring_center")
    if warehouse is not None:
        receipts = receipts.filter(production_order__warehouse=warehouse)
    for receipt in receipts[:limit]:
        units = sum(line.quantity_received for line in receipt.lines.all())
        centre = receipt.production_order.tailoring_center.name
        events.append(
            {
                "at": receipt.posted_at,
                "kind": "receipt",
                "reference": receipt.number,
                "description": f"Receipt {receipt.number} confirmed — {units} items from {centre}",
            }
        )

    shipments = Shipment.objects.filter(shipped_on__gte=since).select_related(
        "school"
    )
    if warehouse is not None:
        shipments = shipments.filter(from_warehouse=warehouse)
    for shipment in shipments[:limit]:
        events.append(
            {
                "at": shipment.created_at,
                "kind": "shipment",
                "reference": shipment.number,
                "description": (
                    f"{shipment.number} shipped to {shipment.school.name}"
                ),
            }
        )

    from inventory.models import InventoryAdjustment

    adjustments = InventoryAdjustment.objects.filter(
        posted_at__isnull=False, adjustment_date__gte=since
    ).select_related("sku", "sku__garment", "reason_code")
    if warehouse is not None:
        adjustments = adjustments.filter(warehouse=warehouse)
    for adjustment in adjustments[:limit]:
        sign = "+" if adjustment.reason_code.direction == "INCREASE" else "-"
        events.append(
            {
                "at": adjustment.posted_at,
                "kind": "adjustment",
                "reference": adjustment.number,
                "description": (
                    f"Inventory adjustment: {sign}{adjustment.quantity} "
                    f"{adjustment.sku.description} — {adjustment.reason_code.name}"
                ),
            }
        )

    production = ProductionOrder.objects.filter(
        order_date__gte=since
    ).select_related("tailoring_center")
    if warehouse is not None:
        production = production.filter(warehouse=warehouse)
    for order in production[:limit]:
        events.append(
            {
                "at": order.created_at,
                "kind": "production_order",
                "reference": order.number,
                "description": (
                    f"Production Order {order.number} submitted to "
                    f"{order.tailoring_center.name}"
                ),
            }
        )

    filled = Backorder.objects.filter(
        status=BackorderStatus.FILLED, assigned_at__gte=since_at
    ).select_related("sku", "order")
    if warehouse is not None:
        filled = filled.filter(order__school__primary_warehouse=warehouse)
    for backorder in filled[:limit]:
        events.append(
            {
                "at": backorder.assigned_at,
                "kind": "backorder",
                "reference": backorder.order.number,
                "description": (
                    f"Backorder on {backorder.order.number} released for picking"
                ),
            }
        )

    events.sort(key=lambda event: event["at"], reverse=True)
    return events[:limit]


# ---------------------------------------------------------------------------
# Order volume — the chart
# ---------------------------------------------------------------------------


def daily_order_volume(warehouse=None, date_from=None, date_to=None):
    """Orders placed per day, for the chart.

    Returns one row per day that had orders, plus the average. Days with no
    orders are **not** filled in with zeros: which days to show is a
    presentation decision, and a chart that wants a flat line across a
    quiet week can add them. Inventing rows here would mean this function
    had to know the shape of the axis.
    """
    orders = _orders_for(warehouse).exclude(status=OrderStatus.CANCELLED)

    if date_from:
        orders = orders.filter(order_date__gte=date_from)
    if date_to:
        orders = orders.filter(order_date__lte=date_to)

    rows = list(
        orders.values("order_date")
        .annotate(orders=Count("id"))
        .order_by("order_date")
    )

    total = sum(row["orders"] for row in rows)
    return {
        "days": [{"date": row["order_date"], "orders": row["orders"]} for row in rows],
        "total": total,
        "average_per_day": round(total / len(rows), 1) if rows else 0,
    }


# ---------------------------------------------------------------------------
# Notifications — the bell
# ---------------------------------------------------------------------------


def notifications(warehouse=None, user=None):
    """What the bell in the header shows, and its badge count.

    ## What this is, and what it deliberately is not

    **Derived, not stored.** These are the same four conditions
    `needs_attention()` reports, counted for the badge. Nothing is written
    when a condition appears, and nothing is marked read when somebody looks.

    That is what the design shows: the bell reads 4 and the Needs Attention
    panel reads "4 ALERTS" — one number, two places. Building a stored inbox
    to produce a figure that is already computable would mean two sources
    that can disagree, and the stored one would be wrong first.

    **The consequences, so nobody is surprised:**

    - The badge never goes down by reading it. It goes down when the low
      stock is replenished, the order is picked, the receipt is reconciled.
      That is arguably right for an operations queue — a warning you can
      dismiss without acting is a warning that stops working — but it is not
      how a social-media bell behaves, and somebody will expect it to be.
    - There is no per-user read state, no history, and nothing arrives while
      the page is open.

    If AsOne wants dismissible notifications, or a record of what was raised
    and when, that is a stored model with per-user read state — a different
    feature, not a bigger version of this one.
    """
    alerts = needs_attention(warehouse, user=user)

    return {
        "unread_count": len(alerts),
        "notifications": [
            {
                "kind": alert["kind"],
                "level": alert["level"],
                "message": alert["message"],
                "count": alert["count"],
                "ref_id": alert.get("ref_id"),
            }
            for alert in alerts
        ],
    }


# ---------------------------------------------------------------------------
# Inventory by warehouse
# ---------------------------------------------------------------------------


def inventory_by_warehouse(as_of=None):
    """Units, value and distinct SKUs held at each warehouse.

    The panel down the right of the dashboard, with a bar per site. The bar
    is proportional and the frontend scales it — returning a percentage here
    would bake in a decision about whether the scale is against the largest
    site or the total, which is a design question.

    Every warehouse appears, including one holding nothing. A site missing
    from the list reads as "no data" when the truth is "no stock", and those
    are different problems.

    One query for the ledger, one for the warehouse list.
    """
    from catalog.models import Warehouse

    ledger = _ledger_totals_by_warehouse(as_of)

    rows = []
    for warehouse in Warehouse.objects.order_by("name"):
        totals = ledger.get(
            warehouse.id, {"units": 0, "value": Decimal("0.00"), "skus": 0}
        )
        rows.append(
            {
                "warehouse_id": warehouse.id,
                "warehouse_name": warehouse.name,
                "units": totals["units"],
                "value": totals["value"],
                "sku_count": totals["skus"],
            }
        )

    return {
        "warehouses": rows,
        "total_units": sum(row["units"] for row in rows),
        "total_value": sum((row["value"] for row in rows), Decimal("0.00")),
        # Distinct SKUs held anywhere — the "Total SKUs" figure in the
        # header. Not the sum of the per-site counts: a shirt at both
        # warehouses is one SKU, not two.
        "total_skus": _distinct_skus_in_stock(as_of),
    }


def _ledger_totals_by_warehouse(as_of=None):
    """Per-warehouse totals, keyed by warehouse id. One query.

    SKUs are counted only where the level is positive, which needs the
    grouping done per SKU first — a warehouse that received a shirt and sent
    it all away holds no shirts, and counting the row would overstate its
    range.
    """
    from inventory.models import StockMovement

    movements = StockMovement.objects.filter(stock_status=StockStatus.AVAILABLE)
    if as_of is not None:
        movements = movements.filter(occurred_on__lte=as_of)

    per_sku = (
        movements.values("warehouse_id", "sku_id")
        .annotate(
            level=Coalesce(Sum("quantity"), Value(0), output_field=IntegerField()),
            value=Coalesce(
                Sum(F("quantity") * F("unit_value"), output_field=MONEY),
                Value(Decimal("0.00"), output_field=MONEY),
                output_field=MONEY,
            ),
        )
        .filter(level__gt=0)
    )

    totals = {}
    for row in per_sku:
        bucket = totals.setdefault(
            row["warehouse_id"], {"units": 0, "value": Decimal("0.00"), "skus": 0}
        )
        bucket["units"] += row["level"]
        bucket["value"] += row["value"]
        bucket["skus"] += 1

    return totals


def _distinct_skus_in_stock(as_of=None):
    """How many different SKUs are held anywhere, counted once each."""
    from inventory.models import StockMovement

    movements = StockMovement.objects.filter(stock_status=StockStatus.AVAILABLE)
    if as_of is not None:
        movements = movements.filter(occurred_on__lte=as_of)

    held = (
        movements.values("sku_id")
        .annotate(level=Coalesce(Sum("quantity"), Value(0), output_field=IntegerField()))
        .filter(level__gt=0)
    )
    return held.count()


# ---------------------------------------------------------------------------
# The weekly inventory report
# ---------------------------------------------------------------------------


def last_complete_week(today=None):
    """Monday to Sunday of the week that has finished.

    The card says "Inventory Weekly Report is ready", and a report is only
    ready once its week is over. Reporting the current week would produce a
    different answer every time somebody opened it, and a figure that moves
    is not a report.
    """
    today = today or timezone.now().date()
    this_monday = today - timedelta(days=today.weekday())
    start = this_monday - timedelta(days=7)
    return start, start + timedelta(days=6)


def weekly_inventory_report(warehouse=None, date_from=None, date_to=None):
    """What moved, per SKU per warehouse, over a week.

    The real content behind "Download Now". One row per SKU and warehouse:

        opening    what was on hand the moment the week began
        received   deliveries from tailoring centers
        adjusted   corrections, damages, returns, losses — signed
        picked     committed to school orders
        shipped    gone to schools
        closing    what is on hand now
        value      what the closing stock is carried at

    **Opening plus the movements equals closing.** That is the property that
    makes it a report rather than a list, and
    `test_the_rows_reconcile` asserts it on every row — if they ever stop
    agreeing, a movement type has been added and is not counted here.

    Rows where nothing moved and nothing is held are omitted. A warehouse
    that never touched a SKU has nothing to say about it.
    """
    from inventory.models import StockMovement

    if date_from is None or date_to is None:
        date_from, date_to = last_complete_week()

    movements = StockMovement.objects.all()
    if warehouse is not None:
        movements = movements.filter(warehouse=warehouse)

    # Opening: everything that happened before the week began, in any
    # status, so a unit picked last week is still this warehouse's stock.
    opening = _levels(movements.filter(occurred_on__lt=date_from))
    closing = _levels(movements.filter(occurred_on__lte=date_to))

    during = movements.filter(occurred_on__gte=date_from, occurred_on__lte=date_to)
    by_type = (
        during.values("warehouse_id", "sku_id", "movement_type")
        .annotate(units=Coalesce(Sum("quantity"), Value(0), output_field=IntegerField()))
    )

    moved = {}
    for row in by_type:
        key = (row["warehouse_id"], row["sku_id"])
        moved.setdefault(key, {})[row["movement_type"]] = row["units"]

    values = _closing_values(movements.filter(occurred_on__lte=date_to))

    keys = set(opening) | set(closing) | set(moved)
    names = _sku_and_warehouse_names(keys)

    rows = []
    for key in sorted(keys, key=lambda k: (names[k]["warehouse"], names[k]["sku"])):
        kinds = moved.get(key, {})
        received = kinds.get(MovementType.RECEIPT, 0)
        adjusted = kinds.get(MovementType.ADJUSTMENT, 0)
        transferred = kinds.get(MovementType.TRANSFER_IN, 0) + kinds.get(
            MovementType.TRANSFER_OUT, 0
        )
        picked = kinds.get(MovementType.PICK, 0)
        shipped = kinds.get(MovementType.SHIPMENT, 0)
        returned = kinds.get(MovementType.RETURN, 0)

        open_units = opening.get(key, 0)
        close_units = closing.get(key, 0)

        if not (open_units or close_units or kinds):
            continue

        rows.append(
            {
                "warehouse": names[key]["warehouse"],
                "sku_number": names[key]["sku_number"],
                "description": names[key]["sku"],
                "opening": open_units,
                "received": received,
                "adjusted": adjusted,
                "transferred": transferred,
                "picked": picked,
                "shipped": shipped,
                "returned": returned,
                "closing": close_units,
                "value": values.get(key, Decimal("0.00")),
            }
        )

    return {
        "date_from": date_from,
        "date_to": date_to,
        "rows": rows,
        "total_closing_units": sum(row["closing"] for row in rows),
        "total_closing_value": sum(
            (row["value"] for row in rows), Decimal("0.00")
        ),
    }


def _levels(movements):
    """Net units per (warehouse, sku), across every stock status.

    Every status on purpose. A unit sitting in PICK has not left the
    building, and an opening balance that ignored it would show stock
    vanishing on the day it was picked and reappearing if the order was
    cancelled.
    """
    rows = movements.values("warehouse_id", "sku_id").annotate(
        units=Coalesce(Sum("quantity"), Value(0), output_field=IntegerField())
    )
    return {(row["warehouse_id"], row["sku_id"]): row["units"] for row in rows}


def _closing_values(movements):
    rows = movements.values("warehouse_id", "sku_id").annotate(
        value=Coalesce(
            Sum(F("quantity") * F("unit_value"), output_field=MONEY),
            Value(Decimal("0.00"), output_field=MONEY),
            output_field=MONEY,
        )
    )
    return {(row["warehouse_id"], row["sku_id"]): row["value"] for row in rows}


def _sku_and_warehouse_names(keys):
    """Names for every (warehouse, sku) pair, in two queries rather than one
    per row."""
    from catalog.models import Warehouse

    warehouse_ids = {key[0] for key in keys}
    sku_ids = {key[1] for key in keys}

    warehouses = {
        w.id: w.name for w in Warehouse.objects.filter(id__in=warehouse_ids)
    }
    skus = {
        s.id: s for s in Sku.objects.filter(id__in=sku_ids).select_related("garment", "size")
    }

    return {
        key: {
            "warehouse": warehouses.get(key[0], ""),
            "sku": skus[key[1]].description if key[1] in skus else "",
            "sku_number": skus[key[1]].number if key[1] in skus else "",
        }
        for key in keys
    }


# ---------------------------------------------------------------------------
# The school's own dashboard
# ---------------------------------------------------------------------------
#
# Everything above this line answers a warehouse question: units in bins,
# SKUs under their floor, what is loading today. A school has none of those —
# it holds no stock — so pointing it at those tiles would give it a page of
# figures about somebody else's building.
#
# What a school actually needs is the state of its own paperwork: what it
# owes, what is coming, what turned up, and what is stuck. Every number below
# is that school's and no other's.


def school_order_counts(school):
    """The school's orders, grouped the way its screen asks the question.

    Not `values("status").annotate(...)` over the raw statuses, because the
    school does not think in five statuses — it thinks in "what do I owe",
    "what is coming", "what should I check for", "what is done". Released and
    Picked are one answer to a school: the warehouse has it.
    """
    counts = dict(
        SchoolOrder.objects.filter(school=school)
        .values_list("status")
        .annotate(n=Count("id"))
    )

    def at(*statuses):
        return sum(counts.get(status, 0) for status in statuses)

    return {
        # What the school owes money on. The only bucket it can act on by
        # paying, and the only one that can still be cancelled.
        "awaiting_payment": at(OrderStatus.HOLD),
        # Paid and with the warehouse. Nothing for the school to do but wait.
        "in_progress": at(OrderStatus.RELEASED, OrderStatus.PICKED),
        # Left the warehouse, not yet confirmed by the school. **This is the
        # actionable one** — every row here is a parcel somebody should be
        # looking for, and it is the tile that makes the Shipped/Completed
        # split worth having.
        "awaiting_confirmation": at(OrderStatus.SHIPPED),
        "completed": at(OrderStatus.COMPLETED),
        "cancelled": at(OrderStatus.CANCELLED),
        "total": sum(counts.values()),
    }


def school_amount_outstanding(school):
    """What the school still owes — the value of its unpaid invoices.

    Hold is the unpaid state: releasing an order *is* the payment
    confirmation. Cancelled orders are excluded because a void invoice is
    not a debt, and everything past Hold has been paid for.

    Summed in the database rather than in Python: the same reasoning as
    every other money figure here, and it keeps a school with a long history
    from pulling its whole order book across to add it up.
    """
    total = (
        SchoolOrderLine.objects.filter(
            order__school=school, order__status=OrderStatus.HOLD
        )
        .annotate(
            line=Coalesce(
                F("quantity") * F("unit_price"), Value(Decimal("0.00")), output_field=MONEY
            )
        )
        .aggregate(total=Coalesce(Sum("line"), Value(Decimal("0.00")), output_field=MONEY))
    )
    return total["total"]


def school_deliveries_to_confirm(school):
    """Parcels sent to this school that nobody has said arrived.

    The school's to-do list, and the reason `confirm_receipt()` exists. A
    shipment that left three weeks ago and never turned up is invisible
    without this — it looks exactly like one that arrived safely.

    `days_in_transit` is here rather than computed on the client so that the
    ordering and the number agree: oldest first, because the oldest is the
    one worth chasing.
    """
    today = timezone.localdate()

    shipments = (
        Shipment.objects.filter(school=school, received_at__isnull=True)
        .select_related("from_warehouse")
        .prefetch_related("lines__order")
        .order_by("shipped_on")
    )

    return [
        {
            "id": shipment.id,
            "number": shipment.number,
            # F42: a van can carry several orders, so the row names the
            # shipment and lists what is on it rather than pretending to one.
            "order_id": next((line.order_id for line in shipment.lines.all()), None),
            "order_number": ", ".join(
                sorted({line.order.number for line in shipment.lines.all()})
            ),
            "student_name": ", ".join(
                sorted({line.order.student_name for line in shipment.lines.all()})
            ),
            "shipped_on": shipment.shipped_on,
            "days_in_transit": (today - shipment.shipped_on).days,
            # Usually the school's own warehouse — but a backorder may be
            # filled by another one shipping direct (D2), and a school
            # expecting a parcel from Namayemba should not be confused by one
            # arriving from Serere.
            "from_warehouse": shipment.from_warehouse.name,
        }
        for shipment in shipments
    ]


def school_backorders(school):
    """What the school ordered that the warehouse could not fill.

    The answer to the question a school gets asked by a parent: it is not
    that the order was lost, it is that the shirt is not made yet.
    """
    backorders = (
        Backorder.objects.filter(order__school=school)
        .exclude(status=BackorderStatus.FILLED)
        .select_related("sku", "order")
        .order_by("order__order_date")
    )

    return [
        {
            "id": backorder.id,
            "order_id": backorder.order_id,
            "order_number": backorder.order.number,
            "student_name": backorder.order.student_name,
            "sku_number": backorder.sku.number,
            "sku_description": backorder.sku.description,
            "quantity": backorder.quantity,
            "status": backorder.status,
        }
        for backorder in backorders
    ]


def school_dashboard(school):
    """Everything the school's screen shows, in one round trip.

    One endpoint rather than five, because unlike the warehouse dashboard
    none of these is expensive and none of them is separately forbidden —
    they are all the same school's rows, so splitting them would buy nothing
    and cost four requests.
    """
    return {
        "school": {"id": school.id, "name": school.name},
        # The warehouse that serves them. A school reads stock levels there
        # and nowhere else — it is not a site they control, it is the one
        # their orders are filled from.
        "warehouse": (
            {
                "id": school.primary_warehouse_id,
                "name": school.primary_warehouse.name,
            }
            if school.primary_warehouse_id
            else None
        ),
        "orders": school_order_counts(school),
        "amount_outstanding": school_amount_outstanding(school),
        "deliveries_to_confirm": school_deliveries_to_confirm(school),
        "backorders": school_backorders(school),
    }

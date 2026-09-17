"""Backorders — F43, F44, F45, F46.

What a school's own warehouse could not supply, and how another one fills it.

## The rule this serves

Decision D2, Jim on 24 August:

> "The warehouses should have the capability to transfer 'Backorders' to
> another warehouse with Inventory. The fulfilling warehouse will then ship
> directly to the appropriate school."

Two halves, and they must not be collapsed. **Ordering** is fixed to the
school's primary warehouse. **Fulfilment** may come from anywhere with stock,
and goes straight to the school — it does not route back through the school's
own warehouse first.

## What a backorder *is*, under the pack's rule

Page 8 settles it:

> 1. Orders held until enough inventory is received to release a picklist
> 2. Orders released in a FIFO sequence
> 3. Option to transfer an order to another warehouse with Inventory

So an order short of stock is **held whole**. Nothing ships, and no pick
list is generated. Nobody *creates* a backorder: it is the state a released
order falls into when its warehouse cannot fill it, and it clears when stock
arrives or the order moves to a warehouse that has it.

That is why `orders_awaiting_stock()` is a query and not a table. The
per-SKU `Backorder` row below belongs to the other reading — part-filling —
and is only ever written by `pick_available()`.

## Why picking short is opt-in

`pick_order()` has always refused an order it cannot fill completely, and
that refusal is right for the ordinary case: a clerk who asked for a pick
list wants to know before they walk to the shelf. Backorders make partial
picking meaningful, but turning it on by default would change what every
existing caller does — including `seed_scenario` and Denis's F39 tests.

So `pick_available()` is a separate entry point. `pick_order()` is untouched.
"""

from django.db import transaction
from django.utils import timezone

from ..models import Backorder
from ..models.backorders import BackorderStatus
from ..models.school_orders import OrderStatus
from .fulfilment import check_availability


class CannotAssign(Exception):
    """The backorder is not open, so it cannot be handed to a warehouse."""


class NoStockToFill(Exception):
    """The warehouse offered to fill it does not hold enough."""


class NothingToPick(Exception):
    """Not one unit of the order is available, so there is no pick to make."""


@transaction.atomic
def pick_available(order, *, picked_by):
    """Pick what the warehouse has, and record the rest as backorders — F43.

    The partial-fulfilment counterpart to `pick_order()`, which refuses an
    order it cannot fill in full. Both exist because they answer different
    questions: "can I fill this?" and "fill what you can, we will chase the
    rest".

    Returns ``(order, backorders)``.

    Refused if nothing at all is available — that is not a partial pick, it
    is an order the warehouse cannot start, and writing a backorder for the
    whole thing while marking the order Picked would be a lie.

    **The payment gate applies here too.** `REQUIRE_RELEASE_BEFORE_PICK` is
    checked against the same rule `pick_order()` uses, because a gate on one
    door and not the other is not a gate: an unpaid order refused a full pick
    could simply be part-picked instead, reserving the stock and raising
    backorders against an invoice nobody has paid.
    """
    from inventory.models import MovementType, StockStatus
    from inventory.services import average_unit_value, post_movement

    from .fulfilment import REQUIRE_RELEASE_BEFORE_PICK

    if order.status == OrderStatus.CANCELLED:
        raise NothingToPick(f"{order.number} is cancelled.")
    if REQUIRE_RELEASE_BEFORE_PICK and order.status == OrderStatus.HOLD:
        raise NothingToPick(
            f"{order.number} has not been paid for. An order must be released "
            "before the warehouse picks it."
        )
    if order.status in (OrderStatus.PICKED, OrderStatus.SHIPPED):
        raise NothingToPick(f"{order.number} has already been picked.")

    warehouse = order.warehouse
    rows = check_availability(order)

    fillable = [row for row in rows if min(row["needed"], row["available"]) > 0]
    if not fillable:
        raise NothingToPick(
            f"{warehouse.name} holds none of what {order.number} needs, so "
            "there is nothing to pick. Assign the whole order to another "
            "warehouse instead."
        )

    picked_on = timezone.now().date()

    for row in fillable:
        sku = row["sku"]
        quantity = min(row["needed"], row["available"])
        unit_value = average_unit_value(sku, warehouse)

        post_movement(
            warehouse=warehouse, sku=sku, quantity=-quantity,
            movement_type=MovementType.PICK, stock_status=StockStatus.AVAILABLE,
            unit_value=unit_value, document_number=order.number,
            occurred_on=picked_on, created_by=picked_by,
        )
        post_movement(
            warehouse=warehouse, sku=sku, quantity=quantity,
            movement_type=MovementType.PICK, stock_status=StockStatus.PICK,
            unit_value=unit_value, document_number=order.number,
            occurred_on=picked_on, created_by=picked_by,
        )

    backorders = [
        Backorder(
            order=order,
            sku=row["sku"],
            quantity=row["shortfall"],
            created_by=picked_by,
            notes=f"{warehouse.name} was short at picking.",
        )
        for row in rows
        if row["shortfall"] > 0
    ]
    Backorder.objects.bulk_create(backorders)

    order.status = OrderStatus.PICKED
    order.save(update_fields=["status"])
    return order, backorders


def open_backorders(warehouse=None):
    """What is still owed — F44.

    ``warehouse`` narrows to backorders **raised by** that warehouse: the
    ones it could not supply. Not the ones it has agreed to fill; that is
    `assigned_to()`.
    """
    queryset = Backorder.objects.filter(
        status=BackorderStatus.OPEN
    ).select_related("order", "order__school", "sku", "sku__garment")

    if warehouse is not None:
        queryset = queryset.filter(order__school__primary_warehouse=warehouse)
    return queryset


def assigned_to(warehouse):
    """Backorders another warehouse has handed to ``warehouse`` to fill."""
    return Backorder.objects.filter(
        filled_by_warehouse=warehouse, status=BackorderStatus.ASSIGNED
    ).select_related("order", "order__school", "sku", "sku__garment")


def warehouses_that_could_fill(backorder):
    """Which warehouses hold enough to fill this — F45's shortlist.

    The screen that offers a transfer needs somewhere to send it, and asking
    a clerk to guess which warehouse has stock is how a backorder gets sent
    to one that does not.
    """
    from catalog.models import Warehouse
    from inventory.services import stock_level

    origin = backorder.origin_warehouse
    return [
        warehouse
        for warehouse in Warehouse.objects.exclude(pk=origin.pk)
        if stock_level(backorder.sku, warehouse) >= backorder.quantity
    ]


@transaction.atomic
def assign_backorder(backorder, *, warehouse, assigned_by):
    """Hand a backorder to a warehouse that has the stock — F45.

    The transfer AsOne asked for. Nothing moves in the ledger here: the
    receiving warehouse still has its stock, and will pick and ship it in
    the ordinary way. What changes is who owes the school.

    Refused if that warehouse does not actually hold enough. Sending a
    backorder to an empty warehouse produces a queue nobody can clear, and
    the clerk sending it cannot see the other site's shelves.
    """
    from inventory.services import stock_level

    if not backorder.can_be_assigned:
        raise CannotAssign(
            f"That backorder is {backorder.get_status_display().lower()} and "
            "cannot be reassigned."
        )

    if warehouse == backorder.origin_warehouse:
        raise CannotAssign(
            f"{warehouse.name} is the warehouse that ran short. A backorder "
            "has to go somewhere that has the stock."
        )

    available = stock_level(backorder.sku, warehouse)
    if available < backorder.quantity:
        raise NoStockToFill(
            f"{warehouse.name} holds {available} of {backorder.sku.number} "
            f"and the backorder needs {backorder.quantity}."
        )

    backorder.status = BackorderStatus.ASSIGNED
    backorder.filled_by_warehouse = warehouse
    backorder.assigned_at = timezone.now()
    backorder.assigned_by = assigned_by
    backorder.save(
        update_fields=["status", "filled_by_warehouse", "assigned_at", "assigned_by"]
    )
    return backorder


@transaction.atomic
def fill_backorder(backorder, *, filled_by, shipped_on=None, waybill_number="", notes=""):
    """The assigned warehouse picks and ships direct to the school — F46.

    This is the half of D2 that overrides the definitions page: the stock
    does **not** route back through the school's own warehouse. It goes from
    the shelf that had it, straight to the school.

    Reserves and ships in one step, because there is nothing between them
    for the fulfilling warehouse to decide — it already accepted the job
    when the backorder was assigned to it.

    Returns the Shipment.
    """
    from inventory.models import MovementType, StockStatus
    from inventory.services import average_unit_value, post_movement, stock_level

    from ..models import Shipment, ShipmentLine

    if backorder.status != BackorderStatus.ASSIGNED:
        raise CannotAssign(
            f"That backorder is {backorder.get_status_display().lower()}. Only "
            "an assigned backorder can be filled."
        )

    warehouse = backorder.filled_by_warehouse
    sku, quantity = backorder.sku, backorder.quantity

    available = stock_level(sku, warehouse)
    if available < quantity:
        raise NoStockToFill(
            f"{warehouse.name} now holds only {available} of {sku.number}. "
            "Something else took the stock since this was assigned."
        )

    shipped_on = shipped_on or timezone.now().date()
    unit_value = average_unit_value(sku, warehouse)

    shipment = Shipment.objects.create(
        school=backorder.order.school,
        from_warehouse=warehouse,
        shipped_on=shipped_on,
        shipped_by=filled_by,
        waybill_number=waybill_number.strip(),
        notes=notes or f"Backorder filled direct from {warehouse.name} (D2).",
    )

    # Straight out of AVAILABLE into SHIPPED. There is no PICK step: the
    # stock was never reserved here, and inventing a reservation it held for
    # no time would put two rows in the ledger that say nothing.
    post_movement(
        warehouse=warehouse, sku=sku, quantity=-quantity,
        movement_type=MovementType.SHIPMENT, stock_status=StockStatus.AVAILABLE,
        unit_value=unit_value, document_number=shipment.number,
        occurred_on=shipped_on, created_by=filled_by,
    )
    post_movement(
        warehouse=warehouse, sku=sku, quantity=quantity,
        movement_type=MovementType.SHIPMENT, stock_status=StockStatus.SHIPPED,
        unit_value=unit_value, document_number=shipment.number,
        occurred_on=shipped_on, created_by=filled_by,
    )

    # The line names the order, not the shipment — a backorder filled direct
    # is a one-order van, but it is the same shape as a consolidated one.
    ShipmentLine.objects.create(
        shipment=shipment, order=backorder.order, sku=sku, quantity=quantity
    )

    backorder.status = BackorderStatus.FILLED
    backorder.save(update_fields=["status"])
    return shipment


def eligible_for_release(warehouse=None):
    """Open backorders some warehouse could fill today — F45's shortlist.

    The "Release All Eligible" queue. An OPEN backorder nobody can fill is a
    waiting game; one where stock has since arrived somewhere is a job
    somebody could do this afternoon and has not noticed.

    Checks **every** warehouse, not just the school's own: decision D2 lets
    another warehouse ship direct, so stock at Serere can fill a Namayemba
    shortfall. `warehouse` narrows to backorders *raised by* that site,
    which is what its own staff are chasing — not where the stock is.
    """
    queryset = Backorder.objects.filter(status=BackorderStatus.OPEN).select_related(
        "order", "order__school", "order__school__primary_warehouse", "sku"
    )
    if warehouse is not None:
        queryset = queryset.filter(order__school__primary_warehouse=warehouse)

    return [
        backorder
        for backorder in queryset
        if warehouses_that_could_fill(backorder)
    ]


@transaction.atomic
def release_eligible(*, released_by, warehouse=None, backorders=None):
    """Assign and ship every backorder that can go — the bulk action.

    Each one is assigned to the warehouse holding the most of that SKU and
    shipped direct to the school, which is what `assign` then `fill` do one
    at a time. Doing it in bulk changes nothing about either: the same
    checks run per backorder, and the same ledger rows are written.

    **All or nothing.** One transaction, so a backorder that cannot be
    filled halfway through does not leave half the queue released and the
    rest untouched — a clerk who pressed one button should get one outcome.

    Returns the shipments created, which is what the confirmation screen
    reports back.
    """
    from inventory.services import stock_level

    if backorders is None:
        backorders = eligible_for_release(warehouse)
    else:
        backorders = list(backorders)

    if not backorders:
        raise NoStockToFill("No backorder is currently fillable.")

    shipments = []
    for backorder in backorders:
        options = warehouses_that_could_fill(backorder)
        if not options:
            raise NoStockToFill(
                f"{backorder.order.number} / {backorder.sku.number} cannot be "
                "filled from anywhere."
            )

        # The warehouse holding the most, so one release does not strip a
        # site that is only just covering its own orders.
        best = max(options, key=lambda w: stock_level(backorder.sku, w))

        assign_backorder(backorder, warehouse=best, assigned_by=released_by)
        shipments.append(fill_backorder(backorder, filled_by=released_by))

    return shipments


# ---------------------------------------------------------------------------
# The whole order moves — F43, F44, F45 under the pack's hold-complete rule
#
# The three functions below are the p.8 flow: see what is waiting, see who
# could fill it, hand it over. They work on the *order*, because under
# hold-complete there are no per-SKU shortfall rows to work on — the order
# itself is the thing that is short.
# ---------------------------------------------------------------------------


class OrderNotTransferable(Exception):
    """The order is in a state where moving it would mean something wrong."""


def orders_awaiting_stock(warehouse=None):
    """Released orders their warehouse cannot fill — F43, in FIFO order.

    The queue AsOne's p.8 describes: "orders held until enough inventory is
    received", "released in a FIFO sequence". Ordered by when the school
    placed them, so the sequence is the one the pack asks for and not the
    order somebody happened to look at them in.

    Each entry is ``{"order": ..., "shortfalls": [...]}`` — the rows from
    `check_availability()` that are actually short, so a screen can say
    *what* it is waiting on rather than only that it is waiting.

    ``warehouse`` narrows to orders that warehouse is responsible for,
    which after a transfer is not the same set as the schools it serves.

    Derived, not stored. A held order has no life of its own to record: it
    is simply a released order that cannot be picked yet, and the moment
    stock arrives it stops being one. Storing that would mean keeping a row
    in step with a number it does not own.
    """
    from ..models import SchoolOrder

    queryset = (
        SchoolOrder.objects.filter(status=OrderStatus.RELEASED)
        .select_related("school", "school__primary_warehouse", "fulfilled_by_warehouse")
        .order_by("created_at", "number")
    )

    waiting = []
    for order in queryset:
        if warehouse is not None and order.warehouse != warehouse:
            continue
        short = [row for row in check_availability(order) if row["shortfall"] > 0]
        if short:
            waiting.append({"order": order, "shortfalls": short})
    return waiting


def warehouses_that_could_fill_order(order):
    """Which warehouses hold enough for **every line** — F45's shortlist.

    Every line, not some: under hold-complete a transfer is only worth
    making to a warehouse that can finish the job. Offering one that would
    itself come up short just moves the waiting somewhere else.

    Excludes the warehouse currently responsible — see `transfer_order()`.
    """
    from catalog.models import Warehouse

    current = order.warehouse
    return [
        warehouse
        for warehouse in Warehouse.objects.exclude(pk=current.pk)
        if not any(row["shortfall"] > 0 for row in check_availability(order, warehouse))
    ]


@transaction.atomic
def transfer_order(order, *, warehouse, transferred_by, reason=""):
    """Hand a held order to a warehouse that has the stock — F45.

    p.8's third backorder option, and the whole-order reading of D2. The
    school keeps its primary warehouse; what changes is who fills this one
    order, and the receiving warehouse ships straight to the school.

    Nothing moves in the ledger. No stock is reserved at either end — the
    receiving warehouse picks in the ordinary way afterwards, and that pick
    is what touches inventory. What changes here is responsibility.

    Refused unless the target can fill every line, so a transfer cannot
    leave an order waiting in a second place.
    """
    if order.status == OrderStatus.CANCELLED:
        raise OrderNotTransferable(f"{order.number} is cancelled.")

    if order.status in (OrderStatus.PICKED, OrderStatus.SHIPPED, OrderStatus.COMPLETED):
        # Stock is already reserved or gone at the current warehouse.
        # Re-pointing the order would strand that reservation with nothing
        # referring to it, and the ledger would still say it is held for a
        # pick nobody is going to make.
        raise OrderNotTransferable(
            f"{order.number} has already been picked. Stock is reserved at "
            f"{order.warehouse.name}; release it there before moving the order."
        )

    if order.status == OrderStatus.HOLD:
        raise OrderNotTransferable(
            f"{order.number} has not been paid for. An order is only "
            "transferred because a warehouse cannot fill it, and nobody has "
            "tried to fill this one yet."
        )

    if warehouse == order.warehouse:
        raise OrderNotTransferable(
            f"{warehouse.name} is already filling {order.number}."
        )

    short = [row for row in check_availability(order, warehouse) if row["shortfall"] > 0]
    if short:
        listed = ", ".join(
            f"{row['sku'].number} (needs {row['needed']}, has {row['available']})"
            for row in short
        )
        raise NoStockToFill(
            f"{warehouse.name} cannot fill {order.number} either: {listed}."
        )

    order.fulfilled_by_warehouse = warehouse
    order.transferred_at = timezone.now()
    order.transferred_by = transferred_by
    order.transfer_reason = reason.strip()
    order.save(
        update_fields=[
            "fulfilled_by_warehouse",
            "transferred_at",
            "transferred_by",
            "transfer_reason",
        ]
    )
    return order

"""Shapes for the dashboard. None of these are models — every figure is
computed, so they are all plain Serializers."""

from rest_framework import serializers


class DashboardSummarySerializer(serializers.Serializer):
    """The six tiles across the top — F62."""

    available_units = serializers.IntegerField(
        help_text="Units on hand and free to promise."
    )
    inventory_value = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        help_text="What that stock is carried at, in UGX.",
    )
    units_awaiting_pick = serializers.IntegerField(
        help_text="Garments on orders the warehouse has not picked yet."
    )
    orders_awaiting_dispatch = serializers.IntegerField(
        help_text="Orders picked but not yet shipped — off the shelf, still in the building."
    )
    outstanding_backorders = serializers.IntegerField(
        help_text="Backorders open or assigned, not yet shipped."
    )
    units_shipped_today = serializers.IntegerField(
        help_text="Garments that left this warehouse today, across every van."
    )
    skus_below_minimum = serializers.IntegerField(
        help_text="SKUs at or under their reorder floor."
    )


class AttentionAlertSerializer(serializers.Serializer):
    """One row of the "Needs Attention" list.

    A count and a sentence, not the underlying records — the row links
    through to the screen that has those.
    """

    kind = serializers.CharField(
        help_text="Stable identifier for the frontend to route on: "
        "low_stock, orders_on_hold, receipts_unreconciled, backorders_fillable, "
        "registrations_pending."
    )
    level = serializers.CharField(help_text="CRITICAL, HOLD, INSPECTION or READY.")
    count = serializers.IntegerField()
    message = serializers.CharField()
    ref_id = serializers.IntegerField(
        required=False,
        allow_null=True,
        help_text="The one record this alert is about, for kinds that are "
        "one row per record rather than a rollup — currently only "
        "registrations_pending, where it is the RegistrationRequest id. "
        "Absent for every other kind: those are a count over many records, "
        "with no single one to link to.",
    )


class ActivityEventSerializer(serializers.Serializer):
    """One line of the recent activity timeline."""

    at = serializers.DateTimeField()
    kind = serializers.CharField(
        help_text="receipt, shipment, adjustment, production_order or backorder."
    )
    reference = serializers.CharField(help_text="The document number, for linking.")
    description = serializers.CharField()


class OrderVolumeDaySerializer(serializers.Serializer):
    date = serializers.DateField()
    orders = serializers.IntegerField()


class OrderVolumeSerializer(serializers.Serializer):
    """The daily orders chart.

    Days with no orders are absent rather than zero — see
    `daily_order_volume()` for why that is the caller's decision.
    """

    days = OrderVolumeDaySerializer(many=True)
    total = serializers.IntegerField()
    average_per_day = serializers.FloatField()


class NotificationSerializer(serializers.Serializer):
    """One item behind the bell. Same shape as an attention alert, because
    it is the same condition seen from the header rather than the panel."""

    kind = serializers.CharField()
    level = serializers.CharField()
    message = serializers.CharField()
    count = serializers.IntegerField()
    ref_id = serializers.IntegerField(required=False, allow_null=True)


class NotificationsSerializer(serializers.Serializer):
    """The bell: a badge count and the list behind it.

    `unread_count` is the number of *conditions* currently true, not a
    per-user inbox — reading them does not clear it. See
    `dashboard/services.py::notifications` for why, and for what a real
    inbox would need.
    """

    unread_count = serializers.IntegerField(
        help_text="Badge number. Falls when the underlying problem is fixed, not when read."
    )
    notifications = NotificationSerializer(many=True)


class WarehouseInventorySerializer(serializers.Serializer):
    """One site's line in the "Inventory by Warehouse" panel."""

    warehouse_id = serializers.IntegerField()
    warehouse_name = serializers.CharField()
    units = serializers.IntegerField()
    value = serializers.DecimalField(max_digits=18, decimal_places=2)
    sku_count = serializers.IntegerField(
        help_text="Distinct SKUs actually held here — not the catalogue size."
    )


class InventoryByWarehouseSerializer(serializers.Serializer):
    """The panel. The bar is proportional and the frontend scales it —
    returning a percentage would bake in whether the scale is against the
    largest site or the total, which is a design decision."""

    warehouses = WarehouseInventorySerializer(many=True)
    total_units = serializers.IntegerField()
    total_value = serializers.DecimalField(max_digits=18, decimal_places=2)
    total_skus = serializers.IntegerField(
        help_text="Distinct SKUs held anywhere, counted once — not the sum of the per-site counts."
    )


class WeeklyReportRowSerializer(serializers.Serializer):
    """One SKU at one warehouse, over the week.

    Opening plus the movements equals closing, on every row.
    """

    warehouse = serializers.CharField()
    sku_number = serializers.CharField()
    description = serializers.CharField()
    opening = serializers.IntegerField()
    received = serializers.IntegerField()
    adjusted = serializers.IntegerField(help_text="Signed — corrections, damages, losses.")
    transferred = serializers.IntegerField(help_text="Signed — net of in and out.")
    picked = serializers.IntegerField()
    shipped = serializers.IntegerField()
    returned = serializers.IntegerField()
    closing = serializers.IntegerField()
    value = serializers.DecimalField(max_digits=18, decimal_places=2)


class WeeklyReportSerializer(serializers.Serializer):
    """The weekly inventory report, as data. `/download/` is the same thing
    as a spreadsheet."""

    date_from = serializers.DateField()
    date_to = serializers.DateField()
    rows = WeeklyReportRowSerializer(many=True)
    total_closing_units = serializers.IntegerField()
    total_closing_value = serializers.DecimalField(max_digits=18, decimal_places=2)


# ---------------------------------------------------------------------------
# The school's own dashboard
# ---------------------------------------------------------------------------


class SchoolOrderCountsSerializer(serializers.Serializer):
    """The school's orders, in the four buckets a school thinks in."""

    awaiting_payment = serializers.IntegerField(
        help_text="Unpaid invoices. The only bucket the school can still cancel."
    )
    in_progress = serializers.IntegerField(
        help_text="Paid and with the warehouse. Nothing for the school to do."
    )
    awaiting_confirmation = serializers.IntegerField(
        help_text="Shipped, and the school has not said it arrived. The actionable one."
    )
    completed = serializers.IntegerField()
    cancelled = serializers.IntegerField()
    total = serializers.IntegerField()


class DeliveryToConfirmSerializer(serializers.Serializer):
    """A parcel sent to this school that nobody has confirmed arrived."""

    id = serializers.IntegerField()
    number = serializers.CharField()
    order_id = serializers.IntegerField()
    order_number = serializers.CharField()
    student_name = serializers.CharField()
    shipped_on = serializers.DateField()
    days_in_transit = serializers.IntegerField(
        help_text="Oldest first — the oldest is the one worth chasing."
    )
    from_warehouse = serializers.CharField(
        help_text="Not always the school's own: a backorder may ship direct from another (D2)."
    )


class SchoolBackorderSerializer(serializers.Serializer):
    """Something the school ordered that the warehouse could not fill."""

    id = serializers.IntegerField()
    order_id = serializers.IntegerField()
    order_number = serializers.CharField()
    student_name = serializers.CharField()
    sku_number = serializers.CharField()
    sku_description = serializers.CharField()
    quantity = serializers.IntegerField()
    status = serializers.CharField()


class SchoolSiteSerializer(serializers.Serializer):
    id = serializers.IntegerField()
    name = serializers.CharField()


class SchoolDashboardSerializer(serializers.Serializer):
    """F62 for a school — everything its screen shows, in one round trip."""

    school = SchoolSiteSerializer()
    warehouse = SchoolSiteSerializer(
        allow_null=True,
        help_text="The warehouse that fills this school's orders. Not a site it controls.",
    )
    orders = SchoolOrderCountsSerializer()
    amount_outstanding = serializers.DecimalField(
        max_digits=18,
        decimal_places=2,
        help_text="The value of the school's unpaid invoices.",
    )
    deliveries_to_confirm = DeliveryToConfirmSerializer(many=True)
    backorders = SchoolBackorderSerializer(many=True)

"""Serializers for school orders.

Shape only. The interesting asymmetry is between reading and writing: a
school *writes* kits and items, and *reads back* the exploded SKU lines the
warehouse will pick. Those are deliberately different shapes, because they
answer different questions.
"""

from rest_framework import serializers

from catalog.models import Kit, Sku, Warehouse

from .models import Backorder, SchoolOrder, SchoolOrderLine, Shipment, ShipmentLine


class SchoolOrderLineSerializer(serializers.ModelSerializer):
    """One line as the warehouse and the invoice see it — always a SKU."""

    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    from_kit_number = serializers.CharField(
        source="from_kit.kit_number", read_only=True, default=None
    )
    line_total = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )

    class Meta:
        model = SchoolOrderLine
        fields = (
            "id",
            "sku",
            "sku_number",
            "sku_description",
            "quantity",
            "unit_price",
            "line_total",
            "from_kit",
            "from_kit_number",
        )
        read_only_fields = fields


class KitLineInputSerializer(serializers.Serializer):
    """A kit the school chose. Exploded into SKUs on save."""

    kit = serializers.PrimaryKeyRelatedField(queryset=Kit.objects.all())
    quantity = serializers.IntegerField(min_value=1, default=1)


class SkuLineInputSerializer(serializers.Serializer):
    """An individual item the school chose."""

    sku = serializers.PrimaryKeyRelatedField(queryset=Sku.objects.all())
    quantity = serializers.IntegerField(min_value=1)


class InvoiceKitGroupSerializer(serializers.Serializer):
    """A kit on the invoice, with the garments it contains beneath it.

    AsOne's definition of an invoice names the kit number "if used", so the
    school sees the thing it actually chose rather than a flat list of
    garments it did not ask for by name.
    """

    kit_number = serializers.CharField(source="kit.kit_number")
    kit_name = serializers.CharField(source="kit.name")
    subtotal = serializers.DecimalField(max_digits=14, decimal_places=2)
    lines = SchoolOrderLineSerializer(many=True)


class InvoiceSerializer(serializers.Serializer):
    """The order as a document — F34.

    Same number as the order: AsOne treats the two as one thing, since the
    school uses the invoice number and the student's name to hand the right
    parcel to the right child.
    """

    number = serializers.CharField(help_text="The invoice number, also the order number.")
    student_name = serializers.CharField()
    school_name = serializers.CharField(source="school.name")
    order_date = serializers.DateField()
    status = serializers.CharField()
    status_display = serializers.CharField(source="get_status_display")
    total = serializers.DecimalField(max_digits=14, decimal_places=2)

    kits = InvoiceKitGroupSerializer(many=True)
    items = SchoolOrderLineSerializer(many=True, help_text="Ordered individually.")

    cancelled_at = serializers.DateTimeField(allow_null=True)
    cancellation_reason = serializers.CharField(allow_blank=True)


class CancelOrderSerializer(serializers.Serializer):
    """Withdrawing an unpaid invoice — F36."""

    reason = serializers.CharField(
        max_length=200,
        required=False,
        allow_blank=True,
        default="",
        help_text="Why the school cancelled. Optional, but useful when a parent asks.",
    )


class ReleaseOrderSerializer(serializers.Serializer):
    """Confirming payment on an order — F35.

    `payment_reference` is free text because we do not yet know what the
    real one looks like: what confirms payment is open question Q2.
    """

    payment_reference = serializers.CharField(
        max_length=100,
        required=False,
        allow_blank=True,
        default="",
        help_text="Receipt number, mobile money reference — whatever identifies the payment.",
    )


class ShipmentLineSerializer(serializers.ModelSerializer):
    """One SKU on a van, and whose it is.

    Since F42 a shipment carries several orders, so the line — not the
    shipment — is what names the student. The packing list is built on this.
    """

    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    order_number = serializers.CharField(source="order.number", read_only=True)
    student_name = serializers.CharField(source="order.student_name", read_only=True)

    class Meta:
        model = ShipmentLine
        fields = (
            "id",
            "order",
            "order_number",
            "student_name",
            "sku",
            "sku_number",
            "sku_description",
            "quantity",
        )


class ShipmentSerializer(serializers.ModelSerializer):
    """What left a warehouse — F41."""

    from_warehouse_name = serializers.CharField(
        source="from_warehouse.name", read_only=True
    )
    school_name = serializers.CharField(source="school.name", read_only=True)
    # F42: a van carries several orders, so the list column is a count and
    # the numbers themselves are a list, not one field.
    order_count = serializers.IntegerField(read_only=True)
    order_numbers = serializers.SerializerMethodField()
    shipped_by_name = serializers.CharField(
        source="shipped_by.get_full_name", read_only=True
    )
    status = serializers.CharField(read_only=True)
    total_quantity = serializers.IntegerField(read_only=True)
    lines = ShipmentLineSerializer(many=True, read_only=True)

    def get_order_numbers(self, shipment) -> list:
        """Read off the prefetched lines, so a list costs no extra query."""
        return sorted({line.order.number for line in shipment.lines.all()})

    class Meta:
        model = Shipment
        fields = (
            "id",
            "number",
            "school",
            "school_name",
            "order_count",
            "order_numbers",
            "status",
            "total_quantity",
            "from_warehouse",
            "from_warehouse_name",
            "shipped_on",
            "shipped_by",
            "shipped_by_name",
            "waybill_number",
            "carrier_method",
            "notes",
            "received_at",
            "received_by",
            "receipt_notes",
            "lines",
        )
        read_only_fields = fields


class ConfirmReceiptSerializer(serializers.Serializer):
    """The school confirming a parcel arrived — F41's other half."""

    shipment = serializers.IntegerField(
        required=False,
        help_text="Which shipment. May be omitted when the order has only one.",
    )
    notes = serializers.CharField(
        required=False,
        allow_blank=True,
        default="",
        help_text="Anything wrong — short, damaged, wrong student. Recorded, not acted on.",
    )


class ShipOrderSerializer(serializers.Serializer):
    """Sending a picked order out — F41.

    `from_warehouse` is optional and defaults to the order's own. It exists
    because decision D2 lets a backorder ship direct from whichever
    warehouse actually filled it.
    """

    from_warehouse = serializers.PrimaryKeyRelatedField(
        queryset=Warehouse.objects.all(),
        required=False,
        allow_null=True,
        help_text="Defaults to the school's own warehouse. Set for a backorder filled elsewhere.",
    )
    shipped_on = serializers.DateField(
        required=False, help_text="Defaults to today."
    )
    waybill_number = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=""
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class OrderOnHoldSerializer(serializers.ModelSerializer):
    """One row of the still-on-hold report — F53."""

    school_name = serializers.CharField(source="school.name", read_only=True)
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)
    line_count = serializers.SerializerMethodField()

    class Meta:
        model = SchoolOrder
        fields = (
            "id",
            "number",
            "school",
            "school_name",
            "student_name",
            "order_date",
            "total",
            "line_count",
        )
        read_only_fields = fields

    def get_line_count(self, obj) -> int:
        return len(obj.lines.all())


class SchoolOrderSerializer(serializers.ModelSerializer):
    """An order, reading. Doubles as the invoice — same number, same lines."""

    lines = SchoolOrderLineSerializer(many=True, read_only=True)
    school_name = serializers.CharField(source="school.name", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)
    # Null unless the order was transferred — F45. `warehouse_name` above
    # already says who is filling it, but not *why* it is not the school's own
    # warehouse, and that is the thing a reader needs explaining when a
    # Namayemba school's order is being packed at Serere.
    transferred_to_name = serializers.CharField(
        source="fulfilled_by_warehouse.name", read_only=True, default=None
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    created_by_name = serializers.CharField(
        source="created_by.get_full_name", read_only=True
    )
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = SchoolOrder
        fields = (
            "id",
            "number",
            "school",
            "school_name",
            "warehouse_name",
            "transferred_to_name",
            "student_name",
            "order_date",
            "status",
            "status_display",
            "priority",
            "notes",
            "total",
            "created_by",
            "created_by_name",
            "created_at",
            "cancelled_at",
            "cancellation_reason",
            "released_at",
            "payment_reference",
            "lines",
        )
        read_only_fields = (
            "id",
            "number",
            "school",
            "status",
            "created_by",
            "created_at",
            "cancelled_at",
            "cancellation_reason",
            "released_at",
            "payment_reference",
        )


class SchoolOrderWriteSerializer(serializers.Serializer):
    """Placing an order — F30, F31.

    `school` is not a field. A school clerk orders for their own school and
    no other; taking it from the request body would invite a school to place
    an order against someone else's.

    `status` is not a field either. Every order starts on Hold (F32) and
    only payment confirmation moves it — see open question Q2.
    """

    student_name = serializers.CharField(
        max_length=150,
        help_text="The student this uniform is for. Free text — students have no accounts.",
    )
    order_date = serializers.DateField()
    kits = KitLineInputSerializer(many=True, required=False, default=list)
    skus = SkuLineInputSerializer(many=True, required=False, default=list)
    notes = serializers.CharField(required=False, allow_blank=True, default="")

    def validate_student_name(self, value):
        if not value.strip():
            raise serializers.ValidationError(
                "A student's name is required — it is how the school hands the "
                "uniform to the right child."
            )
        return value.strip()

    def validate(self, attrs):
        if not attrs.get("kits") and not attrs.get("skus"):
            raise serializers.ValidationError(
                {"skus": "An order needs at least one kit or item."}
            )
        return attrs


class OrderDemandRowSerializer(serializers.Serializer):
    """One SKU the warehouse has to pick, however many places it came from."""

    sku_number = serializers.CharField()
    sku_description = serializers.CharField()
    quantity = serializers.IntegerField()


class OrderAvailabilityRowSerializer(serializers.Serializer):
    """One SKU on the order, and whether there is enough of it — F37."""

    sku_number = serializers.CharField(source="sku.number")
    sku_description = serializers.CharField(source="sku.description")
    needed = serializers.IntegerField()
    available = serializers.IntegerField()
    shortfall = serializers.IntegerField(
        help_text="How many short. Zero means this line can be filled."
    )


class BackorderSerializer(serializers.ModelSerializer):
    """What a school is still owed — F44."""

    order_number = serializers.CharField(source="order.number", read_only=True)
    # The warehouse's own picking hint, carried through from the order so the
    # exceptions queue can be worked most-urgent-first like the backlog.
    priority = serializers.CharField(source="order.priority", read_only=True)
    priority_display = serializers.CharField(
        source="order.get_priority_display", read_only=True
    )
    # Free stock for this SKU **anywhere**, and the best single warehouse.
    # Anywhere, because decision D2 lets another warehouse ship direct — a
    # backorder is fillable if the goods exist somewhere, not only at the
    # school's own site.
    available = serializers.SerializerMethodField()
    fillable_at = serializers.SerializerMethodField()
    school_name = serializers.CharField(source="order.school.name", read_only=True)
    student_name = serializers.CharField(source="order.student_name", read_only=True)
    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    origin_warehouse_name = serializers.CharField(
        source="order.school.primary_warehouse.name",
        read_only=True,
        help_text="The warehouse that ran short.",
    )
    def _stock(self, backorder):
        """Free stock for this SKU, per warehouse, best first.

        Cached on the instance because both computed fields want it and a
        list of thirty backorders should not ask twice per row.
        """
        cached = getattr(backorder, "_free_stock", None)
        if cached is None:
            from inventory.services import stock_levels

            cached = sorted(
                (
                    (row["warehouse__name"], row["level"])
                    for row in stock_levels()
                    if row["sku_id"] == backorder.sku_id and row["level"] > 0
                ),
                key=lambda pair: -pair[1],
            )
            backorder._free_stock = cached
        return cached

    def get_available(self, backorder) -> int:
        """Units on hand anywhere. Zero means nobody can fill it yet."""
        return sum(level for _, level in self._stock(backorder))

    def get_fillable_at(self, backorder) -> str | None:
        """The warehouse holding the most of it, or None.

        Named rather than just counted, because the clerk's next question
        after "can this go?" is "from where?".
        """
        rows = self._stock(backorder)
        return rows[0][0] if rows else None

    filled_by_warehouse_name = serializers.CharField(
        source="filled_by_warehouse.name", read_only=True, default=None
    )

    class Meta:
        model = Backorder
        fields = (
            "id", "order", "order_number", "school_name", "student_name",
            "sku", "sku_number", "sku_description", "quantity",
            "status", "status_display",
            "priority",
            "priority_display",
            "available",
            "fillable_at",
            "origin_warehouse_name",
            "filled_by_warehouse", "filled_by_warehouse_name",
            "assigned_at", "created_at", "notes",
        )
        read_only_fields = fields


class AssignBackorderSerializer(serializers.Serializer):
    """Handing a backorder to a warehouse that has the stock — F45."""

    warehouse = serializers.PrimaryKeyRelatedField(
        queryset=Warehouse.objects.all(),
        help_text="A warehouse holding enough to fill it — never the one that ran short.",
    )


class TransferOrderSerializer(serializers.Serializer):
    """Moving a whole held order to a warehouse with stock — F45.

    The whole-order counterpart to AssignBackorderSerializer. Which of the
    two is the live path depends on how AsOne answer part-shipping: under
    the pack's hold-complete rule (p.8) an order short of stock is held
    entire, so there are no per-SKU rows to assign and the order itself is
    what moves.
    """

    warehouse = serializers.PrimaryKeyRelatedField(
        queryset=Warehouse.objects.all(),
        help_text=(
            "A warehouse holding enough of every line to finish the order. "
            "Get the list from `transfer-candidates/`."
        ),
    )
    reason = serializers.CharField(
        max_length=200,
        required=False,
        allow_blank=True,
        default="",
        help_text="Why it moved. A transfer is somebody's judgement, so it is worth recording who decided and why.",
    )


class FillBackorderSerializer(serializers.Serializer):
    """The assigned warehouse shipping direct to the school — F46."""

    shipped_on = serializers.DateField(required=False, help_text="Defaults to today.")
    waybill_number = serializers.CharField(
        max_length=50, required=False, allow_blank=True, default=""
    )
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class PackingListLineSerializer(serializers.Serializer):
    """One item in the parcel, and who it is for.

    AsOne's definitions page: the school uses the **invoice number and the
    student's name together** to hand shipments to the right child. On a
    consolidated van (F42) that pairing has to be per line — one sheet
    covers several students.
    """

    invoice_number = serializers.CharField()
    student_name = serializers.CharField()
    sku_number = serializers.CharField()
    description = serializers.CharField()
    quantity = serializers.IntegerField()


class PackingListSerializer(serializers.Serializer):
    """The document that travels with the goods — F40."""

    shipment_number = serializers.CharField()
    shipped_on = serializers.DateField()
    waybill_number = serializers.CharField()
    from_warehouse = serializers.CharField()
    # F42: a consolidated van carries several invoices, so the pairing of
    # invoice number and student moved onto the lines — which is where the
    # school actually reads it when handing parcels over.
    order_numbers = serializers.ListField(
        child=serializers.CharField(),
        help_text="Every invoice number on this van.",
    )
    school = serializers.CharField()
    school_address = serializers.CharField()
    is_direct_from_another_warehouse = serializers.BooleanField(
        help_text="True for a backorder filled elsewhere and shipped direct (D2)."
    )
    lines = PackingListLineSerializer(many=True)
    total_units = serializers.IntegerField()


class PartProcessedOrderSerializer(serializers.ModelSerializer):
    """An order picked but not yet despatched — F52, F54."""

    school_name = serializers.CharField(source="school.name", read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    warehouse_name = serializers.CharField(
        source="school.primary_warehouse.name", read_only=True
    )
    total = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = SchoolOrder
        fields = (
            "id", "number", "school", "school_name", "warehouse_name",
            "student_name", "order_date", "status", "status_display", "total",
        )
        read_only_fields = fields


class CostedShipmentSerializer(serializers.Serializer):
    """What went to a school and what it was worth — F57."""

    school_id = serializers.IntegerField(source="shipment__school_id")
    school_name = serializers.CharField(source="shipment__school__name")
    shipments = serializers.IntegerField()
    units = serializers.IntegerField()
    value = serializers.DecimalField(
        max_digits=18, decimal_places=2,
        help_text="Valued at what the school was charged, snapshotted when the order was placed.",
    )


class DespatchSerializer(serializers.Serializer):
    """Sending a school's picked orders out on one van — F42."""

    school = serializers.IntegerField(help_text="Who the van is going to.")
    orders = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text=(
            "Which orders to load. Omit to send every picked order waiting "
            "for that school at this warehouse."
        ),
    )
    from_warehouse = serializers.IntegerField(
        required=False,
        help_text="Defaults to the caller's own warehouse.",
    )
    shipped_on = serializers.DateField(required=False)
    waybill_number = serializers.CharField(
        max_length=60, required=False, allow_blank=True
    )
    carrier_method = serializers.CharField(
        max_length=120, required=False, allow_blank=True
    )
    notes = serializers.CharField(required=False, allow_blank=True)


class ReadyToDespatchSerializer(serializers.Serializer):
    """A school with orders picked and waiting — the despatch queue."""

    school_id = serializers.IntegerField()
    school__name = serializers.CharField()
    orders = serializers.IntegerField(help_text="Picked orders waiting for this school.")
    units = serializers.IntegerField(help_text="Garments across those orders.")


class PickingQueueRowSerializer(serializers.ModelSerializer):
    """An order waiting to be picked — the backlog row."""

    school_name = serializers.CharField(source="school.name", read_only=True)
    # `warehouse`, not `school.primary_warehouse`. After an F45 transfer they
    # are different warehouses, and the one that matters on a picking backlog
    # is the one that has to pick it.
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)
    transferred_to_name = serializers.CharField(
        source="fulfilled_by_warehouse.name", read_only=True, default=None
    )
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    priority_display = serializers.CharField(
        source="get_priority_display", read_only=True
    )
    item_count = serializers.SerializerMethodField()
    sku_sample = serializers.SerializerMethodField()

    class Meta:
        model = SchoolOrder
        fields = (
            "id",
            "number",
            "school",
            "school_name",
            "warehouse_name",
            "transferred_to_name",
            "student_name",
            "order_date",
            "status",
            "status_display",
            "priority",
            "priority_display",
            "item_count",
            "sku_sample",
        )

    def get_item_count(self, order) -> int:
        """Garments, not lines — what the warehouse actually pulls."""
        return sum(line.quantity for line in order.lines.all())

    def get_sku_sample(self, order) -> list:
        """The first few SKUs, so a clerk can see what kind of pick it is."""
        return [line.sku.number for line in order.lines.all()[:3]]


class PickingSummarySerializer(serializers.Serializer):
    """The three tiles above the picking backlog."""

    ready_to_pick = serializers.IntegerField(
        help_text="Released and paid for, nothing off the shelf yet."
    )
    picked = serializers.IntegerField(
        help_text="Off the shelf and reserved, waiting for a van."
    )
    completed_today = serializers.IntegerField(
        help_text="Orders picked today, counted from the ledger rows picking writes."
    )


class PaginatedPickingQueueSerializer(serializers.Serializer):
    """The backlog, one page of it, in the shape every other list uses."""

    count = serializers.IntegerField()
    next = serializers.CharField(allow_null=True)
    previous = serializers.CharField(allow_null=True)
    results = PickingQueueRowSerializer(many=True)


class PickingQueueSerializer(serializers.Serializer):
    """The picking screen's landing payload: the tiles and a page of backlog.

    `summary` counts the whole queue while `orders` is one page of it. That
    asymmetry is on purpose — "12 ready to pick" means twelve, not twelve on
    this page.
    """

    summary = PickingSummarySerializer()
    orders = PaginatedPickingQueueSerializer()


class ReleaseEligibleSerializer(serializers.Serializer):
    """Releasing every backorder that can go — the bulk action."""

    backorders = serializers.ListField(
        child=serializers.IntegerField(),
        required=False,
        help_text="Which to release. Omit to release everything eligible.",
    )

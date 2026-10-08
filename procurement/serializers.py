"""Serializers for group and production orders.

Orders are written with their lines in one request. A header saved on its own
would appear in the open-orders view as goods on their way that nobody
actually ordered, so the lines are nested and written in the same
transaction — see procurement/services.py.
"""

from rest_framework import serializers

from catalog.models import Sku
from catalog.services import PriceNotSet

from .models import (
    GroupOrder,
    GroupOrderLine,
    ProductionOrder,
    ProductionOrderLine,
    Receipt,
    ReceiptLine,
)
from .models.base import OrderStatus


class OrderLineSerializer(serializers.ModelSerializer):
    """A line, reading. Includes the SKU's details so a client can render the
    document without a second request per line."""

    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    line_total = serializers.DecimalField(
        max_digits=14, decimal_places=2, read_only=True
    )

    class Meta:
        fields = (
            "id",
            "sku",
            "sku_number",
            "sku_description",
            "quantity",
            "unit_price",
            "line_total",
        )
        read_only_fields = ("id", "line_total")


class GroupOrderLineSerializer(OrderLineSerializer):
    class Meta(OrderLineSerializer.Meta):
        model = GroupOrderLine


class ProductionOrderLineSerializer(OrderLineSerializer):
    class Meta(OrderLineSerializer.Meta):
        model = ProductionOrderLine


class OrderLineInputSerializer(serializers.Serializer):
    """A line, writing.

    `unit_price` is optional. Left out, the garment's price on the order date
    is copied onto the line and fixed there. Supplied, it is taken as agreed
    with the Tailoring Center — AsOne negotiates these, and the system should
    record what was agreed rather than overrule it.
    """

    sku = serializers.PrimaryKeyRelatedField(queryset=Sku.objects.filter(is_active=True))
    quantity = serializers.IntegerField(min_value=1)
    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, required=False, allow_null=True
    )

    def validate_sku(self, value):
        """An inactive SKU cannot be ordered.

        Filtered on the queryset above, which produces "invalid pk" — this
        turns it into a message that says why.
        """
        if not value.is_active:
            raise serializers.ValidationError(
                f"{value.number} is retired and cannot be ordered."
            )
        return value


class OrderWriteMixin(serializers.ModelSerializer):
    """Shared write behaviour: nested lines, no duplicate SKUs, real prices."""

    lines = OrderLineInputSerializer(many=True, write_only=True)

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("An order needs at least one line.")

        # The database refuses duplicates per order; catching it here names
        # the SKU instead of failing on an integrity error.
        seen = set()
        duplicates = {
            line["sku"].number
            for line in value
            if line["sku"].pk in seen or seen.add(line["sku"].pk)
        }
        if duplicates:
            raise serializers.ValidationError(
                f"Each SKU may appear once. Repeated: {', '.join(sorted(duplicates))}."
            )
        return value

    def validate(self, attrs):
        """Refuse an order that cannot be costed.

        A group order funds the Tailoring Centers, so a line worth nothing
        would under-fund them by exactly the amount nobody noticed.
        """
        order_date = attrs.get("order_date")
        unpriced = []
        for line in attrs.get("lines", []):
            if line.get("unit_price") is not None:
                continue
            try:
                from catalog.services import price_for

                price_for(line["sku"].garment, order_date)
            except PriceNotSet:
                unpriced.append(line["sku"].number)

        if unpriced:
            raise serializers.ValidationError(
                {
                    "lines": (
                        "These SKUs have no price on the order date, so the order "
                        f"cannot be costed: {', '.join(sorted(unpriced))}. Set a "
                        "price, or supply unit_price on the line."
                    )
                }
            )
        return attrs


class GroupOrderSerializer(serializers.ModelSerializer):
    """A group order, reading."""

    lines = GroupOrderLineSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True)
    total_quantity = serializers.IntegerField(read_only=True)
    total_value = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)

    class Meta:
        model = GroupOrder
        fields = (
            "id",
            "number",
            "order_date",
            "due_in_warehouse_date",
            "status",
            "status_display",
            "notes",
            "created_by",
            "created_by_name",
            "created_at",
            "lines",
            "total_quantity",
            "total_value",
        )
        read_only_fields = ("id", "number", "created_by", "created_at")


class GroupOrderWriteSerializer(OrderWriteMixin):
    class Meta:
        model = GroupOrder
        fields = ("order_date", "due_in_warehouse_date", "notes", "lines")


class ProductionOrderSerializer(serializers.ModelSerializer):
    """A production order, reading."""

    lines = ProductionOrderLineSerializer(many=True, read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)
    tailoring_center_name = serializers.CharField(
        source="tailoring_center.name", read_only=True
    )
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)
    group_order_number = serializers.CharField(
        source="group_order.number", read_only=True, default=None
    )
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True)
    total_quantity = serializers.IntegerField(read_only=True)
    total_value = serializers.DecimalField(max_digits=16, decimal_places=2, read_only=True)
    # How much has actually arrived, which is a different question from the
    # document's own status — see ProductionOrder.fulfilment_status.
    quantity_received = serializers.IntegerField(read_only=True)
    fulfilment_status = serializers.CharField(read_only=True)
    fulfilment_status_display = serializers.SerializerMethodField()
    line_count = serializers.SerializerMethodField()

    FULFILMENT_LABELS = {
        "AWAITING": "Awaiting delivery",
        "PARTIAL": "Partially received",
        "RECEIVED": "Received",
        "CLOSED": "Closed",
        "CANCELLED": "Cancelled",
    }

    def get_fulfilment_status_display(self, order) -> str:
        return self.FULFILMENT_LABELS.get(order.fulfilment_status, order.fulfilment_status)

    def get_line_count(self, order) -> int:
        """How many SKUs are on it — the design's "6 SKUs" column."""
        return len(order.lines.all())

    class Meta:
        model = ProductionOrder
        fields = (
            "id",
            "number",
            "order_date",
            "due_in_warehouse_date",
            "status",
            "status_display",
            "tailoring_center",
            "tailoring_center_name",
            "warehouse",
            "warehouse_name",
            "group_order",
            "group_order_number",
            "notes",
            "created_by",
            "created_by_name",
            "created_at",
            "lines",
            "line_count",
            "total_quantity",
            "total_value",
            "quantity_received",
            "fulfilment_status",
            "fulfilment_status_display",
        )
        read_only_fields = ("id", "number", "created_by", "created_at")


class ProductionOrderWriteSerializer(OrderWriteMixin):
    class Meta:
        model = ProductionOrder
        fields = (
            "order_date",
            "due_in_warehouse_date",
            "tailoring_center",
            "warehouse",
            "group_order",
            "notes",
            "lines",
        )


class ReconciliationRowSerializer(serializers.Serializer):
    """One SKU compared between a group order and its production orders."""

    sku_number = serializers.CharField(source="sku.number")
    sku_description = serializers.CharField(source="sku.description")
    requested = serializers.IntegerField(help_text="Quantity on the group order.")
    ordered = serializers.IntegerField(help_text="Quantity across its production orders.")
    difference = serializers.IntegerField(
        help_text="Ordered minus requested. Negative means the TCs were asked for less."
    )


class OrderAmendSerializer(serializers.Serializer):
    """What PATCH may change on an order.

    Header fields only. Amending an order's **lines** is F18 ("Should"), and
    it is not built — so it is not silently half-accepted here. A client
    sending `lines` gets an error rather than the quiet impression that its
    change was applied.

    Cancelling is a status change, not a delete: an order funds a Tailoring
    Center, so the document has to survive being withdrawn.

    **An order goods have already arrived against cannot be cancelled.** That
    guard is here rather than in the view because it is a fact about the
    document, not about who is asking: receipts point at this order and carry
    the value the ledger was written from, so cancelling it would leave posted
    stock attributed to an order that claims it was never placed. Close it
    instead — that says the order is finished, which is what actually
    happened.
    """

    status = serializers.ChoiceField(choices=OrderStatus.choices, required=False)
    due_in_warehouse_date = serializers.DateField(required=False, allow_null=True)
    notes = serializers.CharField(required=False, allow_blank=True)

    #: Named in the "nothing to change" message. Overridden by the subclass
    #: below, so the message cannot list a field the serializer does not take.
    amendable = "status, due_in_warehouse_date, notes"

    def validate(self, attrs):
        if not attrs:
            raise serializers.ValidationError(
                f"Nothing to change. Amendable fields: {self.amendable}."
            )

        # `quantity_received` is a production-order property; this serializer
        # also amends group orders, which have no receipts and so nothing to
        # orphan. getattr rather than a type check, so a third document type
        # that grows receipts is covered the day it does.
        order = self.context.get("order")
        if (
            attrs.get("status") == OrderStatus.CANCELLED
            and getattr(order, "quantity_received", 0) > 0
        ):
            raise serializers.ValidationError(
                {
                    "status": (
                        f"{order.number} cannot be cancelled: "
                        f"{order.quantity_received} units have already been "
                        "received against it. Close it instead."
                    )
                }
            )

        return attrs


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------



class ProductionOrderAmendSerializer(OrderAmendSerializer):
    """`OrderAmendSerializer`, plus the group order link.

    Attaching after the fact matters more than it looks. The link is what
    makes a group order mean anything — `reconcile()` counts the production
    orders pointing at it — and a production order raised without one leaves
    the requirement reading as wholly unmet. The create form did not ask for
    it for a while, so there are real orders out there that need attaching,
    and the alternative was cancelling a document that funds a Tailoring
    Center and raising it again.

    Nullable, so a wrong link can be undone. Nothing downstream depends on
    it: receipts point at the production order, not at the group order, so
    moving the link moves a report and nothing else.

    Not on `OrderAmendSerializer` itself, because that one also serves group
    orders, and a group order has no group order.
    """

    group_order = serializers.PrimaryKeyRelatedField(
        queryset=GroupOrder.objects.all(),
        required=False,
        allow_null=True,
        help_text="The group order this breaks down. Null detaches it.",
    )

    amendable = "status, due_in_warehouse_date, notes, group_order"

class ReceiptLineSerializer(serializers.ModelSerializer):
    """A receipt line, reading. `discrepancy` is the whole point of F20."""

    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    discrepancy = serializers.IntegerField(read_only=True)

    class Meta:
        model = ReceiptLine
        fields = (
            "id",
            "sku",
            "sku_number",
            "sku_description",
            "quantity_received",
            "quantity_on_packing_list",
            "discrepancy",
            "discrepancy_note",
        )


class ReceiptLineInputSerializer(serializers.Serializer):
    """A receipt line, writing.

    `quantity_on_packing_list` is optional because a handwritten packing list
    does not always give one — and "the paper did not say" is a different
    fact from "the paper agreed".
    """

    sku = serializers.PrimaryKeyRelatedField(queryset=Sku.objects.all())
    quantity_received = serializers.IntegerField(min_value=1)
    quantity_on_packing_list = serializers.IntegerField(
        min_value=0, required=False, allow_null=True
    )
    discrepancy_note = serializers.CharField(
        max_length=200, required=False, allow_blank=True
    )


class ReceiptSerializer(serializers.ModelSerializer):
    """A receipt, reading."""

    lines = ReceiptLineSerializer(many=True, read_only=True)
    production_order_number = serializers.CharField(
        source="production_order.number", read_only=True
    )
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)
    tailoring_center_name = serializers.CharField(
        source="tailoring_center.name", read_only=True
    )
    created_by_name = serializers.CharField(source="created_by.get_full_name", read_only=True)
    is_posted = serializers.BooleanField(read_only=True)
    has_discrepancy = serializers.BooleanField(read_only=True)

    class Meta:
        model = Receipt
        fields = (
            "id",
            "number",
            "production_order",
            "production_order_number",
            "warehouse_name",
            "tailoring_center_name",
            "packing_list_number",
            "carrier_name",
            "date_received",
            "notes",
            "posted_at",
            "is_posted",
            "has_discrepancy",
            "created_by",
            "created_by_name",
            "created_at",
            "lines",
        )
        read_only_fields = ("id", "number", "posted_at", "created_by", "created_at")


class ReceiptWriteSerializer(serializers.ModelSerializer):
    """Recording what arrived. Does not touch stock — posting does that."""

    lines = ReceiptLineInputSerializer(many=True, write_only=True)

    class Meta:
        model = Receipt
        fields = (
            "production_order",
            "packing_list_number",
            "carrier_name",
            "date_received",
            "notes",
            "lines",
        )

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("A receipt needs at least one line.")

        seen = set()
        duplicates = {
            line["sku"].number
            for line in value
            if line["sku"].pk in seen or seen.add(line["sku"].pk)
        }
        if duplicates:
            raise serializers.ValidationError(
                f"Each SKU may appear once. Repeated: {', '.join(sorted(duplicates))}."
            )
        return value


class OutstandingRowSerializer(serializers.Serializer):
    """One SKU on an order: ordered, received so far, still to come.

    `sku` is the id, because the receiving screen posts these rows straight
    back as receipt lines — without it the client would have to match on the
    SKU number, which is a display string.
    """

    # An IntegerField over the id rather than PrimaryKeyRelatedField: on a
    # plain Serializer the latter has no queryset to infer from, so the
    # generated schema types it as a string and the client cannot key a
    # lookup on it.
    sku = serializers.IntegerField(source="sku.id")
    sku_number = serializers.CharField(source="sku.number")
    sku_description = serializers.CharField(source="sku.description")
    sku_size = serializers.CharField(source="sku.size.name")
    ordered = serializers.IntegerField()
    shipped = serializers.IntegerField(
        help_text="What the TC's packing lists claimed they sent, across posted receipts."
    )
    received = serializers.IntegerField(help_text="What was actually counted.")
    outstanding = serializers.IntegerField()


# ---------------------------------------------------------------------------
# Costed reports — F55, F56
# ---------------------------------------------------------------------------


class GroupOrderTotalSerializer(serializers.Serializer):
    """The single figure at the top of the costed report — F55.

    Exists so `value` goes out as a **decimal string**, like every other money
    field in this API. The view used to return the raw dict, and DRF's JSON
    encoder turns a bare `Decimal` into a float — so the totals read
    `24000000.0` while the rows beneath them read `"24000000.00"`. A client
    summing money with exact arithmetic then had a float in the middle of it,
    which is the one thing the money handling on both sides exists to avoid.
    """

    orders = serializers.IntegerField()
    quantity = serializers.IntegerField()
    value = serializers.DecimalField(max_digits=18, decimal_places=2)


class GroupOrderCostedSerializer(serializers.Serializer):
    """One group order on the costed report — F55."""

    number = serializers.CharField()
    order_date = serializers.DateField()
    due_in_warehouse_date = serializers.DateField(allow_null=True)
    status = serializers.CharField()
    line_count = serializers.IntegerField()
    quantity = serializers.IntegerField(help_text="Units ordered.")
    value = serializers.DecimalField(
        max_digits=18, decimal_places=2, help_text="At the price agreed on the day."
    )


class ReceiptsByTailoringCenterSerializer(serializers.Serializer):
    """What one Tailoring Center delivered, costed — F56."""

    tailoring_center_id = serializers.IntegerField()
    tailoring_center_name = serializers.CharField()
    receipts = serializers.IntegerField()
    quantity = serializers.IntegerField(help_text="Units actually counted in.")
    value = serializers.DecimalField(max_digits=18, decimal_places=2)


class ReceiptCostedSerializer(serializers.Serializer):
    """One receipt on the costed report, for checking a total."""

    number = serializers.CharField()
    date_received = serializers.DateField()
    packing_list_number = serializers.CharField()
    tailoring_center_name = serializers.CharField(
        source="production_order.tailoring_center.name"
    )
    warehouse_name = serializers.CharField(source="production_order.warehouse.name")
    production_order_number = serializers.CharField(source="production_order.number")
    quantity = serializers.IntegerField()
    value = serializers.DecimalField(max_digits=18, decimal_places=2)

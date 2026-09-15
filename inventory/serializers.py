"""Serializers for the stock ledger.

Everything here is read-only. The ledger is written by services — a receipt
being posted, later an adjustment — never by a client sending a movement.
"""

from config.validators import CaseInsensitiveUniqueValidator
from catalog.models import Sku, Warehouse
from rest_framework import serializers

from .models import (
    InventoryAdjustment,
    ReasonCode,
    StockMovement,
    WarehouseTransfer,
    WarehouseTransferLine,
)


class StockMovementSerializer(serializers.ModelSerializer):
    """One ledger row. The audit trail (F48) is a list of these."""

    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)
    movement_type_display = serializers.CharField(
        source="get_movement_type_display", read_only=True
    )
    created_by_name = serializers.CharField(
        source="created_by.display_name", read_only=True
    )
    total_value = serializers.DecimalField(
        max_digits=16, decimal_places=2, read_only=True
    )

    class Meta:
        model = StockMovement
        fields = (
            "id",
            "number",
            "occurred_on",
            "warehouse",
            "warehouse_name",
            "sku",
            "sku_number",
            "sku_description",
            "quantity",
            "movement_type",
            "movement_type_display",
            "stock_status",
            "unit_value",
            "total_value",
            "document_number",
            "source",
            "destination",
            "created_by",
            "created_by_name",
            "created_at",
        )
        read_only_fields = fields


class StockLevelSerializer(serializers.Serializer):
    """A derived stock level — F47.

    Not a model. There is no stock-level table: this is a `Sum()` over the
    ledger, computed on read.
    """

    sku_id = serializers.IntegerField()
    sku_number = serializers.CharField(source="sku__number")
    sku_description = serializers.CharField(source="sku__description")
    warehouse_id = serializers.IntegerField()
    warehouse_name = serializers.CharField(source="warehouse__name")
    level = serializers.IntegerField(help_text="Units available to pick.")
    reserved = serializers.IntegerField(
        help_text=(
            "Units set aside for an order that has not shipped. Still "
            "physically on the shelf, so a physical count sees level + "
            "reserved, not level."
        )
    )
    value = serializers.DecimalField(
        max_digits=16, decimal_places=2, help_text="Value of the available stock."
    )


class ReorderAlertSerializer(serializers.Serializer):
    """A SKU at or below its reorder floor — F50."""

    sku_number = serializers.CharField(source="sku.number")
    sku_description = serializers.CharField(source="sku.description")
    warehouse_name = serializers.CharField(source="warehouse.name")
    level = serializers.IntegerField()
    minimum = serializers.IntegerField()
    shortfall = serializers.IntegerField(
        help_text="How far below the floor. Zero means exactly at it."
    )


class ReasonCodeSerializer(serializers.ModelSerializer):
    """An inventory adjustment reason code — F13.

    Writable, unlike everything else in this app. The ledger is written by
    services; this is master data Central Office maintains, and AsOne said
    there would be more codes than the four they listed.
    """

    code = serializers.CharField(
        max_length=20,
        validators=[CaseInsensitiveUniqueValidator(queryset=ReasonCode.objects.all())],
    )
    name = serializers.CharField(
        max_length=120,
        validators=[CaseInsensitiveUniqueValidator(queryset=ReasonCode.objects.all())],
    )

    class Meta:
        model = ReasonCode
        fields = ("id", "code", "name", "description", "direction", "is_active")


# ---------------------------------------------------------------------------
# Warehouse transfers — F25
# ---------------------------------------------------------------------------


class WarehouseTransferLineSerializer(serializers.ModelSerializer):
    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)

    class Meta:
        model = WarehouseTransferLine
        fields = ("id", "sku", "sku_number", "sku_description", "quantity", "unit_value")
        read_only_fields = ("id", "unit_value")


class WarehouseTransferLineInputSerializer(serializers.Serializer):
    sku = serializers.PrimaryKeyRelatedField(queryset=Sku.objects.all())
    quantity = serializers.IntegerField(min_value=1)


class WarehouseTransferSerializer(serializers.ModelSerializer):
    lines = WarehouseTransferLineSerializer(many=True, read_only=True)
    from_warehouse_name = serializers.CharField(source="from_warehouse.name", read_only=True)
    to_warehouse_name = serializers.CharField(source="to_warehouse.name", read_only=True)
    reason_code_name = serializers.CharField(
        source="reason_code.name", read_only=True, default=None
    )
    created_by_name = serializers.CharField(source="created_by.display_name", read_only=True)
    is_posted = serializers.BooleanField(read_only=True)

    class Meta:
        model = WarehouseTransfer
        fields = (
            "id",
            "number",
            "from_warehouse",
            "from_warehouse_name",
            "to_warehouse",
            "to_warehouse_name",
            "transfer_date",
            "reason_code",
            "reason_code_name",
            "notes",
            "posted_at",
            "is_posted",
            "created_by",
            "created_by_name",
            "created_at",
            "lines",
        )
        read_only_fields = ("id", "number", "posted_at", "created_by", "created_at")


class WarehouseTransferWriteSerializer(serializers.ModelSerializer):
    """Preparing a transfer. Does not move stock — posting does that."""

    lines = WarehouseTransferLineInputSerializer(many=True, write_only=True)

    class Meta:
        model = WarehouseTransfer
        fields = (
            "from_warehouse",
            "to_warehouse",
            "transfer_date",
            "reason_code",
            "notes",
            "lines",
        )

    def validate(self, attrs):
        if attrs["from_warehouse"] == attrs["to_warehouse"]:
            raise serializers.ValidationError(
                {"to_warehouse": "A transfer must be between two different warehouses."}
            )
        return attrs

    def validate_lines(self, value):
        if not value:
            raise serializers.ValidationError("A transfer needs at least one line.")

        seen = set()
        repeated = {
            line["sku"].number
            for line in value
            if line["sku"].pk in seen or seen.add(line["sku"].pk)
        }
        if repeated:
            raise serializers.ValidationError(
                f"Each SKU may appear once. Repeated: {', '.join(sorted(repeated))}."
            )
        return value


# ---------------------------------------------------------------------------
# Inventory adjustments — F23
# ---------------------------------------------------------------------------


class InventoryAdjustmentSerializer(serializers.ModelSerializer):
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)
    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    reason_code_code = serializers.CharField(source="reason_code.code", read_only=True)
    reason_code_name = serializers.CharField(source="reason_code.name", read_only=True)
    created_by_name = serializers.CharField(source="created_by.display_name", read_only=True)
    is_posted = serializers.BooleanField(read_only=True)

    class Meta:
        model = InventoryAdjustment
        fields = (
            "id",
            "number",
            "warehouse",
            "warehouse_name",
            "sku",
            "sku_number",
            "sku_description",
            "quantity",
            "reason_code",
            "reason_code_code",
            "reason_code_name",
            "adjustment_date",
            "unit_value",
            "notes",
            "posted_at",
            "is_posted",
            "created_by",
            "created_by_name",
            "created_at",
        )
        read_only_fields = ("id", "number", "unit_value", "posted_at", "created_by", "created_at")


class InventoryAdjustmentWriteSerializer(serializers.ModelSerializer):
    """Preparing an adjustment. Does not touch the ledger — posting does that."""

    class Meta:
        model = InventoryAdjustment
        fields = ("warehouse", "sku", "quantity", "reason_code", "adjustment_date", "notes")

    def validate(self, attrs):
        """Refuse an adjustment for a SKU with no catalog price.

        Mirrors the group order's line-pricing check: an adjustment that can
        never be valued should not be written down in the first place, not
        discovered as a failure only once someone tries to post it.
        """
        from catalog.services import PriceNotSet, price_for_sku

        try:
            price_for_sku(attrs["sku"], attrs["adjustment_date"])
        except PriceNotSet:
            raise serializers.ValidationError(
                {
                    "sku": (
                        f"{attrs['sku'].number} has no price on "
                        f"{attrs['adjustment_date']:%Y-%m-%d}, so this adjustment "
                        "cannot be valued."
                    )
                }
            )
        return attrs


# ---------------------------------------------------------------------------
# Physical count correction — F24
# ---------------------------------------------------------------------------


class CountCorrectionSerializer(serializers.Serializer):
    """Input for F24: what was actually counted, nothing else.

    No quantity-and-direction fields here — that comparison is the whole
    point of F24, and it happens in services.correct_count(), not from
    anything the caller supplies.
    """

    warehouse = serializers.PrimaryKeyRelatedField(queryset=Warehouse.objects.all())
    sku = serializers.PrimaryKeyRelatedField(queryset=Sku.objects.all())
    counted_quantity = serializers.IntegerField(
        min_value=0, help_text="What was actually found on the shelf."
    )
    adjustment_date = serializers.DateField(help_text="The date the count was taken.")
    notes = serializers.CharField(required=False, allow_blank=True, default="")


class CostedAdjustmentSerializer(serializers.Serializer):
    """One reason code's effect on the value of stock — F58.

    `value` is signed the way the ledger is: negative where stock left,
    positive where it came back, so the rows sum to the net effect.
    """

    reason_code = serializers.CharField()
    reason_name = serializers.CharField()
    adjustments = serializers.IntegerField(help_text="How many adjustments were posted.")
    units = serializers.IntegerField(help_text="Net units, signed.")
    value = serializers.DecimalField(max_digits=18, decimal_places=2)
    treatment = serializers.CharField(
        help_text="How this is treated financially. Unanswered until AsOne settles question Q6."
    )

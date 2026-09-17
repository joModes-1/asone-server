"""The Settings screen — one row, read by everyone, written by leads only."""

from rest_framework import serializers

from catalog.models import Warehouse

from .models import Settings


class WarehouseSummarySerializer(serializers.ModelSerializer):
    """Thin, the same reasoning as `accounts.serializers.WarehouseSummarySerializer`:
    just enough for the Settings screen to label the choice without a second
    request to the catalog app."""

    class Meta:
        model = Warehouse
        fields = ("id", "name")
        read_only_fields = fields


class SettingsSerializer(serializers.ModelSerializer):
    """The whole Settings screen, one document.

    `default_warehouse` accepts an id on write and returns the summary object
    on read, the same split `UserAdminSerializer` uses for a user's own site.
    """

    default_warehouse = serializers.PrimaryKeyRelatedField(
        queryset=Warehouse.objects.filter(is_active=True), allow_null=True, required=False
    )
    default_warehouse_detail = WarehouseSummarySerializer(source="default_warehouse", read_only=True)
    timezone_display = serializers.CharField(source="get_timezone_display", read_only=True)
    currency_display = serializers.CharField(source="get_currency_display", read_only=True)
    default_paper_size_display = serializers.CharField(
        source="get_default_paper_size_display", read_only=True
    )
    packing_list_layout_display = serializers.CharField(
        source="get_packing_list_layout_display", read_only=True
    )
    updated_by_name = serializers.CharField(
        source="updated_by.get_full_name", read_only=True, default=None
    )

    class Meta:
        model = Settings
        fields = (
            "organization_name",
            "default_warehouse",
            "default_warehouse_detail",
            "timezone",
            "timezone_display",
            "currency",
            "currency_display",
            "default_minimum_stock_threshold",
            "critical_safety_buffer_percent",
            "auto_trigger_tailoring_center_reorder",
            "low_stock_alerts_enabled",
            "receipt_discrepancy_alerts_enabled",
            "backorder_allocation_alerts_enabled",
            "auto_sync_interval_minutes",
            "offline_data_retention_days",
            "default_paper_size",
            "default_paper_size_display",
            "packing_list_layout",
            "packing_list_layout_display",
            "updated_at",
            "updated_by_name",
        )
        read_only_fields = ("updated_at", "updated_by_name")

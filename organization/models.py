"""AsOne's own configuration — one row, not per-user, not per-site.

Organization Name, the default warehouse hub, timezone and currency are
facts about the company running this system, the same way there is one
AsOne Logistics, not one per person signed into it. Settings.load() is the
only way anything should read this — see its docstring for why there is
exactly one row.
"""

from django.core.exceptions import ValidationError
from django.db import models


class Settings(models.Model):
    """The single settings row. Enforced by ``pk=1`` — see ``load()``.

    Every field here is either a stored fact (organization name, default
    warehouse, timezone, currency) or a stored preference with no code
    reading it yet (the sync and printing fields below) — each says which
    it is in its own help text, so nobody assumes a toggle does something
    it does not.
    """

    class Timezone(models.TextChoices):
        EAT = "Africa/Kampala", "East Africa Time (EAT) / Kampala (UTC+3)"

    class Currency(models.TextChoices):
        UGX = "UGX", "Ugandan Shilling (UGX)"

    class PaperSize(models.TextChoices):
        A4 = "A4", "A4 (Standard Ugandan Format)"
        LETTER = "LETTER", "US Letter"

    class PackingListLayout(models.TextChoices):
        SKU_GROUPED = "SKU_GROUPED", "Standard SKU-Grouped"
        ORDER_GROUPED = "ORDER_GROUPED", "Grouped by Order"

    # ---------------------------------------------------------------------
    # General Settings
    # ---------------------------------------------------------------------
    organization_name = models.CharField(max_length=200, default="AsOne Logistics")
    default_warehouse = models.ForeignKey(
        "catalog.Warehouse",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        help_text=(
            "The site a new session's warehouse filter starts on. Nullable: "
            "a fresh install has no warehouses yet, and 'all warehouses' is a "
            "legitimate starting point too."
        ),
    )
    timezone = models.CharField(
        max_length=64, choices=Timezone.choices, default=Timezone.EAT
    )
    currency = models.CharField(
        max_length=8, choices=Currency.choices, default=Currency.UGX
    )

    # ---------------------------------------------------------------------
    # Inventory Parameters
    #
    # A suggested starting point for a new SKU's own MinimumStockLevel, not
    # an override of it — that model is per-SKU-per-warehouse (see its own
    # docstring) because two sites serve different numbers of schools, and
    # nothing here changes an existing row when this default changes.
    # ---------------------------------------------------------------------
    default_minimum_stock_threshold = models.PositiveIntegerField(
        default=150,
        help_text=(
            "Suggested starting value when setting a new SKU's minimum stock "
            "level. Does not change any level already set."
        ),
    )
    critical_safety_buffer_percent = models.PositiveIntegerField(
        default=10,
        help_text="How far below the minimum counts as critical, as a percentage of it.",
    )
    auto_trigger_tailoring_center_reorder = models.BooleanField(
        default=True,
        help_text=(
            "Stored for a future automatic-reorder feature. Nothing places a "
            "production order from this today — see procurement/services.py "
            "for how production orders are actually raised."
        ),
    )

    # ---------------------------------------------------------------------
    # System Alerts
    #
    # These genuinely gate `dashboard.services.needs_attention()` — turning
    # one off removes that alert kind from the Needs Attention panel and the
    # bell for everyone, org-wide. Not a per-user notification preference:
    # there is no per-user notification delivery in this system at all (the
    # bell is computed live, not stored — see notifications()'s own
    # docstring), so this is the only lever that exists to have.
    # ---------------------------------------------------------------------
    low_stock_alerts_enabled = models.BooleanField(default=True)
    receipt_discrepancy_alerts_enabled = models.BooleanField(default=True)
    backorder_allocation_alerts_enabled = models.BooleanField(default=True)

    # ---------------------------------------------------------------------
    # Synchronization
    #
    # Stored only. This is a plain server-rendered web app with no offline
    # cache anywhere in it — nothing reads either field today. Kept because
    # the design calls for them and a value that round-trips honestly is
    # better than a control that silently does nothing when saved, but
    # wiring an actual offline mode is a separate, much larger feature.
    # ---------------------------------------------------------------------
    auto_sync_interval_minutes = models.PositiveIntegerField(
        default=5,
        help_text="Stored for a future offline mode. No code reads this value today.",
    )
    offline_data_retention_days = models.PositiveIntegerField(
        default=30,
        help_text="Stored for a future offline mode. No code reads this value today.",
    )

    # ---------------------------------------------------------------------
    # Printing Preferences
    #
    # Stored only, for the same reason as Synchronization above: there is no
    # packing-list print or export feature in the app yet for these to
    # configure.
    # ---------------------------------------------------------------------
    default_paper_size = models.CharField(
        max_length=16, choices=PaperSize.choices, default=PaperSize.A4
    )
    packing_list_layout = models.CharField(
        max_length=16,
        choices=PackingListLayout.choices,
        default=PackingListLayout.SKU_GROUPED,
    )

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        "accounts.User", null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )

    class Meta:
        verbose_name = "Settings"
        verbose_name_plural = "Settings"

    def clean(self):
        if self.pk not in (None, 1):
            raise ValidationError("There is only one Settings row. Edit it instead of adding another.")

    def save(self, *args, **kwargs):
        self.pk = 1
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("The Settings row cannot be deleted.")

    @classmethod
    def load(cls) -> "Settings":
        """The one settings row, creating it with its defaults on first read.

        Never call ``Settings.objects.get(...)`` or construct one directly —
        this is the only path that guarantees the singleton exists and that
        every reader sees the same row.
        """
        settings, _ = cls.objects.get_or_create(pk=1)
        return settings

    def __str__(self):
        return self.organization_name

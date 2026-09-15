"""Query filters for adjustments and transfers.

`filterset_fields` gets both of these nearly right and then fails on the two
questions the screens actually ask.

**"Show me the last 30 days."** `adjustment_date` as a plain field filters on
one exact day. A date range needs two bounds, so it needs a declared filter.

**"Which of these have not been posted yet?"** `posted_at` as a plain field
filters on an exact timestamp — a value no client can supply and nobody would
want to. What matters is whether it is set at all: an unposted document is a
draft that has moved no stock, and the difference between the two is the
whole reason creating and posting are separate steps.

Declared here rather than inline on the viewsets so the two read alike, and
so a lookup added for one is not quietly missing from the other.
"""

import django_filters

from .models import InventoryAdjustment, WarehouseTransfer


class PostedFilterMixin(django_filters.FilterSet):
    """`?posted=true|false` — committed to the ledger, or still a draft."""

    posted = django_filters.BooleanFilter(
        field_name="posted_at",
        lookup_expr="isnull",
        exclude=True,
        label="Posted to the ledger",
    )


class InventoryAdjustmentFilter(PostedFilterMixin):
    """F23. `date_from`/`date_to` are inclusive, and either may stand alone."""

    date_from = django_filters.DateFilter(field_name="adjustment_date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="adjustment_date", lookup_expr="lte")

    class Meta:
        model = InventoryAdjustment
        fields = ("warehouse", "sku", "reason_code", "posted", "date_from", "date_to")


class WarehouseTransferFilter(PostedFilterMixin):
    """F25. Source and destination are separate filters, never one "site"."""

    date_from = django_filters.DateFilter(field_name="transfer_date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="transfer_date", lookup_expr="lte")

    class Meta:
        model = WarehouseTransfer
        fields = ("from_warehouse", "to_warehouse", "posted", "date_from", "date_to")

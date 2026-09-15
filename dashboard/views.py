"""HTTP for the dashboard — F62.

Four read-only endpoints behind one screen. Views here do even less than
usual: resolve which warehouse is being asked about, call a service, return
it.

## Who sees this

The leads, Finance, and warehouse staff for their own site. **Not school
staff** — the checklist gives them a dashboard too ("own schools"), but the
tiles here are warehouse tiles: stock in bins, trucks loading, SKUs below
their floor. A school's dashboard is a different screen with different
numbers, and it is not built. Pretending this one serves them would give
them a page of figures about somebody else's warehouse.
"""

import csv

from django.http import HttpResponse
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import AUTHENTICATED, ALL_SITE_ROLES, has_role
from catalog.models import Warehouse
from rest_framework.permissions import BasePermission

from . import services
from .serializers import (
    ActivityEventSerializer,
    AttentionAlertSerializer,
    DashboardSummarySerializer,
    InventoryByWarehouseSerializer,
    NotificationsSerializer,
    OrderVolumeSerializer,
    SchoolDashboardSerializer,
    WeeklyReportSerializer,
)

Role = User.Role


class CanSeeWarehouseDashboard(BasePermission):
    """Leads, Finance, and warehouse staff for their own site.

    School staff are excluded — see the module docstring. If AsOne wants a
    school-facing dashboard, it is a different endpoint, not this one with a
    wider audience.
    """

    message = "Your role does not have a warehouse dashboard."

    def has_permission(self, request, view) -> bool:
        return has_role(
            request.user, *ALL_SITE_ROLES, Role.FINANCE, Role.WAREHOUSE_STAFF
        )


class CanSeeSchoolDashboard(BasePermission):
    """School staff, and only school staff.

    The mirror of `CanSeeWarehouseDashboard`. Not widened to the leads: this
    endpoint reads `request.user.school`, so there is nothing for a lead to
    see through it — a lead wanting a school's position reads the order
    reports, which are scoped and already theirs.
    """

    message = "Your role does not have a school dashboard."

    def has_permission(self, request, view) -> bool:
        return has_role(request.user, Role.SCHOOL_STAFF)


class _WarehouseScoped(APIView):
    """Resolves which warehouse the caller is asking about.

    Warehouse staff are pinned to their own whatever they ask for, the same
    as every other site-scoped endpoint. The all-locations roles may pass
    `?warehouse=` to switch, which is the selector at the top of the design,
    and see every site when they do not.
    """

    permission_classes = [*AUTHENTICATED, CanSeeWarehouseDashboard]

    def warehouse_for(self, request):
        if request.user.role == Role.WAREHOUSE_STAFF:
            # A warehouse account with no warehouse is a broken account, not
            # a licence to see every site.
            if request.user.warehouse_id is None:
                raise DRFValidationError(
                    {
                        "warehouse": (
                            "This account has no warehouse set, so there is "
                            "nothing to show. Ask a lead to correct it."
                        )
                    }
                )
            return request.user.warehouse

        raw = request.query_params.get("warehouse")
        if not raw:
            return None
        return get_object_or_404(Warehouse.objects.all(), pk=raw)


WAREHOUSE_PARAM = OpenApiParameter(
    "warehouse",
    int,
    description=(
        "Which warehouse to report on. Ignored for warehouse staff, who "
        "always see their own. Omitted by a lead means every site."
    ),
)


@extend_schema(
    tags=["Dashboard"],
    summary="The headline figures",
    parameters=[WAREHOUSE_PARAM],
    responses=DashboardSummarySerializer,
    description=(
        "F62 — the six tiles across the top of the dashboard, in one call.\n\n"
        "One call rather than six because this is the first screen anybody "
        "opens, and six round trips over a rural connection is the difference "
        "between a screen that loads and one that does not.\n\n"
        "Every figure is recomputed from the app that owns it, so a tile can "
        "never disagree with the screen it links to."
    ),
)
class SummaryView(_WarehouseScoped):
    def get(self, request):
        rows = services.summary(self.warehouse_for(request))
        return Response(DashboardSummarySerializer(rows).data)


@extend_schema(
    tags=["Dashboard"],
    summary="Needs attention",
    parameters=[WAREHOUSE_PARAM],
    responses=AttentionAlertSerializer(many=True),
    description=(
        "The alert list — four kinds of thing somebody should look at, each "
        "as a count and a sentence.\n\n"
        "Rows with a count of zero are **omitted**, so an empty list means "
        "there is genuinely nothing to do. `kind` is stable and is what the "
        "frontend should route on; `message` is for reading, not parsing."
    ),
)
class AttentionView(_WarehouseScoped):
    def get(self, request):
        rows = services.needs_attention(self.warehouse_for(request), user=request.user)
        return Response(AttentionAlertSerializer(rows, many=True).data)


@extend_schema(
    tags=["Dashboard"],
    summary="Recent activity",
    parameters=[
        WAREHOUSE_PARAM,
        OpenApiParameter("limit", int, description="Rows to return. Default 10, max 50."),
    ],
    responses=ActivityEventSerializer(many=True),
    description=(
        "What has happened at this site lately, newest first — receipts "
        "confirmed, orders shipped, stock adjusted, production orders "
        "raised, backorders released.\n\n"
        "Covers the last seven days. For anything older, the movement ledger "
        "is the record; this is a dashboard, not history."
    ),
)
class ActivityView(_WarehouseScoped):
    def get(self, request):
        try:
            limit = min(int(request.query_params.get("limit", 10)), 50)
        except ValueError:
            raise DRFValidationError({"limit": "Must be a whole number."}) from None

        rows = services.recent_activity(self.warehouse_for(request), limit=limit)
        return Response(ActivityEventSerializer(rows, many=True).data)


@extend_schema(
    tags=["Dashboard"],
    summary="Daily order volume",
    parameters=[
        WAREHOUSE_PARAM,
        OpenApiParameter("from", str, description="Inclusive start date, YYYY-MM-DD."),
        OpenApiParameter("to", str, description="Inclusive end date, YYYY-MM-DD."),
    ],
    responses=OrderVolumeSerializer,
    description=(
        "Orders placed per day, for the chart. Cancelled orders are "
        "excluded — they were withdrawn, and counting them would overstate "
        "demand.\n\n"
        "**Days with no orders are absent, not zero.** Whether a quiet day "
        "should show as a gap or a flat line is a decision about the axis, "
        "and the chart is better placed to make it than this endpoint."
    ),
)
class OrderVolumeView(_WarehouseScoped):
    def get(self, request):
        rows = services.daily_order_volume(
            self.warehouse_for(request),
            date_from=_date_param(request, "from"),
            date_to=_date_param(request, "to"),
        )
        return Response(OrderVolumeSerializer(rows).data)


@extend_schema(
    tags=["Dashboard"],
    summary="Notifications — the bell",
    parameters=[WAREHOUSE_PARAM],
    responses=NotificationsSerializer,
    description=(
        "The badge count in the header and the list behind it.\n\n"
        "**These are derived, not stored.** They are the same conditions "
        "`attention/` reports, counted — which is what the design shows: the "
        "bell reads 4 and the Needs Attention panel reads \"4 ALERTS\".\n\n"
        "So `unread_count` **does not fall when somebody reads them.** It "
        "falls when the low stock is replenished, the order is picked, the "
        "receipt is reconciled. That suits an operations queue — a warning "
        "you can dismiss without acting is a warning that stops working — "
        "but it is not how a social-media bell behaves.\n\n"
        "There is no per-user read state and no history. If AsOne wants "
        "dismissible notifications, that is a stored model, not a wider "
        "version of this."
    ),
)
class NotificationsView(_WarehouseScoped):
    def get(self, request):
        rows = services.notifications(self.warehouse_for(request), user=request.user)
        return Response(NotificationsSerializer(rows).data)


@extend_schema(
    tags=["Dashboard"],
    summary="Inventory by warehouse",
    parameters=[
        OpenApiParameter("as_of", str, description="Levels as at a date, YYYY-MM-DD."),
    ],
    responses=InventoryByWarehouseSerializer,
    description=(
        "Units, value and distinct SKUs at each site — the panel with a bar "
        "per warehouse.\n\n"
        "Every warehouse appears, including one holding nothing: a site "
        "missing from the list reads as \"no data\" when the truth is \"no "
        "stock\", and those are different problems.\n\n"
        "`total_skus` counts each SKU once however many sites hold it, so it "
        "is **not** the sum of the per-site counts."
    ),
)
class InventoryByWarehouseView(_WarehouseScoped):
    """Deliberately not warehouse-scoped: the panel compares sites, so
    showing a clerk only their own would leave one bar and nothing to
    compare it with. They may already see other warehouses' totals on the
    stock-levels report, so this reveals nothing new."""

    def get(self, request):
        rows = services.inventory_by_warehouse(as_of=_date_param(request, "as_of"))
        return Response(InventoryByWarehouseSerializer(rows).data)


@extend_schema(
    tags=["Dashboard"],
    summary="The weekly inventory report",
    parameters=[
        WAREHOUSE_PARAM,
        OpenApiParameter("from", str, description="Inclusive start, YYYY-MM-DD."),
        OpenApiParameter("to", str, description="Inclusive end, YYYY-MM-DD."),
    ],
    responses=WeeklyReportSerializer,
    description=(
        "What moved, per SKU per warehouse, over a week — opening, "
        "received, adjusted, transferred, picked, shipped, returned, "
        "closing, and what the closing stock is worth.\n\n"
        "**Opening plus the movements equals closing on every row.** That is "
        "what makes it a report rather than a list.\n\n"
        "Defaults to the **last complete week**, Monday to Sunday. The card "
        "says the report is ready, and a report is only ready once its week "
        "is over — reporting the current week would give a different answer "
        "every time somebody opened it.\n\n"
        "`/download/` returns the same thing as a spreadsheet."
    ),
)
class WeeklyReportView(_WarehouseScoped):
    def get(self, request):
        rows = services.weekly_inventory_report(
            self.warehouse_for(request),
            date_from=_date_param(request, "from"),
            date_to=_date_param(request, "to"),
        )
        return Response(WeeklyReportSerializer(rows).data)


@extend_schema(
    tags=["Dashboard"],
    summary="Download the weekly report as a spreadsheet",
    parameters=[
        WAREHOUSE_PARAM,
        OpenApiParameter("from", str, description="Inclusive start, YYYY-MM-DD."),
        OpenApiParameter("to", str, description="Inclusive end, YYYY-MM-DD."),
    ],
    responses={(200, "text/csv"): OpenApiTypes.BINARY},
    description=(
        "The same report as CSV, which opens in Excel — which is what "
        "Central Office will actually do with it.\n\n"
        "The filename carries the period and the site, so a folder of these "
        "stays readable months later."
    ),
)
class WeeklyReportDownloadView(WeeklyReportView):
    def get(self, request):
        warehouse = self.warehouse_for(request)
        report = services.weekly_inventory_report(
            warehouse,
            date_from=_date_param(request, "from"),
            date_to=_date_param(request, "to"),
        )

        site = warehouse.name.lower().replace(" ", "-") if warehouse else "all-sites"
        filename = (
            f"asone-inventory-{site}-{report['date_from']}-to-{report['date_to']}.csv"
        )

        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="{filename}"'

        writer = csv.writer(response)
        writer.writerow(
            [
                "Warehouse", "SKU", "Description", "Opening", "Received",
                "Adjusted", "Transferred", "Picked", "Shipped", "Returned",
                "Closing", "Value (UGX)",
            ]
        )
        for row in report["rows"]:
            writer.writerow(
                [
                    row["warehouse"], row["sku_number"], row["description"],
                    row["opening"], row["received"], row["adjusted"],
                    row["transferred"], row["picked"], row["shipped"],
                    row["returned"], row["closing"], row["value"],
                ]
            )
        writer.writerow([])
        writer.writerow(
            ["", "", "Total", "", "", "", "", "", "", "",
             report["total_closing_units"], report["total_closing_value"]]
        )
        return response


def _date_param(request, name):
    """Read a `YYYY-MM-DD` query parameter, or None.

    A date that will not parse is a 400 naming the parameter, never a
    silently ignored filter — a chart quietly covering all time because
    somebody typed "01/09/2026" is worse than an error.
    """
    from datetime import date

    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise DRFValidationError(
            {name: f"'{raw}' is not a date. Use YYYY-MM-DD."}
        ) from None


@extend_schema(tags=["Dashboard"])
class SchoolDashboardView(APIView):
    """F62 for a school — the other dashboard.

    Every tile above this one is a warehouse tile: units in bins, SKUs under
    their floor, what is loading today. A school holds no stock and runs no
    warehouse, so those figures describe somebody else's building. This
    endpoint exists so that "the school gets a dashboard too" does not have
    to mean widening `CanSeeWarehouseDashboard` and handing a school clerk a
    page of numbers that are not about them.

    What a school is told is the state of its own paperwork: what it owes,
    what the warehouse is working on, **what has been sent and not
    confirmed**, and what is stuck on a backorder. The third of those is the
    only one it can act on, and it is the reason Shipped and Completed are
    separate statuses.

    Scoped to the caller's own school and nothing else — there is no
    `?school=` parameter, deliberately. A school clerk has exactly one
    school, and a lead wanting to look at a school's position has the order
    reports, which are already scoped and already theirs to read.
    """

    permission_classes = [*AUTHENTICATED, CanSeeSchoolDashboard]

    @extend_schema(
        summary="The school's own dashboard",
        responses=SchoolDashboardSerializer,
        description=(
            "Orders by state, what is owed, parcels awaiting confirmation, "
            "and outstanding backorders — all for the caller's own school.\n\n"
            "`deliveries_to_confirm` is the actionable list: each row is a "
            "parcel that left a warehouse and that nobody has said arrived. "
            "Confirming one is `POST /api/orders/school-orders/{id}/"
            "confirm-receipt/`, and the order becomes **Completed** once "
            "every parcel on it is confirmed."
        ),
    )
    def get(self, request):
        school = request.user.school
        if school is None:
            # A school account with no school is a broken account. Saying so
            # is better than an empty dashboard that looks like a school with
            # no orders.
            raise DRFValidationError(
                {
                    "school": (
                        "This account is not attached to a school, so it has "
                        "no dashboard. Ask a lead to set one."
                    )
                }
            )

        data = services.school_dashboard(school)
        return Response(SchoolDashboardSerializer(data).data)

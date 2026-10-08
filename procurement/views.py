"""HTTP layer for ordering from the Tailoring Centers.

Two permission shapes appear here, and the difference is the whole point of
AsOne's matrix:

    Group orders      leads write, Finance reads. No warehouse involvement.
    Production orders leads write; warehouse staff work with **their own
                      warehouse's** orders; Finance reads all of them.

The second needs `scope_to_user_site()` as well as a permission class. A
permission class lets a Namayemba clerk open the production orders screen;
only scoping stops them reading Serere's.
"""

from datetime import date

from django.db.models import Prefetch
from drf_spectacular.utils import OpenApiParameter, extend_schema
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import (
    AUTHENTICATED,
    CanViewFinancialReports,
    CanReceiveAndShip,
    MasterDataAccess,
    scope_to_user_site,
)

from . import reports, services
from .models import (
    GroupOrder,
    GroupOrderLine,
    ProductionOrder,
    ProductionOrderLine,
    Receipt,
    ReceiptLine,
)
from .serializers import (
    GroupOrderCostedSerializer,
    GroupOrderTotalSerializer,
    GroupOrderSerializer,
    OrderAmendSerializer,
    ProductionOrderAmendSerializer,
    OutstandingRowSerializer,
    ReceiptCostedSerializer,
    ReceiptSerializer,
    ReceiptWriteSerializer,
    ReceiptsByTailoringCenterSerializer,
    GroupOrderWriteSerializer,
    ProductionOrderSerializer,
    ProductionOrderWriteSerializer,
    ReconciliationRowSerializer,
)

Role = User.Role


class OrderViewSetMixin:
    """Shared behaviour for the two order documents.

    Orders are **never deleted**. They are a financial record — a group order
    funds the Tailoring Centers, and a production order is a commitment to
    one. Cancel by setting the status; the number then stays retired, which
    is the point of drawing it from a sequence.
    """

    http_method_names = ["get", "post", "patch", "head", "options"]

    #: Set by each viewset. Used to render the response after a write, so the
    #: client gets the order number and totals rather than an echo of what it
    #: sent — the write serializer has no `number`, because the system
    #: assigns it.
    read_serializer_class = None

    def create(self, request, *args, **kwargs):
        """Create through the service, respond with the full document.

        DRF would render the response with the *write* serializer, which
        omits `number`, `id` and the saved lines. A client would then have to
        re-fetch just to learn what it had created.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        order = self.create_order(serializer.validated_data)

        return Response(
            self.read_serializer_class(order).data, status=status.HTTP_201_CREATED
        )

    def create_order(self, data):
        """Raise the order. Implemented per document type."""
        raise NotImplementedError

    def partial_update(self, request, *args, **kwargs):
        """Amend the header. Lines are F18 and are not editable yet."""
        order = self.get_object()

        # `get_serializer_class()`, not `OrderAmendSerializer` by name — a
        # production order takes one more field than a group order (the link
        # to its group order), and hardcoding the base class here meant the
        # subclass was declared, returned by `get_serializer_class`, and then
        # quietly ignored on the one code path that uses it.
        #
        # The order goes in the context so the serializer can refuse to
        # cancel one that goods have already arrived against.
        serializer = self.get_serializer_class()(
            data=request.data, context={"order": order}
        )
        serializer.is_valid(raise_exception=True)

        for field, value in serializer.validated_data.items():
            setattr(order, field, value)
        order.save(update_fields=[*serializer.validated_data, "status"])

        return Response(self.read_serializer_class(order).data)


@extend_schema(tags=["Procurement — group orders"])
class GroupOrderViewSet(OrderViewSetMixin, viewsets.ModelViewSet):
    """F16 — the consolidated requirement across all Tailoring Centers.

    Leads write; Finance reads (the matrix gives them costed reports on group
    orders). Warehouse and school staff have no access at all.
    """

    permission_classes = [*AUTHENTICATED, MasterDataAccess]
    read_roles = (Role.FINANCE,)
    filterset_fields = ("status", "order_date")

    def get_queryset(self):
        return GroupOrder.objects.select_related("created_by").prefetch_related(
            Prefetch(
                "lines",
                queryset=GroupOrderLine.objects.select_related("sku", "sku__garment"),
            )
        )

    read_serializer_class = GroupOrderSerializer

    def get_serializer_class(self):
        if self.action == "create":
            return GroupOrderWriteSerializer
        if self.action == "partial_update":
            return OrderAmendSerializer
        return GroupOrderSerializer

    @extend_schema(
        summary="Raise a group order",
        request=GroupOrderWriteSerializer,
        responses={201: GroupOrderSerializer},
        description=(
            "Header and lines in one request, written in one transaction.\n\n"
            "Leave `unit_price` off a line and the garment's price on the "
            "order date is copied onto it and fixed there. A later reprice "
            "does not restate what this order was worth."
        ),
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def create_order(self, data):
        return services.create_group_order(created_by=self.request.user, **data)

    @extend_schema(
        summary="Group order against its production orders",
        responses=ReconciliationRowSerializer(many=True),
        description=(
            "AsOne's rule is that production orders \"should initially sum up "
            "to the Group Order\".\n\n"
            "Reported, not enforced: a warehouse ordering a little extra for "
            "safety stock is a judgement call for a person, not an error for "
            "the system. A negative `difference` means the Tailoring Centers "
            "have been asked for less than the requirement — the case worth "
            "chasing. Cancelled production orders are excluded."
        ),
    )
    @action(detail=True, methods=["get"])
    def reconciliation(self, request, pk=None):
        rows = services.reconcile(self.get_object())
        return Response(ReconciliationRowSerializer(rows, many=True).data)


@extend_schema(tags=["Procurement — production orders"])
class ProductionOrderViewSet(OrderViewSetMixin, viewsets.ModelViewSet):
    """F17 — one warehouse's order on one Tailoring Center.

    A warehouse may order from any TC, not only its primary one (p.4), so the
    Tailoring Center is chosen per order.
    """

    permission_classes = [*AUTHENTICATED, MasterDataAccess]
    read_roles = (Role.WAREHOUSE_STAFF, Role.FINANCE)
    filterset_fields = ("status", "tailoring_center", "warehouse", "group_order")

    def get_queryset(self):
        queryset = ProductionOrder.objects.select_related(
            "tailoring_center", "warehouse", "group_order", "created_by"
        ).prefetch_related(
            Prefetch(
                "lines",
                queryset=ProductionOrderLine.objects.select_related(
                    "sku", "sku__garment"
                ),
            ),
            # `fulfilment_status` sums posted receipt lines in Python. Without
            # this the list is two queries per row.
            "receipts__lines",
        )
        # A permission class opens the screen; only this stops a Namayemba
        # clerk reading Serere's orders.
        return scope_to_user_site(queryset, self.request.user, warehouse_field="warehouse")

    read_serializer_class = ProductionOrderSerializer

    def get_serializer_class(self):
        if self.action == "create":
            return ProductionOrderWriteSerializer
        if self.action == "partial_update":
            return ProductionOrderAmendSerializer
        return ProductionOrderSerializer

    @extend_schema(
        summary="Raise a production order",
        request=ProductionOrderWriteSerializer,
        responses={201: ProductionOrderSerializer},
        description=(
            "Header and lines in one request, written in one transaction.\n\n"
            "`group_order` is optional: the first season's orders break down "
            "a group order, but reorders and emergency orders later in the "
            "year have none."
        ),
    )
    def create(self, request, *args, **kwargs):
        return super().create(request, *args, **kwargs)

    def create_order(self, data):
        return services.create_production_order(created_by=self.request.user, **data)


@extend_schema(
    tags=["Procurement — production orders"],
    summary="Open production orders",
    responses=ProductionOrderSerializer(many=True),
    description=(
        "F22 — orders placed on the Tailoring Centers but not yet closed.\n\n"
        "Warehouse staff see their own warehouse only. Receipts are not built "
        "yet, so \"open\" currently means the status is Open; once receipts "
        "exist this should also cover orders fully received but not closed."
    ),
)
class OpenProductionOrderView(APIView):
    permission_classes = [*AUTHENTICATED, MasterDataAccess]
    read_roles = (Role.WAREHOUSE_STAFF, Role.FINANCE)

    def get(self, request):
        queryset = scope_to_user_site(
            ProductionOrder.objects.select_related(
                "tailoring_center", "warehouse", "group_order", "created_by"
            ).prefetch_related("lines__sku", "receipts__lines"),
            request.user,
            warehouse_field="warehouse",
        )
        orders = services.open_production_orders(queryset)
        return Response(ProductionOrderSerializer(orders, many=True).data)


# ---------------------------------------------------------------------------
# Receipts
# ---------------------------------------------------------------------------


@extend_schema(tags=["Procurement — receipts"])
class ReceiptViewSet(viewsets.ModelViewSet):
    """F19, F20, F21 — what arrived, and what it adds to stock.

    Unlike the order documents, **warehouse staff write here.** The matrix
    gives them "Warehouse Receiving and Shipping" for their own warehouse,
    and receiving is the one inbound step that happens at the warehouse
    rather than at Central Office — the Tailoring Centers are not system
    users, so a clerk keys in their handwritten packing list.

    Scoped by warehouse through the production order, so a Namayemba clerk
    cannot see or post against Serere's deliveries.
    """

    permission_classes = [*AUTHENTICATED, CanReceiveAndShip]
    filterset_fields = ("production_order", "posted_at")
    # Receipts are never deleted: once posted they are the source of ledger
    # rows, and before posting they are still a record that a delivery was
    # keyed in. An unwanted one is superseded, not erased.
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        queryset = Receipt.objects.select_related(
            "production_order__warehouse",
            "production_order__tailoring_center",
            "created_by",
        ).prefetch_related(
            Prefetch(
                "lines",
                queryset=ReceiptLine.objects.select_related("sku", "sku__garment"),
            )
        )
        return scope_to_user_site(
            queryset, self.request.user, warehouse_field="production_order__warehouse"
        )

    def get_serializer_class(self):
        return ReceiptWriteSerializer if self.action == "create" else ReceiptSerializer

    @extend_schema(
        summary="Record a delivery",
        request=ReceiptWriteSerializer,
        responses={201: ReceiptSerializer},
        description=(
            "Records what arrived. **Does not change stock** — posting does "
            "that, as a separate step.\n\n"
            "The two are separate because AsOne's flow has the warehouse "
            "check the delivery against the Tailoring Center's handwritten "
            "packing list and resolve differences first. Send "
            "`quantity_on_packing_list` alongside `quantity_received` and the "
            "difference is recorded rather than argued away.\n\n"
            "Every SKU must appear on the production order."
        ),
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = dict(serializer.validated_data)
        order = data.pop("production_order")
        lines = data.pop("lines")

        # A clerk must not be able to receive against another warehouse's
        # order — the queryset scopes reads, this scopes writes.
        if not scope_to_user_site(
            ProductionOrder.objects.filter(pk=order.pk),
            request.user,
            warehouse_field="warehouse",
        ).exists():
            raise PermissionDenied("That production order is not for your warehouse.")

        try:
            receipt = services.create_receipt(
                production_order=order, lines=lines, created_by=request.user, **data
            )
        except services.NotOnTheOrder as exc:
            raise DRFValidationError({"lines": str(exc)}) from exc

        return Response(
            ReceiptSerializer(receipt).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        summary="Post a receipt to inventory",
        request=None,
        responses={200: ReceiptSerializer},
        description=(
            "F21 — writes one permanent ledger row per line and raises stock.\n\n"
            "Each row is valued at the price on the production order line, "
            "not today's price list: the stock is worth what was paid for "
            "it.\n\n"
            "Can only be done once. A receipt posted twice would double the "
            "stock, and the ledger is append-only, so there is no way to take "
            "it back — a miscount is corrected with an inventory adjustment."
        ),
    )
    @action(detail=True, methods=["post"])
    def post_to_inventory(self, request, pk=None):
        receipt = self.get_object()

        try:
            services.post_receipt(receipt, posted_by=request.user)
        except services.ReceiptAlreadyPosted as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc

        receipt.refresh_from_db()
        return Response(ReceiptSerializer(receipt).data)


@extend_schema(
    tags=["Procurement — receipts"],
    summary="What is still to come on an order",
    responses=OutstandingRowSerializer(many=True),
    description=(
        "Ordered minus received, per SKU. Counts **posted** receipts only — "
        "an unposted receipt is paperwork somebody is still checking, not "
        "goods the warehouse can rely on."
    ),
)
class OutstandingOnOrderView(APIView):
    permission_classes = [*AUTHENTICATED, CanReceiveAndShip]

    def get(self, request, pk=None):
        order = get_object_or_404(
            scope_to_user_site(
                ProductionOrder.objects.all(), request.user, warehouse_field="warehouse"
            ),
            pk=pk,
        )
        rows = services.outstanding_on_order(order)
        return Response(OutstandingRowSerializer(rows, many=True).data)


# ---------------------------------------------------------------------------
# Costed reports — F55, F56
# ---------------------------------------------------------------------------
#
# The "Financial Reports" column of AsOne's matrix: Program Lead, Operations
# Manager and Finance. Warehouse and school staff are excluded — the matrix
# says School Staff "cannot see costs beyond their own price list".


def _period(request):
    """Read `?from=` and `?to=`. Both ends inclusive, as a person expects
    when they ask for September."""
    parsed = []
    for key in ("from", "to"):
        raw = request.query_params.get(key)
        if not raw:
            parsed.append(None)
            continue
        try:
            parsed.append(date.fromisoformat(raw))
        except ValueError:
            raise DRFValidationError(
                {key: f"'{raw}' is not a date. Use YYYY-MM-DD."}
            ) from None
    return tuple(parsed)


@extend_schema(
    tags=["Procurement — reports"],
    summary="Group orders, costed",
    parameters=[
        OpenApiParameter("from", str, description="Order date on or after, YYYY-MM-DD."),
        OpenApiParameter("to", str, description="Order date on or before, YYYY-MM-DD."),
        OpenApiParameter(
            "include_cancelled", bool, description="Include withdrawn orders."
        ),
    ],
    responses=GroupOrderCostedSerializer(many=True),
    description=(
        "F55 — what was committed to the Tailoring Centers.\n\n"
        "Valued at the price agreed when each order was raised, not today's "
        "price list: a September order is worth what was agreed in "
        "September.\n\n"
        "Cancelled orders are excluded unless asked for — money was never "
        "committed against a withdrawn order, and counting it would overstate "
        "the funding.\n\n"
        "The response carries a `totals` object alongside the rows."
    ),
)
class GroupOrdersCostedView(APIView):
    permission_classes = [*AUTHENTICATED, CanViewFinancialReports]

    def get(self, request):
        date_from, date_to = _period(request)
        include_cancelled = request.query_params.get("include_cancelled") == "true"

        rows = reports.group_orders_costed(date_from, date_to, include_cancelled)
        return Response(
            {
                "totals": GroupOrderTotalSerializer(
                    reports.group_order_total(date_from, date_to, include_cancelled)
                ).data,
                "orders": GroupOrderCostedSerializer(rows, many=True).data,
            }
        )


@extend_schema(
    tags=["Procurement — reports"],
    summary="Receipts from Tailoring Centers, costed",
    parameters=[
        OpenApiParameter("from", str, description="Date received on or after."),
        OpenApiParameter("to", str, description="Date received on or before."),
        OpenApiParameter("tailoring_center", int, description="Limit to one TC."),
        OpenApiParameter("warehouse", int, description="Limit to one warehouse."),
        OpenApiParameter("detail", bool, description="Also return receipt by receipt."),
    ],
    responses=ReceiptsByTailoringCenterSerializer(many=True),
    description=(
        "F56 — what each Tailoring Center actually delivered, and what it is "
        "worth.\n\n"
        "Valued at what AsOne agreed to pay that TC, and counted at what "
        "actually arrived — a short delivery is worth what came off the van, "
        "not what the handwritten packing list claimed.\n\n"
        "**Only posted receipts.** An unposted receipt is paperwork somebody "
        "is still checking; it is not goods received and not money owed.\n\n"
        "Add `?detail=true` for the individual receipts behind the totals — a "
        "summary nobody can drill into is a number people stop trusting."
    ),
)
class ReceiptsCostedView(APIView):
    permission_classes = [*AUTHENTICATED, CanViewFinancialReports]

    def get(self, request):
        date_from, date_to = _period(request)
        tailoring_center = request.query_params.get("tailoring_center") or None
        warehouse = request.query_params.get("warehouse") or None

        rows = reports.receipts_costed(
            date_from, date_to, tailoring_center=tailoring_center, warehouse=warehouse
        )
        payload = {
            "by_tailoring_center": ReceiptsByTailoringCenterSerializer(
                rows, many=True
            ).data
        }

        if request.query_params.get("detail") == "true":
            payload["receipts"] = ReceiptCostedSerializer(
                reports.receipt_detail_costed(date_from, date_to, tailoring_center),
                many=True,
            ).data

        return Response(payload)

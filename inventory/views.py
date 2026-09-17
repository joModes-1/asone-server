"""HTTP layer for the stock ledger.

Read-only throughout. Stock changes as a consequence of a document being
posted — a receipt today, an adjustment or a shipment later — never because
a client asked for a movement directly. There is deliberately no endpoint
that writes here.

Everything is scoped by warehouse: a Namayemba clerk sees Namayemba's stock
and nothing else, per AsOne's matrix.
"""

from datetime import date

from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from django.db.models import Prefetch
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.generics import get_object_or_404
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import (
    AUTHENTICATED,
    CanAdjustInventory,
    CanMoveStockBetweenWarehouses,
    CanReceiveAndShip,
    MasterDataAccess,
    scope_to_user_site,
)
from catalog.models import Warehouse
from catalog.services import PriceNotSet

from . import reports, services
from .filters import (
    InventoryAdjustmentFilter,
    StockMovementFilter,
    WarehouseTransferFilter,
)
from .models import (
    InventoryAdjustment,
    ReasonCode,
    StockMovement,
    WarehouseTransfer,
    WarehouseTransferLine,
)
from .serializers import (
    CostedAdjustmentSerializer,
    CountCorrectionSerializer,
    InventoryAdjustmentSerializer,
    InventoryAdjustmentWriteSerializer,
    ReasonCodeSerializer,
    WarehouseTransferSerializer,
    WarehouseTransferWriteSerializer,
    ReorderAlertSerializer,
    StockLevelSerializer,
    StockMovementSerializer,
)

Role = User.Role


def _as_of(request):
    """Read `?as_of=YYYY-MM-DD`. A count sheet from last Friday is checked
    against what the system thought last Friday, not against today."""
    raw = request.query_params.get("as_of")
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise DRFValidationError(
            {"as_of": f"'{raw}' is not a date. Use YYYY-MM-DD."}
        ) from None


def _date_param(request, name):
    """Read a `YYYY-MM-DD` query parameter, or None.

    Same refusal as `_as_of`: a date that will not parse is a 400 naming the
    parameter, never a silently ignored filter — a report quietly covering
    all time because someone typed "01/09/2026" is worse than an error.
    """
    raw = request.query_params.get(name)
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise DRFValidationError(
            {name: f"'{raw}' is not a date. Use YYYY-MM-DD."}
        ) from None


def _warehouse(request):
    """Read `?warehouse=<id>`, if given."""
    raw = request.query_params.get("warehouse")
    if not raw:
        return None
    return get_object_or_404(Warehouse.objects.all(), pk=raw)


@extend_schema(
    tags=["Inventory"],
    summary="Stock levels",
    parameters=[
        OpenApiParameter("warehouse", int, description="Limit to one warehouse."),
        OpenApiParameter("as_of", str, description="Level as at a date, YYYY-MM-DD."),
        OpenApiParameter("include_zero", bool, description="Include SKUs at zero."),
    ],
    responses=StockLevelSerializer(many=True),
    description=(
        "F47 — units on hand per SKU per warehouse.\n\n"
        "There is no stock-level table. Every figure here is summed from the "
        "ledger on read, which is what makes the audit trail and the stock "
        "level incapable of disagreeing.\n\n"
        "Warehouse staff see their own warehouse only. School staff see the "
        "warehouse that serves their school — the stock is the warehouse's, "
        "not the school's."
    ),
)
class StockLevelView(APIView):
    # F47 gives Finance "All sites" — they post the inventory adjustments and
    # cannot do that without seeing what is on hand. CanReceiveAndShip does
    # not include Finance, so the view declares its own read audience.
    #
    # School Staff read it as well. AsOne asked for schools to see inventory,
    # and what they meant is a school looking at the warehouse that serves it
    # — "are my shirts there yet" — not stock held at the school. Schools
    # hold none: the ledger has a warehouse column and no school column.
    permission_classes = [*AUTHENTICATED, MasterDataAccess]
    read_roles = (Role.WAREHOUSE_STAFF, Role.FINANCE, Role.SCHOOL_STAFF)

    def get(self, request):
        user = request.user
        warehouse = _warehouse(request)

        # Site-bound roles are pinned whatever they ask for. A school clerk
        # has no warehouse of their own, so they get the one their school
        # orders from — D4, a school orders from one warehouse and no other.
        if user.role == Role.WAREHOUSE_STAFF:
            warehouse = user.warehouse
        elif user.role == Role.SCHOOL_STAFF:
            warehouse = user.school.primary_warehouse if user.school_id else None

        # A site-bound account with no site is a broken account. `clean()`
        # refuses to save one, but nothing stops objects.create(), and if one
        # exists then leaving `warehouse` as None here would hand them every
        # warehouse in the country. Empty is the safe reading — the same
        # deny-by-default scope_to_user_site() applies.
        if warehouse is None and user.role in (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF):
            return Response([])

        rows = services.stock_levels(
            warehouse=warehouse,
            as_of=_as_of(request),
            include_zero=request.query_params.get("include_zero") == "true",
        )
        return Response(StockLevelSerializer(rows, many=True).data)


@extend_schema(
    tags=["Inventory"],
    summary="Below-minimum reorder alerts",
    parameters=[
        OpenApiParameter("warehouse", int, description="Limit to one warehouse."),
        OpenApiParameter("as_of", str, description="As at a date, YYYY-MM-DD."),
    ],
    responses=ReorderAlertSerializer(many=True),
    description=(
        "F50 — SKUs at or below the minimum level set for that warehouse, "
        "which is what should trigger a replenishment order on the Tailoring "
        "Centers.\n\n"
        "A SKU with a floor set but no stock at all is included: zero is "
        "below any positive minimum, and that is exactly the case worth "
        "alerting on."
    ),
)
class ReorderAlertView(APIView):
    # F50 leaves the Finance cell blank, unlike F47 next door. Reordering is
    # an operational decision, not a financial one.
    permission_classes = [*AUTHENTICATED, MasterDataAccess]
    read_roles = (Role.WAREHOUSE_STAFF,)

    def get(self, request):
        warehouse = _warehouse(request)
        if request.user.role == Role.WAREHOUSE_STAFF:
            warehouse = request.user.warehouse

        alerts = services.below_minimum(warehouse=warehouse, as_of=_as_of(request))
        return Response(ReorderAlertSerializer(alerts, many=True).data)


@extend_schema(tags=["Inventory"])
class StockMovementViewSet(viewsets.ReadOnlyModelViewSet):
    """The ledger itself — F48, the audit trail by SKU.

    Read-only, and not by omission: `StockMovement` refuses to be updated or
    deleted at model level too. Filter with `?sku=` for one product's whole
    history.
    """

    serializer_class = StockMovementSerializer
    # F48 gives Finance "All sites" on the audit trail.
    permission_classes = [*AUTHENTICATED, MasterDataAccess]
    read_roles = (Role.WAREHOUSE_STAFF, Role.FINANCE)
    # A declared filterset rather than `filterset_fields`, for the date
    # range: "everything that ever happened to this SKU" is the right default
    # for an audit trail and the wrong thing to put on a screen.
    filterset_class = StockMovementFilter

    def get_queryset(self):
        queryset = StockMovement.objects.select_related(
            "sku", "sku__garment", "warehouse", "created_by"
        )
        return scope_to_user_site(
            queryset, self.request.user, warehouse_field="warehouse"
        )


@extend_schema(tags=["Inventory"])
class ReasonCodeViewSet(viewsets.ModelViewSet):
    """Why an inventory adjustment was made — F13.

    Master data, so the leads maintain it and Finance reads it. Warehouse and
    school staff have no access: the matrix gives the Inventory Adj column to
    Finance alone, and the reason codes belong to that column.

    **No DELETE.** A code is retired with `is_active`, because past
    adjustments point at it — an audit trail that cannot say why a movement
    happened is not an audit trail. Phase 2 will add that foreign key.
    """

    queryset = ReasonCode.objects.all()
    serializer_class = ReasonCodeSerializer
    permission_classes = [*AUTHENTICATED, MasterDataAccess]
    read_roles = (Role.FINANCE,)
    filterset_fields = ("is_active",)
    search_fields = ("code", "name")
    http_method_names = ["get", "post", "patch", "head", "options"]


# ---------------------------------------------------------------------------
# Warehouse transfers — F25
# ---------------------------------------------------------------------------


@extend_schema(tags=["Inventory"])
class WarehouseTransferViewSet(viewsets.ModelViewSet):
    """Stock moving between the two warehouses — F25.

    Unlike the other inventory adjustments, F25 gives this to **both leads
    and Finance**, not Finance alone. A rebalance between warehouses is an
    operational decision that happens to have no financial effect, so the
    matrix widens the cell here — worth noticing, because every neighbouring
    feature is Finance-only.

    Warehouse staff have no access at all: a transfer commits two sites, and
    a clerk can only see one of them.

    Not the same as a **backorder transfer**, which is Phase 3: there a
    warehouse ships direct to a school without the goods ever reaching the
    school's own warehouse (decision D2).
    """

    permission_classes = [*AUTHENTICATED, CanMoveStockBetweenWarehouses]
    filterset_class = WarehouseTransferFilter
    # Never deleted: an unposted transfer still records that someone intended
    # to move stock, and a posted one is the source of ledger rows.
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        return WarehouseTransfer.objects.select_related(
            "from_warehouse", "to_warehouse", "reason_code", "created_by"
        ).prefetch_related(
            Prefetch(
                "lines",
                queryset=WarehouseTransferLine.objects.select_related("sku"),
            )
        )

    def get_serializer_class(self):
        if self.action == "create":
            return WarehouseTransferWriteSerializer
        return WarehouseTransferSerializer

    @extend_schema(
        summary="Prepare a transfer",
        request=WarehouseTransferWriteSerializer,
        responses={201: WarehouseTransferSerializer},
        description=(
            "Writes the transfer down. **Does not move stock** — posting does "
            "that, as a separate step, so a transfer can be checked against "
            "what is actually on the shelf first.\n\n"
            "Refused if the source warehouse does not hold what is being "
            "moved. Stock is checked again at posting, because it can move in "
            "between."
        ),
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        data = dict(serializer.validated_data)
        lines = data.pop("lines")

        try:
            transfer = services.create_transfer(
                created_by=request.user,
                from_warehouse=data.pop("from_warehouse"),
                to_warehouse=data.pop("to_warehouse"),
                lines=lines,
                **data,
            )
        except services.NotEnoughStock as exc:
            raise DRFValidationError({"lines": str(exc)}) from exc

        return Response(
            WarehouseTransferSerializer(transfer).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        summary="Post a transfer to the ledger",
        request=None,
        responses={200: WarehouseTransferSerializer},
        description=(
            "Writes **two** permanent ledger rows per line: one out of the "
            "source, one into the destination, at the same unit value.\n\n"
            "Total inventory value across the two warehouses is unchanged — "
            "AsOne owns the stock either side, so a transfer changes where it "
            "is, not what it is worth.\n\n"
            "Atomic: a half-posted transfer would take stock out of one "
            "warehouse without putting it in the other, and the goods would "
            "simply cease to exist.\n\n"
            "Can only be done once."
        ),
    )
    @action(detail=True, methods=["post"], url_path="post-to-ledger")
    def post_to_ledger(self, request, pk=None):
        transfer = self.get_object()

        try:
            services.post_transfer(transfer, posted_by=request.user)
        except services.TransferAlreadyPosted as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc
        except services.NotEnoughStock as exc:
            raise DRFValidationError({"lines": str(exc)}) from exc

        transfer.refresh_from_db()
        return Response(WarehouseTransferSerializer(transfer).data)


# ---------------------------------------------------------------------------
# Inventory adjustments — F23
# ---------------------------------------------------------------------------


@extend_schema(tags=["Inventory"])
class InventoryAdjustmentViewSet(viewsets.ModelViewSet):
    """A quantity change against a reason code — F23.

    The generic shape the rest of Phase 2 reuses. Returns (F26) and damages
    (F27) are this same document posted with a different reason code — RET
    or DMG — and needed no new code; see test_returns_and_damages.py for the
    proof.

    Physical count correction (F24) is *not* just another reason code: F24's
    actual requirement is that the system compares a count to what it
    already thinks is on hand and posts the difference itself, rather than
    asking whoever is counting to do that subtraction and pick CORR_UP or
    CORR_DOWN by hand. See the `correct_count` action below, and
    services.correct_count().

    Finance only, for both reading and writing — unlike most master data,
    this is not routed through MasterDataAccess. AsOne's "Inventory Adj"
    column is Finance-only across the board, with no read-only access for
    anyone else, including the leads (see accounts.permissions.
    CanAdjustInventory and open question Q3 on why even they are excluded).

    Never deleted: an unposted adjustment still records that someone
    intended a correction, and a posted one is the source of a ledger row.
    """

    permission_classes = [*AUTHENTICATED, CanAdjustInventory]
    filterset_class = InventoryAdjustmentFilter
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        return InventoryAdjustment.objects.select_related(
            "warehouse", "sku", "sku__garment", "reason_code", "created_by"
        )

    def get_serializer_class(self):
        if self.action == "create":
            return InventoryAdjustmentWriteSerializer
        return InventoryAdjustmentSerializer

    @extend_schema(
        summary="Prepare an inventory adjustment",
        request=InventoryAdjustmentWriteSerializer,
        responses={201: InventoryAdjustmentSerializer},
        description=(
            "Writes the adjustment down. **Does not change stock** — posting "
            "does that, as a separate step, so it can be checked first.\n\n"
            "Refused if the SKU has no catalog price on the adjustment date, "
            "or if the reason code decreases stock and there is not enough "
            "on hand to take it from."
        ),
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            adjustment = services.create_adjustment(
                created_by=request.user, **serializer.validated_data
            )
        except services.NotEnoughStock as exc:
            raise DRFValidationError({"quantity": str(exc)}) from exc

        return Response(
            InventoryAdjustmentSerializer(adjustment).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        summary="Post an adjustment to the ledger",
        request=None,
        responses={200: InventoryAdjustmentSerializer},
        description=(
            "Writes **one** permanent ledger row. The reason code decides "
            "whether the quantity increases or decreases stock — the person "
            "posting it never chooses the sign.\n\n"
            "Value is taken from the SKU's catalog price on the adjustment "
            "date, looked up again in case it changed since the adjustment "
            "was written down. Can only be done once."
        ),
    )
    @action(detail=True, methods=["post"], url_path="post-to-ledger")
    def post_to_ledger(self, request, pk=None):
        adjustment = self.get_object()

        try:
            services.post_adjustment(adjustment, posted_by=request.user)
        except services.AdjustmentAlreadyPosted as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc
        except services.NotEnoughStock as exc:
            raise DRFValidationError({"quantity": str(exc)}) from exc
        except PriceNotSet as exc:
            raise DRFValidationError({"sku": str(exc)}) from exc

        adjustment.refresh_from_db()
        return Response(InventoryAdjustmentSerializer(adjustment).data)

    @extend_schema(
        summary="Correct a physical count — F24",
        request=CountCorrectionSerializer,
        responses={
            200: InventoryAdjustmentSerializer,
            201: InventoryAdjustmentSerializer,
            409: OpenApiResponse(description="CORR_UP/CORR_DOWN missing or retired."),
        },
        description=(
            "Compares what was actually counted to what the system thinks "
            "is on hand, and posts the difference — the comparison is done "
            "here, not by whoever is counting.\n\n"
            "**201** if the count differed and a CORR_UP/CORR_DOWN "
            "adjustment was posted immediately (not left as an unposted "
            "draft, unlike the rest of this endpoint). **200** with "
            "`adjustment: null` if the count matched exactly — nothing to "
            "correct, and nothing gets written for it.\n\n"
            "**400** if the SKU has no catalog price on the count date, or "
            "if the count is short of what is actually on hand (should not "
            "happen — the comparison is against the same figure — but "
            "re-checked anyway, same as everywhere else).\n\n"
            "**409**, not 400, if the CORR_UP/CORR_DOWN reason code this "
            "direction needs does not exist or has been retired — the "
            "request itself was fine, and nothing the caller could change "
            "about it would fix a gap in Central Office's own master data. "
            "Same reasoning config/exceptions.py already uses for a refused "
            "delete."
        ),
    )
    @action(detail=False, methods=["post"], url_path="correct-count")
    def correct_count(self, request):
        serializer = CountCorrectionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            adjustment = services.correct_count(
                created_by=request.user, **serializer.validated_data
            )
        except services.NotEnoughStock as exc:
            raise DRFValidationError({"counted_quantity": str(exc)}) from exc
        except services.CorrectionReasonCodeMissing as exc:
            # 409, not 400: the request was well formed, and nothing the
            # caller can change about it would help — the same reasoning
            # config/exceptions.py already applies to a refused delete.
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except PriceNotSet as exc:
            raise DRFValidationError({"sku": str(exc)}) from exc

        if adjustment is None:
            return Response(
                {"adjustment": None, "detail": "The count matches the system. Nothing to correct."}
            )

        return Response(
            InventoryAdjustmentSerializer(adjustment).data, status=status.HTTP_201_CREATED
        )


@extend_schema(
    tags=["Inventory"],
    summary="Adjustments, costed",
    parameters=[
        OpenApiParameter("from", str, description="Inclusive start date, YYYY-MM-DD."),
        OpenApiParameter("to", str, description="Inclusive end date, YYYY-MM-DD."),
        OpenApiParameter("warehouse", int, description="Limit to one warehouse."),
    ],
    responses=CostedAdjustmentSerializer(many=True),
    description=(
        "F58 — what adjustments did to the value of stock, by reason code.\n\n"
        "Counted from the **ledger**, not from adjustment documents: an "
        "adjustment created and never posted has moved nothing and is "
        "correctly absent. Value is signed the way the ledger is, so the "
        "rows sum to the net effect on the value of stock.\n\n"
        "**Open question Q6.** AsOne's p.6 marks the financial treatment of "
        "damaged stock \"To be determined\", so every row's `treatment` "
        "currently reads \"not yet classified\". The operational figures — "
        "units and value — are complete and do not depend on that answer. "
        "See `FINANCIAL_TREATMENT` in `inventory/reports.py`."
    ),
)
class CostedAdjustmentsView(APIView):
    """F58. Finance and the leads — the same audience as the other costed
    reports, since this is a money question rather than a warehouse one."""

    permission_classes = [*AUTHENTICATED, MasterDataAccess]
    read_roles = (Role.FINANCE,)

    def get(self, request):
        warehouse = _warehouse(request)

        rows = reports.adjustments_costed(
            date_from=_date_param(request, "from"),
            date_to=_date_param(request, "to"),
            warehouse=warehouse,
        )
        return Response(CostedAdjustmentSerializer(rows, many=True).data)

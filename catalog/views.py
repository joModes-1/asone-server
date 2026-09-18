"""HTTP layer for master data.

Views translate between HTTP and the rest of the system and nothing else.
Anything worth testing lives in catalog/services.py.

Every viewset here declares two things:

    permission_classes   who may write — always the leads
    read_roles           who may read — different for each table

The split comes straight from AsOne's access matrix, where editing is one
column but "view only" is granted table by table. See
`accounts.permissions.MasterDataAccess`.
"""

from datetime import date

from django.db import transaction
from django.db.models import Count
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import AUTHENTICATED, MasterDataAccess

from . import services
from .models import (
    Garment,
    GarmentPrice,
    Kit,
    KitItem,
    MinimumStockLevel,
    School,
    Size,
    Sku,
    TailoringCenter,
    Warehouse,
)
from .serializers import (
    GarmentPriceSerializer,
    GarmentSerializer,
    KitItemSerializer,
    KitSerializer,
    MinimumStockLevelSerializer,
    PriceListRowSerializer,
    RepriceSerializer,
    SchoolSerializer,
    SizeSerializer,
    SkuSerializer,
    TailoringCenterSerializer,
    WarehouseSerializer,
)

Role = User.Role
# AUTHENTICATED, not a bare IsAuthenticated: it carries the
# pending-password check, so an account still on the password a lead
# typed for it cannot reach master data.
MASTER_DATA = [*AUTHENTICATED, MasterDataAccess]


def _requested_date(request) -> date:
    """Read `?on=YYYY-MM-DD`, defaulting to today.

    Prices are dated, so nearly every read here can be asked "as at when".

    A malformed date is the caller's mistake, so it is a 400. Left unhandled,
    `fromisoformat` raises ValueError and DRF reports a 500 — which sends
    someone hunting a server fault that is not there.
    """
    raw = request.query_params.get("on")
    if not raw:
        return date.today()

    try:
        return date.fromisoformat(raw)
    except ValueError:
        raise DRFValidationError(
            {"on": f"'{raw}' is not a date. Use YYYY-MM-DD."}
        ) from None


def _price_list_level(request):
    """Which price list this caller may see — F29 and F51 in one place.

    Two different features read the same data and must not be given the same
    freedom:

      F51 (report)  Leads, warehouse and Finance ask for the list they want.
      F29 (POS)     A school works from **its own** list. The system knows
                    which from the school on their account, so nobody has to
                    choose — and choosing wrongly is not possible.

    That matters in both directions. Left to a default, a High School was
    served the Primary list silently; left to a query parameter, a Primary
    school could pull up High School garments its students cannot order.

    A school asking for its own level explicitly is fine — a frontend may
    well send it. Asking for the other one is refused rather than quietly
    corrected, because it means the client believes something untrue and a
    silent correction would hide that.
    """
    user = request.user

    if user.role != User.Role.SCHOOL_STAFF:
        return _requested_level(request)

    if user.school is None:
        raise PermissionDenied(
            "Your account is not attached to a school, so it has no price list."
        )

    own_level = user.school.level
    asked_for = request.query_params.get("level")

    if asked_for and asked_for != own_level:
        raise PermissionDenied(
            f"{user.school.name} is a {user.school.get_level_display()}. "
            "You can only work from its price list."
        )

    return own_level


def _requested_level(request, *, required=True):
    """Read `?level=PS|HS`.

    Validated rather than passed through. An unrecognised level used to match
    nothing but the BOTH garments, so a typo returned a short price list with
    a 200 — a wrong answer is worse than an error, because nobody checks it.
    """
    raw = request.query_params.get("level")
    if not raw:
        if required:
            return School.Level.PRIMARY
        return None

    valid = {choice.value for choice in School.Level}
    if raw not in valid:
        raise DRFValidationError(
            {"level": f"'{raw}' is not a school level. Use {' or '.join(sorted(valid))}."}
        )
    return raw


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------


@extend_schema(tags=["Master data — sites"])
class TailoringCenterViewSet(viewsets.ModelViewSet):
    """Where uniforms are made. Not system users — they exist so production
    orders and receipts have something to point at."""

    queryset = TailoringCenter.objects.all().order_by("name")
    serializer_class = TailoringCenterSerializer
    permission_classes = MASTER_DATA
    # Finance reads these for the same reason warehouse staff do: it is the
    # role that posts count corrections and write-offs, and "is this SKU
    # below its floor" is the context for deciding. Setting the floor is
    # still the leads' alone.
    read_roles = (Role.WAREHOUSE_STAFF,)
    filterset_fields = ("is_active",)
    search_fields = ("name",)


@extend_schema(tags=["Master data — sites"])
class WarehouseViewSet(viewsets.ModelViewSet):
    """Where finished stock is held.

    Finance reads this, which the matrix's "Warehouses — view: Warehouse
    Staff" line does not say on its face. It follows from two cells that do:
    Finance's scope is *all locations*, and F23 gives them adjustments at
    *all sites*. An adjustment names the warehouse it is posted at, so a role
    that cannot list warehouses cannot post one — the picker on the New
    Adjustment screen came up empty and there was no way to choose a site.

    Read only, as for everybody outside the leads. Editing a warehouse is
    still the Table Updates column.
    """

    queryset = Warehouse.objects.select_related("primary_tailoring_center").order_by("name")
    serializer_class = WarehouseSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF, Role.FINANCE)
    filterset_fields = ("primary_tailoring_center", "is_active")


@extend_schema(tags=["Master data — sites"])
class SchoolViewSet(viewsets.ModelViewSet):
    """The customers. Each orders from one primary warehouse."""

    queryset = School.objects.select_related("primary_warehouse").order_by("name")
    serializer_class = SchoolSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF)
    filterset_fields = ("level", "primary_warehouse", "is_active")

    def get_queryset(self):
        """Schools, each with what it currently has in flight.

        Annotated rather than counted per row: a list of forty schools
        should be one query, not forty-one.

        "Active" is an order the school is still waiting on. Completed and
        cancelled orders are history, and a school with only those is not
        busy.
        """
        from django.db.models import Count, Q

        from orders.models.school_orders import OrderStatus

        return (
            super()
            .get_queryset()
            .annotate(
                active_orders_count=Count(
                    "orders",
                    filter=~Q(
                        orders__status__in=(
                            OrderStatus.COMPLETED,
                            OrderStatus.CANCELLED,
                        )
                    ),
                    distinct=True,
                )
            )
        )


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


@extend_schema(tags=["Master data — products"])
class SizeViewSet(viewsets.ModelViewSet):
    """Garment sizes, shared across garments so "10" means one thing.

    Leads only, per F05. Other roles reach a size through the SKU that uses
    it — `size_name` is on every SKU — so nothing is hidden from them in
    practice, and this matches what AsOne wrote.
    """

    queryset = Size.objects.all()
    serializer_class = SizeSerializer
    permission_classes = MASTER_DATA
    # Writing stays with the leads, per F05. Reading is open to Finance as
    # well, because the inventory screen filters by size and Finance is the
    # role that posts stock corrections against those rows — a filter that
    # 403s for the one person allowed to act on the table is the same bug
    # the warehouse picker had. A size is the string "10"; there is nothing
    # in it to protect.
    read_roles = (Role.FINANCE,)


@extend_schema(tags=["Master data — products"])
class GarmentViewSet(viewsets.ModelViewSet):
    """Uniform components, before a size is chosen.

    Price lives here rather than on the SKU, so `current_price` is a real
    field of a garment and `POST /garments/{id}/reprice/` is how it changes.
    """

    serializer_class = GarmentSerializer
    permission_classes = MASTER_DATA
    # F05 gives garments to the leads alone — unlike F06, which gives SKUs to
    # everyone as view-only. Odd on its face, since a SKU carries its
    # garment's name, but it is what AsOne's matrix says. Worth confirming
    # with them rather than quietly widening.
    read_roles = ()
    filterset_fields = ("school_level", "is_active")
    search_fields = ("name", "colour")

    def get_queryset(self):
        # Annotated rather than looked up per row: without this, listing 45
        # garments costs 46 queries.
        return services.with_current_price(
            Garment.objects.annotate(sku_count=Count("skus"))
        ).order_by("name")

    @extend_schema(
        summary="Change a garment's price",
        request=RepriceSerializer,
        responses={201: GarmentPriceSerializer},
        description=(
            "Closes the current open-ended price on `active_from` and opens a "
            "new one.\n\n"
            "This is the only sanctioned way to change a price. Editing a "
            "price row in place would rewrite history, and an invoice "
            "reprinted next term would no longer match the original."
        ),
    )
    @action(detail=True, methods=["post"])
    def reprice(self, request, pk=None):
        garment = self.get_object()

        serializer = RepriceSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        # Two writes — closing the old price and opening the new one. A
        # failure between them would leave the garment unpriced.
        with transaction.atomic():
            price = services.reprice(
                garment,
                serializer.validated_data["unit_price"],
                serializer.validated_data["active_from"],
            )

        return Response(
            GarmentPriceSerializer(price).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        summary="This garment's price history",
        responses=GarmentPriceSerializer(many=True),
    )
    @action(detail=True, methods=["get"])
    def prices(self, request, pk=None):
        garment = self.get_object()
        return Response(
            GarmentPriceSerializer(garment.prices.order_by("-active_date"), many=True).data
        )


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


@extend_schema(tags=["Master data — pricing"])
class GarmentPriceViewSet(viewsets.ModelViewSet):
    """The pricing table, across all garments.

    Day-to-day changes should go through `POST /garments/{id}/reprice/`, which
    closes the previous price for you. This viewset is for corrections and
    auditing.
    """

    queryset = GarmentPrice.objects.select_related("garment").order_by(
        "garment__name", "-active_date"
    )
    serializer_class = GarmentPriceSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.SCHOOL_STAFF, Role.FINANCE)
    filterset_fields = ("garment",)

    # No DELETE. The promise that a March invoice reprints at March's price
    # depends entirely on that price row still existing — deleting one
    # silently rewrites history, and nothing in the database prevents it
    # (no other model points at GarmentPrice).
    #
    # A row entered by mistake is corrected with PATCH, so nothing is lost by
    # removing the verb.
    http_method_names = ["get", "post", "patch", "put", "head", "options"]


@extend_schema(
    tags=["Master data — pricing"],
    summary="Price list for Primary or High School",
    parameters=[
        OpenApiParameter("level", str, description="`PS` or `HS`.", required=True),
        OpenApiParameter("on", str, description="Date, `YYYY-MM-DD`. Defaults to today."),
    ],
    responses=PriceListRowSerializer(many=True),
    description=(
        "Active garments on that price list with their price on that date, "
        "ordered by name. Garments marked `BOTH` appear on each list.\n\n"
        "**School staff do not pass `level`** — F29 says a school works from "
        "its own list, so the level comes from the school on their account. "
        "Asking for the other school level is refused, not quietly "
        "corrected. Every other role names the list it wants (F51).\n\n"
        "Garments with no price on the date are **omitted, not shown at "
        "zero** — a price list is a document a school orders from, and a line "
        "with no price is worse than no line. Use `/price-lists/gaps/` to "
        "find them before publishing."
    ),
)
class PriceListView(APIView):
    """F15 — generate price lists at garment level."""

    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF, Role.FINANCE)

    def get(self, request):
        level = _price_list_level(request)
        rows = services.price_list(level, _requested_date(request))
        return Response(PriceListRowSerializer(rows, many=True).data)


@extend_schema(
    tags=["Master data — pricing"],
    summary="Active garments with no price",
    parameters=[
        OpenApiParameter("on", str, description="Date, `YYYY-MM-DD`. Defaults to today."),
        OpenApiParameter("level", str, description="Limit to `PS` or `HS`."),
    ],
    responses=GarmentSerializer(many=True),
    description=(
        "The gap report behind a price list. Run it before publishing one, or "
        "a garment silently disappears from what the schools can order."
    ),
)
class PriceGapView(APIView):
    permission_classes = MASTER_DATA
    read_roles = (Role.FINANCE,)

    def get(self, request):
        gaps = services.garments_without_a_price(
            _requested_date(request), _requested_level(request, required=False)
        )
        return Response(GarmentSerializer(gaps, many=True).data)


# ---------------------------------------------------------------------------
# SKUs
# ---------------------------------------------------------------------------


@extend_schema(tags=["Master data — SKUs"])
class SkuViewSet(viewsets.ModelViewSet):
    """One garment in one size. What is counted, ordered and picked.

    The control number is assigned by the system and is read-only: it is
    printed on pick lists and packing lists, and must mean the same thing
    forever.
    """

    serializer_class = SkuSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF, Role.FINANCE)
    filterset_fields = ("garment", "size", "is_active", "garment__school_level")
    search_fields = ("number", "description")

    # No DELETE. The control number is issued the moment a SKU is created and
    # is printed on pick lists and packing lists; deleting the row destroys
    # the record of what that number meant, while the sequence never hands it
    # out again. Retire a SKU with `is_active` instead.
    http_method_names = ["get", "post", "patch", "put", "head", "options"]

    def get_queryset(self):
        # A SKU's price is its garment's price, so the subquery correlates on
        # garment_id. 200 SKUs in one query rather than 201.
        # Garment, then size in *size* order.
        #
        # The model orders by `description`, which is deliberate — pick lists
        # print "in Description sequence" (p.2) and are built from order lines
        # elsewhere, so that stays. But description is a string, and a picker
        # sorted by it runs 10, 12, 14, 16, 8: size 8 lands after size 16
        # because "8" sorts after "1". `Size.sort_order` exists for exactly
        # this, and every screen that offers a SKU to choose from reads this
        # endpoint.
        return services.with_current_price(
            Sku.objects.select_related("garment", "size"),
            garment_field="garment_id",
        ).order_by("garment__name", "size__sort_order")


@extend_schema(tags=["Master data — SKUs"])
class MinimumStockLevelViewSet(viewsets.ModelViewSet):
    """The level that triggers a replenishment order, per SKU per warehouse."""

    queryset = MinimumStockLevel.objects.select_related(
        "sku", "sku__garment", "warehouse"
    ).order_by("warehouse__name", "sku__description")
    serializer_class = MinimumStockLevelSerializer
    permission_classes = MASTER_DATA
    # Finance reads these for the same reason warehouse staff do: it is the
    # role that posts count corrections and write-offs, and "is this SKU
    # below its floor" is the context for deciding. Setting the floor is
    # still the leads' alone.
    read_roles = (Role.WAREHOUSE_STAFF, Role.FINANCE)
    filterset_fields = ("warehouse", "sku")


# ---------------------------------------------------------------------------
# Kits
# ---------------------------------------------------------------------------


@extend_schema(tags=["Master data — kits"])
class KitViewSet(viewsets.ModelViewSet):
    """Bundles of SKUs sold as one unit, e.g. a new-student starter kit.

    `current_price` is computed fresh on every read — see
    catalog.services.compute_kit_price(). Unlike Garment, there is no
    `reprice` action here: a kit has no price of its own to change, only
    components whose prices already have that endpoint.
    """

    serializer_class = KitSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.SCHOOL_STAFF, Role.FINANCE)
    filterset_fields = ("school_level", "is_active")
    search_fields = ("kit_number", "name")

    def get_queryset(self):
        # Annotated rather than counted per row, same reasoning as
        # GarmentViewSet.get_queryset — one query instead of one per kit.
        return Kit.objects.annotate(item_count=Count("items")).order_by(
            "school_level", "name"
        )


@extend_schema(tags=["Master data — kits"])
class KitItemViewSet(viewsets.ModelViewSet):
    """One line of a kit's bill of materials: a component SKU and its quantity."""

    queryset = KitItem.objects.select_related("kit", "sku").order_by("kit", "sku")
    serializer_class = KitItemSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.SCHOOL_STAFF, Role.FINANCE)
    filterset_fields = ("kit", "sku")

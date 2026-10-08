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
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from catalog import importing, kit_importing
from accounts.models import User
from accounts.permissions import AUTHENTICATED, CanUpdateTables, MasterDataAccess

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
    CatalogueImportResultSerializer,
    CatalogueImportSerializer,
    GarmentPriceSerializer,
    GarmentSerializer,
    KitImportResultSerializer,
    KitImportSerializer,
    KitItemSerializer,
    KitSerializer,
    MinimumStockLevelSerializer,
    KitPriceListRowSerializer,
    PriceListRowSerializer,
    UnpriceableKitSerializer,
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
    # Finance is wider than the matrix's line, and follows from two cells
    # that are in it: F34 gives them the invoice, and every costed report
    # they read — shipments, orders on hold, part-processed — is grouped by
    # school. A role that cannot list schools cannot filter its own reports
    # by one, which is what the Orders screen was 403ing on.
    # Widened 20 September 2026 — worth putting to AsOne with Q3.
    read_roles = (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF, Role.FINANCE)
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
    """Garment sizes, shared across garments so "M" means one thing.

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
    # the warehouse picker had. A size is the string "M"; there is nothing
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
    summary="Kit price list for Primary or High School",
    parameters=[
        OpenApiParameter("level", str, description="`PS` or `HS`.", required=True),
        OpenApiParameter("on", str, description="Date, `YYYY-MM-DD`. Defaults to today."),
    ],
    responses=KitPriceListRowSerializer(many=True),
    description=(
        "The **kit** half of F15 and F51 — AsOne asks for printable price "
        "lists at SKU and Uniform Kit level, and `/price-lists/` is the "
        "garment half.\n\n"
        "A kit's price is the sum of its components at their price on the "
        "date, calculated rather than stored: a kit has no price of its own, "
        "and giving it one would let the two disagree the first time a "
        "component moved.\n\n"
        "**A kit that cannot be priced is omitted**, exactly as an unpriced "
        "garment is — and a kit is unpriceable when *any* component has no "
        "price, or when it has no components. That means a fully priced-"
        "looking catalogue can still be missing kits, which is what "
        "`/price-lists/kits/gaps/` is for.\n\n"
        "Unlike garments there is no `BOTH`: a kit belongs to one school "
        "level and appears on one list."
    ),
)
class KitPriceListView(APIView):
    """F15 and F51, at kit level."""

    permission_classes = MASTER_DATA
    read_roles = (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF, Role.FINANCE)

    def get(self, request):
        level = _price_list_level(request)
        rows = services.kit_price_list(level, _requested_date(request))
        return Response(KitPriceListRowSerializer(rows, many=True).data)


@extend_schema(
    tags=["Master data — pricing"],
    summary="Kits that cannot be priced",
    parameters=[
        OpenApiParameter("on", str, description="Date, `YYYY-MM-DD`. Defaults to today."),
        OpenApiParameter("level", str, description="Limit to `PS` or `HS`."),
    ],
    responses=UnpriceableKitSerializer(many=True),
    description=(
        "The gap report behind the kit price list. Each row names the "
        "**component garments** missing a price, because the cause is almost "
        "never the kit itself — reporting only the kit sends somebody to fix "
        "the wrong record."
    ),
)
class KitPriceGapView(APIView):
    permission_classes = MASTER_DATA
    read_roles = (Role.FINANCE,)

    def get(self, request):
        gaps = services.kits_without_a_price(
            _requested_date(request), _requested_level(request, required=False)
        )
        return Response(UnpriceableKitSerializer(gaps, many=True).data)


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
    # The cohort fields are filterable because that is the whole reason they
    # were modelled rather than left inside the kit's name: a school ordering
    # for a Nursery boy boarder has 120 kits to choose from, and narrowing by
    # band, gender and residence leaves the six that differ only by size.
    filterset_fields = (
        "school_level",
        "is_active",
        "fit_group",
        "gender",
        "residence",
        "size",
    )
    search_fields = ("kit_number", "name")

    def get_queryset(self):
        # Annotated rather than counted per row, same reasoning as
        # GarmentViewSet.get_queryset — one query instead of one per kit.
        #
        # `size` is joined because every row prints its name, and ordered on
        # `size__sort_order` because sizes do not sort as text — S, M, L is
        # not alphabetical. The cohort fields come before the name so the six
        # sizes of one kit sit together rather than scattered through the
        # list by whatever each was called.
        return (
            Kit.objects.select_related("size")
            .annotate(item_count=Count("items"))
            .order_by(
                "school_level",
                "fit_group",
                "gender",
                "residence",
                "size__sort_order",
                "name",
            )
        )


@extend_schema(tags=["Master data — kits"])
class KitItemViewSet(viewsets.ModelViewSet):
    """One line of a kit's bill of materials: a component SKU and its quantity."""

    queryset = KitItem.objects.select_related("kit", "sku").order_by("kit", "sku")
    serializer_class = KitItemSerializer
    permission_classes = MASTER_DATA
    read_roles = (Role.SCHOOL_STAFF, Role.FINANCE)
    filterset_fields = ("kit", "sku")


@extend_schema(
    tags=["Catalogue"],
    summary="Import a catalogue spreadsheet",
    request={"multipart/form-data": CatalogueImportSerializer},
    responses={
        200: CatalogueImportResultSerializer,
        400: OpenApiResponse(description="The file, or some of its rows, could not be read."),
    },
    description=(
        "Upload a spreadsheet of garments and create the garments, sizes, "
        "SKUs and prices in it.\n\n"
        "**One row per garment, not per SKU.** `sizes` is semicolon-"
        "separated, and the row's price applies to every one of them — AsOne "
        "price garments, not sizes.\n\n"
        "Required columns: `name`, `school_level`, `sizes`, `unit_price`. "
        "Optional: `code`, `colour`, `colour_code`, `fit_group`, "
        "`active_date`. The three that compose AsOne's five-part SKU number "
        "(`UNI-NUR-CHS-XS-CHK`) are `fit_group`, `code` and `colour_code`; "
        "without them SKUs fall back to the two-part form.\n\n"
        "**Nothing is half-imported.** The whole file is one transaction, so "
        "a bad row on line 40 rolls back lines 1-39 too. Every unreadable "
        "row is reported with its line number, not just the first.\n\n"
        "**Running it twice changes nothing** — garments are matched on "
        "name, school level and fit group.\n\n"
        "Send `dry_run=true` first: it reports what would happen and writes "
        "nothing."
    ),
)
class CatalogueImportView(APIView):
    """`POST /api/catalog/import/` — the Garments tab's import.

    Deliberately **not** an action on `GarmentViewSet`. An import creates
    sizes, SKUs and prices as well as garments, so hanging it off the
    garment resource would understate what it does.

    Behind `CanUpdateTables` rather than `MasterDataAccess`: the latter lets
    several roles *read* master data, and this writes a catalogue.

    Bulk-creating stock is a different operation with different risks and
    lives elsewhere — see the inventory count import, which posts
    adjustments to an append-only ledger and can only be undone by more
    adjustments. This one creates products, which can be edited.
    """

    permission_classes = [*AUTHENTICATED, CanUpdateTables]
    serializer_class = CatalogueImportSerializer
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        form = CatalogueImportSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        upload = form.validated_data["file"]
        dry_run = form.validated_data["dry_run"]

        try:
            tally = importing.import_catalogue(
                upload,
                filename=upload.name or "",
                sheet=form.validated_data.get("sheet") or None,
                dry_run=dry_run,
            )
        except importing.CatalogueFileError as exc:
            # About the file as a whole — wrong type, no rows, a missing
            # column. Named on `file`, because that is the field the person
            # chose and the one they have to change.
            raise DRFValidationError({"file": [str(exc)]}) from None
        except importing.RowsRejected as exc:
            # Every bad row, not the first. Fixing a spreadsheet one error
            # per run is miserable when there are thirty, and a screen can
            # show them all at once in a way a terminal cannot.
            #
            # Returned rather than raised. `DRFValidationError` runs every
            # value through `ErrorDetail`, which stringifies recursively —
            # the line numbers came out as "3" and "4" and a client could
            # not sort or link by them without parsing them back. A plain
            # 400 keeps `line` an integer.
            return Response(
                {
                    "detail": (
                        f"Nothing was imported. {len(exc.errors)} "
                        f"{'row' if len(exc.errors) == 1 else 'rows'} could "
                        "not be read."
                    ),
                    "rows": exc.errors,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        created = tally["garments"] + tally["skus"] + tally["prices"]
        if dry_run:
            detail = (
                f"Dry run — nothing was written. {tally['garments']} garments, "
                f"{tally['skus']} SKUs and {tally['prices']} prices would be created."
            )
        elif created:
            detail = (
                f"Imported {tally['garments']} garments, {tally['sizes']} sizes, "
                f"{tally['skus']} SKUs and {tally['prices']} prices."
            )
        else:
            # Worth saying plainly. A re-import that creates nothing looks
            # like a failure until somebody explains that it means the file
            # is already loaded.
            detail = (
                f"Nothing to do — all {tally['unchanged']} garments in that "
                "file are already in the catalogue."
            )

        return Response(
            CatalogueImportResultSerializer({**tally, "dry_run": dry_run, "detail": detail}).data
        )


@extend_schema(
    tags=["Master data — kits"],
    summary="Import a kit bill of materials",
    request={"multipart/form-data": KitImportSerializer},
    responses={
        200: KitImportResultSerializer,
        400: OpenApiResponse(
            description="The file, or some of its rows, could not be read."
        ),
    },
    description=(
        "Upload a spreadsheet of uniform kits and the components in them.\n\n"
        "**Import the garment catalogue first.** Components are matched to "
        "SKUs that already exist, by number — nothing is created here, so a "
        "kit sheet read against an empty catalogue reports every line as an "
        "unknown SKU.\n\n"
        "**One row per component.** Each row repeats its kit's details, "
        "which is how the sheet is written; rows are grouped by class band, "
        "gender, residence and size, and the repeated details are checked "
        "for agreement rather than taken from whichever row came last.\n\n"
        "Required columns: `fit_group`, `gender`, `residence`, `size`, "
        "`component_sku`. Optional: `kit_number`, `kit_name`, `qty` "
        "(defaults to 1).\n\n"
        "**The kit number in the file is checked, not used.** It is composed "
        "from the cohort — `KIT-NUR-B-DAY-XS` — and a file disagreeing with "
        "what the cohort composes is refused rather than silently "
        "renumbered.\n\n"
        "**For a kit named in the file, the file's components replace what "
        "the kit holds** — a line dropped from the sheet is dropped from the "
        "kit. Kits not named in the file are untouched.\n\n"
        "**Nothing is half-imported** and every unreadable row is reported "
        "with its line number. Send `dry_run=true` first."
    ),
)
class KitImportView(APIView):
    """`POST /api/catalog/kits/import/` — the Kits screen's import.

    Its own view rather than an action on `KitViewSet`, for the reason
    `CatalogueImportView` is not an action on `GarmentViewSet`: an import
    writes kits *and* their components, and may remove components, so
    hanging it off the kit resource would understate what it does.

    Behind `CanUpdateTables`, like the catalogue import — `MASTER_DATA`
    lets several roles read kits, and this writes them.
    """

    permission_classes = [*AUTHENTICATED, CanUpdateTables]
    serializer_class = KitImportSerializer
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        form = KitImportSerializer(data=request.data)
        form.is_valid(raise_exception=True)

        upload = form.validated_data["file"]
        dry_run = form.validated_data["dry_run"]

        try:
            tally = kit_importing.import_kits(
                upload,
                filename=upload.name or "",
                sheet=form.validated_data.get("sheet") or None,
                dry_run=dry_run,
            )
        except kit_importing.CatalogueFileError as exc:
            raise DRFValidationError({"file": [str(exc)]}) from None
        except kit_importing.RowsRejected as exc:
            # A plain 400 rather than `DRFValidationError`, which stringifies
            # recursively and would hand the client "3" where it wants 3.
            # Same reasoning as the catalogue import; see there.
            #
            # Problems and rows are counted separately. One SKU missing from
            # the catalogue is one problem on 120 rows, and reporting it as
            # "120 rows could not be read" alongside a single message reads
            # as though 119 were lost somewhere.
            problems, rows = len(exc.errors), exc.rows_affected
            detail = (
                f"Nothing was imported. {problems} "
                f"{'problem' if problems == 1 else 'problems'}"
            )
            if rows != problems:
                detail += f", across {rows} rows"
            return Response(
                {"detail": detail + ".", "rows": exc.errors},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            KitImportResultSerializer(
                {**tally, "dry_run": dry_run, "detail": _kit_import_detail(tally, dry_run)}
            ).data
        )


def _kit_import_detail(tally, dry_run):
    """One sentence saying what happened, for a screen to show as it is."""
    if dry_run:
        return (
            f"Dry run — nothing was written. {tally['kits']} kits and "
            f"{tally['components']} components would be created."
        )

    if tally["kits"] or tally["components"]:
        detail = (
            f"Imported {tally['kits']} kits and {tally['components']} components."
        )
    else:
        # A re-import that creates nothing reads as a failure until somebody
        # says plainly that it means the file is already loaded.
        detail = (
            f"Nothing to do — all {tally['unchanged']} kits in that file are "
            "already in the system."
        )

    # Said last and said plainly: this is the one thing a kit import does
    # that the catalogue import never does.
    if tally["components_removed"]:
        detail += (
            f" {tally['components_removed']} components were removed, because "
            "the file no longer lists them."
        )
    if tally["components_changed"]:
        detail += f" {tally['components_changed']} quantities changed."

    return detail

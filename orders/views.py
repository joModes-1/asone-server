"""HTTP layer for school orders.

The point of sale is the one part of this system where the *school* writes.
Everywhere else they read. So this is also the one place where site scoping
does real work on writes as well as reads: a school clerk sees their own
school's orders and can only create orders for that school.
"""

from django.db.models import Prefetch
from django.db.models.functions import Coalesce
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status, viewsets
from rest_framework.generics import ListAPIView
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
    CanTransferBackorders,
    scope_to_user_site,
)
from catalog.models import School, Warehouse
from config.pagination import SizedPageNumberPagination
from catalog.services import PriceNotSet

from . import reports, services
from .models import Backorder, SchoolOrder, SchoolOrderLine, Shipment
from .models.school_orders import OrderStatus
from .permissions import (
    CanConfirmPayment,
    CanConfirmReceipt,
    CanReadBackorderReport,
    CanReadFulfilmentReports,
    CanReadPackingList,
    CanReadSchoolOrders,
    SchoolOrderAccess,
)
from .serializers import (
    AssignBackorderSerializer,
    TransferOrderSerializer,
    PickingQueueRowSerializer,
    ReleaseEligibleSerializer,
    PickingQueueSerializer,
    PickingSummarySerializer,
    DespatchSerializer,
    ReadyToDespatchSerializer,
    CostedShipmentSerializer,
    PackingListSerializer,
    PartProcessedOrderSerializer,
    BackorderSerializer,
    CancelOrderSerializer,
    FillBackorderSerializer,
    InvoiceSerializer,
    ConfirmReceiptSerializer,
    ReleaseOrderSerializer,
    ShipOrderSerializer,
    ShipmentSerializer,
    OrderOnHoldSerializer,
    OrderAvailabilityRowSerializer,
    OrderDemandRowSerializer,
    SchoolOrderSerializer,
    SchoolOrderWriteSerializer,
)

def _date_param(request, name):
    """Read a `YYYY-MM-DD` query parameter, or None.

    A date that will not parse is a 400 naming the parameter, never a
    silently ignored filter — a report quietly covering all time because
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


#: F37, F38, F39 are warehouse fulfilment actions on an order, not the
#: School Orders Entry column the rest of this viewset belongs to — Bashir,
#: 30 August 2026: reuse CanReceiveAndShip rather than a new permission
#: class, since it already covers exactly this.
_WAREHOUSE_ACTIONS = frozenset(
    {
        "availability", "pick_list", "pick", "pick_available",
        "ship", "shipments", "backorders", "unpick",
    }
)

#: F40's packing list is readable by the school as well as the warehouse —
#: it is the document they use to hand the parcel over, so it is not a
#: warehouse-only action.

#: F35 is neither column: confirming payment is not School Orders Entry and
#: not Warehouse Receiving. It has its own class because it is the seam for
#: open question Q2 — see orders/permissions.py::CanConfirmPayment.
_PAYMENT_ACTIONS = frozenset({"release"})

#: F41's second half is the school reporting that the parcel turned up. It is
#: not School Orders Entry — nothing about the document changes — so it does
#: not belong behind a permission whose refusal talks about placing orders.
#: Its own class is also where Q7 lands if schools cannot get online.
_RECEIPT_ACTIONS = frozenset({"confirm_receipt"})

#: F45. Not Warehouse Receiving: D5 gives Warehouse Staff a Backorder
#: Transfers capability of its own, and it is the one thing a warehouse user
#: does that reaches past their own site. Its own column, its own class.
_TRANSFER_ACTIONS = frozenset({"transfer", "transfer_candidates"})


@extend_schema(tags=["Orders — point of sale"])
class SchoolOrderViewSet(viewsets.ModelViewSet):
    """A student's uniform order — F30, F31, F32, F33.

    **School staff only.** AsOne's matrix leaves the School Orders Entry
    column blank for both leads, and the Role Access sheet omits the point of
    sale from their screens — see `CanEnterSchoolOrders`, and open question
    Q7, which asks whether schools have the computers to do this at all.

    Scoped to the clerk's own school in both directions: they see their
    school's orders, and an order they create belongs to that school
    whatever the request body says.

    **Orders are never deleted.** A school hands the number to a parent as
    an invoice, so the document has to survive. Cancelling (F36) is the way
    out, and only while the order is unpaid.

    Finance may **read** an order and its invoice — F34 gives them a view —
    but may not place or cancel one. See orders/permissions.py.

    `availability`, `pick_list` and `pick` (F37, F38, F39) are the warehouse
    fulfilment actions on the same order — gated by `CanReceiveAndShip`
    instead, via `get_permissions()`, since they are a different matrix
    column from everything else on this viewset.
    """

    permission_classes = [*AUTHENTICATED, SchoolOrderAccess]
    # SchoolOrderAccess splits read from write, and this is the read half.
    # Placing, amending and cancelling stay School Staff only.
    #
    # F34 gives Finance a view of the invoice. The leads are here for the
    # same reason: they already read every report derived from these orders
    # — on-hold (F53), part-processed, backorders, costed shipments — so
    # denying them the list those are drawn from is inconsistent rather than
    # protective. The matrix column this viewset guards is School Orders
    # *Entry*, which is unchanged: neither lead can place, amend or cancel.
    #
    # NOTE: this widens what AsOne's printed matrix (p.9) shows, which leaves
    # the leads' cell blank. Requested by ERA 92 on 9 September 2026 and
    # still needs AsOne's written confirmation — until it has that, this is
    # the one place the code and the matrix disagree on purpose.
    read_roles = (
        User.Role.FINANCE,
        User.Role.PROGRAM_LEAD,
        User.Role.OPERATIONS_MANAGER,
    )
    filterset_fields = ("status", "order_date")
    search_fields = ("number", "student_name")
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_permissions(self):
        # availability/pick_list/pick are warehouse fulfilment, not the
        # School Orders Entry column the rest of this viewset guards.
        if self.action in _WAREHOUSE_ACTIONS:
            return [permission() for permission in [*AUTHENTICATED, CanReceiveAndShip]]
        if self.action in _PAYMENT_ACTIONS:
            return [permission() for permission in [*AUTHENTICATED, CanConfirmPayment]]
        if self.action in _RECEIPT_ACTIONS:
            return [permission() for permission in [*AUTHENTICATED, CanConfirmReceipt]]
        if self.action in _TRANSFER_ACTIONS:
            return [permission() for permission in [*AUTHENTICATED, CanTransferBackorders]]
        if self.action == "packing_lists":
            # F40 leaves the School Staff cell blank — see CanReadPackingList,
            # which explains why that is worth querying with AsOne.
            return [permission() for permission in [*AUTHENTICATED, CanReadPackingList]]
        return super().get_permissions()

    def get_queryset(self):
        queryset = SchoolOrder.objects.select_related(
            "school", "school__primary_warehouse", "created_by"
        ).prefetch_related(
            Prefetch(
                "lines",
                queryset=SchoolOrderLine.objects.select_related(
                    "sku", "sku__garment", "from_kit"
                ),
            )
        )
        # school_field for School Staff (their own school's orders);
        # warehouse_field for Warehouse Staff reaching availability/
        # pick_list/pick — without both, a warehouse clerk's requests would
        # pass CanReceiveAndShip and then find an empty queryset.
        #
        # The warehouse path is the *effective* one, not the school's own.
        # After an F45 transfer those differ, and D2 is explicit that the
        # warehouse doing the filling must see what it is filling —
        # including for a school that is not "theirs". Scoping on
        # school__primary_warehouse would hide the order from the warehouse
        # that had just accepted it, and keep showing it to the one that
        # gave it away.
        queryset = queryset.annotate(
            effective_warehouse=Coalesce(
                "fulfilled_by_warehouse", "school__primary_warehouse"
            )
        )
        return scope_to_user_site(
            queryset,
            self.request.user,
            school_field="school",
            warehouse_field="effective_warehouse",
        )

    def get_serializer_class(self):
        return SchoolOrderWriteSerializer if self.action == "create" else SchoolOrderSerializer

    @extend_schema(
        summary="Place an order",
        request=SchoolOrderWriteSerializer,
        responses={201: SchoolOrderSerializer},
        description=(
            "A school orders at **kit or item level**, or both — send `kits`, "
            "`skus`, or each. At least one line between them.\n\n"
            "Every kit is exploded into its component SKUs on the way in, "
            "because the warehouse picks garments and never \"a kit\". Those "
            "components are stored: editing the kit's bill of materials next "
            "term will not change an order already placed.\n\n"
            "Lines carry the price on the order date, so a reprinted invoice "
            "still adds up.\n\n"
            "The order lands on **Hold** and stays there — releasing it needs "
            "payment confirmed, and what confirms payment is still an open "
            "question with AsOne.\n\n"
            "`school` is not accepted: the order belongs to the clerk's own "
            "school. Nothing retired can be ordered, and a school can only "
            "order from its own level."
        ),
    )
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        if request.user.school is None:
            raise PermissionDenied(
                "Your account is not attached to a school, so it cannot place orders."
            )

        data = dict(serializer.validated_data)

        try:
            order = services.place_order(
                school=request.user.school,
                created_by=request.user,
                kits=data.pop("kits"),
                skus=data.pop("skus"),
                **data,
            )
        except services.EmptyOrder as exc:
            raise DRFValidationError({"skus": str(exc)}) from exc
        except services.InactiveItem as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc
        except services.WrongSchoolLevel as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc
        except PriceNotSet as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc

        return Response(SchoolOrderSerializer(order).data, status=status.HTTP_201_CREATED)

    @extend_schema(
        summary="What the warehouse has to pick",
        responses=OrderDemandRowSerializer(many=True),
        description=(
            "One row per SKU, however many places it came from.\n\n"
            "An order's lines are kept split by source so the invoice can "
            "show the school which items came from the kit it chose. A pick "
            "list does not care — two shirts in a kit plus one ordered loose "
            "is three shirts off the same shelf."
        ),
    )
    @action(detail=True, methods=["get"])
    def demand(self, request, pk=None):
        order = self.get_object()
        rows = [
            {
                "sku_number": sku.number,
                "sku_description": sku.description,
                "quantity": quantity,
            }
            for sku, quantity in services.order_demand(order)
        ]
        return Response(OrderDemandRowSerializer(rows, many=True).data)

    @extend_schema(
        summary="Can the warehouse fill this order — F37",
        responses=OrderAvailabilityRowSerializer(many=True),
        description=(
            "One row per SKU: how many the order needs, how many are "
            "AVAILABLE at the order's warehouse, and the shortfall (0 means "
            "that line can be filled).\n\n"
            "Read-only — checking does not reserve anything. Post to "
            "`pick/` to actually reserve stock; that action refuses using "
            "this exact same comparison, so the two can never disagree."
        ),
    )
    @action(detail=True, methods=["get"])
    def availability(self, request, pk=None):
        order = self.get_object()
        rows = services.check_availability(order)
        return Response(OrderAvailabilityRowSerializer(rows, many=True).data)

    @extend_schema(
        summary="Pick list for the warehouse — F38",
        responses=OrderDemandRowSerializer(many=True),
        description=(
            "Same data as `demand/`, in the same description order — "
            "printed as the sheet a warehouse works from, reachable by "
            "warehouse roles rather than the school that placed the order."
        ),
    )
    @action(detail=True, methods=["get"], url_path="pick-list")
    def pick_list(self, request, pk=None):
        order = self.get_object()
        rows = [
            {
                "sku_number": sku.number,
                "sku_description": sku.description,
                "quantity": quantity,
            }
            for sku, quantity in services.order_demand(order)
        ]
        return Response(OrderDemandRowSerializer(rows, many=True).data)

    @extend_schema(
        summary="Pick this order — F39",
        request=None,
        responses={200: SchoolOrderSerializer},
        description=(
            "Reserves stock for every line: Available -> Pick. Total stock "
            "at the warehouse is unchanged; what changes is how much of it "
            "is still free to promise to a different order.\n\n"
            "Refused if the order is cancelled or already picked/shipped, "
            "or if any line is short — check `availability/` first."
        ),
    )
    @action(detail=True, methods=["post"])
    def pick(self, request, pk=None):
        order = self.get_object()

        try:
            services.pick_order(order, picked_by=request.user)
        except services.OrderCannotBePicked as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc
        except services.OrderNotFillable as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc

        order.refresh_from_db()
        return Response(SchoolOrderSerializer(order).data)

    @extend_schema(
        summary="Warehouses that could fill this order",
        responses={200: OpenApiTypes.OBJECT},
        description=(
            "F45's shortlist — which warehouses hold enough of **every** "
            "line to finish the order.\n\n"
            "Every line, not some. Under the pack's hold-complete rule "
            "(p.8) a transfer is only worth making to a warehouse that can "
            "finish the job; offering one that would itself come up short "
            "just moves the waiting somewhere else.\n\n"
            "The warehouse currently responsible is excluded. A clerk "
            "cannot see another site's shelves, so asking them to guess is "
            "how an order gets sent somewhere empty."
        ),
    )
    @action(detail=True, methods=["get"], url_path="transfer-candidates")
    def transfer_candidates(self, request, pk=None):
        order = self.get_object()
        return Response(
            [
                {"id": warehouse.pk, "name": warehouse.name}
                for warehouse in services.warehouses_that_could_fill_order(order)
            ]
        )

    @extend_schema(
        summary="Transfer the order to a warehouse with stock",
        request=TransferOrderSerializer,
        responses={200: SchoolOrderSerializer},
        description=(
            "F45 — p.8's \"option to transfer an order to another warehouse "
            "with Inventory\", and decision D2.\n\n"
            "**Nothing moves in the ledger.** No stock is reserved at "
            "either end; the receiving warehouse picks in the ordinary way "
            "afterwards and that pick is what touches inventory. What "
            "changes here is who is responsible.\n\n"
            "The school keeps its primary warehouse — D2 is two rules and "
            "this is only the fulfilment one. Future orders are unaffected."
            "\n\nRefused if the target cannot fill every line, if it is "
            "already the one filling it, or if the order has been picked: "
            "stock is reserved by then, and re-pointing the order would "
            "strand that reservation."
        ),
    )
    @action(detail=True, methods=["post"])
    def transfer(self, request, pk=None):
        order = self.get_object()
        serializer = TransferOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            services.transfer_order(
                order,
                warehouse=serializer.validated_data["warehouse"],
                transferred_by=request.user,
                reason=serializer.validated_data.get("reason", ""),
            )
        except services.OrderNotTransferable as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc
        except services.NoStockToFill as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc

        order.refresh_from_db()
        return Response(SchoolOrderSerializer(order).data)

    @extend_schema(
        summary="The invoice",
        responses=InvoiceSerializer,
        description=(
            "F34 — the order as a document a school can hand to a parent.\n\n"
            "Same number as the order: AsOne treats the two as one thing, "
            "because the school uses the invoice number and the student's "
            "name to give the right parcel to the right child.\n\n"
            "Kit lines are grouped back under the kit the school chose, with "
            "a subtotal — AsOne's definition names the kit number \"if "
            "used\". Individually ordered items are listed separately. That "
            "regrouping is presentation only; the order's lines remain "
            "individual SKUs, which is what the warehouse picks."
        ),
    )
    @action(detail=True, methods=["get"])
    def invoice(self, request, pk=None):
        order = self.get_object()
        grouped = services.invoice_for(order)

        return Response(
            InvoiceSerializer(
                {
                    "number": order.number,
                    "student_name": order.student_name,
                    "school": order.school,
                    "order_date": order.order_date,
                    "status": order.status,
                    "get_status_display": order.get_status_display(),
                    "total": order.total,
                    "kits": grouped["kits"],
                    "items": grouped["items"],
                    "cancelled_at": order.cancelled_at,
                    "cancellation_reason": order.cancellation_reason,
                }
            ).data
        )

    @extend_schema(
        summary="Pick what is available, backorder the rest",
        responses={200: BackorderSerializer(many=True)},
        description=(
            "F43 — the partial counterpart to `pick/`, which refuses an "
            "order it cannot fill completely.\n\n"
            "Reserves every unit the warehouse holds and raises a backorder "
            "for each shortfall. Both endpoints exist because they answer "
            "different questions: *can I fill this?* and *fill what you "
            "can, we will chase the rest*.\n\n"
            "Refused if not one unit is available — that is not a partial "
            "pick, and marking the order Picked would be untrue."
        ),
    )
    @action(detail=True, methods=["post"], url_path="pick-available")
    def pick_available(self, request, pk=None):
        order = self.get_object()

        try:
            _, backorders = services.pick_available(order, picked_by=request.user)
        except services.NothingToPick as exc:
            raise DRFValidationError({"status": str(exc)}) from exc

        return Response(BackorderSerializer(backorders, many=True).data)

    @extend_schema(
        summary="Backorders raised on this order",
        responses=BackorderSerializer(many=True),
    )
    @action(detail=True, methods=["get"])
    def backorders(self, request, pk=None):
        order = self.get_object()
        rows = order.backorders.select_related(
            "order__school__primary_warehouse", "sku__garment", "filled_by_warehouse"
        )
        return Response(BackorderSerializer(rows, many=True).data)

    @extend_schema(
        summary="Ship a picked order",
        request=ShipOrderSerializer,
        responses={201: ShipmentSerializer},
        description=(
            "F41 — what was reserved at pick leaves the warehouse. Two "
            "ledger rows per line: out of Pick, into Shipped.\n\n"
            "Ships **what is actually reserved**, read from the ledger, not "
            "what the order asked for. Those differ whenever a pick was "
            "short.\n\n"
            "`from_warehouse` defaults to the school's own but may be set: "
            "decision D2 lets a backorder ship direct from whichever "
            "warehouse filled it.\n\n"
            "**Open question Q1.** AsOne's chart reads \"Shipped ???\". We "
            "have taken shipped to mean *left the warehouse*, because that "
            "is what a clerk can observe. If they decide arrival is what "
            "counts, that is an added confirmation field, not a change to "
            "when the ledger moves — see `orders/services/shipping.py`."
        ),
    )
    @action(detail=True, methods=["post"])
    def ship(self, request, pk=None):
        order = self.get_object()

        serializer = ShipOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            shipment = services.ship_order(
                order,
                shipped_by=request.user,
                from_warehouse=data.get("from_warehouse"),
                shipped_on=data.get("shipped_on"),
                waybill_number=data["waybill_number"],
                notes=data["notes"],
            )
        except (services.OrderCannotBeShipped, services.NothingToShip) as exc:
            raise DRFValidationError({"status": str(exc)}) from exc

        return Response(
            ShipmentSerializer(shipment).data, status=status.HTTP_201_CREATED
        )

    @extend_schema(
        summary="Undo a pick",
        request=None,
        responses={200: SchoolOrderSerializer},
        description=(
            "Puts a mistakenly picked order back on the shelf — the undo for "
            "F39.\n\n"
            "Picking is one click and it reserves stock, so a wrong click can "
            "refuse the next school's order for a shortfall that is not real. "
            "This posts the **offsetting** ledger pair — out of Pick, back "
            "into Available — at the value the stock is carried at. Nothing "
            "is deleted: the ledger is append-only and the history reads as "
            "what happened.\n\n"
            "Refused once the order has shipped. Stock that has left the "
            "building comes back as a return, not by undoing a pick."
        ),
    )
    @action(detail=True, methods=["post"])
    def unpick(self, request, pk=None):
        order = self.get_object()

        try:
            services.unpick_order(
                order,
                unpicked_by=request.user,
                reason=(request.data or {}).get("reason", ""),
            )
        except services.OrderCannotBeUnpicked as exc:
            raise DRFValidationError({"status": str(exc)}) from exc

        order.refresh_from_db()
        return Response(SchoolOrderSerializer(order).data)

    @extend_schema(
        summary="Shipments for this order",
        responses=ShipmentSerializer(many=True),
        description=(
            "Every despatch against this order. More than one is normal: a "
            "backorder filled by another warehouse ships separately, direct "
            "to the school (D2)."
        ),
    )
    @action(detail=True, methods=["get"])
    def shipments(self, request, pk=None):
        order = self.get_object()
        rows = order.shipments.select_related(
            "from_warehouse", "school", "shipped_by"
        ).prefetch_related("lines__sku", "lines__order")
        return Response(ShipmentSerializer(rows, many=True).data)

    @extend_schema(
        summary="Confirm a parcel arrived",
        request=ConfirmReceiptSerializer,
        responses={200: ShipmentSerializer},
        description=(
            "The school says the shipment arrived. The order becomes "
            "**Completed** once every shipment on it is confirmed — an order "
            "can have two, because a backorder may ship direct from another "
            "warehouse (D2).\n\n"
            "**This does not touch stock.** It left at ship and stays gone. "
            "Recording it here instead would mean stock the warehouse has "
            "physically handed to a driver still counting as theirs.\n\n"
            "`shipment` may be omitted when the order has only one.\n\n"
            "`notes` is for what was wrong — short, damaged, wrong student. "
            "It is recorded and nothing acts on it: what to *do* about a bad "
            "delivery is a question AsOne has not answered."
        ),
    )
    @action(detail=True, methods=["post"], url_path="confirm-receipt")
    def confirm_receipt(self, request, pk=None):
        order = self.get_object()

        serializer = ConfirmReceiptSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        shipment_id = serializer.validated_data.get("shipment")
        outstanding = order.shipments.all()

        if shipment_id is not None:
            shipment = outstanding.filter(pk=shipment_id).first()
            if shipment is None:
                raise DRFValidationError(
                    {"shipment": "That shipment does not belong to this order."}
                )
        elif outstanding.count() == 1:
            shipment = outstanding.first()
        elif not outstanding:
            raise DRFValidationError(
                {"shipment": f"{order.number} has not been shipped yet."}
            )
        else:
            # Guessing which of several parcels arrived would silently
            # complete the wrong one.
            raise DRFValidationError(
                {
                    "shipment": (
                        f"{order.number} has {outstanding.count()} shipments. "
                        "Say which one arrived."
                    )
                }
            )

        try:
            services.confirm_receipt(
                shipment,
                confirmed_by=request.user,
                notes=serializer.validated_data["notes"],
            )
        except services.CannotConfirmReceipt as exc:
            raise DRFValidationError({"shipment": str(exc)}) from exc

        shipment.refresh_from_db()
        return Response(ShipmentSerializer(shipment).data)

    @extend_schema(
        summary="Packing lists for this order",
        responses=PackingListSerializer(many=True),
        description=(
            "F40 — the document that travels with the goods. One per "
            "shipment, because a backorder filled elsewhere travels "
            "separately.\n\n"
            "Carries the **invoice number and the student's name together**, "
            "which is how AsOne's definitions page says a school hands the "
            "right uniform to the right child. Either alone is not enough: "
            "two children can share a name, and a number means nothing to "
            "the person handing out parcels.\n\n"
            "Returns data, not a PDF — rendering it is the frontend's job."
        ),
    )
    @action(detail=True, methods=["get"], url_path="packing-lists")
    def packing_lists(self, request, pk=None):
        order = self.get_object()
        shipments = order.shipments.select_related(
            "from_warehouse", "school", "school__primary_warehouse"
        ).prefetch_related("lines__sku__garment", "lines__order")

        return Response(
            PackingListSerializer(
                [services.packing_list_for(s) for s in shipments], many=True
            ).data
        )

    @extend_schema(
        summary="Confirm payment and release to the warehouse",
        request=ReleaseOrderSerializer,
        responses={200: SchoolOrderSerializer},
        description=(
            "F35 — the invoice is paid, so the warehouse may act on the "
            "order. Records who confirmed it and when.\n\n"
            "**Open question Q2.** AsOne's chart says an order waits on Hold "
            "until \"School Monitor\" confirms payment, and nobody has told "
            "us what School Monitor is. The transition is built; *who may "
            "call it* is the placeholder. It is currently Finance, which is "
            "our reading and not AsOne's instruction — see "
            "`orders/permissions.py::CanConfirmPayment`.\n\n"
            "Refused unless the order is still on Hold: releasing a "
            "cancelled order would resurrect a document the school withdrew, "
            "and releasing a picked one would restate history.\n\n"
            "Note that picking does **not** yet require this — see "
            "`REQUIRE_RELEASE_BEFORE_PICK`."
        ),
    )
    @action(detail=True, methods=["post"])
    def release(self, request, pk=None):
        order = self.get_object()

        serializer = ReleaseOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            services.release_order(
                order,
                released_by=request.user,
                payment_reference=serializer.validated_data["payment_reference"],
            )
        except services.CannotRelease as exc:
            raise DRFValidationError({"status": str(exc)}) from exc

        order.refresh_from_db()
        return Response(SchoolOrderSerializer(order).data)

    @extend_schema(
        summary="Cancel an unpaid invoice",
        request=CancelOrderSerializer,
        responses={200: SchoolOrderSerializer},
        description=(
            "F36 — the school withdraws an order a parent has not paid for.\n\n"
            "The order is **cancelled, not deleted**. The school has already "
            "given that number to a parent, so the document has to survive "
            "and say what became of it. Who cancelled it and when are "
            "recorded.\n\n"
            "Only while the order is still on Hold. Once payment is confirmed "
            "and the order released, cancelling raises questions AsOne has "
            "not answered — whether picked stock goes back on the shelf, and "
            "what happens to money already taken — so it is refused rather "
            "than guessed at."
        ),
    )
    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        order = self.get_object()

        serializer = CancelOrderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            services.cancel_order(
                order,
                cancelled_by=request.user,
                reason=serializer.validated_data["reason"],
            )
        except services.CannotCancel as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc

        order.refresh_from_db()
        return Response(SchoolOrderSerializer(order).data)


@extend_schema(
    tags=["Orders — reports"],
    summary="School orders still on hold",
    responses=OrderOnHoldSerializer(many=True),
    description=(
        "F53 — invoices raised but not yet paid or released.\n\n"
        "The queue a school works from: what is waiting on a parent to pay. "
        "The leads and Finance see every school; a school clerk sees only "
        "its own.\n\n"
        "Oldest first, because that is the order they should be chased in."
    ),
)
class OrdersOnHoldView(ListAPIView):
    """F53. Wider than the point of sale itself — the leads read this too,
    even though they cannot place an order."""

    serializer_class = OrderOnHoldSerializer
    permission_classes = [*AUTHENTICATED, CanReadSchoolOrders]

    def get_queryset(self):
        scoped = scope_to_user_site(
            SchoolOrder.objects.all(), self.request.user, school_field="school"
        )
        return services.orders_on_hold(scoped)


@extend_schema(tags=["Orders — backorders"])
class BackorderViewSet(viewsets.ReadOnlyModelViewSet):
    """What schools are still owed, and who is filling it — F44, F45, F46.

    **The one place a warehouse user reaches past their own site.** Decision
    D5 gives Warehouse Staff the Backorder Transfers column, and a transfer
    is meaningless if the clerk sending it cannot see the receiving
    warehouse. So the scoping here is deliberately wider than everywhere
    else, and deliberately narrow about *what* it widens:

        a clerk sees backorders their own warehouse could not supply
        and backorders another warehouse has assigned to them

    They do not see a third warehouse's outstanding queue. The leads and
    Finance see all of them.

    Read-only as a resource. The two things that change a backorder are
    `assign/` and `fill/`, because both do more than set a field.
    """

    # Declared so drf-spectacular can derive the path parameter type without
    # calling get_queryset(), which needs a request. get_queryset() overrides
    # it for every real call.
    queryset = Backorder.objects.none()
    serializer_class = BackorderSerializer
    permission_classes = [*AUTHENTICATED, CanTransferBackorders]
    filterset_fields = ("status", "sku")

    def get_queryset(self):
        queryset = Backorder.objects.select_related(
            "order",
            "order__school",
            "order__school__primary_warehouse",
            "sku",
            "sku__garment",
            "filled_by_warehouse",
        )

        user = self.request.user
        if user.role != User.Role.WAREHOUSE_STAFF:
            # Leads and Finance are all-locations; CanTransferBackorders has
            # already refused everyone else.
            return queryset

        if user.warehouse_id is None:
            return queryset.none()

        # Ours to chase, or ours to fill. Not a third warehouse's problem.
        from django.db.models import Q

        return queryset.filter(
            Q(order__school__primary_warehouse_id=user.warehouse_id)
            | Q(filled_by_warehouse_id=user.warehouse_id)
        )

    @extend_schema(
        summary="Warehouses that could fill this",
        responses=OpenApiTypes.OBJECT,
        description=(
            "F45's shortlist — warehouses holding enough to fill this "
            "backorder, excluding the one that ran short.\n\n"
            "Exists because a clerk cannot see another site's shelves. "
            "Without it, assigning a backorder is guesswork, and a "
            "backorder sent to an empty warehouse is a queue nobody can "
            "clear."
        ),
    )
    @action(detail=True, methods=["get"], url_path="candidates")
    def candidates(self, request, pk=None):
        backorder = self.get_object()
        return Response(
            [
                {"id": warehouse.pk, "name": warehouse.name}
                for warehouse in services.warehouses_that_could_fill(backorder)
            ]
        )

    @extend_schema(
        summary="Assign to a warehouse with stock",
        request=AssignBackorderSerializer,
        responses={200: BackorderSerializer},
        description=(
            "F45 — hand the backorder to a warehouse that has the stock.\n\n"
            "**Nothing moves in the ledger.** The receiving warehouse still "
            "holds its stock and will ship it in the ordinary way; what "
            "changes is who owes the school.\n\n"
            "Refused if that warehouse does not hold enough, or if it is the "
            "warehouse that ran short in the first place."
        ),
    )
    @action(detail=True, methods=["post"])
    def assign(self, request, pk=None):
        backorder = self.get_object()

        serializer = AssignBackorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            services.assign_backorder(
                backorder,
                warehouse=serializer.validated_data["warehouse"],
                assigned_by=request.user,
            )
        except services.CannotAssign as exc:
            raise DRFValidationError({"status": str(exc)}) from exc
        except services.NoStockToFill as exc:
            raise DRFValidationError({"warehouse": str(exc)}) from exc

        backorder.refresh_from_db()
        return Response(BackorderSerializer(backorder).data)

    @extend_schema(
        summary="Backorders that could be filled today",
        responses=BackorderSerializer(many=True),
        description=(
            "Open backorders some warehouse is holding stock for — the queue "
            "behind Release All Eligible.\n\n"
            "Checks **every** warehouse, not just the school's own: decision "
            "D2 lets another warehouse ship direct, so stock at Serere can "
            "fill a Namayemba shortfall."
        ),
    )
    @action(detail=False, methods=["get"], url_path="eligible")
    def eligible(self, request):
        rows = services.eligible_for_release(request.user.warehouse)
        return Response(BackorderSerializer(rows, many=True).data)

    @extend_schema(
        summary="Release every eligible backorder",
        request=ReleaseEligibleSerializer,
        responses={201: ShipmentSerializer(many=True)},
        description=(
            "Assigns each backorder to the warehouse holding the most of "
            "that SKU and ships it direct to the school — the same two steps "
            "as `assign` then `fill`, done together.\n\n"
            "**All or nothing.** One transaction, so a backorder that cannot "
            "be filled halfway through does not leave half the queue "
            "released: a clerk who pressed one button gets one outcome."
        ),
    )
    @action(detail=False, methods=["post"], url_path="release-eligible")
    def release_eligible(self, request):
        chosen = ReleaseEligibleSerializer(data=request.data)
        chosen.is_valid(raise_exception=True)
        ids = chosen.validated_data.get("backorders")

        queryset = self.get_queryset()
        picked = list(queryset.filter(pk__in=ids)) if ids else None

        if ids and len(picked) != len(set(ids)):
            raise DRFValidationError(
                {"backorders": "Some of those are not yours, or do not exist."}
            )

        try:
            shipments = services.release_eligible(
                released_by=request.user,
                warehouse=request.user.warehouse,
                backorders=picked,
            )
        except services.NoStockToFill as exc:
            raise DRFValidationError({"backorders": str(exc)}) from exc
        except services.CannotAssign as exc:
            raise DRFValidationError({"status": str(exc)}) from exc

        return Response(
            ShipmentSerializer(shipments, many=True).data,
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Ship it direct to the school",
        request=FillBackorderSerializer,
        responses={201: ShipmentSerializer},
        description=(
            "F46 — the assigned warehouse ships straight to the school.\n\n"
            "This is the half of decision D2 that overrides the definitions "
            "page: the stock does **not** route back through the school's "
            "own warehouse. It goes from the shelf that had it to the "
            "school.\n\n"
            "Reserves and ships in one step — the warehouse already "
            "committed when it accepted the backorder, so there is nothing "
            "in between for it to decide."
        ),
    )
    @action(detail=True, methods=["post"])
    def fill(self, request, pk=None):
        backorder = self.get_object()

        serializer = FillBackorderSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        try:
            shipment = services.fill_backorder(
                backorder,
                filled_by=request.user,
                shipped_on=data.get("shipped_on"),
                waybill_number=data["waybill_number"],
                notes=data["notes"],
            )
        except services.CannotAssign as exc:
            raise DRFValidationError({"status": str(exc)}) from exc
        except services.NoStockToFill as exc:
            raise DRFValidationError({"quantity": str(exc)}) from exc

        return Response(
            ShipmentSerializer(shipment).data, status=status.HTTP_201_CREATED
        )


@extend_schema(
    tags=["Orders — reports"],
    summary="Backorders outstanding",
    responses=BackorderSerializer(many=True),
)
class OutstandingBackordersView(ListAPIView):
    """F49 — what schools are still owed, and what each is waiting on.

    The status is the "waiting on": OPEN needs somebody to find stock,
    ASSIGNED needs somebody to put it on a van. Different problems, and a
    report that merged them would hide the one that is stuck.

    The checklist gives this to the leads, Finance, a warehouse for its own
    site and a school for its own orders — the widest audience of the four
    fulfilment reports, because everybody is waiting on it.
    """

    serializer_class = BackorderSerializer
    permission_classes = [*AUTHENTICATED, CanReadBackorderReport]

    def get_queryset(self):
        return scope_to_user_site(
            reports.outstanding_backorders(),
            self.request.user,
            school_field="order__school",
            warehouse_field="order__school__primary_warehouse",
        )


@extend_schema(
    tags=["Orders — reports"],
    summary="Orders picked but not despatched",
    responses=PartProcessedOrderSerializer(many=True),
    description=(
        "F52 and F54 — orders with a pick list and no packing list.\n\n"
        "Stock is off the shelf, committed to a named student, and still in "
        "the building. It is also where stock quietly sits when somebody "
        "picks an order and forgets it.\n\n"
        "**Interpretation worth checking with AsOne.** A packing list comes "
        "into existence with a shipment (F40), so an order picked but "
        "not yet shipped is what this reports. If AsOne means a "
        "separate step between picking and despatch, this report and F40 "
        "both change — "
        "see `orders/reports.py`."
    ),
)
class PartProcessedOrdersView(ListAPIView):
    """F52 and F54, which the checklist lists separately and which ask the
    same question of the data.

    They differ only in audience — F54 includes school staff for their own
    schools, F52 does not — so they share one query rather than being
    written twice and drifting apart. `scope_to_user_site` supplies the
    difference: a school clerk gets their own school's rows.
    """

    serializer_class = PartProcessedOrderSerializer
    permission_classes = [*AUTHENTICATED, CanReadFulfilmentReports]

    def get_queryset(self):
        return scope_to_user_site(
            reports.part_processed_orders(),
            self.request.user,
            school_field="school",
            warehouse_field="school__primary_warehouse",
        )


@extend_schema(
    tags=["Orders — reports"],
    summary="Shipments to schools, costed",
    parameters=[
        OpenApiParameter("from", str, description="Inclusive start date, YYYY-MM-DD."),
        OpenApiParameter("to", str, description="Inclusive end date, YYYY-MM-DD."),
    ],
    responses=CostedShipmentSerializer(many=True),
    description=(
        "F57 — what went to each school and what it was worth.\n\n"
        "Valued at **what the school was charged**, snapshotted when the "
        "order was placed — deliberately a different number from the costed "
        "adjustments report, which values stock at what the warehouse "
        "carries it at. A shipment's value to Finance is what the school "
        "owes; a write-off's value is what the stock cost."
    ),
)
class CostedShipmentsView(APIView):
    """F57. A money report — the leads and Finance, per the checklist."""

    permission_classes = [*AUTHENTICATED, CanViewFinancialReports]

    def get(self, request):
        rows = reports.shipments_costed(
            date_from=_date_param(request, "from"),
            date_to=_date_param(request, "to"),
        )
        return Response(CostedShipmentSerializer(rows, many=True).data)


@extend_schema(tags=["Orders — fulfilment"])
class ShipmentViewSet(viewsets.ReadOnlyModelViewSet):
    """Everything that has left a warehouse — F41.

    **Read only, deliberately.** A shipment is not created by posting to a
    collection; it is created by `ship_order()`, which moves reserved stock
    out of the ledger in the same transaction. Letting a client POST a
    shipment row would let it claim goods left the building without the stock
    ever moving, which is the one thing the ledger exists to prevent.
    Despatch stays `POST /school-orders/{id}/ship/`.

    ## Who sees it

    The same audience as the fulfilment reports: the two leads everywhere,
    warehouse staff for their own site, and a school for its own parcels.
    Finance is excluded — the matrix gives them the costed reports, not the
    operational backlog, and the costed view of exactly these rows already
    exists at `reports/shipments-costed/`.

    Scoping is two-sided: a warehouse clerk sees what left their warehouse, a
    school sees what is coming to it. Those are different columns, so the two
    are handled separately rather than by one call that can only mean one.
    """

    permission_classes = [*AUTHENTICATED, CanReadFulfilmentReports]
    serializer_class = ShipmentSerializer
    filterset_fields = ("from_warehouse", "shipped_on")
    search_fields = ("number", "lines__order__number", "school__name")

    def get_queryset(self):
        queryset = (
            Shipment.objects.select_related("school", "from_warehouse", "shipped_by")
            .prefetch_related("lines__sku", "lines__order")
            .order_by("-shipped_on", "-number")
        )

        user = self.request.user
        if getattr(user, "role", None) == User.Role.SCHOOL_STAFF:
            return scope_to_user_site(queryset, user, school_field="school")

        return scope_to_user_site(queryset, user, warehouse_field="from_warehouse")

    @extend_schema(
        summary="Shipments",
        parameters=[
            OpenApiParameter("school", OpenApiTypes.INT, description="Destination school id."),
            OpenApiParameter(
                "status",
                OpenApiTypes.STR,
                description="SHIPPED (left, not yet confirmed) or DELIVERED (school confirmed).",
            ),
            OpenApiParameter("shipped_from", OpenApiTypes.DATE, description="On or after, YYYY-MM-DD."),
            OpenApiParameter("shipped_to", OpenApiTypes.DATE, description="On or before, YYYY-MM-DD."),
            OpenApiParameter("search", OpenApiTypes.STR, description="Shipment or order number, or school name."),
        ],
        description=(
            "What has left the warehouses, newest first.\n\n"
            "`status` is **derived**, not stored: SHIPPED means it left, "
            "DELIVERED means the school confirmed it arrived. There is no "
            "'preparing' state — a shipment row does not exist until despatch "
            "creates it."
        ),
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    def filter_queryset(self, queryset):
        queryset = super().filter_queryset(queryset)

        school = self.request.query_params.get("school")
        if school:
            queryset = queryset.filter(school_id=school)

        wanted = (self.request.query_params.get("status") or "").upper()
        if wanted == "DELIVERED":
            queryset = queryset.filter(received_at__isnull=False)
        elif wanted == "SHIPPED":
            queryset = queryset.filter(received_at__isnull=True)

        # The design's date-range control. Both ends inclusive, as a person
        # means when they ask for August.
        shipped_from = _date_param(self.request, "shipped_from")
        if shipped_from:
            queryset = queryset.filter(shipped_on__gte=shipped_from)

        shipped_to = _date_param(self.request, "shipped_to")
        if shipped_to:
            queryset = queryset.filter(shipped_on__lte=shipped_to)

        return queryset


@extend_schema(tags=["Orders — fulfilment"])
class DespatchQueueView(APIView):
    """What is picked and waiting to go, grouped by school — F42.

    The despatch screen's list: a school, how many of its orders are ready,
    and how many garments that is. Grouped because the van is per school.
    """

    permission_classes = [*AUTHENTICATED, CanReceiveAndShip]

    @extend_schema(
        summary="Schools with orders ready to despatch",
        responses=ReadyToDespatchSerializer(many=True),
    )
    def get(self, request):
        warehouse = request.user.warehouse
        if warehouse is None:
            requested = request.query_params.get("warehouse")
            if not requested:
                raise DRFValidationError(
                    {"warehouse": "Say which warehouse you are despatching from."}
                )
            warehouse = get_object_or_404(Warehouse, pk=requested)

        rows = services.orders_ready_to_despatch(warehouse)
        return Response(ReadyToDespatchSerializer(rows, many=True).data)


@extend_schema(tags=["Orders — fulfilment"])
class DespatchView(APIView):
    """Send a school's picked orders out on one van — F42.

    AsOne's checklist asks for a consolidated weekly despatch per school. The
    van is the document; the stock moves exactly as it would shipping each
    order on its own, so a consolidated despatch and a single one are worth
    the same to Finance.
    """

    permission_classes = [*AUTHENTICATED, CanReceiveAndShip]

    @extend_schema(
        summary="Despatch to a school",
        request=DespatchSerializer,
        responses={201: ShipmentSerializer},
        description=(
            "Loads every picked order waiting for that school onto one "
            "shipment, or just the ones named in `orders`.\n\n"
            "Refused if an order is not picked, is cancelled, or belongs to "
            "another school — a clerk who asked for it to go needs to know "
            "it did not, rather than find it left behind."
        ),
    )
    def post(self, request):
        serializer = DespatchSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        school = get_object_or_404(School, pk=data["school"])

        warehouse = request.user.warehouse
        if data.get("from_warehouse"):
            warehouse = get_object_or_404(Warehouse, pk=data["from_warehouse"])
        if warehouse is None:
            raise DRFValidationError(
                {"from_warehouse": "Say which warehouse this is leaving from."}
            )

        # A clerk despatches from their own site and no other.
        if not scope_to_user_site(
            Warehouse.objects.filter(pk=warehouse.pk),
            request.user,
            warehouse_field="pk",
        ).exists():
            raise PermissionDenied("That is not your warehouse.")

        orders = None
        if data.get("orders"):
            orders = list(
                scope_to_user_site(
                    SchoolOrder.objects.filter(pk__in=data["orders"]),
                    request.user,
                    warehouse_field="school__primary_warehouse",
                    school_field="school",
                )
            )
            missing = set(data["orders"]) - {order.pk for order in orders}
            if missing:
                raise DRFValidationError(
                    {"orders": f"Not found, or not yours: {sorted(missing)}."}
                )

        try:
            shipment = services.despatch_to_school(
                school=school,
                from_warehouse=warehouse,
                shipped_by=request.user,
                orders=orders,
                shipped_on=data.get("shipped_on"),
                waybill_number=data.get("waybill_number", ""),
                carrier_method=data.get("carrier_method", ""),
                notes=data.get("notes", ""),
            )
        except services.NothingReadyToDespatch as exc:
            raise DRFValidationError({"orders": str(exc)}) from exc
        except (services.OrderCannotBeShipped, services.NothingToShip) as exc:
            raise DRFValidationError({"orders": str(exc)}) from exc

        return Response(
            ShipmentSerializer(shipment).data, status=status.HTTP_201_CREATED
        )


@extend_schema(tags=["Orders — fulfilment"])
class OrdersAwaitingStockView(APIView):
    """Released orders their warehouse cannot fill — F43, F44.

    The queue page 8 of the pack describes: "orders held until enough
    inventory is received to release a picklist", "released in a FIFO
    sequence". Oldest first, because that is what FIFO means here — the
    sequence the schools placed them in, not the sequence somebody opened
    them in.

    Each row carries what it is waiting on, so the screen can say *why* an
    order is stuck rather than only that it is. A clerk looking at this is
    deciding whether to wait for the next delivery or transfer the order,
    and they cannot decide that without seeing the shortfall.
    """

    permission_classes = [*AUTHENTICATED, CanReceiveAndShip]

    @extend_schema(
        summary="Orders waiting for stock",
        responses={200: OpenApiTypes.OBJECT},
        parameters=[
            OpenApiParameter(
                "warehouse",
                OpenApiTypes.INT,
                description="Required for an all-locations role; ignored for a clerk.",
            ),
        ],
        description=(
            "F43 and F44 — the held-order queue, oldest first.\n\n"
            "An order appears here when it has been paid for and its "
            "warehouse cannot fill **every** line. Under the pack's rule "
            "(p.8) nothing part-ships, so a single short line holds the "
            "whole order.\n\n"
            "It leaves the queue by one of two routes: stock arrives and it "
            "can be picked, or it is transferred to a warehouse that has "
            "the stock — `school-orders/{id}/transfer/`.\n\n"
            "Derived on read, not a stored list. There is no backorder "
            "record to create, resolve or clean up."
        ),
    )
    def get(self, request):
        warehouse = request.user.warehouse
        if warehouse is None:
            requested = request.query_params.get("warehouse")
            warehouse = (
                get_object_or_404(Warehouse, pk=requested) if requested else None
            )

        return Response(
            [
                {
                    "order": SchoolOrderSerializer(entry["order"]).data,
                    "waiting_on": [
                        {
                            "sku": row["sku"].number,
                            "description": row["sku"].description,
                            "needed": row["needed"],
                            "available": row["available"],
                            "shortfall": row["shortfall"],
                        }
                        for row in entry["shortfalls"]
                    ],
                }
                for entry in services.orders_awaiting_stock(warehouse)
            ]
        )


class PickingQueueView(APIView):
    """What the warehouse has to pull off the shelves — F38.

    The picking screen's landing view: three counts and the backlog itself,
    most urgent first.

    **There is no "in progress".** `pick_order` is atomic — an order is
    picked or it is not — because a half-finished reservation would let the
    ledger say stock is committed to an order nobody completed. Whether
    picking should be resumable is open question Q4, which AsOne has not
    answered.
    """

    permission_classes = [*AUTHENTICATED, CanReceiveAndShip]

    @extend_schema(
        summary="The picking backlog",
        responses=PickingQueueSerializer,
        parameters=[
            OpenApiParameter(
                "warehouse",
                OpenApiTypes.INT,
                description="Required for an all-locations role; ignored for a clerk.",
            ),
            OpenApiParameter(
                "status",
                OpenApiTypes.STR,
                enum=[OrderStatus.RELEASED, OrderStatus.PICKED],
                description=(
                    "Narrow the rows to one bucket: RELEASED is still to "
                    "pick, PICKED is off the shelf and waiting for a van. "
                    "Omit for both. `summary` is unaffected."
                ),
            ),
            OpenApiParameter("page", OpenApiTypes.INT),
            OpenApiParameter(
                "page_size", OpenApiTypes.INT, description="Capped at 200."
            ),
        ],
        description=(
            "The backlog, most urgent first, paginated.\n\n"
            "`summary` counts the **whole** queue, not the page: a warehouse "
            "asking how much is waiting means all of it, and a tile that "
            "changed as you paged would be worse than no tile. It is also "
            "unaffected by `status`, for the same reason — the tiles are the "
            "totals the filter is chosen from."
        ),
    )
    def get(self, request):
        warehouse = request.user.warehouse
        if warehouse is None:
            requested = request.query_params.get("warehouse")
            warehouse = (
                get_object_or_404(Warehouse, pk=requested) if requested else None
            )

        rows = reports.picking_queue(warehouse)

        # Only the two statuses the backlog can hold. Anything else is a
        # typo'd URL, and answering it with an empty page would look like a
        # warehouse with nothing to do.
        #
        # Named `bucket` rather than `status`: this module imports DRF's
        # `status` for its HTTP codes, and shadowing it here would be a trap
        # for whoever adds the next line to this method.
        bucket = request.query_params.get("status")
        if bucket:
            if bucket not in (OrderStatus.RELEASED, OrderStatus.PICKED):
                raise DRFValidationError(
                    {
                        "status": (
                            f"'{bucket}' is not a backlog status. Use "
                            f"{OrderStatus.RELEASED} or {OrderStatus.PICKED}."
                        )
                    }
                )
            rows = rows.filter(status=bucket)

        paginator = SizedPageNumberPagination()
        page = paginator.paginate_queryset(rows, request, view=self)
        orders = PickingQueueRowSerializer(page, many=True).data

        return Response(
            {
                # Counted over the whole queue, deliberately — see above.
                "summary": PickingSummarySerializer(
                    reports.picking_summary(warehouse)
                ).data,
                "orders": paginator.get_paginated_response(orders).data,
            }
        )

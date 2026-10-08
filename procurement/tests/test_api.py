"""The procurement API, and who may reach it.

The two documents have deliberately different audiences:

    Group orders      leads write, Finance reads. No warehouse involvement.
    Production orders leads write; warehouse staff see their OWN warehouse;
                      Finance reads all.

The second is the interesting one — a permission class opens the screen,
`scope_to_user_site()` decides which rows are on it.
"""

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import TailoringCenter
from procurement.models.base import OrderStatus

from .factories import ORDER_DATE, make_priced_sku, make_unpriced_sku

Role = User.Role


class ProcurementSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.tc = TailoringCenter.objects.create(name="Idudi TC")
        self.sku = make_priced_sku()

        self.users = {
            Role.PROGRAM_LEAD: make_user("sharon", Role.PROGRAM_LEAD),
            Role.OPERATIONS_MANAGER: make_user("andrew", Role.OPERATIONS_MANAGER),
            Role.FINANCE: make_user("musana", Role.FINANCE),
            Role.WAREHOUSE_STAFF: make_user(
                "julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
            ),
            Role.SCHOOL_STAFF: make_user(
                "chrisis", Role.SCHOOL_STAFF, school=self.sites["school_a"]
            ),
        }

    def as_role(self, role):
        self.client.force_authenticate(self.users[role])

    def group_payload(self, **overrides):
        payload = {
            "order_date": ORDER_DATE.isoformat(),
            "lines": [{"sku": self.sku.pk, "quantity": 500}],
        }
        payload.update(overrides)
        return payload

    def production_payload(self, warehouse=None, **overrides):
        payload = {
            "order_date": ORDER_DATE.isoformat(),
            "tailoring_center": self.tc.pk,
            "warehouse": (warehouse or self.sites["namayemba"]).pk,
            "lines": [{"sku": self.sku.pk, "quantity": 300}],
        }
        payload.update(overrides)
        return payload


class GroupOrderAccessTests(ProcurementSetup):
    def test_leads_may_raise_a_group_order(self):
        for role in (Role.PROGRAM_LEAD, Role.OPERATIONS_MANAGER):
            with self.subTest(role=role):
                self.as_role(role)
                response = self.client.post(
                    reverse("procurement:group-order-list"),
                    self.group_payload(),
                    format="json",
                )
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_finance_may_read_but_not_raise(self):
        """The matrix gives Finance costed reports on group orders."""
        self.as_role(Role.FINANCE)

        self.assertEqual(
            self.client.get(reverse("procurement:group-order-list")).status_code,
            status.HTTP_200_OK,
        )
        response = self.client.post(
            reverse("procurement:group-order-list"), self.group_payload(), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_warehouse_and_school_staff_have_no_access_at_all(self):
        for role in (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF):
            with self.subTest(role=role):
                self.as_role(role)
                self.assertEqual(
                    self.client.get(reverse("procurement:group-order-list")).status_code,
                    status.HTTP_403_FORBIDDEN,
                )


class GroupOrderCreationTests(ProcurementSetup):
    def setUp(self):
        super().setUp()
        self.as_role(Role.PROGRAM_LEAD)

    def create(self, **overrides):
        return self.client.post(
            reverse("procurement:group-order-list"),
            self.group_payload(**overrides),
            format="json",
        )

    def test_the_response_carries_the_assigned_number(self):
        """Otherwise a client has to re-fetch to learn what it just created."""
        response = self.create()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["number"].startswith("GO-"))

    def test_the_response_carries_the_saved_lines_and_totals(self):
        response = self.create()

        self.assertEqual(len(response.data["lines"]), 1)
        self.assertEqual(response.data["total_quantity"], 500)
        self.assertEqual(response.data["lines"][0]["unit_price"], "25000.00")

    def test_the_raiser_is_taken_from_the_request(self):
        """Every transaction records the user — never from a form field."""
        response = self.create()

        self.assertEqual(
            response.data["created_by"], self.users[Role.PROGRAM_LEAD].pk
        )

    def test_an_order_with_no_lines_is_refused(self):
        response = self.create(lines=[])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_repeated_sku_is_refused_by_name(self):
        response = self.create(
            lines=[
                {"sku": self.sku.pk, "quantity": 100},
                {"sku": self.sku.pk, "quantity": 200},
            ]
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(self.sku.number, str(response.data))

    def test_an_unpriced_sku_is_refused_with_an_explanation(self):
        unpriced = make_unpriced_sku()
        response = self.create(lines=[{"sku": unpriced.pk, "quantity": 10}])

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(unpriced.number, str(response.data))

    def test_an_unpriced_sku_is_accepted_with_an_agreed_price(self):
        unpriced = make_unpriced_sku()
        response = self.create(
            lines=[{"sku": unpriced.pk, "quantity": 10, "unit_price": "18000.00"}]
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_a_retired_sku_cannot_be_ordered(self):
        self.sku.is_active = False
        self.sku.save(update_fields=["is_active"])

        self.assertEqual(self.create().status_code, status.HTTP_400_BAD_REQUEST)


class ProductionOrderScopingTests(ProcurementSetup):
    """A permission class opens the screen; scoping decides what is on it."""

    def setUp(self):
        super().setUp()
        self.as_role(Role.PROGRAM_LEAD)
        self.client.post(
            reverse("procurement:production-order-list"),
            self.production_payload(self.sites["namayemba"]),
            format="json",
        )
        self.client.post(
            reverse("procurement:production-order-list"),
            self.production_payload(self.sites["serere"]),
            format="json",
        )

    def listed_warehouses(self):
        response = self.client.get(reverse("procurement:production-order-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["warehouse_name"] for row in response.data["results"]}

    def test_a_lead_sees_every_warehouse(self):
        self.as_role(Role.PROGRAM_LEAD)
        self.assertEqual(self.listed_warehouses(), {"Namayemba", "Serere"})

    def test_finance_sees_every_warehouse(self):
        self.as_role(Role.FINANCE)
        self.assertEqual(self.listed_warehouses(), {"Namayemba", "Serere"})

    def test_warehouse_staff_see_only_their_own(self):
        self.as_role(Role.WAREHOUSE_STAFF)
        self.assertEqual(self.listed_warehouses(), {"Namayemba"})

    def test_warehouse_staff_cannot_reach_another_warehouses_order_directly(self):
        """Scoping must hold on detail, not only on the list."""
        self.as_role(Role.PROGRAM_LEAD)
        serere = [
            row
            for row in self.client.get(
                reverse("procurement:production-order-list")
            ).data["results"]
            if row["warehouse_name"] == "Serere"
        ][0]

        self.as_role(Role.WAREHOUSE_STAFF)
        response = self.client.get(
            reverse("procurement:production-order-detail", args=[serere["id"]])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_school_staff_have_no_access(self):
        self.as_role(Role.SCHOOL_STAFF)
        self.assertEqual(
            self.client.get(reverse("procurement:production-order-list")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_warehouse_staff_may_read_but_not_raise_orders(self):
        """The matrix gives Production Orders Entry to the leads."""
        self.as_role(Role.WAREHOUSE_STAFF)
        response = self.client.post(
            reverse("procurement:production-order-list"),
            self.production_payload(),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class OpenProductionOrderViewTests(ProcurementSetup):
    """F22, and it must be scoped like the list it summarises."""

    def setUp(self):
        super().setUp()
        self.as_role(Role.PROGRAM_LEAD)
        self.namayemba = self.client.post(
            reverse("procurement:production-order-list"),
            self.production_payload(self.sites["namayemba"]),
            format="json",
        ).data
        self.serere = self.client.post(
            reverse("procurement:production-order-list"),
            self.production_payload(self.sites["serere"]),
            format="json",
        ).data

    def open_numbers(self):
        response = self.client.get(reverse("procurement:open-production-orders"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {row["number"] for row in response.data}

    def test_open_orders_are_listed(self):
        self.assertEqual(
            self.open_numbers(), {self.namayemba["number"], self.serere["number"]}
        )

    def test_a_closed_order_drops_off(self):
        self.client.patch(
            reverse("procurement:production-order-detail", args=[self.serere["id"]]),
            {"status": OrderStatus.CLOSED},
            format="json",
        )
        self.assertNotIn(self.serere["number"], self.open_numbers())

    def test_warehouse_staff_see_only_their_own_open_orders(self):
        self.as_role(Role.WAREHOUSE_STAFF)
        self.assertEqual(self.open_numbers(), {self.namayemba["number"]})


class AmendmentTests(ProcurementSetup):
    def setUp(self):
        super().setUp()
        self.as_role(Role.PROGRAM_LEAD)
        self.order = self.client.post(
            reverse("procurement:production-order-list"),
            self.production_payload(),
            format="json",
        ).data

    def detail(self):
        return reverse("procurement:production-order-detail", args=[self.order["id"]])

    def test_an_order_can_be_cancelled(self):
        response = self.client.patch(
            self.detail(), {"status": OrderStatus.CANCELLED}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], OrderStatus.CANCELLED)

    def test_an_order_with_goods_received_cannot_be_cancelled(self):
        """Receipts point at the order and carry the value the ledger was
        written from. Cancelling it would leave posted stock attributed to an
        order claiming it was never placed."""
        from procurement.services import create_receipt, post_receipt
        from procurement.models import ProductionOrder

        order = ProductionOrder.objects.get(pk=self.order["id"])
        line = order.lines.first()
        receipt = create_receipt(
            production_order=order,
            lines=[{"sku": line.sku, "quantity_received": 1}],
            created_by=self.users[Role.WAREHOUSE_STAFF],
            packing_list_number="PL-1",
            date_received=ORDER_DATE,
        )
        post_receipt(receipt, posted_by=self.users[Role.WAREHOUSE_STAFF])

        response = self.client.patch(
            self.detail(), {"status": OrderStatus.CANCELLED}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("Close it instead", str(response.data))

    def test_such_an_order_can_still_be_closed(self):
        """The honest ending for an order that was partly delivered."""
        from procurement.services import create_receipt, post_receipt
        from procurement.models import ProductionOrder

        order = ProductionOrder.objects.get(pk=self.order["id"])
        line = order.lines.first()
        receipt = create_receipt(
            production_order=order,
            lines=[{"sku": line.sku, "quantity_received": 1}],
            created_by=self.users[Role.WAREHOUSE_STAFF],
            packing_list_number="PL-2",
            date_received=ORDER_DATE,
        )
        post_receipt(receipt, posted_by=self.users[Role.WAREHOUSE_STAFF])

        response = self.client.patch(
            self.detail(), {"status": OrderStatus.CLOSED}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], OrderStatus.CLOSED)

    def test_an_order_cannot_be_deleted(self):
        """It funds a Tailoring Center — the document has to survive."""
        response = self.client.delete(self.detail())

        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_lines_cannot_be_amended_yet(self):
        """F18 is not built. Silently ignoring the field would be worse."""
        response = self.client.patch(
            self.detail(),
            {"lines": [{"sku": self.sku.pk, "quantity": 999}]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class ReconciliationApiTests(ProcurementSetup):
    def setUp(self):
        super().setUp()
        self.as_role(Role.PROGRAM_LEAD)
        self.group = self.client.post(
            reverse("procurement:group-order-list"),
            self.group_payload(),
            format="json",
        ).data
        self.client.post(
            reverse("procurement:production-order-list"),
            self.production_payload(group_order=self.group["id"]),
            format="json",
        )

    def test_it_reports_the_shortfall(self):
        response = self.client.get(
            reverse("procurement:group-order-reconciliation", args=[self.group["id"]])
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.data[0]
        self.assertEqual(row["requested"], 500)
        self.assertEqual(row["ordered"], 300)
        self.assertEqual(row["difference"], -200)

    def test_an_unlinked_production_order_leaves_the_requirement_untouched(self):
        """The symptom that sent somebody looking for a bug in the sum.

        A production order with no `group_order` is a standalone document —
        a reorder, an emergency order — and must not count against anybody's
        requirement. That is right, and it is also exactly what a *missed*
        link looks like: order 200 of the 1,000 skirts, and the shortfall
        still reads 1,000. Nothing here can tell the two apart, which is why
        the create form has to ask for the link rather than leave it out.
        """
        self.client.post(
            reverse("procurement:production-order-list"),
            self.production_payload(),
            format="json",
        )

        response = self.client.get(
            reverse("procurement:group-order-reconciliation", args=[self.group["id"]])
        )

        row = response.data[0]
        self.assertEqual(row["ordered"], 300, "only the linked order counts")
        self.assertEqual(row["difference"], -200)

    def test_an_order_can_be_attached_to_a_group_order_afterwards(self):
        """The repair path, and the reason it exists.

        The create form did not ask for the link for a while, so there are
        real production orders — on live — that break down a group order and
        do not say so. Cancelling one and raising it again is not a fair
        answer: it funds a Tailoring Center, so the document has to survive.
        """
        standalone = self.client.post(
            reverse("procurement:production-order-list"),
            self.production_payload(),
            format="json",
        ).data

        response = self.client.patch(
            reverse("procurement:production-order-detail", args=[standalone["id"]]),
            {"group_order": self.group["id"]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = self.client.get(
            reverse("procurement:group-order-reconciliation", args=[self.group["id"]])
        ).data
        self.assertEqual(rows[0]["ordered"], 600, "both orders now count")

    def test_a_wrong_link_can_be_undone(self):
        attached = self.client.post(
            reverse("procurement:production-order-list"),
            self.production_payload(group_order=self.group["id"]),
            format="json",
        ).data

        self.client.patch(
            reverse("procurement:production-order-detail", args=[attached["id"]]),
            {"group_order": None},
            format="json",
        )

        rows = self.client.get(
            reverse("procurement:group-order-reconciliation", args=[self.group["id"]])
        ).data
        self.assertEqual(rows[0]["ordered"], 300)

    def test_finance_may_read_it(self):
        self.as_role(Role.FINANCE)
        response = self.client.get(
            reverse("procurement:group-order-reconciliation", args=[self.group["id"]])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

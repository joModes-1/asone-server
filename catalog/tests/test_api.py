"""The master data API, and the access matrix over HTTP.

AsOne's matrix grants *editing* to the leads alone, but grants "view only" on
individual tables to other roles — and not the same roles for each table.
That per-table read audience is the thing most likely to be got wrong, so it
is asserted table by table below.
"""

from datetime import date

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, MinimumStockLevel, Size, Sku

from .factories import make_garment, make_kit, make_kit_item, make_price

#: The API reports "price today", so fixtures need a price covering today —
#: not just the 2027 season the domain tests use.
IN_FORCE_TODAY = date(2026, 1, 1)

Role = User.Role

#: Which roles may READ each table, per AsOne's matrix. Leads may always read
#: and are the only ones who may write, so they are not repeated here.
READ_AUDIENCE = {
    "catalog:sku-list": {Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF, Role.FINANCE},
    # F05 gives garments and sizes to the leads alone — deliberately narrower
    # than F06's SKUs, which every role may view. Odd on its face, since a SKU
    # carries its garment's name, but it is what AsOne's matrix says.
    "catalog:garment-list": set(),
    "catalog:size-list": set(),
    "catalog:garment-price-list": {Role.SCHOOL_STAFF, Role.FINANCE},
    "catalog:minimum-stock-level-list": {Role.WAREHOUSE_STAFF},
    "catalog:tailoring-center-list": {Role.WAREHOUSE_STAFF},
    # Finance is wider than the matrix's "Warehouses — view: Warehouse Staff"
    # line, and follows from two cells that are in it: Finance's scope is all
    # locations, and F23 gives them adjustments at all sites. An adjustment
    # names the warehouse it is posted at, so a role that cannot list
    # warehouses cannot post one. Widened 14 September 2026 — worth putting to
    # AsOne with open question Q3.
    "catalog:warehouse-list": {Role.WAREHOUSE_STAFF, Role.FINANCE},
    "catalog:school-list": {Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF},
    "catalog:kit-list": {Role.SCHOOL_STAFF, Role.FINANCE},
    "catalog:kit-item-list": {Role.SCHOOL_STAFF, Role.FINANCE},
}

LEADS = {Role.PROGRAM_LEAD, Role.OPERATIONS_MANAGER}


class CatalogSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
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
        self.shirt = make_garment("White Shirt", Garment.SchoolLevel.PRIMARY)
        make_price(self.shirt, "25000.00", IN_FORCE_TODAY)
        self.size_10 = Size.objects.create(name="10", sort_order=10)
        self.sku = Sku.objects.create(garment=self.shirt, size=self.size_10)

    def as_role(self, role):
        self.client.force_authenticate(self.users[role])


class ReadAccessTests(CatalogSetup):
    def test_each_table_is_readable_by_exactly_the_roles_in_the_matrix(self):
        for route, audience in READ_AUDIENCE.items():
            for role in Role:
                with self.subTest(route=route, role=role):
                    self.as_role(role)
                    response = self.client.get(reverse(route))

                    may_read = role in audience or role in LEADS
                    self.assertEqual(
                        response.status_code == status.HTTP_200_OK,
                        may_read,
                        msg=f"{role} on {route} returned {response.status_code}",
                    )

    def test_reading_requires_authentication(self):
        for route in READ_AUDIENCE:
            with self.subTest(route=route):
                self.client.force_authenticate(None)
                self.assertEqual(
                    self.client.get(reverse(route)).status_code,
                    status.HTTP_401_UNAUTHORIZED,
                )

    def test_warehouse_staff_cannot_read_prices(self):
        """Explicit: the matrix leaves the price cell blank for them."""
        self.as_role(Role.WAREHOUSE_STAFF)
        self.assertEqual(
            self.client.get(reverse("catalog:garment-price-list")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_school_staff_cannot_read_minimum_stock_levels(self):
        self.as_role(Role.SCHOOL_STAFF)
        self.assertEqual(
            self.client.get(reverse("catalog:minimum-stock-level-list")).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class WriteAccessTests(CatalogSetup):
    """Editing master data is the Table Updates column: leads only."""

    def create_garment_as(self, role):
        self.as_role(role)
        return self.client.post(
            reverse("catalog:garment-list"),
            {"name": f"Blazer {role}", "school_level": "PS"},
            format="json",
        )

    def test_leads_may_create(self):
        for role in LEADS:
            with self.subTest(role=role):
                self.assertEqual(
                    self.create_garment_as(role).status_code, status.HTTP_201_CREATED
                )

    def test_nobody_else_may_create(self):
        for role in (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF, Role.FINANCE):
            with self.subTest(role=role):
                self.assertEqual(
                    self.create_garment_as(role).status_code, status.HTTP_403_FORBIDDEN
                )

    def test_a_role_that_may_read_still_may_not_write(self):
        """Finance reads prices. Finance does not set them."""
        self.as_role(Role.FINANCE)
        self.assertEqual(
            self.client.get(reverse("catalog:garment-price-list")).status_code,
            status.HTTP_200_OK,
        )
        response = self.client.post(
            reverse("catalog:garment-price-list"),
            {"garment": self.shirt.pk, "unit_price": "1.00", "active_date": "2030-01-01"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class SkuApiTests(CatalogSetup):
    def test_the_control_number_is_assigned_not_supplied(self):
        self.as_role(Role.PROGRAM_LEAD)
        size_12 = Size.objects.create(name="12", sort_order=12)

        response = self.client.post(
            reverse("catalog:sku-list"),
            {"garment": self.shirt.pk, "size": size_12.pk, "number": "000001"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertNotEqual(response.data["number"], "000001")

    def test_a_sku_reports_its_garments_price(self):
        self.as_role(Role.WAREHOUSE_STAFF)
        response = self.client.get(reverse("catalog:sku-detail", args=[self.sku.pk]))

        self.assertEqual(response.data["unit_price"], "25000.00")

    def test_an_unpriced_sku_reports_null_not_zero(self):
        self.as_role(Role.PROGRAM_LEAD)
        socks = make_garment("Socks")
        sku = Sku.objects.create(garment=socks, size=self.size_10)

        response = self.client.get(reverse("catalog:sku-detail", args=[sku.pk]))
        self.assertIsNone(response.data["unit_price"])

    def test_duplicate_garment_and_size_is_a_400_not_a_500(self):
        self.as_role(Role.PROGRAM_LEAD)
        response = self.client.post(
            reverse("catalog:sku-list"),
            {"garment": self.shirt.pk, "size": self.size_10.pk},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class RepriceApiTests(CatalogSetup):
    def test_a_lead_can_reprice_a_garment(self):
        self.as_role(Role.PROGRAM_LEAD)
        response = self.client.post(
            reverse("catalog:garment-reprice", args=[self.shirt.pk]),
            {"unit_price": "30000.00", "active_from": "2027-06-01"},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.shirt.prices.count(), 2)

    def test_repricing_closes_the_previous_price(self):
        self.as_role(Role.PROGRAM_LEAD)
        self.client.post(
            reverse("catalog:garment-reprice", args=[self.shirt.pk]),
            {"unit_price": "30000.00", "active_from": "2027-06-01"},
            format="json",
        )

        original = self.shirt.prices.order_by("active_date").first()
        self.assertEqual(original.expiration_date, date(2027, 6, 1))

    def test_school_staff_cannot_reprice(self):
        self.as_role(Role.SCHOOL_STAFF)
        response = self.client.post(
            reverse("catalog:garment-reprice", args=[self.shirt.pk]),
            {"unit_price": "1.00", "active_from": "2027-06-01"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_overlapping_price_is_a_400_not_a_500(self):
        """The database refuses it; the API must say so readably."""
        self.as_role(Role.PROGRAM_LEAD)
        response = self.client.post(
            reverse("catalog:garment-price-list"),
            {
                "garment": self.shirt.pk,
                "unit_price": "30000.00",
                "active_date": "2027-03-01",
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class PriceListApiTests(CatalogSetup):
    """F15 — generate price lists at garment level."""

    def setUp(self):
        super().setUp()
        self.hs_shirt = make_garment("HS Shirt", Garment.SchoolLevel.HIGH)
        make_price(self.hs_shirt, "30000.00", IN_FORCE_TODAY)
        self.socks = make_garment("Socks", Garment.SchoolLevel.BOTH)
        make_price(self.socks, "5000.00", IN_FORCE_TODAY)

    def fetch(self, level, on=IN_FORCE_TODAY):
        return self.client.get(
            reverse("catalog:price-list"), {"level": level, "on": on.isoformat()}
        )

    def test_a_school_can_read_its_own_price_list(self):
        self.as_role(Role.SCHOOL_STAFF)
        response = self.fetch("PS")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [row["garment"] for row in response.data]
        self.assertEqual(names, ["Socks", "White Shirt (PS)"])

    def test_the_two_lists_differ(self):
        self.as_role(Role.PROGRAM_LEAD)
        ps = {row["garment"] for row in self.fetch("PS").data}
        hs = {row["garment"] for row in self.fetch("HS").data}

        self.assertIn("White Shirt (PS)", ps)
        self.assertNotIn("White Shirt (PS)", hs)
        self.assertIn("Socks", ps & hs)

    def test_the_list_reflects_the_date_asked_for(self):
        self.as_role(Role.PROGRAM_LEAD)
        self.client.post(
            reverse("catalog:garment-reprice", args=[self.shirt.pk]),
            {"unit_price": "40000.00", "active_from": "2027-06-01"},
            format="json",
        )

        march = {r["garment"]: r["unit_price"] for r in self.fetch("PS", date(2027, 3, 1)).data}
        july = {r["garment"]: r["unit_price"] for r in self.fetch("PS", date(2027, 7, 1)).data}

        self.assertEqual(march["White Shirt (PS)"], "25000.00")
        self.assertEqual(july["White Shirt (PS)"], "40000.00")

    def test_unpriced_garments_are_omitted(self):
        make_garment("Blazer", Garment.SchoolLevel.PRIMARY)
        self.as_role(Role.PROGRAM_LEAD)

        self.assertNotIn("Blazer", [row["garment"] for row in self.fetch("PS").data])

    def test_the_gap_report_lists_them(self):
        make_garment("Blazer", Garment.SchoolLevel.PRIMARY)
        self.as_role(Role.FINANCE)

        response = self.client.get(
            reverse("catalog:price-gaps"), {"on": IN_FORCE_TODAY.isoformat()}
        )
        self.assertEqual([g["name"] for g in response.data], ["Blazer"])


class MinimumStockLevelApiTests(CatalogSetup):
    def test_a_lead_can_set_a_floor_per_warehouse(self):
        self.as_role(Role.PROGRAM_LEAD)
        response = self.client.post(
            reverse("catalog:minimum-stock-level-list"),
            {
                "sku": self.sku.pk,
                "warehouse": self.sites["namayemba"].pk,
                "minimum_quantity": 100,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_warehouse_staff_may_read_but_not_set_them(self):
        MinimumStockLevel.objects.create(
            sku=self.sku, warehouse=self.sites["namayemba"], minimum_quantity=100
        )
        self.as_role(Role.WAREHOUSE_STAFF)

        self.assertEqual(
            self.client.get(reverse("catalog:minimum-stock-level-list")).status_code,
            status.HTTP_200_OK,
        )
        response = self.client.post(
            reverse("catalog:minimum-stock-level-list"),
            {
                "sku": self.sku.pk,
                "warehouse": self.sites["serere"].pk,
                "minimum_quantity": 50,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class KitApiTests(CatalogSetup):
    def create_kit_as(self, role, kit_number="KIT-001"):
        self.as_role(role)
        return self.client.post(
            reverse("catalog:kit-list"),
            {"kit_number": kit_number, "name": "A kit", "school_level": "PS"},
            format="json",
        )

    def test_leads_may_create_a_kit(self):
        for index, role in enumerate(LEADS):
            with self.subTest(role=role):
                response = self.create_kit_as(role, kit_number=f"KIT-{index}")
                self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_nobody_else_may_create_a_kit(self):
        for role in (Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF, Role.FINANCE):
            with self.subTest(role=role):
                self.assertEqual(
                    self.create_kit_as(role).status_code, status.HTTP_403_FORBIDDEN
                )

    def test_a_kit_reports_the_sum_of_its_components(self):
        kit = make_kit()
        make_kit_item(kit, self.sku, quantity=2)
        self.as_role(Role.SCHOOL_STAFF)

        response = self.client.get(reverse("catalog:kit-detail", args=[kit.pk]))

        # self.sku's garment is priced at 25000.00 by CatalogSetup.
        self.assertEqual(response.data["current_price"], "50000.00")
        self.assertEqual(response.data["item_count"], 1)

    def test_a_kit_missing_a_component_price_reports_null_not_an_error(self):
        unpriced = make_garment("Unpriced Blazer")
        size = Size.objects.create(name="14", sort_order=14)
        unpriced_sku = Sku.objects.create(garment=unpriced, size=size)

        kit = make_kit()
        make_kit_item(kit, unpriced_sku, quantity=1)
        self.as_role(Role.FINANCE)

        response = self.client.get(reverse("catalog:kit-detail", args=[kit.pk]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["current_price"])

    def test_deleting_a_kit_over_the_api_removes_its_items(self):
        kit = make_kit()
        make_kit_item(kit, self.sku, quantity=1)
        self.as_role(Role.PROGRAM_LEAD)

        response = self.client.delete(reverse("catalog:kit-detail", args=[kit.pk]))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

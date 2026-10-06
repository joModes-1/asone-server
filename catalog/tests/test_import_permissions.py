"""Who may run each import, proved for every role.

Two imports, two different columns of AsOne's matrix, and they are not the
same people:

    Import garments      table_updates          Program Lead, Operations Manager
    Import stock count   inventory_adjustments  Finance

That asymmetry is easy to get wrong and expensive when it is: one creates
products, the other writes to an append-only ledger. This walks every role
against both endpoints so a widened permission cannot pass unnoticed.

Open question Q3 sits on the second row — AsOne give inventory adjustments
to Finance alone, which means the warehouse staff who do the counting cannot
post the result. Built as written. If they widen that cell, this test is
where the change gets recorded.
"""

import io

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user

Role = User.Role

CATALOGUE = (
    "name,code,colour,colour_code,school_level,fit_group,sizes,unit_price\n"
    "Checked Shirt,CHS,Checked,CHK,PS,NUR,XS;S,24000\n"
)


def upload(text="", name="catalogue.csv"):
    handle = io.BytesIO(text.encode("utf-8"))
    handle.name = name
    return handle


class ImportPermissions(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.users = {
            Role.PROGRAM_LEAD: make_user("sharon", Role.PROGRAM_LEAD),
            Role.OPERATIONS_MANAGER: make_user("oscar", Role.OPERATIONS_MANAGER),
            Role.FINANCE: make_user("fiona", Role.FINANCE),
            Role.WAREHOUSE_STAFF: make_user(
                "julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
            ),
            Role.SCHOOL_STAFF: make_user(
                "sarah", Role.SCHOOL_STAFF, school=self.sites["school_a"]
            ),
        }

    def post_catalogue(self, role):
        self.client.force_authenticate(self.users[role])
        return self.client.post(
            reverse("catalog:catalogue-import"),
            {"file": upload(CATALOGUE)},
            format="multipart",
        )

    def post_count(self, role):
        self.client.force_authenticate(self.users[role])
        # Deliberately an empty body: a role that may reach this endpoint is
        # refused for the body, a role that may not is refused for the role.
        # 400 therefore means "allowed in", which is what is being checked.
        return self.client.post(
            reverse("inventory:adjustment-correct-count"), {}, format="json"
        )


class ImportingGarments(ImportPermissions):
    """`table_updates` — the leads. A catalogue is master data."""

    ALLOWED = (Role.PROGRAM_LEAD, Role.OPERATIONS_MANAGER)

    def test_the_leads_may(self):
        for role in self.ALLOWED:
            with self.subTest(role=role):
                self.assertEqual(self.post_catalogue(role).status_code, status.HTTP_200_OK)

    def test_nobody_else_may(self):
        for role in (Role.FINANCE, Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF):
            with self.subTest(role=role):
                self.assertEqual(
                    self.post_catalogue(role).status_code, status.HTTP_403_FORBIDDEN
                )

    def test_a_stranger_may_not(self):
        self.client.force_authenticate(None)
        response = self.client.post(
            reverse("catalog:catalogue-import"),
            {"file": upload(CATALOGUE)},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class ImportingAStockCount(ImportPermissions):
    """`inventory_adjustments` — Finance alone, and deliberately not the
    leads. See open question Q3."""

    def test_finance_may(self):
        self.assertNotEqual(
            self.post_count(Role.FINANCE).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_not_even_the_leads_may(self):
        """The surprising one, and the reason the button is hidden from them
        rather than shown and refused."""
        for role in (Role.PROGRAM_LEAD, Role.OPERATIONS_MANAGER):
            with self.subTest(role=role):
                self.assertEqual(
                    self.post_count(role).status_code, status.HTTP_403_FORBIDDEN
                )

    def test_nor_the_warehouse_that_does_the_counting(self):
        """Q3 in one line. Worth confirming with AsOne that this is meant."""
        self.assertEqual(
            self.post_count(Role.WAREHOUSE_STAFF).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_nor_a_school(self):
        self.assertEqual(
            self.post_count(Role.SCHOOL_STAFF).status_code, status.HTTP_403_FORBIDDEN
        )


class TheTwoImportsAreNotTheSamePeople(ImportPermissions):
    def test_finance_may_count_but_not_load_a_catalogue(self):
        self.assertEqual(
            self.post_catalogue(Role.FINANCE).status_code, status.HTTP_403_FORBIDDEN
        )
        self.assertNotEqual(
            self.post_count(Role.FINANCE).status_code, status.HTTP_403_FORBIDDEN
        )

    def test_a_lead_may_load_a_catalogue_but_not_count(self):
        self.assertEqual(
            self.post_catalogue(Role.PROGRAM_LEAD).status_code, status.HTTP_200_OK
        )
        self.assertEqual(
            self.post_count(Role.PROGRAM_LEAD).status_code, status.HTTP_403_FORBIDDEN
        )

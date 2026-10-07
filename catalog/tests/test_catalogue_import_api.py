"""Importing a catalogue from the Garments tab.

The same rules as the `import_catalog` command, reached over HTTP. Both call
`catalog/importing.py`, so what a file may contain cannot depend on which
door it came through — these tests exist to prove the door, not to re-prove
the rules.

The error shape is the interesting part. A terminal can print thirty bad
rows and let somebody scroll; a screen has to be handed them as data. So a
rejection carries `rows`, each with its line number and reason, and the
whole file is refused.
"""

import io
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, Size, Sku

GOOD = (
    "name,code,colour,colour_code,school_level,fit_group,sizes,unit_price,active_date\n"
    "Checked Shirt,CHS,Checked,CHK,PS,NUR,XS;S;M,24000,2026-01-01\n"
    "Sweater,SWT,Grey,GRY,PS,P14,XS;S,45000,2026-01-01\n"
    "Sweater,SWT,Grey,GRY,PS,P57,XS;S,45000,2026-01-01\n"
)


def upload(text, name="catalogue.csv"):
    handle = io.BytesIO(text.encode("utf-8"))
    handle.name = name
    return handle


class ImportSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.url = reverse("catalog:catalogue-import")
        self.client.force_authenticate(self.lead)

    def post(self, text=GOOD, name="catalogue.csv", **extra):
        return self.client.post(
            self.url, {"file": upload(text, name), **extra}, format="multipart"
        )


class ImportingAFile(ImportSetup):
    def test_it_creates_garments_sizes_skus_and_prices(self):
        response = self.post()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Garment.objects.count(), 3)
        self.assertEqual(Sku.objects.count(), 7)
        self.assertEqual(GarmentPrice.objects.count(), 3)
        self.assertEqual(Size.objects.count(), 3)

    def test_the_skus_carry_asones_five_part_numbers(self):
        self.post()

        self.assertTrue(
            Sku.objects.filter(number="UNI-NUR-CHS-XS-CHK").exists(),
            list(Sku.objects.values_list("number", flat=True)),
        )

    def test_one_name_at_two_fit_groups_is_two_garments(self):
        """The thing that was impossible before the fit group existed."""
        self.post()

        self.assertEqual(Garment.objects.filter(name="Sweater").count(), 2)

    def test_the_tally_comes_back(self):
        response = self.post()

        self.assertEqual(response.data["garments"], 3)
        self.assertEqual(response.data["skus"], 7)
        self.assertFalse(response.data["dry_run"])

    def test_running_it_twice_changes_nothing(self):
        self.post()
        response = self.post()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Garment.objects.count(), 3)
        self.assertEqual(response.data["garments"], 0)
        self.assertEqual(response.data["unchanged"], 3)

    def test_a_re_import_says_so_rather_than_looking_like_a_failure(self):
        self.post()
        response = self.post()

        self.assertIn("already in the catalogue", response.data["detail"])

    def test_an_xlsx_upload_is_read_too(self):
        from openpyxl import Workbook

        book = Workbook()
        sheet = book.active
        sheet.append(["name", "school_level", "sizes", "unit_price"])
        sheet.append(["Blazer", "HS", "S;M;L", 85000])
        buffer = io.BytesIO()
        book.save(buffer)
        buffer.seek(0)
        buffer.name = "catalogue.xlsx"

        response = self.client.post(
            self.url, {"file": buffer}, format="multipart"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Garment.objects.get().name, "Blazer")


class ADryRunWritesNothing(ImportSetup):
    def test_it_reports_what_would_happen(self):
        response = self.post(dry_run=True)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["garments"], 3)
        self.assertTrue(response.data["dry_run"])

    def test_and_nothing_is_saved(self):
        self.post(dry_run=True)

        self.assertEqual(Garment.objects.count(), 0)
        self.assertEqual(Sku.objects.count(), 0)


class WhenRowsCannotBeRead(ImportSetup):
    BAD = (
        "name,code,colour,colour_code,school_level,fit_group,sizes,unit_price,active_date\n"
        "Good Shirt,GSH,White,WHT,PS,P14,XS;S,25000,2026-01-01\n"
        "Bad Band,BAD,Blue,BLU,PS,P99,XS,25000,2026-01-01\n"
        "No Price,NOP,Red,RED,PS,NUR,XS,,2026-01-01\n"
    )

    def test_the_whole_file_is_refused(self):
        """Not 'the good row went in'. A catalogue missing rows nobody can
        name is worse than no catalogue."""
        response = self.post(self.BAD)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Garment.objects.count(), 0)

    def test_every_bad_row_is_reported_not_just_the_first(self):
        response = self.post(self.BAD)

        self.assertEqual(len(response.data["rows"]), 2)

    def test_each_one_names_its_line_and_its_reason(self):
        response = self.post(self.BAD)

        rows = {row["line"]: row["message"] for row in response.data["rows"]}
        self.assertIn(3, rows)
        self.assertIn("P99", rows[3])
        self.assertIn(4, rows)
        self.assertIn("unit_price", rows[4])

    def test_the_line_number_is_a_number(self):
        """Raising a DRF ValidationError stringified these, and a screen
        cannot sort or link by "3" without parsing it back first."""
        response = self.post(self.BAD)

        for row in response.data["rows"]:
            self.assertIsInstance(row["line"], int)

    def test_a_missing_column_is_named_on_the_file(self):
        response = self.post("name,colour\nShirt,White\n")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("missing columns", str(response.data["file"]))

    def test_a_file_we_cannot_open_is_refused_kindly(self):
        response = self.post("whatever", name="catalogue.pdf")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(".xlsx or .csv", str(response.data["file"]))

    def test_an_empty_file_is_refused(self):
        response = self.post("name,school_level,sizes,unit_price\n")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("no data rows", str(response.data["file"]))


class WhoMayImport(ImportSetup):
    """Writing a catalogue is the leads' column, not everyone who may read
    master data."""

    def test_a_warehouse_clerk_may_not(self):
        clerk = make_user(
            "julius", User.Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        self.client.force_authenticate(clerk)

        self.assertEqual(self.post().status_code, status.HTTP_403_FORBIDDEN)

    def test_finance_may_not(self):
        self.client.force_authenticate(make_user("fiona", User.Role.FINANCE))

        self.assertEqual(self.post().status_code, status.HTTP_403_FORBIDDEN)

    def test_an_operations_manager_may(self):
        self.client.force_authenticate(make_user("oscar", User.Role.OPERATIONS_MANAGER))

        self.assertEqual(self.post().status_code, status.HTTP_200_OK)

    def test_a_stranger_may_not(self):
        self.client.force_authenticate(None)

        self.assertEqual(self.post().status_code, status.HTTP_401_UNAUTHORIZED)


class ColumnsSpelledTheOtherWay(ImportSetup):
    """A header this did not recognise used to be ignored, not refused.

    AsOne's own workbook says "Color" and "Garment". Neither matched, so the
    colour imported blank and the name came back missing — and nothing said
    why. A quietly dropped column is worse than a rejected file.
    """

    def test_american_colour_is_understood(self):
        self.post(
            "name,school_level,sizes,unit_price,Color\n"
            "Checked Shirt,PS,XS;S,24000,Checked\n"
        )

        self.assertEqual(Garment.objects.get().colour, "Checked")

    def test_garment_is_accepted_for_name(self):
        """What her sheet calls the column."""
        response = self.post("Garment,school_level,sizes,unit_price\nSweater,PS,XS;S,45000\n")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Garment.objects.get().name, "Sweater")

    def test_price_is_accepted_for_unit_price(self):
        response = self.post("name,school_level,sizes,Price\nSocks,BOTH,OS,5000\n")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(GarmentPrice.objects.get().unit_price, Decimal("5000"))

    def test_the_canonical_spellings_still_work(self):
        self.post(
            "name,school_level,sizes,unit_price,colour\n"
            "Checked Shirt,PS,XS;S,24000,Checked\n"
        )

        self.assertEqual(Garment.objects.get().colour, "Checked")


class TheGarmentDescription(ImportSetup):
    """AsOne's own wording, which their workbook repeats against every SKU
    row and which we previously dropped on the floor."""

    def test_it_is_imported_onto_the_garment(self):
        self.post(
            "name,description,school_level,sizes,unit_price\n"
            "Checked Skirt,School Uniform checked skirt for S1\u2013S4,HS,XS;S,24000\n"
        )

        self.assertEqual(
            Garment.objects.get().description,
            "School Uniform checked skirt for S1\u2013S4",
        )

    def test_the_sku_keeps_its_own(self):
        """Theirs never varies by size — checked across all 53 of their
        garment-and-band combinations. The SKU's own description carries the
        size, and pick lists print in description sequence."""
        self.post(
            "name,description,school_level,sizes,unit_price\n"
            "Checked Skirt,School Uniform checked skirt,HS,XS;S,24000\n"
        )

        descriptions = sorted(Sku.objects.values_list("description", flat=True))
        self.assertEqual(len(set(descriptions)), 2)
        for text in descriptions:
            self.assertIn("size", text)

    def test_a_file_without_one_still_imports(self):
        response = self.post("name,school_level,sizes,unit_price\nSocks,BOTH,OS,5000\n")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(Garment.objects.get().description, "")

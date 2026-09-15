"""Importing the catalogue from a spreadsheet.

AsOne sends about 45 garments in four or five sizes each. Typing 200 SKUs
into a form is a day's work and a dozen typos, so the file is read instead —
and the things that make that safe are what these tests hold:

    nothing is half-imported      one bad row rolls the whole file back
    every error is reported       not just the first
    running it twice is a no-op   so a corrected file can be re-sent
    a dry run writes nothing      but still says what it would do
"""

from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from catalog.models import Garment, GarmentPrice, Size, Sku

HEADER = "name,colour,school_level,sizes,unit_price,active_date\n"


class ImportSetup(TestCase):
    def setUp(self):
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def csv(self, body, name="catalog.csv"):
        path = Path(self.dir.name) / name
        path.write_text(HEADER + body, encoding="utf-8")
        return str(path)

    def run_import(self, path, **options):
        out = StringIO()
        call_command("import_catalog", path, stdout=out, **options)
        return out.getvalue()


class OneRowMakesAGarmentItsSizesSkusAndPrice(ImportSetup):
    def test_a_single_row_builds_the_lot(self):
        self.run_import(self.csv("White Shirt,White,BOTH,6;8;10,25000,2026-01-01\n"))

        garment = Garment.objects.get(name="White Shirt")
        self.assertEqual(garment.school_level, Garment.SchoolLevel.BOTH)
        self.assertEqual(garment.colour, "White")
        self.assertEqual(Sku.objects.filter(garment=garment).count(), 3)
        self.assertEqual(
            GarmentPrice.objects.get(garment=garment).unit_price, Decimal("25000")
        )

    def test_sizes_are_shared_between_garments(self):
        """Two garments in size 8 is one Size row, not two — otherwise the
        same shelf label means two different things."""
        self.run_import(
            self.csv(
                "White Shirt,White,BOTH,6;8,25000,2026-01-01\n"
                "Blue Tunic,Blue,PS,6;8,30000,2026-01-01\n"
            )
        )

        self.assertEqual(Size.objects.count(), 2)
        self.assertEqual(Sku.objects.count(), 4)

    def test_numeric_sizes_sort_by_value(self):
        self.run_import(self.csv("White Shirt,White,BOTH,12;6;10,25000,2026-01-01\n"))

        order = list(Size.objects.order_by("sort_order").values_list("name", flat=True))
        self.assertEqual(order, ["6", "10", "12"])

    def test_letter_sizes_sort_the_way_clothes_do(self):
        self.run_import(self.csv("Blazer,Navy,HS,XL;S;M;L,85000,2026-01-01\n"))

        order = list(Size.objects.order_by("sort_order").values_list("name", flat=True))
        self.assertEqual(order, ["S", "M", "L", "XL"])

    def test_a_blank_date_means_today(self):
        self.run_import(self.csv("Socks,White,BOTH,S;M,5000,\n"))

        self.assertEqual(GarmentPrice.objects.get().active_date, date.today())

    def test_prices_with_thousands_separators_are_read(self):
        """A spreadsheet exports 25,000 as often as 25000."""
        self.run_import(self.csv('White Shirt,White,BOTH,6;8,"25,000",2026-01-01\n'))

        self.assertEqual(GarmentPrice.objects.get().unit_price, Decimal("25000"))


class RunningItTwiceChangesNothing(ImportSetup):
    """So a corrected file can be re-sent without cleaning up first."""

    ROW = "White Shirt,White,BOTH,6;8;10,25000,2026-01-01\n"

    def test_a_second_run_creates_nothing(self):
        path = self.csv(self.ROW)
        self.run_import(path)
        before = (Garment.objects.count(), Size.objects.count(), Sku.objects.count())

        self.run_import(path)

        self.assertEqual(
            (Garment.objects.count(), Size.objects.count(), Sku.objects.count()), before
        )

    def test_a_changed_price_on_the_same_day_is_corrected_not_duplicated(self):
        """The database refuses two prices for one garment on one day, so a
        re-import with a fixed figure has to update in place."""
        self.run_import(self.csv(self.ROW))
        self.run_import(self.csv("White Shirt,White,BOTH,6;8;10,27000,2026-01-01\n"))

        self.assertEqual(GarmentPrice.objects.count(), 1)
        self.assertEqual(GarmentPrice.objects.get().unit_price, Decimal("27000"))

    def test_adding_a_size_later_adds_only_that_sku(self):
        self.run_import(self.csv(self.ROW))
        self.run_import(self.csv("White Shirt,White,BOTH,6;8;10;12,25000,2026-01-01\n"))

        self.assertEqual(Sku.objects.count(), 4)


class NothingIsHalfImported(ImportSetup):
    """A catalogue missing the last five garments, with nobody sure which,
    is worse than no catalogue."""

    def test_one_bad_row_rolls_back_the_good_ones(self):
        path = self.csv(
            "White Shirt,White,BOTH,6;8,25000,2026-01-01\n"
            "Broken,Blue,NOT_A_LEVEL,6;8,30000,2026-01-01\n"
            "Blue Tunic,Blue,PS,6;8,30000,2026-01-01\n"
        )

        with self.assertRaises(CommandError):
            self.run_import(path)

        self.assertEqual(Garment.objects.count(), 0)

    def test_every_error_is_reported_not_just_the_first(self):
        """Fixing a spreadsheet one error per run is miserable."""
        path = self.csv(
            ",Blue,PS,6;8,30000,2026-01-01\n"
            "Bad Level,Grey,PRIMARY,6;8,20000,2026-01-01\n"
            "No Sizes,Navy,PS,,15000,2026-01-01\n"
            "Bad Price,White,BOTH,S;M,not-a-number,2026-01-01\n"
        )

        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("import_catalog", path, stdout=out, stderr=out)

        report = out.getvalue()
        for expected in ("line 2", "line 3", "line 4", "line 5"):
            self.assertIn(expected, report)

    def test_line_numbers_match_the_spreadsheet(self):
        """A person counting rows counts the header as line 1."""
        path = self.csv(
            "White Shirt,White,BOTH,6;8,25000,2026-01-01\n"
            ",Blue,PS,6;8,30000,2026-01-01\n"
        )

        out = StringIO()
        with self.assertRaises(CommandError):
            call_command("import_catalog", path, stdout=out, stderr=out)

        self.assertIn("line 3", out.getvalue())

    def test_a_missing_column_is_refused_before_anything_is_read(self):
        path = Path(self.dir.name) / "wrong.csv"
        path.write_text("name,school_level\nWhite Shirt,BOTH\n", encoding="utf-8")

        with self.assertRaises(CommandError):
            self.run_import(str(path))

    def test_a_missing_file_is_a_clear_error(self):
        with self.assertRaises(CommandError):
            self.run_import(str(Path(self.dir.name) / "nope.csv"))


class ADryRunWritesNothing(ImportSetup):
    def test_it_reports_what_it_would_do(self):
        out = self.run_import(
            self.csv("White Shirt,White,BOTH,6;8;10,25000,2026-01-01\n"), dry_run=True
        )

        self.assertIn("would create", out)
        self.assertIn("3 SKUs", out)

    def test_and_writes_none_of_it(self):
        self.run_import(
            self.csv("White Shirt,White,BOTH,6;8;10,25000,2026-01-01\n"), dry_run=True
        )

        self.assertEqual(Garment.objects.count(), 0)
        self.assertEqual(Sku.objects.count(), 0)


class ImportedDataIsOrdinaryData(ImportSetup):
    """No back door: a SKU that arrived by import is one the API would have
    accepted, with a real control number and a working price."""

    def test_skus_get_control_numbers(self):
        self.run_import(self.csv("White Shirt,White,BOTH,6;8,25000,2026-01-01\n"))

        for sku in Sku.objects.all():
            self.assertTrue(sku.number)
            self.assertTrue(sku.description)

    def test_the_price_is_readable_through_the_normal_path(self):
        from catalog.services import price_for_sku

        self.run_import(self.csv("White Shirt,White,BOTH,6;8,25000,2026-01-01\n"))

        sku = Sku.objects.first()
        self.assertEqual(price_for_sku(sku, date(2026, 6, 1)), Decimal("25000"))


class ReadingExcel(ImportSetup):
    """The format AsOne will actually send.

    Excel does not hand over strings. A price is a float, a date is a
    datetime, and a sizes column reading just "8" arrives as 8.0 — which
    would become the size "8.0" if nobody looked. These are the traps that a
    hand-rolled XML reader falls into and a real one does not.
    """

    def workbook(self, rows, header=None, sheet="Catalogue", extra_sheet=False):
        from openpyxl import Workbook

        book = Workbook()
        sheet_obj = book.active
        sheet_obj.title = sheet
        sheet_obj.append(
            header or ["name", "colour", "school_level", "sizes", "unit_price", "active_date"]
        )
        for row in rows:
            sheet_obj.append(row)
        if extra_sheet:
            book.create_sheet("Notes").append(["ignore me"])

        path = Path(self.dir.name) / "catalogue.xlsx"
        book.save(path)
        return str(path)

    def test_an_xlsx_imports(self):
        path = self.workbook([["White Shirt", "White", "BOTH", "6;8;10", 25000, date(2026, 1, 1)]])

        self.run_import(path)

        self.assertEqual(Sku.objects.count(), 3)
        self.assertEqual(GarmentPrice.objects.get().unit_price, Decimal("25000"))

    def test_a_numeric_size_cell_does_not_become_a_decimal(self):
        """Excel stores 8 as 8.0. The size is "8", and a shelf label reading
        "8.0" is a different thing."""
        path = self.workbook([["Socks", "White", "BOTH", 8, 5000, None]])

        self.run_import(path)

        self.assertEqual(Size.objects.get().name, "8")

    def test_a_real_date_cell_is_read_as_a_date(self):
        path = self.workbook([["White Shirt", "White", "BOTH", "6;8", 25000, date(2026, 3, 1)]])

        self.run_import(path)

        self.assertEqual(GarmentPrice.objects.get().active_date, date(2026, 3, 1))

    def test_a_float_price_keeps_its_value(self):
        path = self.workbook([["Blue Tunic", "Blue", "PS", "6;8", 30000.0, None]])

        self.run_import(path)

        self.assertEqual(GarmentPrice.objects.get().unit_price, Decimal("30000.00"))

    def test_untidy_headers_are_matched(self):
        """A spreadsheet that has been passed around rarely has them exact."""
        path = self.workbook(
            [["White Shirt", "White", "BOTH", "6;8", 25000, None]],
            header=[" Name ", "COLOUR", "School Level", "Sizes", "Unit Price", "Active Date"],
        )

        self.run_import(path)

        self.assertEqual(Garment.objects.get().name, "White Shirt")

    def test_blank_rows_left_by_editing_are_skipped(self):
        path = self.workbook(
            [
                ["White Shirt", "White", "BOTH", "6;8", 25000, None],
                [None, None, None, None, None, None],
                ["Socks", "White", "BOTH", "S;M", 5000, None],
            ]
        )

        self.run_import(path)

        self.assertEqual(Garment.objects.count(), 2)

    def test_only_the_first_sheet_is_read_unless_one_is_named(self):
        path = self.workbook(
            [["White Shirt", "White", "BOTH", "6;8", 25000, None]], extra_sheet=True
        )

        self.run_import(path)

        self.assertEqual(Garment.objects.count(), 1)

    def test_naming_a_sheet_that_is_not_there_says_which_are(self):
        path = self.workbook([["White Shirt", "White", "BOTH", "6;8", 25000, None]])

        with self.assertRaises(CommandError):
            self.run_import(path, sheet="Nope")

    def test_a_file_that_is_neither_is_refused_clearly(self):
        path = Path(self.dir.name) / "notes.txt"
        path.write_text("not a spreadsheet", encoding="utf-8")

        with self.assertRaises(CommandError):
            self.run_import(str(path))

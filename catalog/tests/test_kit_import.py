"""Importing uniform kits and their components from a spreadsheet.

AsOne's 2027 catalogue has 120 kits carrying 828 component lines. Nobody is
typing that into a form, so the sheet they already keep is the way in.

The rules that matter here, and what each is for:

* **The catalogue comes first.** Components are matched to SKUs that exist;
  nothing is created. A kit sheet read against an empty catalogue should say
  so in words somebody can act on, not create unpriced garments.
* **The file's kit number is checked, not used.** The number is composed
  from the cohort, and a file that disagrees is a file and a system that
  understand the kit differently.
* **The file is authoritative for the kits it names.** A component dropped
  from the sheet is dropped from the kit — a bill of materials is a recipe,
  not a record of something that happened.
* **Running it twice changes nothing**, and **nothing is half-imported.**
"""

import io

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import make_user
from catalog.models import Garment, Kit, KitItem, Size, Sku

HEADER = (
    "Kit Number,Kit Name,Fit Group,Gender,Residence,Size,Component SKU,Qty\n"
)

#: Sharon's own spellings — the band as "P1–P4" with an en dash, the gender
#: as "Boy", the residence as "Day". None of them are the stored codes.
NURSERY_XS = (
    'KIT-NUR-B-DAY-XS,"Nursery Boy, Day — XS",Nursery,Boy,Day,XS,{shirt},1\n'
    'KIT-NUR-B-DAY-XS,"Nursery Boy, Day — XS",Nursery,Boy,Day,XS,{socks},2\n'
)


def upload(text, name="kits.csv"):
    handle = io.BytesIO(text.encode("utf-8"))
    handle.name = name
    return handle


class KitImportSetup(APITestCase):
    """A small catalogue for the kits to draw components from."""

    def setUp(self):
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.client.force_authenticate(self.lead)
        self.url = reverse("catalog:kit-import")

        self.xs = Size.objects.create(name="XS", sort_order=10)
        self.s = Size.objects.create(name="S", sort_order=20)

        shirt = Garment.objects.create(
            name="Checked Shirt",
            school_level=Garment.SchoolLevel.PRIMARY,
            fit_group=Garment.FitGroup.NURSERY,
            code="CHS",
            colour="Checked",
            colour_code="CHK",
        )
        socks = Garment.objects.create(
            name="Socks",
            school_level=Garment.SchoolLevel.BOTH,
            fit_group=Garment.FitGroup.ALL,
            code="SOC",
            colour="Grey",
            colour_code="GRY",
        )
        self.shirt_xs = Sku.objects.create(garment=shirt, size=self.xs)
        self.socks_xs = Sku.objects.create(garment=socks, size=self.xs)
        self.shirt_s = Sku.objects.create(garment=shirt, size=self.s)

    def body(self, **overrides):
        text = HEADER + NURSERY_XS.format(
            shirt=self.shirt_xs.number, socks=self.socks_xs.number
        )
        return {"file": upload(overrides.get("text", text))}

    def post(self, text=None, **extra):
        payload = self.body(**({"text": text} if text else {}))
        payload.update(extra)
        return self.client.post(self.url, payload, format="multipart")


class ImportingAKit(KitImportSetup):
    def test_a_kit_and_its_components_are_created(self):
        response = self.post()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["kits"], 1)
        self.assertEqual(response.data["components"], 2)

    def test_the_number_is_composed_not_taken_from_the_file(self):
        """Proved by composing it for a kit whose row leaves it blank."""
        text = HEADER + (
            f',"Nursery Boy, Day",Nursery,Boy,Day,XS,{self.shirt_xs.number},1\n'
        )

        self.post(text)

        self.assertEqual(Kit.objects.get().kit_number, "KIT-NUR-B-DAY-XS")

    def test_the_sheets_own_words_are_understood(self):
        """"Nursery", "Boy" and "Day" rather than NUR, B and DAY."""
        self.post()

        kit = Kit.objects.get()
        self.assertEqual(kit.fit_group, Kit.FitGroup.NURSERY)
        self.assertEqual(kit.gender, Kit.Gender.BOY)
        self.assertEqual(kit.residence, Kit.Residence.DAY)

    def test_an_en_dashed_band_is_the_same_band_as_a_hyphenated_one(self):
        """Sharon's sheet writes "P1–P4"; a corrected copy may write "P1-P4"."""
        for written in ("P1–P4", "P1-P4", "P14", "Lower Primary (P1–P4)"):
            with self.subTest(written=written):
                Kit.objects.all().delete()
                # The socks, not the shirt: the shirt is cut for Nursery
                # and `KitItem.clean()` rightly refuses it in a P1–P4 kit.
                # Socks are marked ALL and go into any band's kit.
                text = HEADER + (
                    f',"Lower Primary",{written},Boy,Day,XS,{self.socks_xs.number},1\n'
                )

                response = self.post(text)

                self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
                self.assertEqual(Kit.objects.get().fit_group, Kit.FitGroup.P1_P4)

    def test_the_price_list_follows_the_band(self):
        self.post()

        self.assertEqual(Kit.objects.get().school_level, Kit.SchoolLevel.PRIMARY)

    def test_the_quantity_on_the_row_is_kept(self):
        self.post()

        socks = KitItem.objects.get(sku=self.socks_xs)
        self.assertEqual(socks.quantity, 2)

    def test_a_blank_quantity_means_one(self):
        text = HEADER + f'KIT-NUR-B-DAY-XS,A kit,Nursery,Boy,Day,XS,{self.shirt_xs.number},\n'

        self.post(text)

        self.assertEqual(KitItem.objects.get().quantity, 1)

    def test_a_dry_run_writes_nothing_and_still_counts(self):
        response = self.post(**{"dry_run": True})

        self.assertEqual(response.data["kits"], 1)
        self.assertTrue(response.data["dry_run"])
        self.assertEqual(Kit.objects.count(), 0)


class TheKitsName(KitImportSetup):
    """AsOne name every kit twice over — "Nursery Boy, Day — Nursery — XS".

    All 120 do it, the middle one is the redundant one, and at 37 characters
    the name wraps to two lines on a card that already shows the band in the
    kit number and again on its cohort line.
    """

    def named(self, name, band="Nursery", component=None):
        # No kit number: these rows are about the name, and a number that
        # contradicted the cohort would be refused before the name mattered.
        sku = component or self.shirt_xs.number
        text = HEADER + f',"{name}",{band},Boy,Day,XS,{sku},1\n'
        self.post(text)
        return Kit.objects.get().name

    def test_the_repeated_band_is_dropped(self):
        self.assertEqual(
            self.named("Nursery Boy, Day \u2014 Nursery \u2014 XS"),
            "Nursery Boy, Day \u2014 XS",
        )

    def test_it_matches_however_the_band_is_punctuated(self):
        """The sheet writes "S5–S6" with an en dash in both places, but a
        corrected copy may not."""
        self.assertEqual(
            self.named(
                "A-Level Boy, Day \u2014 S5-S6 \u2014 L",
                band="S5\u2013S6",
                # The socks, not the Nursery shirt: an A-Level kit cannot
                # hold a Nursery garment, which is a different test's point.
                component=self.socks_xs.number,
            ),
            "A-Level Boy, Day \u2014 L",
        )

    def test_a_name_without_the_repeat_is_left_alone(self):
        self.assertEqual(
            self.named("Nursery Boy, Day \u2014 XS"), "Nursery Boy, Day \u2014 XS"
        )

    def test_a_kit_actually_called_the_band_keeps_its_name(self):
        """Only a middle segment is dropped, so this is not emptied."""
        self.assertEqual(self.named("Nursery"), "Nursery")

    def test_a_row_with_no_name_gets_one_composed(self):
        text = HEADER + f',,Nursery,Boy,Day,XS,{self.shirt_xs.number},1\n'

        self.post(text)

        self.assertEqual(Kit.objects.get().name, "Nursery Boy, Day \u2014 XS")


class RunningItTwice(KitImportSetup):
    def test_the_second_run_changes_nothing(self):
        self.post()

        response = self.post()

        self.assertEqual(response.data["kits"], 0)
        self.assertEqual(response.data["unchanged"], 1)
        self.assertEqual(Kit.objects.count(), 1)
        self.assertEqual(KitItem.objects.count(), 2)

    def test_a_changed_quantity_is_applied(self):
        self.post()

        text = HEADER + (
            f'KIT-NUR-B-DAY-XS,A kit,Nursery,Boy,Day,XS,{self.shirt_xs.number},1\n'
            f'KIT-NUR-B-DAY-XS,A kit,Nursery,Boy,Day,XS,{self.socks_xs.number},3\n'
        )
        response = self.post(text)

        self.assertEqual(response.data["components_changed"], 1)
        self.assertEqual(KitItem.objects.get(sku=self.socks_xs).quantity, 3)

    def test_a_component_dropped_from_the_file_is_dropped_from_the_kit(self):
        """The file is the recipe. A line left behind from an earlier import
        is simply wrong, and the count is reported so nobody is surprised."""
        self.post()

        text = HEADER + (
            f'KIT-NUR-B-DAY-XS,A kit,Nursery,Boy,Day,XS,{self.shirt_xs.number},1\n'
        )
        response = self.post(text)

        self.assertEqual(response.data["components_removed"], 1)
        self.assertIn("removed", response.data["detail"])
        self.assertEqual(
            list(KitItem.objects.values_list("sku", flat=True)), [self.shirt_xs.pk]
        )

    def test_a_kit_the_file_does_not_name_is_untouched(self):
        other = Kit.objects.create(
            kit_number="PS-STARTER-01",
            name="PS Starter",
            school_level=Kit.SchoolLevel.PRIMARY,
        )
        KitItem.objects.create(kit=other, sku=self.shirt_s, quantity=1)

        self.post()

        self.assertEqual(other.items.count(), 1)


class WhatIsRefused(KitImportSetup):
    def test_an_unknown_sku_names_it_and_says_what_to_do(self):
        text = HEADER + "KIT-NUR-B-DAY-XS,A kit,Nursery,Boy,Day,XS,UNI-NOPE-XS-RED,1\n"

        response = self.post(text)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        message = response.data["rows"][0]["message"]
        self.assertIn("UNI-NOPE-XS-RED", message)
        self.assertIn("import the", message)
        self.assertEqual(Kit.objects.count(), 0)

    def test_one_missing_sku_is_one_message_however_many_kits_use_it(self):
        """AsOne's socks are in all 120 kits, so a missing socks SKU produced
        120 identical messages — a wall of text saying one thing. The count
        of rows is kept, because that part was true."""
        text = HEADER + "".join(
            f'KIT-NUR-B-DAY-{size},A kit,Nursery,Boy,Day,{size},UNI-NOPE-OS-GRY,1\n'
            for size in ("XS", "S")
        )

        response = self.post(text)

        self.assertEqual(len(response.data["rows"]), 1)
        message = response.data["rows"][0]["message"]
        self.assertIn("used on 2 lines", message)
        self.assertIn("first on line 2", message)
        self.assertEqual(response.data["rows"][0]["line"], 2)
        self.assertEqual(
            response.data["detail"],
            "Nothing was imported. 1 problem, across 2 rows.",
        )

    def test_a_problem_on_one_line_does_not_say_how_many_lines(self):
        """The line number is already on the error; saying it twice is noise."""
        text = HEADER + "KIT-NUR-B-DAY-XS,A kit,Nursery,Boy,Day,XS,UNI-NOPE,1\n"

        response = self.post(text)

        self.assertNotIn("used on", response.data["rows"][0]["message"])
        self.assertEqual(response.data["detail"], "Nothing was imported. 1 problem.")

    def test_problems_are_listed_in_file_order(self):
        text = HEADER + (
            f'KIT-NUR-B-DAY-XS,A kit,Nursery,Boy,Day,XS,{self.shirt_xs.number},1\n'
            "KIT-NUR-B-DAY-S,B kit,Nursery,Boy,Day,S,UNI-SECOND,1\n"
            "KIT-NUR-G-DAY-XS,C kit,Nursery,Girl,Day,XS,UNI-THIRD,1\n"
        )

        response = self.post(text)

        self.assertEqual([row["line"] for row in response.data["rows"]], [3, 4])

    def test_a_file_number_that_contradicts_the_cohort_is_refused(self):
        """Not silently renumbered. The two disagree about what this kit is,
        and that is worth stopping for."""
        text = HEADER + (
            f'KIT-NUR-G-BRD-XL,A kit,Nursery,Boy,Day,XS,{self.shirt_xs.number},1\n'
        )

        response = self.post(text)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        message = response.data["rows"][0]["message"]
        self.assertIn("KIT-NUR-G-BRD-XL", message)
        self.assertIn("KIT-NUR-B-DAY-XS", message)

    def test_the_same_sku_twice_in_one_kit_is_refused(self):
        text = HEADER + (
            f'KIT-NUR-B-DAY-XS,A kit,Nursery,Boy,Day,XS,{self.shirt_xs.number},1\n'
            f'KIT-NUR-B-DAY-XS,A kit,Nursery,Boy,Day,XS,{self.shirt_xs.number},1\n'
        )

        response = self.post(text)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already a component", response.data["rows"][0]["message"])

    def test_one_cohort_under_two_numbers_is_refused(self):
        text = HEADER + (
            f'KIT-NUR-B-DAY-XS,A kit,Nursery,Boy,Day,XS,{self.shirt_xs.number},1\n'
            f'KIT-SOMETHING-ELSE,A kit,Nursery,Boy,Day,XS,{self.socks_xs.number},1\n'
        )

        response = self.post(text)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("cannot be two kits", response.data["rows"][0]["message"])

    def test_an_unreadable_band_says_what_is_allowed(self):
        text = HEADER + f'KIT-X,A kit,Reception,Boy,Day,XS,{self.shirt_xs.number},1\n'

        response = self.post(text)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("'Reception'", response.data["rows"][0]["message"])

    def test_every_bad_row_is_reported_not_just_the_first(self):
        text = HEADER + (
            f'KIT-A,A kit,Reception,Boy,Day,XS,{self.shirt_xs.number},1\n'
            f'KIT-B,B kit,Nursery,Alien,Day,XS,{self.shirt_xs.number},1\n'
        )

        response = self.post(text)

        self.assertEqual(len(response.data["rows"]), 2)
        self.assertEqual([row["line"] for row in response.data["rows"]], [2, 3])

    def test_a_bad_row_rolls_back_the_good_ones(self):
        text = HEADER + (
            f'KIT-NUR-B-DAY-XS,A kit,Nursery,Boy,Day,XS,{self.shirt_xs.number},1\n'
            f'KIT-B,B kit,Nursery,Boy,Day,S,UNI-NOPE,1\n'
        )

        self.post(text)

        self.assertEqual(Kit.objects.count(), 0)

    def test_a_missing_column_is_named_before_any_row_is_read(self):
        text = "Fit Group,Gender,Residence,Size\nNursery,Boy,Day,XS\n"

        response = self.post(text)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("component_sku", str(response.data["file"]))


class WhoMayImport(KitImportSetup):
    def test_a_school_clerk_may_not(self):
        """Kits are master data. A school orders them; it does not write them."""
        self.client.force_authenticate(make_user("clerk", User.Role.SCHOOL_STAFF))

        response = self.post()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Kit.objects.count(), 0)

    def test_a_warehouse_clerk_may_not(self):
        self.client.force_authenticate(make_user("ware", User.Role.WAREHOUSE_STAFF))

        response = self.post()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

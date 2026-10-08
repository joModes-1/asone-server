"""A kit's number is composed from the cohort it is cut for.

`KIT-NUR-B-DAY-XS` — the shape AsOne use in their own 2027 workbook, so a
kit created here and a kit in their sheet carry the same number without a
translation table. Nobody types it, the same arrangement as a SKU number.

Unlike a SKU number it is **not** frozen: the four parts are editable, and
changing a kit from Day to Boarder has to change its number or the number
describes the wrong kit. A SKU number goes on a shelf label; a kit number
does not.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import make_user
from catalog.models import Kit, Size


class ComposingTheNumber(TestCase):
    def setUp(self):
        self.xs = Size.objects.create(name="XS", sort_order=10)
        self.s = Size.objects.create(name="S", sort_order=20)

    def kit(self, **overrides):
        fields = {
            "name": "Nursery Boy, Day — XS",
            "fit_group": Kit.FitGroup.NURSERY,
            "gender": Kit.Gender.BOY,
            "residence": Kit.Residence.DAY,
            "size": self.xs,
        }
        fields.update(overrides)
        kit = Kit(**fields)
        kit.full_clean()
        kit.save()
        return kit

    def test_it_matches_asones_own_workbook(self):
        self.assertEqual(self.kit().kit_number, "KIT-NUR-B-DAY-XS")

    def test_nobody_types_it(self):
        """No number is supplied and `full_clean` does not complain."""
        kit = self.kit()

        self.assertTrue(kit.kit_number)

    def test_every_part_shows_up(self):
        kit = self.kit(
            fit_group=Kit.FitGroup.S5_S6,
            gender=Kit.Gender.GIRL,
            residence=Kit.Residence.BOARDER,
            size=self.s,
        )

        self.assertEqual(kit.kit_number, "KIT-S56-G-BRD-S")

    def test_it_follows_a_change_of_cohort(self):
        """Not frozen, unlike a SKU number — a kit moved from Day to Boarder
        must not keep a number saying Day."""
        kit = self.kit()

        kit.residence = Kit.Residence.BOARDER
        kit.save()

        self.assertEqual(kit.kit_number, "KIT-NUR-B-BRD-XS")

    def test_a_general_kit_keeps_the_number_it_was_given(self):
        kit = Kit(
            kit_number="PS-STARTER-01",
            name="PS Starter Kit",
            school_level=Kit.SchoolLevel.PRIMARY,
        )
        kit.full_clean()
        kit.save()

        self.assertEqual(kit.kit_number, "PS-STARTER-01")

    def test_a_general_kit_without_one_is_refused(self):
        """There is nothing to compose from, so somebody has to say."""
        kit = Kit(name="Nameless", school_level=Kit.SchoolLevel.PRIMARY)

        with self.assertRaises(ValidationError) as ctx:
            kit.full_clean()

        self.assertIn("kit_number", ctx.exception.message_dict)


class ThroughTheApi(APITestCase):
    def setUp(self):
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.client.force_authenticate(self.lead)
        self.xs = Size.objects.create(name="XS", sort_order=10)
        self.url = reverse("catalog:kit-list")

    def post(self, **overrides):
        body = {
            "name": "Nursery Boy, Day — XS",
            "fit_group": "NUR",
            "gender": "B",
            "residence": "DAY",
            "size": self.xs.pk,
        }
        body.update(overrides)
        return self.client.post(self.url, body, format="json")

    def test_a_kit_is_created_without_a_number_being_sent(self):
        response = self.post()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["kit_number"], "KIT-NUR-B-DAY-XS")

    def test_the_cohort_comes_back_readable(self):
        response = self.post()

        self.assertEqual(response.data["fit_group_display"], "Nursery")
        self.assertEqual(response.data["gender_display"], "Boy")
        self.assertEqual(response.data["residence_display"], "Day")
        self.assertEqual(response.data["size_name"], "XS")

    def test_a_second_kit_for_the_same_cohort_is_refused_with_a_400(self):
        """Not a 500 from the database — and it names the kit that holds it."""
        self.post()

        response = self.post(name="A duplicate")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("KIT-NUR-B-DAY-XS", str(response.data["fit_group"]))

    def test_the_cohort_can_be_filtered_on(self):
        """The reason these are fields and not words in the kit's name: a
        school ordering for a boarder narrows 120 kits down to six."""
        self.post()
        self.post(name="A boarder's", residence="BRD")

        response = self.client.get(self.url, {"residence": "BRD"})

        numbers = [row["kit_number"] for row in response.data["results"]]
        self.assertEqual(numbers, ["KIT-NUR-B-BRD-XS"])

    def test_sizes_come_back_in_size_order_not_alphabetical(self):
        """S, M, L is not alphabetical, so the list orders on `sort_order`."""
        small = Size.objects.create(name="S", sort_order=20)
        medium = Size.objects.create(name="M", sort_order=30)
        self.post(name="Medium", size=medium.pk)
        self.post(name="Small", size=small.pk)
        self.post(name="Extra small")

        response = self.client.get(self.url, {"fit_group": "NUR"})

        sizes = [row["size_name"] for row in response.data["results"]]
        self.assertEqual(sizes, ["XS", "S", "M"])

    def test_a_general_kit_still_takes_a_typed_number(self):
        """Several general kits are not a clash, so they are not compared."""
        first = self.client.post(
            self.url,
            {"kit_number": "PS-STARTER-01", "name": "PS Starter", "school_level": "PS"},
            format="json",
        )
        second = self.client.post(
            self.url,
            {"kit_number": "HS-STARTER-01", "name": "HS Starter", "school_level": "HS"},
            format="json",
        )

        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)

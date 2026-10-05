"""A garment says which classes wear it, not just which price list it is on.

The two sound alike and are different questions. `school_level` decides
which price list a garment appears on; `fit_group` says which classes
actually wear it. AsOne's 2027 catalogue has "Sweater" at five fit groups,
and the old uniqueness rule — name plus school level — allowed three. Five
sweaters could not be created, so the catalogue could not be loaded at all.

`TheCatalogueThatCouldNotBeLoaded` is the class to read first: it is the
blocker this was built to clear.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase

from catalog.models import Garment

FitGroup = Garment.FitGroup
SchoolLevel = Garment.SchoolLevel


class TheCatalogueThatCouldNotBeLoaded(TestCase):
    def test_one_name_can_exist_at_every_fit_group(self):
        """AsOne sells a Sweater to all five bands. Before this it could
        hold three, because three is how many school levels there are."""
        for group in FitGroup.values:
            Garment.objects.create(name="Sweater", fit_group=group, colour_code="GRY")

        self.assertEqual(Garment.objects.filter(name="Sweater").count(), 6)

    def test_the_same_name_and_fit_group_twice_is_still_refused(self):
        """Widening the rule must not stop it catching a real duplicate."""
        Garment.objects.create(name="Sweater", fit_group=FitGroup.P1_P4, colour_code="GRY")

        clash = Garment(name="sweater", fit_group=FitGroup.P1_P4, colour_code="GRY")

        with self.assertRaises(ValidationError):
            clash.full_clean()

    def test_garments_with_no_fit_group_behave_as_before(self):
        """Nothing that already worked stops working."""
        Garment.objects.create(name="Blazer", school_level=SchoolLevel.PRIMARY)
        Garment.objects.create(name="Blazer", school_level=SchoolLevel.HIGH)

        self.assertEqual(Garment.objects.filter(name="Blazer").count(), 2)


class ThePriceListFollowsTheFitGroup(TestCase):
    def test_nursery_through_p7_is_primary(self):
        for group in (FitGroup.NURSERY, FitGroup.P1_P4, FitGroup.P5_P7):
            with self.subTest(group=group):
                g = Garment(name=f"Shirt {group}", fit_group=group, colour_code="WHT")
                g.full_clean()
                self.assertEqual(g.school_level, SchoolLevel.PRIMARY)

    def test_s1_upwards_is_high_school(self):
        for group in (FitGroup.S1_S4, FitGroup.S5_S6):
            with self.subTest(group=group):
                g = Garment(name=f"Shirt {group}", fit_group=group, colour_code="WHT")
                g.full_clean()
                self.assertEqual(g.school_level, SchoolLevel.HIGH)

    def test_a_garment_for_every_band_is_on_both_lists(self):
        """Socks. They go to Nursery and to S6 alike."""
        socks = Garment(name="Socks", fit_group=FitGroup.ALL, colour_code="GRY")
        socks.full_clean()

        self.assertEqual(socks.school_level, SchoolLevel.BOTH)

    def test_a_contradiction_is_refused_rather_than_corrected(self):
        wrong = Garment(
            name="Sweater",
            fit_group=FitGroup.P1_P4,
            colour_code="GRY",
            school_level=SchoolLevel.HIGH,
        )

        with self.assertRaises(ValidationError) as ctx:
            wrong.full_clean()

        self.assertIn("school_level", ctx.exception.message_dict)
        self.assertIn("Primary", str(ctx.exception))


class TheItemCode(TestCase):
    """A third of AsOne's codes cannot be derived from the garment name, so
    whoever adds the garment has to be able to say."""

    def test_a_typed_code_is_kept(self):
        """The system would say SWE. AsOne says SWT."""
        sweater = Garment.objects.create(name="Sweater", code="SWT")

        self.assertEqual(sweater.code, "SWT")

    def test_a_code_unrelated_to_the_name_is_kept(self):
        """Necktie is TIE. Nothing derives that."""
        tie = Garment.objects.create(name="Necktie", code="TIE")

        self.assertEqual(tie.code, "TIE")

    def test_a_blank_code_is_still_derived(self):
        """Nobody has to type one."""
        shirt = Garment.objects.create(name="White Shirt")

        self.assertEqual(shirt.code, "WSH")

    def test_colour_codes_are_stored_not_guessed(self):
        """Green and Grey would both shorten to GRE."""
        green = Garment.objects.create(
            name="Green Shorts", fit_group=FitGroup.P1_P4, colour="Green", colour_code="GRN"
        )
        grey = Garment.objects.create(
            name="Grey Shorts", fit_group=FitGroup.P1_P4, colour="Grey", colour_code="GRY"
        )

        self.assertNotEqual(green.colour_code, grey.colour_code)

    def test_a_fit_group_without_a_colour_code_is_refused(self):
        """One without the other makes `sku_code()` emit the short form from
        a row the unique index thinks is distinct — so two garments generate
        one SKU number. Refused in `clean()` and in the database, because
        `objects.create()` skips `clean()`."""
        from django.db import IntegrityError, transaction

        half = Garment(name="Half", fit_group=FitGroup.P1_P4)

        with self.assertRaises(ValidationError) as ctx:
            half.full_clean()
        self.assertIn("colour_code", ctx.exception.message_dict)

        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Garment.objects.create(name="Half", fit_group=FitGroup.P1_P4)

    def test_a_colour_code_without_a_fit_group_is_refused_too(self):
        with self.assertRaises(ValidationError) as ctx:
            Garment(name="Other half", colour_code="GRY").full_clean()

        self.assertIn("fit_group", ctx.exception.message_dict)

    def test_the_stem_is_stored_upper_cased(self):
        """The stored value is the printed value. These used to disagree —
        the index lower-cased one half and the number upper-cased the other."""
        g = Garment.objects.create(
            name="Lower case", fit_group=FitGroup.NURSERY, code="swt", colour_code="gry"
        )

        g.refresh_from_db()
        self.assertEqual(g.code, "SWT")
        self.assertEqual(g.colour_code, "GRY")

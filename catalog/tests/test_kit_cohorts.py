"""A kit knows which cohort it is cut for.

AsOne's 2027 catalogue has 120 kits and four facts tell them apart:

    5 fit groups x 2 genders x 2 residences x 6 sizes = 120

Day versus boarder is the one that prompted modelling these. A boarder's kit
carries the casual and dorm wear a day pupil goes home for, so the two are
different kits — not one kit with a different label — and a school ordering
for a boarder has to be able to ask for one.

Before this they lived inside the kit's *name*, so nothing could filter by
them and two genuinely different kits were distinguished only by a string.
"""

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase

from catalog.models import Garment, Kit, KitItem, Size, Sku


class CohortSetup(TestCase):
    def setUp(self):
        self.xs = Size.objects.create(name="XS", sort_order=10)
        self.s = Size.objects.create(name="S", sort_order=20)

    def kit(self, number="KIT-NUR-B-DAY-XS", **overrides):
        fields = {
            "kit_number": number,
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


class TheFourDimensions(CohortSetup):
    def test_a_kit_records_day_or_boarder(self):
        day = self.kit()
        boarder = self.kit("KIT-NUR-B-BRD-XS", residence=Kit.Residence.BOARDER)

        self.assertEqual(day.residence, "DAY")
        self.assertEqual(boarder.residence, "BRD")
        self.assertEqual(boarder.get_residence_display(), "Boarder")

    def test_boarders_can_be_filtered_for(self):
        """The point of modelling it rather than leaving it in the name."""
        self.kit()
        self.kit("KIT-NUR-B-BRD-XS", residence=Kit.Residence.BOARDER)
        self.kit("KIT-NUR-G-BRD-XS", gender=Kit.Gender.GIRL,
                 residence=Kit.Residence.BOARDER)

        boarders = Kit.objects.filter(residence=Kit.Residence.BOARDER)

        self.assertEqual(boarders.count(), 2)

    def test_the_four_together_identify_a_kit(self):
        self.kit()
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                Kit.objects.create(
                    kit_number="DIFFERENT-NUMBER", name="A duplicate",
                    school_level=Kit.SchoolLevel.PRIMARY,
                    fit_group=Kit.FitGroup.NURSERY, gender=Kit.Gender.BOY,
                    residence=Kit.Residence.DAY, size=self.xs,
                )

    def test_the_same_cohort_in_another_size_is_a_different_kit(self):
        self.kit()
        other = self.kit("KIT-NUR-B-DAY-S", size=self.s)

        self.assertEqual(Kit.objects.count(), 2)
        self.assertEqual(other.size.name, "S")


class AllFourOrNone(CohortSetup):
    """A kit that knows it is for boarders but not which class band is not
    something anybody can order."""

    def test_a_half_specified_kit_is_refused(self):
        kit = Kit(
            kit_number="KIT-HALF", name="Half a kit",
            school_level=Kit.SchoolLevel.PRIMARY,
            residence=Kit.Residence.BOARDER,
        )

        with self.assertRaises(ValidationError) as ctx:
            kit.full_clean()

        self.assertIn("fit_group", ctx.exception.message_dict)

    def test_a_general_kit_leaves_all_four_blank(self):
        """The demo starter kits predate these fields. Inventing a gender
        for one would be worse than recording that none was stated."""
        kit = Kit(
            kit_number="PS-STARTER-01", name="PS Starter Kit",
            school_level=Kit.SchoolLevel.PRIMARY,
        )
        kit.full_clean()
        kit.save()

        self.assertFalse(kit.is_cohort_specific)

    def test_several_general_kits_do_not_clash(self):
        """The uniqueness rule is conditional for exactly this reason."""
        for number in ("PS-STARTER-01", "HS-STARTER-01", "PS-SPORT-01"):
            Kit.objects.create(
                kit_number=number, name=number,
                school_level=Kit.SchoolLevel.PRIMARY,
            )

        self.assertEqual(Kit.objects.filter(fit_group="").count(), 3)


class ThePriceListFollowsTheFitGroup(CohortSetup):
    """`KitItem.clean()` refuses a component off the kit's price list, so a
    kit claiming the wrong one would reject the garments it needs."""

    def test_nursery_through_p7_is_primary(self):
        for group in (Kit.FitGroup.NURSERY, Kit.FitGroup.P1_P4, Kit.FitGroup.P5_P7):
            with self.subTest(group=group):
                kit = self.kit(f"KIT-{group}-B-DAY-XS", fit_group=group)
                self.assertEqual(kit.school_level, Kit.SchoolLevel.PRIMARY)

    def test_s1_upwards_is_high_school(self):
        for group in (Kit.FitGroup.S1_S4, Kit.FitGroup.S5_S6):
            with self.subTest(group=group):
                kit = self.kit(f"KIT-{group}-B-DAY-XS", fit_group=group)
                self.assertEqual(kit.school_level, Kit.SchoolLevel.HIGH)

    def test_it_is_derived_rather_than_asked_for(self):
        """The helper never passes school_level; the fit group supplies it."""
        kit = self.kit()

        self.assertEqual(kit.school_level, Kit.SchoolLevel.PRIMARY)

    def test_a_general_kit_must_still_state_its_price_list(self):
        """Nothing to derive it from, so somebody has to say."""
        kit = Kit(kit_number="KIT-GENERAL", name="General")

        with self.assertRaises(ValidationError) as ctx:
            kit.full_clean()

        self.assertIn("school_level", ctx.exception.message_dict)

    def test_a_contradiction_is_refused_rather_than_silently_corrected(self):
        """Being told is better than having it quietly changed underneath."""
        kit = Kit(
            kit_number="KIT-WRONG", name="Contradictory",
            school_level=Kit.SchoolLevel.HIGH,
            fit_group=Kit.FitGroup.P1_P4, gender=Kit.Gender.BOY,
            residence=Kit.Residence.DAY, size=self.xs,
        )

        with self.assertRaises(ValidationError) as ctx:
            kit.full_clean()

        self.assertIn("school_level", ctx.exception.message_dict)
        self.assertIn("Primary", str(ctx.exception))


class ComponentsMatchTheKitSize(CohortSetup):
    """A kit built in one size is made of components in that size.

    The one real exception is the one-size items — socks and neckties — which
    go into every kit regardless. Checked against AsOne's 2027 bill of
    materials: of 828 component lines, 144 are a size other than the kit's,
    and every one of the 144 is a one-size item.

    Without this, a size-M kit could be built with size-XXL shorts and
    nothing would notice until a pupil opened the bag.
    """

    def setUp(self):
        super().setUp()
        self.one_size = Size.objects.create(
            name="OS", sort_order=99, is_one_size=True
        )
        self.shirt = Garment.objects.create(
            name="White Shirt", school_level=Garment.SchoolLevel.BOTH
        )
        self.socks = Garment.objects.create(
            name="Socks", school_level=Garment.SchoolLevel.BOTH
        )
        self.kit_xs = self.kit()

    def item(self, garment, size):
        line = KitItem(
            kit=self.kit_xs,
            sku=Sku.objects.create(garment=garment, size=size),
            quantity=1,
        )
        line.full_clean()
        line.save()
        return line

    def test_a_component_in_the_kits_size_is_accepted(self):
        line = self.item(self.shirt, self.xs)

        self.assertEqual(line.sku.size.name, "XS")

    def test_a_component_in_another_size_is_refused(self):
        with self.assertRaises(ValidationError) as ctx:
            self.item(self.shirt, self.s)

        self.assertIn("sku", ctx.exception.message_dict)
        self.assertIn("built in XS", str(ctx.exception))

    def test_a_one_size_component_is_accepted_anyway(self):
        """Socks and neckties go into every kit whatever size it is."""
        line = self.item(self.socks, self.one_size)

        self.assertEqual(line.sku.size.name, "OS")

    def test_the_rule_is_the_flag_not_the_name(self):
        """A size called OS that was never flagged is still the wrong size.

        The flag is what carries the meaning — a rule that depended on the
        spelling would stop applying the moment somebody typed "One Size".
        """
        unflagged = Size.objects.create(name="O/S", sort_order=98)

        with self.assertRaises(ValidationError):
            self.item(self.socks, unflagged)

    def test_a_general_kit_has_nothing_to_check_against(self):
        """Kits that state no cohort carry no size, so the rule is silent."""
        general = Kit.objects.create(
            kit_number="PS-STARTER-01", name="PS Starter Kit",
            school_level=Kit.SchoolLevel.PRIMARY,
        )
        line = KitItem(
            kit=general,
            sku=Sku.objects.create(garment=self.shirt, size=self.s),
            quantity=1,
        )
        line.full_clean()
        line.save()

        self.assertIsNone(general.size)

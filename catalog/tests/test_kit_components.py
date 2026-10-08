"""What may be in a kit, and what happens when something may not.

Two rules, both narrower than they first look, and both measured against
AsOne's 2027 bill of materials rather than guessed:

* **A kit's components are cut for the kit's own class band.** Of its 828
  component lines, 120 name a different band and all 120 are `ALL` items —
  the socks. The price-list rule alone is too loose to catch this, because
  Nursery, P1–P4 and P5–P7 are all Primary.
* **A kit's components are in the kit's own size.** 144 lines differ and all
  144 are one-size items. That rule was already here; this file pins the
  band one beside it.

And the thing that happens when a rule is broken: **nothing is created.**
A kit saved with three of its five components is in the catalogue, is
orderable, and is wrong.
"""

from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import make_user
from catalog.models import Garment, Kit, KitItem, Size, Sku


class Catalogue:
    """A Nursery shirt, an Upper Primary shirt and the socks everyone wears."""

    def build(self):
        self.xs = Size.objects.create(name="XS", sort_order=10)
        self.m = Size.objects.create(name="M", sort_order=30)
        self.os = Size.objects.create(name="OS", sort_order=99, is_one_size=True)

        def garment(name, band, code, colour_code, level=Garment.SchoolLevel.PRIMARY):
            return Garment.objects.create(
                name=name,
                school_level=level,
                fit_group=band,
                code=code,
                colour="Grey",
                colour_code=colour_code,
            )

        self.nursery_shirt = Sku.objects.create(
            garment=garment("Checked Shirt", Garment.FitGroup.NURSERY, "CHS", "CHK"),
            size=self.xs,
        )
        self.upper_shirt = Sku.objects.create(
            garment=garment("Checked Shirt", Garment.FitGroup.P5_P7, "CHS", "CHK"),
            size=self.xs,
        )
        self.nursery_shirt_m = Sku.objects.create(
            garment=self.nursery_shirt.garment, size=self.m
        )
        self.socks = Sku.objects.create(
            garment=garment(
                "Socks",
                Garment.FitGroup.ALL,
                "SOC",
                "GRY",
                level=Garment.SchoolLevel.BOTH,
            ),
            size=self.os,
        )

        self.kit = Kit.objects.create(
            name="Nursery Boy, Day — XS",
            fit_group=Kit.FitGroup.NURSERY,
            gender=Kit.Gender.BOY,
            residence=Kit.Residence.DAY,
            size=self.xs,
        )


class TheClassBandRule(TestCase, Catalogue):
    def setUp(self):
        self.build()

    def line(self, sku):
        item = KitItem(kit=self.kit, sku=sku, quantity=1)
        item.full_clean()
        return item

    def test_a_garment_of_the_kits_own_band_is_allowed(self):
        self.line(self.nursery_shirt)  # does not raise

    def test_an_all_band_garment_goes_into_any_kit(self):
        """The socks. 120 of AsOne's 828 lines are exactly this."""
        self.line(self.socks)  # does not raise

    def test_another_bands_garment_is_refused(self):
        """Both shirts are Primary, so the price-list rule lets this through.
        They are still two different garments."""
        with self.assertRaises(ValidationError) as ctx:
            self.line(self.upper_shirt)

        message = str(ctx.exception)
        self.assertIn("Upper Primary", message)
        self.assertIn("Nursery", message)

    def test_a_kit_stating_no_band_is_left_to_the_price_list_rule(self):
        """The general kits predate the field; refusing them would make them
        unmaintainable."""
        general = Kit.objects.create(
            kit_number="PS-STARTER-01",
            name="PS Starter",
            school_level=Kit.SchoolLevel.PRIMARY,
        )

        item = KitItem(kit=general, sku=self.upper_shirt, quantity=1)
        item.full_clean()  # does not raise

    def test_the_size_rule_still_applies_alongside_it(self):
        """Right band, wrong size — and the socks prove it is not the band
        check doing the work."""
        with self.assertRaises(ValidationError) as ctx:
            self.line(self.nursery_shirt_m)

        self.assertIn("built in XS", str(ctx.exception))


class CreatingAKitWithItsComponents(APITestCase, Catalogue):
    """One request, one transaction.

    This was two calls from the screen — POST the kit, then POST each line —
    and they can half-succeed. A kit holding three of its five components is
    worse than either outcome, because nothing downstream can tell.
    """

    def setUp(self):
        self.build()
        self.kit.delete()
        self.client.force_authenticate(make_user("sharon", User.Role.PROGRAM_LEAD))
        self.url = reverse("catalog:kit-list")

    def post(self, items):
        return self.client.post(
            self.url,
            {
                "name": "Nursery Boy, Day — XS",
                "fit_group": "NUR",
                "gender": "B",
                "residence": "DAY",
                "size": self.xs.pk,
                "items": items,
            },
            format="json",
        )

    def test_the_kit_and_its_components_arrive_together(self):
        response = self.post(
            [
                {"sku": self.nursery_shirt.pk, "quantity": 1},
                {"sku": self.socks.pk, "quantity": 3},
            ]
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        kit = Kit.objects.get()
        self.assertEqual(kit.kit_number, "KIT-NUR-B-DAY-XS")
        self.assertEqual(kit.items.count(), 2)
        self.assertEqual(kit.items.get(sku=self.socks).quantity, 3)

    def test_a_quantity_defaults_to_one(self):
        self.post([{"sku": self.nursery_shirt.pk}])

        self.assertEqual(KitItem.objects.get().quantity, 1)

    def test_one_refused_component_means_no_kit_at_all(self):
        """The point of the whole exercise. The first line is fine."""
        response = self.post(
            [
                {"sku": self.nursery_shirt.pk, "quantity": 1},
                {"sku": self.upper_shirt.pk, "quantity": 1},
            ]
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Kit.objects.count(), 0)
        self.assertEqual(KitItem.objects.count(), 0)

    def test_the_refusal_names_the_line_that_is_wrong(self):
        response = self.post(
            [
                {"sku": self.nursery_shirt.pk, "quantity": 1},
                {"sku": self.upper_shirt.pk, "quantity": 1},
            ]
        )

        # Keyed by position, so the screen can mark the row rather than
        # printing a paragraph above the table.
        self.assertIn("1", response.data["items"])
        self.assertIn("Upper Primary", str(response.data["items"]["1"]))

    def test_the_same_sku_twice_is_refused_as_a_quantity_mistake(self):
        response = self.post(
            [
                {"sku": self.nursery_shirt.pk, "quantity": 1},
                {"sku": self.nursery_shirt.pk, "quantity": 1},
            ]
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("quantity", str(response.data["items"]["1"]))
        self.assertEqual(Kit.objects.count(), 0)

    def test_a_kit_may_still_be_created_with_no_components(self):
        """Allowed, and the screens say it cannot be priced or ordered until
        it has some. Refusing it would make the import's job harder for no
        gain."""
        response = self.client.post(
            self.url,
            {
                "name": "Nursery Boy, Day — XS",
                "fit_group": "NUR",
                "gender": "B",
                "residence": "DAY",
                "size": self.xs.pk,
            },
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)


class ReplacingComponentsOnAnEdit(APITestCase, Catalogue):
    def setUp(self):
        self.build()
        KitItem.objects.create(kit=self.kit, sku=self.nursery_shirt, quantity=1)
        self.client.force_authenticate(make_user("sharon", User.Role.PROGRAM_LEAD))
        self.url = reverse("catalog:kit-detail", args=[self.kit.pk])

    def test_sending_items_replaces_them(self):
        response = self.client.patch(
            self.url, {"items": [{"sku": self.socks.pk, "quantity": 2}]}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            list(self.kit.items.values_list("sku", flat=True)), [self.socks.pk]
        )

    def test_not_sending_them_leaves_them_alone(self):
        """A PATCH that renames a kit must not empty it."""
        self.client.patch(self.url, {"name": "A new name"}, format="json")

        self.assertEqual(self.kit.items.count(), 1)

    def test_an_empty_list_does_empty_it(self):
        self.client.patch(self.url, {"items": []}, format="json")

        self.assertEqual(self.kit.items.count(), 0)

    def test_a_refused_replacement_keeps_what_was_there(self):
        self.client.patch(
            self.url, {"items": [{"sku": self.upper_shirt.pk}]}, format="json"
        )

        self.assertEqual(
            list(self.kit.items.values_list("sku", flat=True)),
            [self.nursery_shirt.pk],
        )

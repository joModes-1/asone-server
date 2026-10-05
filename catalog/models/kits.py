"""Uniform Kits — bundles of SKUs sold as one unit.

AsOne's vocabulary: a Uniform Kit is what a school orders for a new student —
a fixed bundle of SKUs, e.g. a "PS Starter Kit" of one shirt, one tunic and a
pair of socks. The Point-of-Sale phase explodes a kit order into the SKUs
underneath it; this module only defines what is *in* a kit and what it costs.

**Kit price is never stored.** It is always the sum of the current prices of
the SKUs that make it up — see `compute_kit_price()` in catalog/services.py.
A stored total would go stale silently the moment one component's garment
was repriced, and nobody would notice until a school was invoiced the wrong
amount. Same reasoning as GarmentPrice; see catalog/models/pricing.py.
"""

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.db.models.functions import Lower


class Kit(models.Model):
    """A bundle of SKUs sold as one unit, e.g. a new-student starter kit.

    `kit_number` is entered by whoever creates the kit, unlike `Sku.number`.
    There is no requirement here for a system-assigned, never-reused
    sequence — kits are few (a handful, not the ~200 SKUs), and nothing in
    AsOne's brief calls for that guarantee at kit level.
    """

    class SchoolLevel(models.TextChoices):
        """Which price list this kit's components are drawn from.

        Deliberately two values, not three: unlike Garment.SchoolLevel,
        there is no "BOTH" here. AsOne's kits are described as school-level
        specific — a PS starter kit differs from an HS one — so there is no
        case yet for one kit appearing on both lists.

        Derived from `fit_group` where one is set — see `clean()`. The two
        cannot be allowed to disagree: `KitItem.clean()` refuses a component
        whose garment is not on this price list, so a kit claiming HS while
        its fit group says P1–P4 would reject exactly the garments it needs.
        """

        PRIMARY = "PS", "Primary School"
        HIGH = "HS", "High School"

    class FitGroup(models.TextChoices):
        """The class band a kit is cut for.

        Finer than `school_level`, and answering a different question. The
        school level says which price list a kit belongs on; the fit group
        says which classes actually wear it. A P1–P4 sweater and a P5–P7
        sweater are both Primary and are not the same garment.

        The codes are AsOne's own, from the 2027 SKU workbook, so a kit
        number can be composed from them without a translation table.
        """

        NURSERY = "NUR", "Nursery"
        P1_P4 = "P14", "Lower Primary (P1–P4)"
        P5_P7 = "P57", "Upper Primary (P5–P7)"
        S1_S4 = "S14", "O Level (S1–S4)"
        S5_S6 = "S56", "A Level (S5–S6)"

    class Gender(models.TextChoices):
        """Who the kit is cut for. AsOne's kits are gendered below S6 —
        a boy's kit has shorts where a girl's has a dress."""

        BOY = "B", "Boy"
        GIRL = "G", "Girl"

    class Residence(models.TextChoices):
        """Day pupil or boarder.

        A boarder's kit carries the casual and dorm wear a day pupil goes
        home for, so the two differ by several components, not by a label.
        Modelled rather than left in the kit's name because a school
        ordering for a boarder must be able to *filter* for one.
        """

        DAY = "DAY", "Day"
        BOARDER = "BRD", "Boarder"

    #: Which class band, gender, residence and size this kit is cut for.
    #:
    #: AsOne's 2027 catalogue has 120 kits and these four are exactly what
    #: tell them apart: 5 fit groups x 2 genders x 2 residences x 6 sizes.
    #: They lived inside the kit's *name* before this ("Nursery Boy, Day —
    #: Nursery — XS"), which meant nothing could filter or group by them and
    #: two genuinely different kits differed only by a string.
    #:
    #: Nullable, because a kit need not target a cohort — the demo starter
    #: kits predate these fields and inventing a gender for them would be
    #: worse than recording that none was stated. `clean()` enforces
    #: all-or-nothing, so a half-specified kit cannot exist.
    FIT_FIELDS = ("fit_group", "gender", "residence", "size")

    #: Which price list each fit group draws from. The single statement of
    #: the rule; `clean()` applies it so `school_level` cannot drift.
    PRICE_LIST_BY_FIT_GROUP = {
        FitGroup.NURSERY: SchoolLevel.PRIMARY,
        FitGroup.P1_P4: SchoolLevel.PRIMARY,
        FitGroup.P5_P7: SchoolLevel.PRIMARY,
        FitGroup.S1_S4: SchoolLevel.HIGH,
        FitGroup.S5_S6: SchoolLevel.HIGH,
    }

    kit_number = models.CharField(
        max_length=20,
        help_text='Entered by whoever creates the kit, e.g. "PS-STARTER-01".',
    )
    name = models.CharField(max_length=120, help_text='For example "PS Starter Kit".')

    # What this kit is for, in a sentence — "Standard kit issued to central
    # regional schools". A school clerk choosing between two kits has only
    # the name and the component list to go on otherwise, and the names are
    # deliberately short.
    description = models.CharField(
        max_length=300,
        blank=True,
        help_text="Who this kit is for, in a sentence. Shown to schools choosing one.",
    )
    # `blank=True` so `clean()` can fill it in from `fit_group`. Django runs
    # field validation *before* `clean()`, so a field marked required here
    # would be refused as empty before the derivation ever ran. It is still
    # required in practice — `clean()` refuses a kit that ends up without
    # one — but the requirement is stated there, where the derivation is.
    school_level = models.CharField(
        max_length=2, choices=SchoolLevel.choices, blank=True
    )

    fit_group = models.CharField(
        max_length=3,
        choices=FitGroup.choices,
        blank=True,
        help_text="The class band this kit is cut for, e.g. P1\u2013P4.",
    )
    gender = models.CharField(
        max_length=1,
        choices=Gender.choices,
        blank=True,
        help_text="Who the kit is cut for.",
    )
    residence = models.CharField(
        max_length=3,
        choices=Residence.choices,
        blank=True,
        help_text="Day pupil or boarder \u2014 a boarder's kit carries dorm wear.",
    )
    size = models.ForeignKey(
        "catalog.Size",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="kits",
        help_text="The size this kit is built in. One kit per size.",
    )

    # AsOne's "Active Y/N", same as Garment and Sku. Kits are deactivated,
    # never deleted — a school's past order may point at one.
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive kits stay in reports but cannot be ordered.",
    )

    class Meta:
        ordering = ["school_level", "fit_group", "gender", "residence", "name"]
        constraints = [
            # Kit numbers are typed by hand, so case-insensitive: "PS-01" and
            # "ps-01" are the same kit to everyone except the database.
            models.UniqueConstraint(Lower("kit_number"), name="unique_kit_number"),
            # One kit per cohort-and-size. Two rows for "Nursery boy, day,
            # XS" would split the same kit in two and a school choosing
            # between them would be picking at random.
            #
            # Conditional, because the four fields are optional together:
            # the demo starter kits leave them blank and several blank rows
            # are not a clash. Postgres treats NULLs as distinct anyway, but
            # these are blank strings rather than NULLs, so the condition is
            # doing real work.
            models.UniqueConstraint(
                fields=["fit_group", "gender", "residence", "size"],
                name="unique_kit_per_cohort_and_size",
                condition=~models.Q(fit_group=""),
            ),
            # `school_level` was made blank-able so `clean()` could derive it
            # from the fit group — and `clean()` is not called by
            # `objects.create()` or by a DRF serializer's `.save()`, so
            # `POST /api/catalog/kits/` began creating kits with no price
            # list at all. `KitItem.clean()` then measured every component
            # against "", and refused all of them.
            #
            # The field cannot go back to being required, or the derivation
            # could never run. This says the same thing one step later,
            # where nothing can skip it.
            models.CheckConstraint(
                condition=~models.Q(school_level=""),
                name="kit_has_a_price_list",
            ),
        ]

    @property
    def is_cohort_specific(self) -> bool:
        """True when this kit names the cohort it is cut for.

        False for the generic kits that predate these fields. Kept as a
        property rather than repeated as four `if`s at every call site.
        """
        return bool(self.fit_group)

    def clean(self):
        """Keep the four cohort fields, and the price list, consistent.

        Two rules, both model-level so the admin enforces them too — Central
        Office builds kits there.
        """
        super().clean()
        errors = {}

        # All four or none. A kit that knows it is for boarders but not
        # which class band is not a kit anybody can order, and it would slip
        # past the uniqueness constraint as well.
        stated = [f for f in self.FIT_FIELDS if getattr(self, f, None)]
        if stated and len(stated) != len(self.FIT_FIELDS):
            missing = [f for f in self.FIT_FIELDS if f not in stated]
            for field in missing:
                errors[field] = (
                    "A kit cut for a particular cohort must state all of "
                    "fit group, gender, residence and size. Leave all four "
                    "blank for a general kit."
                )

        # The price list follows from the fit group, rather than being a
        # second thing to keep in step by hand. Nursery through P7 is
        # Primary; S1 upwards is High.
        if self.fit_group:
            implied = self.PRICE_LIST_BY_FIT_GROUP[self.FitGroup(self.fit_group)]
            if self.school_level and self.school_level != implied:
                # Refused rather than silently corrected. Being told the two
                # disagree is worth more than having one quietly changed
                # underneath whoever set it.
                errors["school_level"] = (
                    f"{self.get_fit_group_display()} is "
                    f"{self.SchoolLevel(implied).label}, not "
                    f"{self.get_school_level_display()}. Its components are "
                    "drawn from that price list."
                )
            else:
                self.school_level = implied

        # Still required — see the field. A general kit has no fit group to
        # derive it from, so whoever creates one has to say.
        if not self.school_level and "school_level" not in errors:
            errors["school_level"] = "This field cannot be blank."

        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        """Derive the price list here too, not only in `clean()`.

        `objects.create()` skips `clean()`, so a kit made that way kept
        whatever school level it was handed while a validated one got the
        level its fit group implies. The same gap bit `Garment` — see the
        comment on its `save()` — and it matters more here, because
        `KitItem.clean()` refuses a component that is off the kit's price
        list. A kit carrying the wrong one rejects the garments it needs.
        """
        if self.fit_group:
            self.school_level = self.PRICE_LIST_BY_FIT_GROUP[
                self.FitGroup(self.fit_group)
            ]

        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.kit_number} — {self.name}"


class KitItem(models.Model):
    """One line of a kit's bill of materials: a component SKU and how many."""

    # CASCADE, not this codebase's default PROTECT — see SETUP.md, where
    # PROTECT is the deliberate default everywhere, as a safety net against
    # accidentally losing data a delete did not mean to touch. This field is
    # the one legitimate exception: a KitItem has no meaning independent of
    # its Kit — it is a line on the Kit's own bill of materials, not a fact
    # about anything else — so when a Kit is deleted, its line items should
    # go with it automatically rather than blocking the delete or being left
    # behind as orphaned rows. Do not "fix" this back to PROTECT thinking it
    # was missed; it is intentional. Contrast with `sku` below, which keeps
    # PROTECT: a SKU is real master data that exists independently of any
    # one kit, so deleting one that a kit still depends on is refused,
    # exactly as it would be anywhere else in this codebase.
    kit = models.ForeignKey(Kit, on_delete=models.CASCADE, related_name="items")
    sku = models.ForeignKey(
        "catalog.Sku", on_delete=models.PROTECT, related_name="kit_items"
    )
    quantity = models.PositiveIntegerField(
        validators=[MinValueValidator(1)],
        help_text="How many of this SKU one kit contains.",
    )

    class Meta:
        ordering = ["kit", "sku"]
        constraints = [
            # One line per SKU per kit — two rows for the same SKU would
            # split its quantity across two line items instead of one.
            models.UniqueConstraint(fields=["kit", "sku"], name="unique_sku_per_kit")
        ]

    def clean(self):
        """Keep a kit's components consistent with the kit itself.

        Both rules are model-level rather than serializer-level so the admin
        enforces them too — Central Office builds kits there.
        """
        super().clean()
        errors = {}

        # A retired SKU cannot be ordered, so a kit containing one cannot be
        # fulfilled. Better refused when the kit is built than discovered by
        # a warehouse holding a pick list it cannot complete.
        if self.sku_id and not self.sku.is_active:
            errors["sku"] = (
                f"{self.sku.number} is retired and cannot be added to a kit."
            )

        # A Primary kit must not contain a High-School-only garment, or the
        # point of sale would happily sell a PS student an HS blazer.
        # Garments marked BOTH belong on either kit — the same rule that
        # decides which price list a garment appears on.
        if self.sku_id and self.kit_id:
            garment = self.sku.garment
            if not garment.appears_on_price_list(self.kit.school_level):
                errors["sku"] = (
                    f"{self.sku.number} is a {garment.get_school_level_display()} "
                    f"garment and does not belong in a "
                    f"{self.kit.get_school_level_display()} kit."
                )

        # A kit built in one size is made of components in that size — with
        # one real exception. Socks and neckties come in a single size and go
        # into every kit regardless.
        #
        # Checked against AsOne's 2027 bill of materials: of 828 component
        # lines, 144 are a size other than the kit's, and every one of the
        # 144 is a one-size item. So the rule is exact, not a tolerance.
        #
        # Without it, a size-M kit could be built with size-XXL shorts and
        # nothing would notice until a pupil opened the bag. A kit with no
        # size of its own — the general kits that state no cohort — has
        # nothing to check against and is left alone.
        if self.sku_id and self.kit_id and self.kit.size_id:
            size = self.sku.size
            if size.pk != self.kit.size_id and not size.is_one_size:
                errors["sku"] = (
                    f"{self.sku.number} is size {size.name}, but this kit is "
                    f"built in {self.kit.size.name}. Only one-size items go "
                    "into a kit of a different size."
                )

        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.quantity} × {self.sku.number} in {self.kit.kit_number}"

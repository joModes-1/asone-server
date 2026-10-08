"""Garments and sizes.

AsOne's vocabulary, from p.2 of the pack. Three words that sound similar and
are not:

    Garment  a uniform component. "White Shirt". About 45 of them.
    Size     a garment attribute. Each garment comes in four or five.
    SKU      one garment in one size. What is counted, ordered and picked.

Only the first two live here. The SKU is F06 and lands separately.
"""

from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models.functions import Lower


class Garment(models.Model):
    """A uniform component, before a size is chosen.

    **Price hangs off this model, not off the SKU.** AsOne's rule is that
    "all White Shirts have the same price regardless of size" (p.2). Modelling
    price here makes that structurally impossible to violate, rather than a
    rule someone has to remember across ~200 SKU rows. The client's own
    pricing table is keyed by part number, so this is a deliberate departure —
    it enforces their stated rule instead of their table shape.

    See catalog/models/pricing.py.
    """

    class SchoolLevel(models.TextChoices):
        """Which price list this garment belongs on.

        AsOne asked for separate Primary and High School price lists (p.7).
        Garments common to both carry BOTH and appear on each.

        Derived from `fit_group` where one is set — see `clean()`. The two
        answer different questions and must not be allowed to disagree.
        """

        PRIMARY = "PS", "Primary School"
        HIGH = "HS", "High School"
        BOTH = "BOTH", "Both"

    class FitGroup(models.TextChoices):
        """The class band this garment is cut for.

        **Not the same question as `school_level`, despite both sounding
        like "level".** The school level says which *price list* a garment
        appears on; the fit group says which *classes wear it*. A P1–P4
        sweater and a P5–P7 sweater are both Primary and are not the same
        garment — AsOne's 2027 catalogue has "Sweater" at five fit groups.

        The bands nest inside the two price lists, which is why one can be
        derived from the other (`PRICE_LIST_BY_FIT_GROUP`):

            Primary School   Nursery
                             Lower Primary  P1–P4
                             Upper Primary  P5–P7

            High School      O Level        S1–S4
                             A Level        S5–S6

        Confirmed by AsOne on 5 October 2026. The Ugandan names are in the
        labels because that is what the schools say; the class ranges are
        kept beside them because that is what a storekeeper reads off a
        pupil's form.

        Keeping them apart is what lets that catalogue exist at all. The
        old uniqueness rule was name-plus-school-level, which allowed three
        garments called "Sweater" and AsOne needs five.

        `Kit.FitGroup` carries the same five bands and no ALL: a kit is cut
        for one band, while a garment like socks genuinely spans every one.
        The same asymmetry as `SchoolLevel` and `Kit.SchoolLevel`, for the
        same reason.

        The codes are AsOne's own, from the 2027 SKU workbook, so a SKU
        number composes from them without a translation table.
        """

        NURSERY = "NUR", "Nursery"
        P1_P4 = "P14", "Lower Primary (P1–P4)"
        P5_P7 = "P57", "Upper Primary (P5–P7)"
        S1_S4 = "S14", "O Level (S1–S4)"
        S5_S6 = "S56", "A Level (S5–S6)"
        ALL = "ALL", "All levels"

    #: Which price list each fit group draws from. The single statement of
    #: the rule; `clean()` applies it so `school_level` cannot drift.
    PRICE_LIST_BY_FIT_GROUP = {
        FitGroup.NURSERY: SchoolLevel.PRIMARY,
        FitGroup.P1_P4: SchoolLevel.PRIMARY,
        FitGroup.P5_P7: SchoolLevel.PRIMARY,
        FitGroup.S1_S4: SchoolLevel.HIGH,
        FitGroup.S5_S6: SchoolLevel.HIGH,
        FitGroup.ALL: SchoolLevel.BOTH,
    }

    name = models.CharField(max_length=120, help_text='For example "White Shirt".')

    # The readable half of every SKU code beneath this garment: BTU-10 is
    # this garment's BTU and size 10. Derived from the name on first save and
    # then frozen, because it is printed on shelf labels and pick lists — a
    # code that moved when somebody tidied a name would stop matching the
    # labels already on the shelves.
    code = models.CharField(
        max_length=8,
        # **Not globally unique**, and it used to be.
        #
        # That was right while a SKU number read `<code>-<size>`: the code
        # had to be unique or two garments' SKUs would collide. The number
        # is now `UNI-<fit>-<code>-<size>-<colour>`, and AsOne share one
        # item code across fit groups on purpose — "Sweater" is SWT at all
        # five bands, which a global unique refused outright.
        #
        # What must stay unique is the triple a SKU number is built from;
        # see `unique_sku_stem` in Meta.
        blank=True,
        help_text=(
            "Short code used in SKU numbers, for example BTU. Filled in from "
            "the name if left blank, and never changed afterwards."
        ),
    )
    school_level = models.CharField(
        max_length=4,
        choices=SchoolLevel.choices,
        default=SchoolLevel.BOTH,
        help_text="Which price list this garment appears on.",
    )

    fit_group = models.CharField(
        max_length=3,
        choices=FitGroup.choices,
        blank=True,
        help_text=(
            "The class band this garment is cut for, e.g. P1\u2013P4. Sets the "
            "price list, and forms the second part of every SKU number "
            "beneath it."
        ),
    )

    #: What this garment is, in AsOne's own words — "School Uniform checked
    #: shirt for Nursery".
    #:
    #: On their Phase 1 list as "Products: garments, descriptions, active or
    #: inactive", and in their 2027 workbook as a column repeated against
    #: every SKU row. It belongs here rather than on the SKU: their text
    #: never varies by size, and all 53 of theirs describe the garment.
    #:
    #: `Sku.description` stays generated ("Checked Skirt Checked size XS").
    #: Copying this onto every SKU would give all six sizes of a garment the
    #: same text, and pick lists print "in Description sequence" (p.2) — so
    #: the sizes within a garment would come out in no particular order.
    description = models.CharField(
        max_length=200,
        blank=True,
        help_text='What this garment is, e.g. "School Uniform checked shirt for Nursery".',
    )

    colour = models.CharField(max_length=40, blank=True)

    # The colour's short code: Checked -> CHK, Grey -> GRY. Third part of a
    # SKU number.
    #
    # Stored rather than shortened from `colour` on the fly, because the
    # short forms are not a rule. Green and Grey would both abbreviate to
    # GRE, so AsOne picked GRN and GRY; Navy is NVY and White is WHT, which
    # no first-three-letters rule produces either. They are chosen, and a
    # chosen value has to be recorded rather than guessed at.
    #
    # Same reasoning as `colour_hex` below, which is stored for the same
    # reason: deriving it means only the colours the app was taught work.
    colour_code = models.CharField(
        max_length=4,
        blank=True,
        help_text='Short code used in SKU numbers, for example CHK for Checked.',
    )

    # The swatch shown beside the colour name.
    #
    # Stored rather than derived, because deriving it means the app can only
    # draw colours it has been taught: a garment in a shade nobody
    # anticipated — purple, mustard — had no dot, or worse, a dot that
    # claimed to be a colour it was not. AsOne picks the shade once here and
    # every screen draws the same one.
    #
    # Blank means "no swatch chosen", which screens draw as an explicit
    # neutral rather than as white — white is a real uniform colour.
    colour_hex = models.CharField(
        max_length=7,
        blank=True,
        help_text='The swatch for this colour, as "#RRGGBB". Optional.',
        validators=[
            RegexValidator(
                r"^#[0-9a-fA-F]{6}$",
                message='A colour must look like "#1e3a8a".',
            )
        ],
    )

    # AsOne's "Active Y/N". Garments are deactivated, never deleted — the
    # stock ledger and past invoices point at them, and PROTECT would refuse
    # the delete anyway.
    is_active = models.BooleanField(
        default=True,
        help_text="Inactive garments stay in reports but cannot be ordered.",
    )

    class Meta:
        ordering = ["name", "school_level"]
        constraints = [
            # "White Shirt" for Primary and "White Shirt" for High School are
            # two garments — they can carry different prices. The same name at
            # the same level is a duplicate.
            # Case-insensitive on the name: "White Shirt" and "white shirt"
            # at the same level are one garment, and letting both exist would
            # split its price and its SKUs in two.
            models.UniqueConstraint(
                Lower("name"),
                "school_level",
                "fit_group",
                name="unique_garment_per_level_and_fit",
            ),
            # Two garments sharing fit group, item code and colour code
            # would generate identical SKU numbers at every shared size.
            # `Sku.number` is unique, so it would surface as an opaque
            # database error partway through an import; refused here, the
            # clash is named while somebody can still fix the spreadsheet.
            #
            # For garments carrying no fit group or colour code this is a
            # uniqueness rule on the code alone — exactly what the field's
            # own `unique=True` used to say.
            models.UniqueConstraint(
                "fit_group",
                Lower("code"),
                Lower("colour_code"),
                name="unique_sku_stem",
            ),
            # Both halves of the stem, or neither.
            #
            # `clean()` says this too, and that is not enough on its own:
            # `objects.create()` skips it, which is exactly how the price
            # list derivation went wrong before. A half-filled pair makes
            # `sku_code()` emit the short form from a row the unique index
            # considers distinct, so two garments generate one SKU number.
            models.CheckConstraint(
                condition=(
                    models.Q(fit_group="", colour_code="")
                    | (~models.Q(fit_group="") & ~models.Q(colour_code=""))
                ),
                name="fit_group_and_colour_code_together",
            ),
        ]

    def clean(self):
        """Keep the price list in step with the fit group, and the SKU stem whole.

        The two are different questions — which classes wear this, and which
        price list it appears on — but the second follows from the first.
        Nursery through P7 is Primary, S1 upwards is High, and a garment
        that spans every band appears on both lists.

        Model-level rather than serializer-level so the admin enforces it
        too, and so an import cannot write a pair that disagrees.
        """
        super().clean()

        # Both or neither.
        #
        # `sku_code()` falls back to the two-part `<code>-<size>` unless it
        # has both, while `unique_sku_stem` treats all three as the key. A
        # garment with a fit group but no colour code therefore sat in its
        # own slot in the constraint and still generated the short number —
        # so two such garments sharing a code produced the *same*
        # `Sku.number` and the insert blew up on a unique index somewhere
        # else entirely. Refusing the half-filled pair is what keeps the
        # constraint and the generator talking about the same thing.
        if bool(self.fit_group) != bool(self.colour_code):
            missing = "colour_code" if self.fit_group else "fit_group"
            raise ValidationError(
                {
                    missing: (
                        "A garment needs both a fit group and a colour code, "
                        "or neither. They are two parts of the same SKU "
                        "number, and one without the other produces the "
                        "short form while looking like it will not."
                    )
                }
            )

        if not self.fit_group:
            return

        implied = self.PRICE_LIST_BY_FIT_GROUP[self.FitGroup(self.fit_group)]

        # BOTH is this field's default, so a garment carrying it has very
        # likely not had a price list chosen at all — whoever filled the
        # form named the fit group and left this alone. Deriving it is the
        # right answer there, and refusing would be an error message about
        # a value nobody typed.
        #
        # A *stated* PS or HS that disagrees is a different matter, and is
        # refused: being told the two conflict is worth more than having
        # one changed underneath whoever chose it. Same split as Kit.clean(),
        # which can be stricter only because its own field has no default.
        stated = self.school_level and self.school_level != self.SchoolLevel.BOTH
        if stated and self.school_level != implied:
            raise ValidationError(
                {
                    "school_level": (
                        f"{self.get_fit_group_display()} garments are "
                        f"{self.SchoolLevel(implied).label}, not "
                        f"{self.get_school_level_display()}."
                    )
                }
            )
        self.school_level = implied

    def save(self, *args, **kwargs):
        # The price list follows the fit group **however the row is made**,
        # not only when something calls full_clean().
        #
        # Deriving it in clean() alone was a bug: `objects.create()` skips
        # clean(), so those rows kept the field default of BOTH while
        # validated rows got PS or HS. Two garments with the same name and
        # fit group then differed by a value nobody had chosen, and slid
        # straight past `unique_garment_per_level_and_fit`. The duplicate
        # the rule exists to stop was created without a murmur.
        #
        # clean() still refuses a *stated* contradiction, which is a
        # different job: telling somebody their two answers disagree.
        if self.fit_group:
            self.school_level = self.PRICE_LIST_BY_FIT_GROUP[
                self.FitGroup(self.fit_group)
            ]

        # Both halves of the stem are stored upper-cased.
        #
        # `sku_code()` used to upper-case `colour_code` on its way into the
        # number but not `code`, while the constraint lower-cased `code` and
        # not `colour_code`. So "GRY" and "gry" were two rows to the index
        # and one number to the printer, and a lower-case item code reached
        # a shelf label in the wrong case. Normalising here means the stored
        # value is the printed value.
        self.code = (self.code or "").upper()
        self.colour_code = (self.colour_code or "").upper()

        # Assigned here rather than in a signal, so it is visible where it
        # happens, and only when missing — see the field comment for why an
        # existing code must never move.
        if not self.code:
            from catalog.services import garment_code

            self.code = garment_code(self)

        super().save(*args, **kwargs)

    def __str__(self):
        """The name somebody picks from, and it has to tell two garments apart.

        This read `Sweater (PS)` — the price list — which stopped
        distinguishing anything the moment class bands arrived. AsOne have
        Sweater at Nursery, Lower Primary and Upper Primary, all three on the
        Primary list, so a production-order picker offered three identical
        lines and the person choosing had no way to tell which was which.
        Reported as "repeated garment/SKU records"; they were not repeats.

        The band is the finer answer and implies the price list anyway, so it
        replaces it where there is one. Garments with no band keep the old
        form, which is still the best available for them.
        """
        if self.fit_group:
            return f"{self.name} ({self.get_fit_group_display()})"
        if self.school_level == self.SchoolLevel.BOTH:
            return self.name
        return f"{self.name} ({self.school_level})"

    def appears_on_price_list(self, level) -> bool:
        """Whether this garment belongs on the PS or HS price list."""
        return self.school_level in (level, self.SchoolLevel.BOTH)

    def fits_class_band(self, band) -> bool:
        """Whether this garment is cut for `band` — a `Kit.FitGroup` value.

        `ALL` fits every band: socks and neckties are the same garment
        whichever class wears them, which is exactly why `Garment.FitGroup`
        carries an ALL and `Kit.FitGroup` does not.

        A garment with no band stated fits any kit. Those predate the field
        and refusing them would make old kits unmaintainable.
        """
        return not self.fit_group or self.fit_group in (band, self.FitGroup.ALL)


class Size(models.Model):
    """A garment attribute, shared across garments so "M" means one thing.

    `sort_order` exists because sizes do not sort usefully as text: XS, S, M,
    L, XL and XXL have no alphabetical order at all — sorted as text, "L"
    lands between "XS" and "M". Pick lists and price lists read better in
    size order.
    """

    name = models.CharField(max_length=20, help_text='For example "M" or "XL".')
    sort_order = models.PositiveSmallIntegerField(
        default=0, help_text="Smallest first. Ties fall back to name."
    )

    # Socks and neckties come in one size and go into every kit regardless of
    # the size the kit is built in.
    #
    # A flag rather than a check for the name "OS", because matching on a
    # name means the rule only works for sizes somebody remembered to spell
    # that way — "One Size", "OS", "O/S" and "Free Size" are the same fact
    # and would not all be caught. AsOne marks it once here and
    # `KitItem.clean()` reads the flag.
    is_one_size = models.BooleanField(
        default=False,
        help_text=(
            "This size fits any kit, whatever size the kit is built in \u2014 "
            "socks and neckties."
        ),
    )

    class Meta:
        ordering = ["sort_order", "name"]
        constraints = [
            models.UniqueConstraint(Lower("name"), name="unique_size_name"),
        ]

    def __str__(self):
        return self.name


#: Distinct from SCHOOL_LEVEL_CHOICES: a garment may be marked BOTH, because
#: it appears on either price list. A school and a kit cannot be "both".
GARMENT_SCHOOL_LEVEL_CHOICES = Garment.SchoolLevel.choices

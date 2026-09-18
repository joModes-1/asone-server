"""Garments and sizes.

AsOne's vocabulary, from p.2 of the pack. Three words that sound similar and
are not:

    Garment  a uniform component. "White Shirt". About 45 of them.
    Size     a garment attribute. Each garment comes in four or five.
    SKU      one garment in one size. What is counted, ordered and picked.

Only the first two live here. The SKU is F06 and lands separately.
"""

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
        """

        PRIMARY = "PS", "Primary School"
        HIGH = "HS", "High School"
        BOTH = "BOTH", "Both"

    name = models.CharField(max_length=120, help_text='For example "White Shirt".')

    # The readable half of every SKU code beneath this garment: BTU-10 is
    # this garment's BTU and size 10. Derived from the name on first save and
    # then frozen, because it is printed on shelf labels and pick lists — a
    # code that moved when somebody tidied a name would stop matching the
    # labels already on the shelves.
    code = models.CharField(
        max_length=8,
        unique=True,
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
    colour = models.CharField(max_length=40, blank=True)

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
                Lower("name"), "school_level", name="unique_garment_per_level"
            )
        ]

    def save(self, *args, **kwargs):
        # Assigned here rather than in a signal, so it is visible where it
        # happens, and only when missing — see the field comment for why an
        # existing code must never move.
        if not self.code:
            from catalog.services import garment_code

            self.code = garment_code(self)

        super().save(*args, **kwargs)

    def __str__(self):
        if self.school_level == self.SchoolLevel.BOTH:
            return self.name
        return f"{self.name} ({self.school_level})"

    def appears_on_price_list(self, level) -> bool:
        """Whether this garment belongs on the PS or HS price list."""
        return self.school_level in (level, self.SchoolLevel.BOTH)


class Size(models.Model):
    """A garment attribute, shared across garments so "10" means one thing.

    `sort_order` exists because sizes do not sort usefully as text: "10" sorts
    before "8", and S/M/L has no alphabetical order at all. Pick lists and
    price lists read better in size order.
    """

    name = models.CharField(max_length=20, help_text='For example "10" or "S".')
    sort_order = models.PositiveSmallIntegerField(
        default=0, help_text="Smallest first. Ties fall back to name."
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

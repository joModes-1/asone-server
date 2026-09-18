"""Where AsOne makes, holds and sells uniforms."""

from django.db import models
from django.db.models.functions import Lower


class TailoringCenter(models.Model):
    """Where uniforms are made. Idudi, Serere, Rwanyabihuka.

    Not system users — AsOne was explicit about this. Their packing lists are
    handwritten and the receiving warehouse keys them in. They exist as rows
    so production orders and receipts have something to point at.
    """

    name = models.CharField(max_length=120)
    address = models.TextField(blank=True)

    # Sites are deactivated, never deleted — the same rule accounts follow.
    # Every transaction that happened here points at this row, so PROTECT
    # refuses the delete; a closed site has to be able to say it is closed
    # while its history stays readable.
    is_active = models.BooleanField(
        default=True,
        help_text="A closed site stays in reports but takes no new work.",
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            # Case-insensitive. A plain `unique=True` is case-SENSITIVE in
            # Postgres, so "Idudi" and "idudi" would be two Tailoring Centers
            # — and production orders would silently split between them.
            # This has already happened once with warehouses.
            models.UniqueConstraint(Lower("name"), name="unique_tailoring_center_name"),
        ]

    def __str__(self):
        return self.name


class Warehouse(models.Model):
    """Where finished stock is held. Namayemba and Serere.

    Namayemba also hosts AsOne's central office.
    """

    name = models.CharField(max_length=120)
    address = models.TextField(blank=True)

    # Sites are deactivated, never deleted — the same rule accounts follow.
    # Every transaction that happened here points at this row, so `PROTECT`
    # refuses the delete and a closed site has to be able to say it is closed
    # while its history stays readable.
    is_active = models.BooleanField(
        default=True,
        help_text="A closed site stays in reports but takes no new work.",
    )

    # "Warehouses have a primary TC but can order on any TC" (p.4), so this is
    # a default for production orders, not a restriction. Nullable because a
    # warehouse may be set up before its Tailoring Center exists.
    primary_tailoring_center = models.ForeignKey(
        TailoringCenter, null=True, blank=True, on_delete=models.PROTECT
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            # See TailoringCenter. "Namayemba" and "Namayemba warehouse" are
            # a different problem this cannot solve — but "Namayemba" and
            # "namayemba" it can, and stock split across two spellings of one
            # warehouse is unrecoverable without a manual merge.
            models.UniqueConstraint(Lower("name"), name="unique_warehouse_name"),
        ]

    def __str__(self):
        return self.name


class School(models.Model):
    """An AsOne school. The customer, and the ship-to address.

    Students have no accounts. The school is the customer; the student's name
    is free text on the order, and the school hands the uniform over using the
    invoice number.
    """

    class Level(models.TextChoices):
        """Primary or High School.

        Drives which price list a school sees — AsOne asked for separate PS
        and HS price lists (p.7).
        """

        PRIMARY = "PS", "Primary School"
        HIGH = "HS", "High School"

    name = models.CharField(max_length=120)
    level = models.CharField(max_length=2, choices=Level.choices)
    address = models.TextField(blank=True)

    # How many children the school has, which is what sizes an order against
    # expected demand.
    #
    # "Students", not "pupils": this field covers primary and high schools
    # alike, and pupils is primary-school wording. One word has to serve both
    # levels, and that word is students.
    #
    # Null rather than 0 for "nobody has told us": a school with no students
    # and a school nobody has counted are different things, and averaging the
    # second as zero would understate demand.
    student_count = models.PositiveIntegerField(
        null=True,
        blank=True,
        help_text="Students enrolled. Left blank until the school reports it.",
    )

    # Sites are deactivated, never deleted — the same rule accounts follow.
    # Every transaction that happened here points at this row, so PROTECT
    # refuses the delete; a closed site has to be able to say it is closed
    # while its history stays readable.
    is_active = models.BooleanField(
        default=True,
        help_text="A closed site stays in reports but takes no new work.",
    )

    # A school orders from this warehouse and no other. A backorder may still
    # be *filled* by a different warehouse shipping direct to the school; see
    # the decision log, which matters when shipments are modelled.
    #
    # PROTECT, not CASCADE: deleting a warehouse must never silently delete
    # the schools that order from it.
    primary_warehouse = models.ForeignKey(
        Warehouse, on_delete=models.PROTECT, related_name="schools"
    )

    class Meta:
        ordering = ["name"]
        constraints = [
            # "Namayemba Primary School" and "Namayemba primary school" both
            # existed in the development database until this was added.
            models.UniqueConstraint(Lower("name"), name="unique_school_name"),
        ]

    def __str__(self):
        return f"{self.name} ({self.get_level_display()})"


#: Referenced by SPECTACULAR_SETTINGS["ENUM_NAME_OVERRIDES"] so the generated
#: API client gets a readable type name. Kit.SchoolLevel has the same two
#: values and deliberately shares this name — one enum, one meaning.
SCHOOL_LEVEL_CHOICES = School.Level.choices

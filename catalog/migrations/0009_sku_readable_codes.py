"""Give every SKU a code a warehouse clerk can read.

A SKU number used to be a bare sequence value — `100015`. Unique, never
reused, and meaningless: somebody holding a garment could not check its shelf
label against a pick list without looking the number up first. Every screen
in the client showed a column of six-digit numbers that said nothing about
what they were.

They now read `GTR-14`: the garment's code, then the size. Uniqueness comes
from the pair rather than from a counter, which is the same fact
`unique_sku_per_garment_size` already states.

Three steps, in this order and for this reason:

  1. Add `Garment.code` **without** the unique index. Django gives every
     existing row the field's blank default, and a unique index would refuse
     the second row before the backfill had a chance to run.
  2. Fill in the codes, garments first because SKU numbers are built from
     them.
  3. Put the unique index on, now that the values are distinct.

Renumbering is safe here and would not be later. Nothing stores a copy of a
SKU number — every serializer reads `sku.number` live — so the new codes
appear everywhere at once with no rows left pointing at the old ones. Once
AsOne has printed shelf labels or sent out packing lists, that stops being
true, and this is the last moment renumbering costs nothing.

Reverse restores sequence numbers drawn fresh from `catalog_sku_number_seq`.
They will not be the numbers that were there before — the sequence never goes
backwards, which is the property that makes it worth having — so this is a
way back to the old *shape*, not to the old values.
"""

from django.db import migrations, models


def assign_codes(apps, schema_editor):
    from catalog.services import garment_code, sku_code

    Garment = apps.get_model("catalog", "Garment")
    Sku = apps.get_model("catalog", "Sku")

    # Oldest first, so the plain stem goes to the garment that has been on
    # the system longest and re-running on a copy of the data gives the same
    # answer.
    taken = set()
    for garment in Garment.objects.order_by("pk"):
        if not garment.code:
            garment.code = garment_code(garment, taken=taken)
            garment.save(update_fields=["code"])
        taken.add(garment.code)

    for sku in Sku.objects.select_related("garment", "size").order_by("pk"):
        sku.number = sku_code(sku.garment, sku.size)
        sku.save(update_fields=["number"])


def restore_sequence_numbers(apps, schema_editor):
    """Back to bare sequence numbers — see the module docstring."""
    from catalog.services import next_sku_number

    Sku = apps.get_model("catalog", "Sku")
    for sku in Sku.objects.order_by("pk"):
        sku.number = next_sku_number()
        sku.save(update_fields=["number"])


class Migration(migrations.Migration):

    dependencies = [
        ("catalog", "0008_kit_description"),
    ]

    operations = [
        migrations.AddField(
            model_name="garment",
            name="code",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Short code used in SKU numbers, for example BTU. Filled "
                    "in from the name if left blank, and never changed "
                    "afterwards."
                ),
                max_length=8,
            ),
        ),
        migrations.AlterField(
            model_name="sku",
            name="number",
            field=models.CharField(
                editable=False,
                help_text=(
                    "System assigned from the garment and size, for example "
                    "GTR-14."
                ),
                max_length=24,
                unique=True,
            ),
        ),
        migrations.RunPython(assign_codes, restore_sequence_numbers),
        migrations.AlterField(
            model_name="garment",
            name="code",
            field=models.CharField(
                blank=True,
                help_text=(
                    "Short code used in SKU numbers, for example BTU. Filled "
                    "in from the name if left blank, and never changed "
                    "afterwards."
                ),
                max_length=8,
                unique=True,
            ),
        ),
    ]

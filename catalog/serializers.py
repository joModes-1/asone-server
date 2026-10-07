"""Serializers for master data.

Shape only — which fields may be read, which may be written. Business logic
lives in catalog/services.py.

Money is serialised as a string, not a float. `2500.10` cannot be represented
exactly in binary floating point, and a price that arrives at the frontend as
2500.099999 becomes a wrong invoice. DRF does this by default for
DecimalField; it is spelled out here so nobody "fixes" it later.
"""

import copy
from decimal import Decimal

from config.validators import CaseInsensitiveUniqueValidator
from rest_framework import serializers

from .models import (
    Garment,
    GarmentPrice,
    Kit,
    KitItem,
    MinimumStockLevel,
    School,
    Size,
    Sku,
    TailoringCenter,
    Warehouse,
)
from .services import (
    CURRENT_PRICE_ANNOTATION,
    EmptyKit,
    PriceNotSet,
    compute_kit_price,
    kit_prices,
    price_for,
)


# ---------------------------------------------------------------------------
# Sites
# ---------------------------------------------------------------------------


class TailoringCenterSerializer(serializers.ModelSerializer):
    name = serializers.CharField(
        max_length=120,
        validators=[CaseInsensitiveUniqueValidator(queryset=TailoringCenter.objects.all())],
    )

    class Meta:
        model = TailoringCenter
        fields = ("id", "name", "address", "is_active")


class WarehouseSerializer(serializers.ModelSerializer):
    name = serializers.CharField(
        max_length=120,
        validators=[CaseInsensitiveUniqueValidator(queryset=Warehouse.objects.all())],
    )
    primary_tailoring_center_name = serializers.CharField(
        source="primary_tailoring_center.name", read_only=True, default=None
    )

    class Meta:
        model = Warehouse
        fields = (
            "id",
            "name",
            "address",
            "primary_tailoring_center",
            "primary_tailoring_center_name",
            "is_active",
        )


class SchoolSerializer(serializers.ModelSerializer):
    name = serializers.CharField(
        max_length=120,
        validators=[CaseInsensitiveUniqueValidator(queryset=School.objects.all())],
    )
    level_display = serializers.CharField(source="get_level_display", read_only=True)
    primary_warehouse_name = serializers.CharField(
        source="primary_warehouse.name", read_only=True
    )

    # How much this school currently has in flight. Annotated by the
    # viewset, not counted per row — see SchoolViewSet.get_queryset.
    active_orders_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = School
        fields = (
            "id",
            "name",
            "level",
            "level_display",
            "address",
            "student_count",
            "primary_warehouse",
            "primary_warehouse_name",
            "is_active",
            "active_orders_count",
        )


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------


class SizeSerializer(serializers.ModelSerializer):
    name = serializers.CharField(
        max_length=20,
        validators=[CaseInsensitiveUniqueValidator(queryset=Size.objects.all())],
    )

    class Meta:
        model = Size
        fields = ("id", "name", "sort_order")


class GarmentSerializer(serializers.ModelSerializer):
    school_level_display = serializers.CharField(
        source="get_school_level_display", read_only=True
    )
    fit_group_display = serializers.CharField(
        source="get_fit_group_display", read_only=True
    )
    current_price = serializers.SerializerMethodField()
    sku_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Garment
        fields = (
            "id",
            "name",
            # The real, stored code — not something a client should guess.
            # Every SKU beneath this garment is numbered `<code>-<size>`, so
            # a screen that previews a SKU number needs the genuine one.
            "code",
            "description",
            "school_level",
            "school_level_display",
            "fit_group",
            "fit_group_display",
            "colour",
            "colour_code",
            "colour_hex",
            "is_active",
            "current_price",
            "sku_count",
        )

    def get_fields(self):
        """`code` may be set when a garment is created, never afterwards.

        It used to be read-only always, derived from the name. That was
        right while the system chose its own codes and wrong the moment
        AsOne's catalogue arrived: their codes are mnemonics, not a rule —
        SWT for Sweater, TIE for Necktie, GYSH for Grey Shorts because
        Green Shorts already had GSH. Roughly a third cannot be derived
        from the name at all, so whoever adds the garment has to be able to
        say.

        Still frozen after creation. Every SKU beneath the garment is
        numbered from it and those numbers are on shelf labels and pick
        lists; a code that moved when somebody tidied a name would stop
        matching the labels already on the shelves.

        Left blank on creation it is still derived, so nothing has to type
        one — see `Garment.save()`.
        """
        fields = super().get_fields()
        if self.instance is not None:
            fields["code"].read_only = True
        return fields

    def validate(self, attrs):
        """Reject a garment that already exists at the same level and fit.

        A single-field validator cannot do this: the constraint is on
        `Lower(name)`, `school_level` **and** `fit_group` together, because
        "White Shirt" for Primary and for High School are two garments that
        may carry different prices — and a P1–P4 sweater and a P5–P7 sweater
        are two more.

        Left to the database this is an IntegrityError, which DRF reports as
        a 500.
        """
        name = attrs.get("name", getattr(self.instance, "name", None))
        level = attrs.get("school_level", getattr(self.instance, "school_level", None))
        fit = attrs.get("fit_group", getattr(self.instance, "fit_group", "")) or ""

        # The level stated may not be the level stored: `Garment.clean()`
        # derives it from the fit group. Ask the same question here, or a
        # garment whose fit group implies HS would be checked against the
        # PS it was submitted with and the clash would be missed.
        if fit:
            level = Garment.PRICE_LIST_BY_FIT_GROUP[Garment.FitGroup(fit)]

        # The SKU stem, which `unique_sku_stem` enforces. `code` used to be
        # `unique=True`, which handed DRF a validator for free; making it
        # writable and moving uniqueness onto the triple took that away, so
        # a duplicate item code came back as a 500 from the database rather
        # than a 400 naming the field.
        code = (attrs.get("code", getattr(self.instance, "code", "")) or "").upper()
        colour_code = (
            attrs.get("colour_code", getattr(self.instance, "colour_code", "")) or ""
        ).upper()

        if code:
            stem_clash = Garment.objects.filter(
                fit_group=fit, code__iexact=code, colour_code__iexact=colour_code
            )
            if self.instance is not None:
                stem_clash = stem_clash.exclude(pk=self.instance.pk)
            if stem_clash.exists():
                other = stem_clash.first()
                raise serializers.ValidationError(
                    {
                        "code": (
                            f'"{other.name}" already uses the code {code} here. '
                            "Two garments sharing a code, fit group and colour "
                            "code would generate the same SKU numbers."
                        )
                    }
                )

        # Both halves of the stem or neither — `Garment.clean()` and the
        # database both say so, and saying it here names the field.
        if bool(fit) != bool(colour_code):
            missing = "colour_code" if fit else "fit_group"
            raise serializers.ValidationError(
                {
                    missing: (
                        "A garment needs both a fit group and a colour code, "
                        "or neither."
                    )
                }
            )

        if name and level:
            clash = Garment.objects.filter(
                name__iexact=name, school_level=level, fit_group=fit
            )
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            if clash.exists():
                where = (
                    f"{dict(Garment.FitGroup.choices)[fit]} "
                    if fit
                    else f"{dict(Garment.SchoolLevel.choices)[level]} "
                )
                raise serializers.ValidationError(
                    {
                        "name": (
                            f"A {where}garment called \"{name}\" already "
                            "exists. Names are not case-sensitive."
                        )
                    }
                )
        return attrs

    def get_current_price(self, obj) -> str | None:
        """Today's price, or null where none is set.

        Null rather than 0. A garment with no price is a master-data gap, and
        showing it as free would be a lie the frontend might act on.

        Reads the annotation the viewset attaches, so a list of 45 garments
        costs one query rather than 46. Falls back to a direct lookup for an
        object that was not annotated — a freshly created one, for instance.
        """
        return _annotated_price(obj) if _is_annotated(obj) else _looked_up_price(obj)


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------


class GarmentPriceSerializer(serializers.ModelSerializer):
    garment_name = serializers.CharField(source="garment.__str__", read_only=True)

    class Meta:
        model = GarmentPrice
        fields = (
            "id",
            "garment",
            "garment_name",
            "unit_price",
            "active_date",
            "expiration_date",
        )

    def validate(self, attrs):
        """Run the model's own rules, so the database never has to refuse.

        The price table is guarded by a check constraint and an exclusion
        constraint. Left to the database, a violation arrives as an
        IntegrityError — a 500. Running `full_clean()` here turns it into a
        400 naming the field.

        The instance is **copied** rather than rebuilt. An earlier version
        constructed a fresh `GarmentPrice` and assigned its `pk`, which left
        `_state.adding` True: Django then treated a legitimate correction as a
        new row with a duplicate primary key and refused it. A copy carries
        that state, so `full_clean()` correctly excludes the row from its own
        overlap check.
        """
        instance = copy.deepcopy(self.instance) if self.instance else GarmentPrice()
        for field, value in attrs.items():
            setattr(instance, field, value)

        instance.full_clean()
        return attrs


class RepriceSerializer(serializers.Serializer):
    """Input for the reprice action — the sanctioned way to change a price."""

    unit_price = serializers.DecimalField(
        max_digits=12, decimal_places=2, min_value=Decimal("0.01")
    )
    active_from = serializers.DateField(
        help_text="First day the new price applies. The current price is closed on this date."
    )


class PriceListRowSerializer(serializers.Serializer):
    """One line of a PS or HS price list."""

    garment_id = serializers.IntegerField(source="garment.id")
    garment = serializers.CharField(source="garment.__str__")
    colour = serializers.CharField(source="garment.colour")
    unit_price = serializers.DecimalField(max_digits=12, decimal_places=2)


class KitPriceListRowSerializer(serializers.Serializer):
    """One line of the **kit** price list — F15, F51.

    `unit_price` is the sum of the kit's components at their price on the
    date, calculated rather than stored: a kit has no price of its own, and
    giving it one would let the two disagree the first time a component moved.
    """

    kit_id = serializers.IntegerField(source="kit.id")
    kit_number = serializers.CharField(source="kit.kit_number")
    name = serializers.CharField(source="kit.name")
    item_count = serializers.IntegerField(help_text="Garments in the kit, counting quantities.")
    unit_price = serializers.DecimalField(max_digits=14, decimal_places=2)


class UnpriceableKitSerializer(serializers.Serializer):
    """A kit that would be left off a price list, and why.

    The cause is usually a component, not the kit — so the row names the
    garments that are missing a price rather than only reporting the kit,
    which would send somebody to fix the wrong record.
    """

    kit_id = serializers.IntegerField(source="kit.id")
    kit_number = serializers.CharField(source="kit.kit_number")
    name = serializers.CharField(source="kit.name")
    school_level = serializers.CharField(source="kit.school_level")
    unpriced_components = serializers.ListField(child=serializers.CharField())
    has_no_items = serializers.BooleanField(
        help_text="True when the kit is empty — a different problem with the same symptom."
    )


# ---------------------------------------------------------------------------
# SKUs
# ---------------------------------------------------------------------------


class SkuSerializer(serializers.ModelSerializer):
    garment_name = serializers.CharField(source="garment.__str__", read_only=True)
    size_name = serializers.CharField(source="size.name", read_only=True)
    # Which price list this SKU appears on — PS, HS, or BOTH. A picker that
    # offers a SKU the server will refuse is the screen's mistake rather than
    # the user's: a kit is for one level, and KitItem.clean() rejects a
    # Primary kit holding a High-School-only garment.
    garment_school_level = serializers.CharField(
        source="garment.school_level", read_only=True
    )

    # Colour, read through the same way the school level is.
    #
    # Colour lives on the garment and garments are leads-only (F05), so every
    # other role — Finance, warehouse, school — saw an empty Colour column on
    # the inventory screen: the data was there and they simply were not
    # allowed to fetch the row it sat on. Reading it through here answers the
    # question without widening a permission that is correct as it stands,
    # and costs nothing: the viewset already selects the garment.
    # AsOne's own wording for the garment, read through for the same reason
    # colour is: it lives on a row most roles may not fetch.
    garment_description = serializers.CharField(
        source="garment.description", read_only=True
    )
    garment_colour = serializers.CharField(source="garment.colour", read_only=True)
    garment_colour_hex = serializers.CharField(
        source="garment.colour_hex", read_only=True
    )

    unit_price = serializers.SerializerMethodField()

    class Meta:
        model = Sku
        fields = (
            "id",
            "number",
            "garment",
            "garment_name",
            "garment_school_level",
            "garment_description",
            "garment_colour",
            "garment_colour_hex",
            "size",
            "size_name",
            "description",
            "is_active",
            "unit_price",
        )
        # The control number is assigned by the system and never changes —
        # it is printed on pick lists and packing lists.
        read_only_fields = ("id", "number")

    def get_unit_price(self, obj) -> str | None:
        """Read through to the garment's price. SKUs carry no price of their own.

        Annotated in bulk by the viewset — see GarmentSerializer.get_current_price.
        """
        return _annotated_price(obj) if _is_annotated(obj) else _looked_up_price(obj.garment)


class MinimumStockLevelSerializer(serializers.ModelSerializer):
    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)
    warehouse_name = serializers.CharField(source="warehouse.name", read_only=True)

    class Meta:
        model = MinimumStockLevel
        fields = (
            "id",
            "sku",
            "sku_number",
            "sku_description",
            "warehouse",
            "warehouse_name",
            "minimum_quantity",
        )


# ---------------------------------------------------------------------------
# Kits
# ---------------------------------------------------------------------------


class KitItemSerializer(serializers.ModelSerializer):
    """One line of a kit's bill of materials.

    Validation lives on the model so the admin enforces it too; this runs it,
    so a rule breach comes back as a 400 naming the field rather than saving
    a kit that cannot be fulfilled.
    """

    kit_number = serializers.CharField(source="kit.kit_number", read_only=True)
    sku_number = serializers.CharField(source="sku.number", read_only=True)
    sku_description = serializers.CharField(source="sku.description", read_only=True)

    class Meta:
        model = KitItem
        fields = (
            "id",
            "kit",
            "kit_number",
            "sku",
            "sku_number",
            "sku_description",
            "quantity",
        )

    def validate(self, attrs):
        """Run KitItem.clean() — retired SKUs, and level mismatches.

        The instance is copied rather than rebuilt, so an edit is not treated
        as a new row. See GarmentPriceSerializer.validate for why that
        distinction bites.
        """
        instance = copy.deepcopy(self.instance) if self.instance else KitItem()
        for field, value in attrs.items():
            setattr(instance, field, value)

        instance.full_clean(exclude=["kit"] if instance.kit_id is None else None)
        return attrs


class KitListSerializer(serializers.ListSerializer):
    """Prices every kit on the page in one query before rendering.

    Without this, `get_current_price` runs `compute_kit_price` per kit, and
    that runs a price lookup per component — 52 queries for ten kits of four
    items, measured. The totals are computed once here and handed to the
    child serializer.
    """

    def to_representation(self, data):
        kits = list(data)
        self.child._price_map = kit_prices(kits)
        return super().to_representation(kits)


class KitSerializer(serializers.ModelSerializer):
    kit_number = serializers.CharField(
        max_length=20,
        validators=[CaseInsensitiveUniqueValidator(queryset=Kit.objects.all())],
    )
    school_level_display = serializers.CharField(
        source="get_school_level_display", read_only=True
    )
    current_price = serializers.SerializerMethodField()
    item_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Kit
        fields = (
            "id",
            "kit_number",
            "name",
            "description",
            "school_level",
            "school_level_display",
            "is_active",
            "current_price",
            "item_count",
        )
        list_serializer_class = KitListSerializer

    def get_current_price(self, obj) -> str | None:
        """Today's kit price, or null where it cannot be priced.

        Null rather than a failed request: one kit with a pricing gap — an
        unpriced component, or no components at all — should not stop the
        rest of a kit list from loading, the same way an unpriced garment
        does not stop the garment list from loading (see GarmentSerializer).
        compute_kit_price() itself still raises for any caller that needs to
        know not just that the total is missing, but why.
        """
        # On a list, the totals were computed in one query by
        # KitListSerializer. On a single kit there is nothing to batch, so
        # fall back to computing it directly.
        price_map = getattr(self, "_price_map", None)
        if price_map is not None and obj.pk in price_map:
            amount = price_map[obj.pk]
            return str(amount) if amount is not None else None

        try:
            return str(compute_kit_price(obj))
        except (PriceNotSet, EmptyKit):
            return None


# ---------------------------------------------------------------------------
# Price rendering
# ---------------------------------------------------------------------------
# Both Garment and Sku expose "the price today". Shared here so the two cannot
# drift in how they represent an unpriced item.


def _is_annotated(obj) -> bool:
    return hasattr(obj, CURRENT_PRICE_ANNOTATION)


def _annotated_price(obj) -> str | None:
    amount = getattr(obj, CURRENT_PRICE_ANNOTATION)
    return str(amount) if amount is not None else None


def _looked_up_price(garment) -> str | None:
    try:
        return str(price_for(garment))
    except PriceNotSet:
        return None


class CatalogueImportSerializer(serializers.Serializer):
    """The upload behind `POST /api/catalog/import/`."""

    file = serializers.FileField(
        help_text="The catalogue as .xlsx, .xlsm or .csv."
    )
    sheet = serializers.CharField(
        required=False,
        allow_blank=True,
        help_text="Which worksheet to read. Defaults to the first one.",
    )
    dry_run = serializers.BooleanField(
        default=False,
        help_text=(
            "Check the file and report what would be imported, then roll it "
            "back. Nothing is written."
        ),
    )


class CatalogueImportResultSerializer(serializers.Serializer):
    """What an import did, or would have done.

    `unchanged` is the count of garments already present — worth showing,
    because a re-import that reports nothing created and everything
    unchanged is the system saying "this file is already loaded", which
    looks like a failure until it is named.
    """

    garments = serializers.IntegerField()
    sizes = serializers.IntegerField()
    skus = serializers.IntegerField()
    prices = serializers.IntegerField()
    unchanged = serializers.IntegerField()
    dry_run = serializers.BooleanField()
    detail = serializers.CharField()

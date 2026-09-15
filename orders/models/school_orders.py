"""A school order — F30, F31, F32, F33.

One student's uniform, ordered by their school. Four features that read as
separate items on the checklist are really one document:

    F30  ordered at kit and/or item level
    F31  the student's name, as free text
    F32  created on Hold until payment is confirmed
    F33  a kit becomes its component SKUs for the warehouse to pick

Two things AsOne was explicit about (p.7):

**Students have no accounts.** The school is the customer and the ship-to
address. The student's name is a free-text field on the order, used with the
invoice number so the school can hand the right parcel to the right child.

**A school orders at kit or item level; the warehouse always picks SKUs.**
So a kit line is exploded into its components when the order is placed — and
that explosion is stored, not recomputed. If Central Office edits the kit's
bill of materials next term, an order placed today must still mean what it
meant today. Same reasoning as dated prices: a document records what was
true when it was written.
"""

from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models


class OrderStatus(models.TextChoices):
    """Where an order is in its life — p.7 and p.8.

    Hold and Cancelled come from the point of sale; the rest are what the
    warehouse does with it.

    RELEASED means payment has been confirmed. *What* confirms it is still
    open question Q2 ("School Monitor") — but that question decides who
    calls `release_order()`, not what releasing means, so the status is
    reachable and the unknown lives in one permission class.

    SHIPPED and COMPLETED are **not** the same thing, and the gap between
    them is the point. Shipped means it left the warehouse; completed means
    the school says it arrived. Everything sitting in that gap is a parcel
    somebody should be chasing — without the distinction, a delivery that
    never turned up looks identical to one that did.
    """

    HOLD = "HOLD", "On hold — awaiting payment"
    RELEASED = "RELEASED", "Released to the warehouse"
    PICKED = "PICKED", "Picked"
    SHIPPED = "SHIPPED", "Shipped"
    COMPLETED = "COMPLETED", "Received by the school"
    CANCELLED = "CANCELLED", "Cancelled"


#: Named for the generated API client. Hold, Released, Picked, Shipped,
#: Cancelled — nothing like a procurement order's three states.
SCHOOL_ORDER_STATUS_CHOICES = OrderStatus.choices


class OrderPriority(models.TextChoices):
    """How urgently an order should be picked.

    **Not in AsOne's pack.** It appears in the picking-queue design and
    nowhere in the checklist, the definitions page or the open questions, so
    it is carried at the mildest reading: a hint the warehouse sets to order
    its own day, never anything the system acts on. Nothing schedules,
    escalates or reorders a queue by it.

    Worth confirming with AsOne before anything is built that depends on it —
    if it should drive FIFO release (F43), that is a different feature with
    different consequences.
    """

    NORMAL = "NORMAL", "Normal"
    HIGH = "HIGH", "High"
    URGENT = "URGENT", "Urgent"


class SchoolOrder(models.Model):
    """One student's uniform order, placed by their school.

    The number doubles as the invoice number. AsOne's definitions page treats
    them as one thing — "the Invoice# and Student's Name will be used by the
    school to deliver shipments to the correct students" — so there is no
    separate invoice numbering to keep in step.
    """

    number = models.CharField(
        max_length=16,
        unique=True,
        editable=False,
        help_text="System assigned. Also the invoice number. Never reused.",
    )

    school = models.ForeignKey(
        "catalog.School", on_delete=models.PROTECT, related_name="orders"
    )

    # Free text, deliberately. Students have no accounts and are not a table:
    # the school knows who this is, and needs the name printed so it can hand
    # the parcel to the right child.
    student_name = models.CharField(
        max_length=150,
        help_text="The student this uniform is for. Free text — students have no accounts.",
    )

    order_date = models.DateField(help_text="The date the school placed it.")
    status = models.CharField(
        max_length=12, choices=OrderStatus.choices, default=OrderStatus.HOLD
    )
    notes = models.TextField(blank=True)

    # See OrderPriority: a warehouse hint, not a scheduling rule.
    priority = models.CharField(
        max_length=8,
        choices=OrderPriority.choices,
        default=OrderPriority.NORMAL,
        help_text="A picking hint for the warehouse. Nothing in the system acts on it.",
    )

    created_by = models.ForeignKey(
        "accounts.User", on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    # F36. An order is cancelled, never deleted — the school has handed the
    # number to a parent, so the document has to survive and say what became
    # of it. Who cancelled it is recorded for the same reason every other
    # transaction records a user (p.9).
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    cancellation_reason = models.CharField(
        max_length=200,
        blank=True,
        help_text="Why the school cancelled. Optional, but useful when a parent asks.",
    )

    # F35. Payment confirmed, so the warehouse may act on it. What confirms
    # payment is open question Q2 ("School Monitor") — that question decides
    # *who or what* calls this, not what happens when it does, so the fields
    # are the same whichever way AsOne answers. The seam is the permission
    # class `CanConfirmPayment`, not this model.
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    payment_reference = models.CharField(
        max_length=100,
        blank=True,
        help_text=(
            "Whatever identifies the payment — a receipt number, a mobile "
            "money reference. Free text until AsOne says what School Monitor is."
        ),
    )

    # F45 — "Option to transfer an order to another warehouse with Inventory"
    # (pack p.8), restated by Jim as decision D2. Null is the ordinary case:
    # the school's own warehouse fills it. Set only by `transfer_order()`,
    # and only before picking — once stock is reserved somewhere, moving the
    # order would leave that reservation behind with nothing pointing at it.
    #
    # Deliberately NOT a change to `school.primary_warehouse`. Ordering and
    # fulfilment are two rules in D2 and collapsing them would let a transfer
    # silently re-home every future order the school places.
    fulfilled_by_warehouse = models.ForeignKey(
        "catalog.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="orders_accepted",
        help_text=(
            "Set when the order was transferred to a warehouse holding stock. "
            "Empty means the school's own warehouse fills it."
        ),
    )
    transferred_at = models.DateTimeField(null=True, blank=True)
    transferred_by = models.ForeignKey(
        "accounts.User",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    transfer_reason = models.CharField(
        max_length=200,
        blank=True,
        help_text="Why it moved. Recorded because a transfer is a person's judgement, not a rule the system applied.",
    )

    class Meta:
        ordering = ["-order_date", "-number"]
        indexes = [
            # The two questions a school asks of this table: what is
            # outstanding for us, and where is this student's order.
            models.Index(fields=["school", "status"]),
            models.Index(fields=["student_name"]),
        ]

    def __str__(self):
        return f"{self.number} for {self.student_name}"

    @property
    def warehouse(self):
        """Which warehouse fills this.

        The school's own, until somebody transfers the order to one that has
        the stock — p.8's "option to transfer an order to another warehouse
        with Inventory", and decision D2.

        The two halves of D2 stay separate here. **Ordering** is fixed:
        `school.primary_warehouse` never changes, and a school still places
        orders on one warehouse and no other. **Fulfilment** is what moves,
        and it moves by setting `fulfilled_by_warehouse`.

        Everything downstream — picking, availability, pick lists, shipping —
        reads this property rather than the school's warehouse, so a transfer
        redirects all of them at once.
        """
        return self.fulfilled_by_warehouse or self.school.primary_warehouse

    @property
    def was_transferred(self):
        """True when another warehouse took this on."""
        return self.fulfilled_by_warehouse_id is not None

    @property
    def shipments(self):
        """Every despatch carrying part of this order.

        A property rather than a related manager since F42: a shipment is
        addressed to a *school* and carries several orders, so the link runs
        through the lines. Reads like the old related name on purpose —
        `order.shipments.filter(...)` still means what it always did.

        Distinct, because an order with three SKUs on one van must not count
        that van three times.
        """
        from orders.models.shipments import Shipment

        return Shipment.objects.filter(lines__order=self).distinct()

    @property
    def is_cancelled(self) -> bool:
        return self.status == OrderStatus.CANCELLED

    @property
    def can_be_cancelled(self) -> bool:
        """Only while it is still on Hold — F36 says "an **unpaid** invoice".

        Once payment is confirmed and the order is released, cancelling
        raises questions nobody has answered: whether stock already picked
        goes back, and what happens to money already taken. That is open
        question Q5, so this refuses rather than guesses.
        """
        return self.status == OrderStatus.HOLD

    @property
    def is_released(self) -> bool:
        return self.released_at is not None

    @property
    def can_be_released(self) -> bool:
        """Only from Hold — F35.

        Releasing a cancelled order would resurrect a document the school
        withdrew; releasing one already picked or shipped would restate
        history. Both are refused rather than tolerated.
        """
        return self.status == OrderStatus.HOLD

    @property
    def total(self):
        """What the school owes. Summed from the lines, never stored.

        A stored total would go stale the moment a line changed, and the
        first anyone would know is an invoice that does not add up.
        """
        return sum((line.line_total for line in self.lines.all()), start=0)

    def clean(self):
        super().clean()
        if self.student_name is not None and not self.student_name.strip():
            raise ValidationError(
                {
                    "student_name": (
                        "A student's name is required — it is how the school "
                        "hands the uniform to the right child."
                    )
                }
            )

    def save(self, *args, **kwargs):
        if not self.number:
            from orders.services import next_order_number

            self.number = next_order_number()

        # Stored trimmed: " Miriam " and "Miriam" are the same child, and a
        # stray space is enough to lose an order in a search.
        if self.student_name:
            self.student_name = self.student_name.strip()

        super().save(*args, **kwargs)


class SchoolOrderLine(models.Model):
    """One SKU on an order, and what it cost when the order was placed.

    Always a SKU, even when the school ordered a kit. The warehouse picks
    individual garments off a shelf; it never picks "a kit". `from_kit`
    records which kit a line came from so the invoice can still show the
    school what it asked for.
    """

    order = models.ForeignKey(
        SchoolOrder, on_delete=models.CASCADE, related_name="lines"
    )
    sku = models.ForeignKey("catalog.Sku", on_delete=models.PROTECT, related_name="+")
    quantity = models.PositiveIntegerField(validators=[MinValueValidator(1)])

    # Copied from the price list when the order was placed, exactly as a
    # production order line copies it. An invoice reprinted next term has to
    # show what the school was actually charged.
    unit_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        help_text="The price on the day the order was placed. Never recalculated.",
    )

    # Set when the line came from exploding a kit. Null for a line the school
    # ordered as an individual item. Kept so the invoice can group a kit back
    # together, and so nobody has to guess later why six lines appeared at once.
    from_kit = models.ForeignKey(
        "catalog.Kit",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="order_lines",
        help_text="The kit this line was exploded from, if any.",
    )

    class Meta:
        ordering = ["sku__description"]
        constraints = [
            # One line per SKU per source. The same shirt can legitimately
            # appear twice on one order — once inside a kit and once ordered
            # separately — but not twice from the same place.
            models.UniqueConstraint(
                fields=["order", "sku", "from_kit"],
                name="unique_sku_per_order_source",
            ),
        ]

    def __str__(self):
        return f"{self.quantity} x {self.sku.number}"

    @property
    def line_total(self):
        return self.unit_price * self.quantity

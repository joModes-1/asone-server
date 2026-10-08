"""The Group Order — F16.

AsOne's definition (p.2):

    "The consolidated uniform requirements total across all 3 TCs. Used to
    initially fund the TCs."

One group order covers every Tailoring Center, and the production orders
placed on individual TCs should sum back up to it.

Q14 — SETTLED, 8 October 2026. The pack appeared to contradict itself: the
definition above says consolidated across all three TCs, while the header
layout on p.4 gives a Group Order a single `TC` and `Ship to Warehouse`.

AsOne have confirmed the definition. A group order is the order the
warehouses place across **all** the Tailoring Centers, entered once, and the
production orders — one per TC — sum up to it. So there is no TC and no
warehouse here, and the p.4 header belongs to the production order beside it
on that page. Those two fields are what make a production order a production
order; putting them here would have made the two documents the same thing
and made "production orders sum up to the group order" meaningless.

`reconcile()` in procurement/services.py is the other half of this: it
totals every production order under the group order whatever TC it went to,
pinned by `test_orders_on_different_tcs_sum_to_the_one_group_order`.

OPEN QUESTION Q11 — whether the Group Order stays relevant beyond the initial
capital phase is still with the client. Nothing here assumes it does:
production orders can exist without one.
"""

from django.db import models

from .base import OrderDocument, OrderLine


class GroupOrder(OrderDocument):
    """The consolidated requirement across all Tailoring Centers."""

    class Meta(OrderDocument.Meta):
        abstract = False
        verbose_name = "group order"

    def save(self, *args, **kwargs):
        # Assigned here rather than in a signal, so it is visible where it
        # happens, and only when missing — the number is printed on documents
        # and must never change.
        if not self.number:
            from procurement.services import next_group_order_number

            self.number = next_group_order_number()
        super().save(*args, **kwargs)


class GroupOrderLine(OrderLine):
    """One SKU on a group order."""

    # CASCADE, not PROTECT — one of only two deliberate exceptions in this
    # codebase. A line has no meaning apart from its order, and removing an
    # order should not require emptying it by hand first.
    order = models.ForeignKey(
        GroupOrder, on_delete=models.CASCADE, related_name="lines"
    )

    class Meta(OrderLine.Meta):
        abstract = False
        constraints = [
            # Two rows for the same SKU would make the order total a question
            # rather than a fact, and would break reconciliation against the
            # production orders.
            models.UniqueConstraint(
                fields=["order", "sku"], name="unique_sku_per_group_order"
            )
        ]

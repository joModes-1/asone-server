"""Seed backorders worth looking at — F44.

A backorder is what is left when a warehouse picks an order short, so this
places orders bigger than the shelf and picks what is there. Built through
the service layer so the rows are the real thing: one per SKU, priced, and
with the ledger written exactly as a short pick writes it.

Deliberately makes both kinds, because the screen treats them differently:

    fillable      another warehouse holds the stock, so it can be released
    stuck         nobody holds it, so it needs a production order instead
"""

from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from catalog.models import School, Sku, Warehouse
from inventory.models import MovementType
from inventory.services import post_movement, stock_level
from orders.models import Backorder
from orders.models.school_orders import OrderPriority
from orders import services


class Command(BaseCommand):
    help = "Create backorders by picking orders short."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=6)

    def handle(self, *args, **options):
        wanted = options["count"]

        clerk = User.objects.filter(role=User.Role.SCHOOL_STAFF).first()
        keeper = User.objects.filter(role=User.Role.WAREHOUSE_STAFF).first()
        finance = User.objects.filter(role=User.Role.FINANCE).first()
        if not (clerk and keeper and finance):
            self.stderr.write("Need a school clerk, a warehouse user and Finance.")
            return

        schools = list(School.objects.select_related("primary_warehouse"))
        skus = list(Sku.objects.filter(is_active=True).select_related("garment"))
        others = {w.id: w for w in Warehouse.objects.all()}
        if not (schools and skus):
            self.stderr.write("Need schools and SKUs.")
            return

        today = timezone.localdate()
        priorities = [OrderPriority.URGENT, OrderPriority.HIGH, OrderPriority.NORMAL]
        made = 0

        for index in range(wanted):
            school = schools[index % len(schools)]
            home = school.primary_warehouse

            # A school orders from its own level and from anything marked for
            # both — the same rule as its price list (F29). Picking any SKU
            # would be refused for half the schools.
            allowed = [
                s for s in skus if s.garment.appears_on_price_list(school.level)
            ]
            if not allowed:
                self.stderr.write(f"  no SKU on {school.name}'s price list")
                continue
            sku = allowed[index % len(allowed)]

            on_hand = stock_level(sku, home)
            # Order more than the shelf holds, so the pick is short.
            ordered = on_hand + 15

            order = services.place_order(
                school=school,
                student_name=f"Backorder Child {index + 1}",
                order_date=today - timedelta(days=index),
                skus=[{"sku": sku, "quantity": ordered}],
                created_by=clerk,
            )
            order.priority = priorities[index % len(priorities)]
            order.save(update_fields=["priority"])

            services.release_order(order, released_by=finance)

            try:
                services.pick_available(order, picked_by=keeper)
            except Exception as exc:  # noqa: BLE001 — a seed reports and moves on
                self.stderr.write(f"  skipped {order.number}: {exc}")
                continue

            # Half of them get stock somewhere else, so they are releasable.
            # The rest stay stuck, which is what the "View TC" path is for.
            if index % 2 == 0:
                elsewhere = next(
                    (w for w in others.values() if w.id != home.id), None
                )
                if elsewhere:
                    post_movement(
                        warehouse=elsewhere,
                        sku=sku,
                        quantity=ordered,
                        movement_type=MovementType.RECEIPT,
                        unit_value=Decimal("25000.00"),
                        document_number="RC-SEED",
                        occurred_on=today,
                        created_by=keeper,
                    )

            made += 1
            self.stdout.write(
                f"  {order.number}  {school.name}  {sku.number}  "
                f"short by {ordered - on_hand}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"{made} orders picked short. Backorders open: "
                f"{Backorder.objects.filter(status='OPEN').count()}"
            )
        )

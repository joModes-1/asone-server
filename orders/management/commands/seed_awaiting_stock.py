"""Seed held orders — F43, F44, the queue AsOne's p.8 describes.

An order is *awaiting stock* when it has been paid for and the warehouse
responsible cannot fill **every** line. Nothing part-ships, so one short line
holds the whole order. There is no record to create: this places real orders
bigger than the shelf and releases them, and they appear in the queue because
of what they are, not because a row was written saying so.

Not the same as `seed_backorders`, which builds the per-SKU Backorder rows
that belong to the part-shipping reading of decision D2 — AsOne have not
confirmed that, and those tables stay empty until they do.

Deliberately makes both kinds, because the transfer dialogue treats them
differently:

    transferable  another warehouse holds enough of every line
    stuck         nobody does, so it needs a production order instead
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from accounts.models import User
from catalog.models import School, Sku
from inventory.services import stock_level
from orders import services
from orders.models.school_orders import OrderPriority, OrderStatus, SchoolOrder


class Command(BaseCommand):
    help = "Place and release orders their warehouse cannot fill."

    def add_arguments(self, parser):
        parser.add_argument("--count", type=int, default=4)

    def handle(self, *args, **options):
        wanted = options["count"]

        already = sum(
            1
            for order in SchoolOrder.objects.filter(status=OrderStatus.RELEASED)
            if any(row["shortfall"] > 0 for row in services.check_availability(order))
        )
        if already >= wanted:
            self.stdout.write(f"{already} orders already waiting — nothing to do.")
            return

        clerk = User.objects.filter(role=User.Role.SCHOOL_STAFF).first()
        finance = User.objects.filter(role=User.Role.FINANCE).first()
        if not (clerk and finance):
            self.stderr.write("Need a school clerk and a Finance user.")
            return

        schools = list(School.objects.select_related("primary_warehouse"))
        skus = list(Sku.objects.filter(is_active=True).select_related("garment"))
        if not (schools and skus):
            self.stderr.write("Need schools and SKUs.")
            return

        today = timezone.localdate()
        priorities = [OrderPriority.URGENT, OrderPriority.HIGH, OrderPriority.NORMAL]
        made = 0

        for index in range(already, wanted):
            school = schools[index % len(schools)]
            home = school.primary_warehouse

            # A school orders from its own level and from anything marked for
            # both — the same rule as its price list (F29). Picking any SKU
            # would be refused for half the schools.
            allowed = [s for s in skus if s.garment.appears_on_price_list(school.level)]
            if not allowed:
                self.stderr.write(f"  no SKU on {school.name}'s price list")
                continue

            sku = allowed[index % len(allowed)]
            on_hand = stock_level(sku, home)

            # Every other one is short by a little and every other by a lot.
            # A small shortfall is usually transferable — some other site has
            # a few spare — and a large one usually is not, which is what puts
            # both shapes in front of whoever is reviewing the screen.
            ordered = on_hand + (3 if index % 2 == 0 else 400)

            order = services.place_order(
                school=school,
                student_name=f"Waiting Child {index + 1}",
                order_date=today - timedelta(days=index + 1),
                skus=[{"sku": sku, "quantity": ordered}],
                created_by=clerk,
            )
            order.priority = priorities[index % len(priorities)]
            order.save(update_fields=["priority"])

            # Releasing *is* the payment confirmation — the order is held on
            # HOLD until somebody says the money arrived, and nothing tries to
            # fill it before then. An unreleased order is not waiting for
            # stock; it is waiting for a parent.
            services.release_order(
                order, released_by=finance, payment_reference=f"MM-{order.number}"
            )
            made += 1
            self.stdout.write(
                f"  {order.number}: {school.name} ordered {ordered} of "
                f"{sku.number}, {home.name} holds {on_hand}"
            )

        self.stdout.write(self.style.SUCCESS(f"Released {made} orders short of stock."))

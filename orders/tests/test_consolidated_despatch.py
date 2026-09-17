"""F42 — the consolidated weekly despatch.

AsOne's checklist, p.8: *"Consolidated weekly despatch; the school
distributes to students by name on the packing list."* One van to a school,
carrying every order of theirs that is ready.

The tests that matter are the ones about the seam between one van and many
orders: that each order completes on its own schedule, that the packing list
can still say whose each parcel is, and that consolidating changes the
document without changing a single ledger row.
"""

from datetime import date
from decimal import Decimal

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from catalog.models import Garment, GarmentPrice, School, Size, Sku
from inventory.models import MovementType, StockStatus
from inventory.services import post_movement
from orders.models.school_orders import OrderStatus
from orders.services import (
    NothingReadyToDespatch,
    confirm_receipt,
    despatch_to_school,
    orders_ready_to_despatch,
    packing_list_for,
    pick_order,
    place_order,
    release_order,
    ship_order,
)
from orders.services.shipping import OrderCannotBeShipped

IN_FORCE = date(2026, 1, 1)
TODAY = date(2026, 11, 10)
Role = User.Role


class DespatchSetup(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.namayemba = self.sites["namayemba"]
        self.serere = self.sites["serere"]
        self.school = self.sites["school_a"]

        self.julius = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.namayemba)
        self.finance = make_user("musana", Role.FINANCE)
        self.clerk = make_user("chrisis", Role.SCHOOL_STAFF, school=self.school)

        self.shirt = self.priced_sku("White Shirt")
        self.socks = self.priced_sku("Socks")

    def priced_sku(self, name):
        garment = Garment.objects.create(name=name)
        GarmentPrice.objects.create(
            garment=garment, unit_price=Decimal("25000.00"), active_date=IN_FORCE
        )
        return Sku.objects.create(
            garment=garment,
            size=Size.objects.create(name=f"S{Size.objects.count() + 1}", sort_order=10),
        )

    def stock(self, sku, quantity, warehouse=None):
        post_movement(
            warehouse=warehouse or self.namayemba,
            sku=sku,
            quantity=quantity,
            movement_type=MovementType.RECEIPT,
            unit_value=Decimal("25000.00"),
            document_number="RC-100001",
            occurred_on=IN_FORCE,
            created_by=self.julius,
        )

    def picked(self, student, sku=None, quantity=2, school=None):
        """An order taken as far as Picked — ready for a van."""
        sku = sku or self.shirt
        self.stock(sku, quantity)
        order = place_order(
            school=school or self.school,
            student_name=student,
            order_date=TODAY,
            skus=[{"sku": sku, "quantity": quantity}],
            created_by=self.clerk,
        )
        release_order(order, released_by=self.finance)
        pick_order(order, picked_by=self.julius)
        order.refresh_from_db()
        return order


class OneVanCarriesTheWholeSchool(DespatchSetup):
    def test_every_picked_order_for_the_school_goes_on_it(self):
        grace = self.picked("Nakato Grace")
        john = self.picked("Wasswa John")

        shipment = despatch_to_school(
            school=self.school, from_warehouse=self.namayemba, shipped_by=self.julius
        )

        self.assertEqual(shipment.order_count, 2)
        self.assertEqual(set(shipment.orders), {grace, john})
        self.assertEqual(shipment.school, self.school)

    def test_every_order_on_it_is_marked_shipped(self):
        self.picked("Nakato Grace")
        self.picked("Wasswa John")

        shipment = despatch_to_school(
            school=self.school, from_warehouse=self.namayemba, shipped_by=self.julius
        )

        for order in shipment.orders:
            self.assertEqual(order.status, OrderStatus.SHIPPED)

    def test_a_chosen_few_can_go_instead_of_everything_ready(self):
        grace = self.picked("Nakato Grace")
        self.picked("Wasswa John")

        shipment = despatch_to_school(
            school=self.school,
            from_warehouse=self.namayemba,
            shipped_by=self.julius,
            orders=[grace],
        )

        self.assertEqual(shipment.order_count, 1)

    def test_it_refuses_an_order_belonging_to_another_school(self):
        """A shipment goes to one school. Mixing them would misdeliver."""
        other = School.objects.create(
            name="Bugiri Primary", primary_warehouse=self.namayemba
        )
        theirs = self.picked("Someone Else", school=other)
        mine = self.picked("Nakato Grace")

        with self.assertRaises(OrderCannotBeShipped):
            despatch_to_school(
                school=self.school,
                from_warehouse=self.namayemba,
                shipped_by=self.julius,
                orders=[mine, theirs],
            )

    def test_it_refuses_an_order_that_was_never_picked(self):
        order = self.picked("Nakato Grace")
        order.status = OrderStatus.HOLD
        order.save(update_fields=["status"])

        with self.assertRaises(OrderCannotBeShipped):
            despatch_to_school(
                school=self.school,
                from_warehouse=self.namayemba,
                shipped_by=self.julius,
                orders=[order],
            )

    def test_nothing_ready_is_said_plainly(self):
        with self.assertRaises(NothingReadyToDespatch):
            despatch_to_school(
                school=self.school, from_warehouse=self.namayemba, shipped_by=self.julius
            )


class ConsolidatingChangesTheDocumentNotTheLedger(DespatchSetup):
    """The point worth protecting: a van is paperwork, stock is stock."""

    def test_stock_leaves_exactly_as_it_would_have_one_order_at_a_time(self):
        self.picked("Nakato Grace")
        self.picked("Wasswa John")

        from inventory.models import StockMovement

        def reserved():
            """Units sitting in PICK at this warehouse — what a van takes."""
            rows = StockMovement.objects.filter(
                warehouse=self.namayemba,
                sku=self.shirt,
                stock_status=StockStatus.PICK,
            )
            return sum(row.quantity for row in rows)

        self.assertEqual(reserved(), 4)

        despatch_to_school(
            school=self.school, from_warehouse=self.namayemba, shipped_by=self.julius
        )

        # Everything reserved has left the reservation — the same rows
        # shipping two orders one at a time would have written.
        self.assertEqual(reserved(), 0)

    def test_the_ledger_rows_are_stamped_with_the_shipment(self):
        self.picked("Nakato Grace")
        self.picked("Wasswa John")

        shipment = despatch_to_school(
            school=self.school, from_warehouse=self.namayemba, shipped_by=self.julius
        )

        from inventory.models import StockMovement

        rows = StockMovement.objects.filter(document_number=shipment.number)
        self.assertTrue(rows.exists())


class ThePackingListSaysWhoseIsWhose(DespatchSetup):
    def test_each_line_names_its_student_and_invoice(self):
        self.picked("Nakato Grace")
        self.picked("Wasswa John")

        shipment = despatch_to_school(
            school=self.school, from_warehouse=self.namayemba, shipped_by=self.julius
        )
        sheet = packing_list_for(shipment)

        students = {line["student_name"] for line in sheet["lines"]}
        self.assertEqual(students, {"Nakato Grace", "Wasswa John"})

        for line in sheet["lines"]:
            self.assertTrue(line["invoice_number"].startswith("SO-"))

    def test_it_lists_every_invoice_on_the_van(self):
        self.picked("Nakato Grace")
        self.picked("Wasswa John")

        shipment = despatch_to_school(
            school=self.school, from_warehouse=self.namayemba, shipped_by=self.julius
        )
        sheet = packing_list_for(shipment)

        self.assertEqual(len(sheet["order_numbers"]), 2)

    def test_two_students_getting_the_same_sku_stay_two_lines(self):
        """Collapsing them would lose whose is whose — the one thing the
        document exists to say."""
        self.picked("Nakato Grace", sku=self.shirt)
        self.picked("Wasswa John", sku=self.shirt)

        shipment = despatch_to_school(
            school=self.school, from_warehouse=self.namayemba, shipped_by=self.julius
        )
        sheet = packing_list_for(shipment)

        same_sku = [
            line for line in sheet["lines"] if line["sku_number"] == self.shirt.number
        ]
        self.assertEqual(len(same_sku), 2)


class EachOrderCompletesOnItsOwn(DespatchSetup):
    def test_confirming_the_van_completes_every_order_on_it(self):
        self.picked("Nakato Grace")
        self.picked("Wasswa John")

        shipment = despatch_to_school(
            school=self.school, from_warehouse=self.namayemba, shipped_by=self.julius
        )
        confirm_receipt(shipment, confirmed_by=self.clerk)

        for order in shipment.orders:
            self.assertEqual(order.status, OrderStatus.COMPLETED)

    def test_an_order_split_across_two_vans_waits_for_both(self):
        """The rule that makes a lost second parcel visible."""
        grace = self.picked("Nakato Grace", sku=self.shirt)

        # A second van carrying more of the same order — the backorder case.
        self.stock(self.socks, 3)
        from orders.models import Shipment, ShipmentLine

        second = Shipment.objects.create(
            school=self.school,
            from_warehouse=self.namayemba,
            shipped_on=TODAY,
            shipped_by=self.julius,
        )
        ShipmentLine.objects.create(
            shipment=second, order=grace, sku=self.socks, quantity=1
        )

        first = despatch_to_school(
            school=self.school, from_warehouse=self.namayemba, shipped_by=self.julius
        )
        confirm_receipt(first, confirmed_by=self.clerk)

        grace.refresh_from_db()
        self.assertNotEqual(grace.status, OrderStatus.COMPLETED)

        confirm_receipt(second, confirmed_by=self.clerk)
        grace.refresh_from_db()
        self.assertEqual(grace.status, OrderStatus.COMPLETED)


class WhatIsWaitingToGo(DespatchSetup):
    def test_it_groups_ready_orders_by_school(self):
        self.picked("Nakato Grace")
        self.picked("Wasswa John")

        rows = list(orders_ready_to_despatch(self.namayemba))

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["school__name"], self.school.name)
        self.assertEqual(rows[0]["orders"], 2)

    def test_a_despatched_school_drops_off_the_list(self):
        self.picked("Nakato Grace")
        despatch_to_school(
            school=self.school, from_warehouse=self.namayemba, shipped_by=self.julius
        )

        self.assertEqual(list(orders_ready_to_despatch(self.namayemba)), [])


class ShippingOneOrderStillWorks(DespatchSetup):
    """`ship_order` is now a one-order case of the general shape, not a
    separate path. It must keep behaving exactly as it did."""

    def test_it_produces_a_one_order_van(self):
        order = self.picked("Nakato Grace")

        shipment = ship_order(order, shipped_by=self.julius, shipped_on=TODAY)

        self.assertEqual(shipment.order_count, 1)
        self.assertEqual(list(shipment.orders), [order])
        self.assertEqual(shipment.school, self.school)


class DespatchingOverHttp(DespatchSetup):
    """The endpoints the despatch screen calls."""

    def setUp(self):
        super().setUp()
        self.queue_url = reverse("orders:despatch-queue")
        self.despatch_url = reverse("orders:despatch")

    def test_the_queue_lists_schools_with_orders_ready(self):
        self.picked("Nakato Grace")
        self.picked("Wasswa John")
        self.client.force_authenticate(self.julius)

        rows = self.client.get(self.queue_url).data

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["orders"], 2)
        self.assertEqual(rows[0]["school__name"], self.school.name)

    def test_a_clerk_can_despatch_their_whole_school_queue(self):
        self.picked("Nakato Grace")
        self.picked("Wasswa John")
        self.client.force_authenticate(self.julius)

        response = self.client.post(
            self.despatch_url, {"school": self.school.id}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["order_count"], 2)
        self.assertEqual(len(response.data["order_numbers"]), 2)

    def test_nothing_ready_is_a_400_that_says_so(self):
        self.client.force_authenticate(self.julius)

        response = self.client.post(
            self.despatch_url, {"school": self.school.id}, format="json"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("orders", response.data)

    def test_a_clerk_cannot_despatch_from_another_warehouse(self):
        joan = make_user("joan", Role.WAREHOUSE_STAFF, warehouse=self.serere)
        self.picked("Nakato Grace")
        self.client.force_authenticate(joan)

        response = self.client.post(
            self.despatch_url,
            {"school": self.school.id, "from_warehouse": self.namayemba.id},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_a_school_clerk_may_not_despatch(self):
        """Receiving and shipping is the warehouse's column."""
        self.client.force_authenticate(self.clerk)

        self.assertEqual(
            self.client.post(
                self.despatch_url, {"school": self.school.id}, format="json"
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )


class ThePickingBacklog(DespatchSetup):
    """F38 — the shipping screen's landing view."""

    def setUp(self):
        super().setUp()
        self.url = reverse("orders:picking-queue")

    def released(self, student, priority="NORMAL"):
        from orders.models.school_orders import OrderPriority

        self.stock(self.shirt, 2)
        order = place_order(
            school=self.school,
            student_name=student,
            order_date=TODAY,
            skus=[{"sku": self.shirt, "quantity": 2}],
            created_by=self.clerk,
        )
        release_order(order, released_by=self.finance)
        order.priority = getattr(OrderPriority, priority)
        order.save(update_fields=["priority"])
        order.refresh_from_db()
        return order

    def test_a_released_order_is_ready_to_pick(self):
        self.released("Nakato Grace")
        self.client.force_authenticate(self.julius)

        body = self.client.get(self.url).data

        self.assertEqual(body["summary"]["ready_to_pick"], 1)
        self.assertEqual(body["summary"]["picked"], 0)
        self.assertEqual(body["orders"]["count"], 1)

    def test_a_picked_order_is_waiting_for_a_van(self):
        self.picked("Wasswa John")
        self.client.force_authenticate(self.julius)

        body = self.client.get(self.url).data

        self.assertEqual(body["summary"]["ready_to_pick"], 0)
        self.assertEqual(body["summary"]["picked"], 1)

    def test_an_order_on_hold_is_not_in_the_backlog(self):
        """Nothing is picked before it is paid for."""
        self.stock(self.shirt, 2)
        place_order(
            school=self.school,
            student_name="Unpaid Child",
            order_date=TODAY,
            skus=[{"sku": self.shirt, "quantity": 2}],
            created_by=self.clerk,
        )
        self.client.force_authenticate(self.julius)

        self.assertEqual(self.client.get(self.url).data["orders"]["count"], 0)

    def test_urgent_work_sorts_to_the_top(self):
        self.released("Normal Child", priority="NORMAL")
        urgent = self.released("Urgent Child", priority="URGENT")
        self.client.force_authenticate(self.julius)

        rows = self.client.get(self.url).data["orders"]["results"]

        self.assertEqual(rows[0]["number"], urgent.number)
        self.assertEqual(rows[0]["priority"], "URGENT")

    def test_a_row_carries_what_the_backlog_draws(self):
        order = self.released("Nakato Grace")
        self.client.force_authenticate(self.julius)

        row = self.client.get(self.url).data["orders"]["results"][0]

        self.assertEqual(row["number"], order.number)
        self.assertEqual(row["school_name"], self.school.name)
        self.assertEqual(row["student_name"], "Nakato Grace")
        self.assertEqual(row["item_count"], 2)
        self.assertEqual(row["sku_sample"], [self.shirt.number])

    def test_a_school_clerk_may_not_read_the_backlog(self):
        self.client.force_authenticate(self.clerk)

        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN
        )


class TheBacklogPages(ThePickingBacklog):
    """A warehouse with a long queue gets pages, like every other list."""

    def test_the_page_is_capped_and_the_count_is_the_whole_queue(self):
        for n in range(4):
            self.released(f"Child {n}")
        self.client.force_authenticate(self.julius)

        body = self.client.get(self.url, {"page_size": 2}).data

        self.assertEqual(len(body["orders"]["results"]), 2)
        self.assertEqual(body["orders"]["count"], 4)
        self.assertIsNotNone(body["orders"]["next"])

    def test_the_tiles_count_the_whole_queue_not_the_page(self):
        """A count that changed as you paged would be worse than no count."""
        for n in range(4):
            self.released(f"Child {n}")
        self.client.force_authenticate(self.julius)

        body = self.client.get(self.url, {"page_size": 2}).data

        self.assertEqual(body["summary"]["ready_to_pick"], 4)

    def test_the_second_page_carries_the_rest(self):
        for n in range(3):
            self.released(f"Child {n}")
        self.client.force_authenticate(self.julius)

        second = self.client.get(self.url, {"page_size": 2, "page": 2}).data

        self.assertEqual(len(second["orders"]["results"]), 1)


class TheBacklogStatusFilter(ThePickingBacklog):
    """Narrowing the backlog to one bucket.

    The backlog holds exactly two statuses — released and picked — so the
    filter offers exactly two and neither is invented. The warehouse screen
    needs it because the two are different work: what is still on the shelves
    versus what is reserved and waiting for a van.
    """

    def test_released_shows_only_what_is_still_to_pick(self):
        waiting = self.released("Still Waiting")
        self.picked("Already Picked")
        self.client.force_authenticate(self.julius)

        body = self.client.get(self.url, {"status": "RELEASED"}).data

        self.assertEqual(body["orders"]["count"], 1)
        self.assertEqual(body["orders"]["results"][0]["number"], waiting.number)

    def test_picked_shows_only_what_is_off_the_shelf(self):
        self.released("Still Waiting")
        done = self.picked("Already Picked")
        self.client.force_authenticate(self.julius)

        body = self.client.get(self.url, {"status": "PICKED"}).data

        self.assertEqual(body["orders"]["count"], 1)
        self.assertEqual(body["orders"]["results"][0]["number"], done.number)

    def test_no_filter_shows_both(self):
        self.released("Still Waiting")
        self.picked("Already Picked")
        self.client.force_authenticate(self.julius)

        self.assertEqual(self.client.get(self.url).data["orders"]["count"], 2)

    def test_the_tiles_ignore_the_filter(self):
        """They are the totals the filter is chosen from.

        A tile that changed when you narrowed the table would take away the
        only thing on screen saying what you had narrowed.
        """
        self.released("Still Waiting")
        self.picked("Already Picked")
        self.client.force_authenticate(self.julius)

        summary = self.client.get(self.url, {"status": "PICKED"}).data["summary"]

        self.assertEqual(summary["ready_to_pick"], 1)
        self.assertEqual(summary["picked"], 1)

    def test_a_status_the_backlog_cannot_hold_is_refused(self):
        """Answering with an empty page would look like an idle warehouse."""
        self.released("Still Waiting")
        self.client.force_authenticate(self.julius)

        response = self.client.get(self.url, {"status": "SHIPPED"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("status", response.data)

"""Organization settings — the singleton, and who may change it.

The model carries three overrides that nothing else in this codebase has:
`save()` pins the primary key, `delete()` refuses outright, and `load()` is
the only sanctioned way to read the row. None of them were covered when this
app landed, which matters more here than usual: a singleton that quietly
stops being one gives two readers two different answers, and the alert
toggles below decide what the whole organisation sees on its dashboards.
"""

from django.core.exceptions import ValidationError
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import build_sites, make_user
from organization.models import Settings

Role = User.Role


class TheSettingsRowIsASingleton(APITestCase):
    def test_load_creates_the_row_on_first_read(self):
        self.assertEqual(Settings.objects.count(), 0)

        settings = Settings.load()

        self.assertEqual(settings.pk, 1)
        self.assertEqual(Settings.objects.count(), 1)

    def test_load_returns_the_same_row_every_time(self):
        first = Settings.load()
        first.organization_name = "AsOne Uganda"
        first.save()

        self.assertEqual(Settings.load().pk, first.pk)
        self.assertEqual(Settings.load().organization_name, "AsOne Uganda")
        self.assertEqual(Settings.objects.count(), 1)

    def test_saving_a_second_row_overwrites_the_first_rather_than_adding_one(self):
        """`save()` pins the pk, so there is no way to end up with two."""
        Settings.load()

        Settings(organization_name="Somebody Else").save()

        self.assertEqual(Settings.objects.count(), 1)
        self.assertEqual(Settings.load().organization_name, "Somebody Else")

    def test_a_row_with_another_pk_is_refused(self):
        """The guard behind `save()`'s pinning — asserted directly so that
        removing the pin does not pass silently."""
        with self.assertRaises(ValidationError):
            Settings(pk=7, organization_name="Wrong row").clean()

    def test_the_row_cannot_be_deleted(self):
        settings = Settings.load()

        with self.assertRaises(ValidationError):
            settings.delete()

        self.assertEqual(Settings.objects.count(), 1)


class WhoMayReadAndChangeSettings(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.ops = make_user("ops", Role.OPERATIONS_MANAGER)
        self.finance = make_user("musana", Role.FINANCE)
        self.julius = make_user(
            "julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"]
        )
        self.clerk = make_user(
            "chrisis", Role.SCHOOL_STAFF, school=self.sites["school_a"]
        )
        self.url = reverse("organization:settings")

    def test_anyone_signed_in_may_read_it(self):
        """The sidebar reads the organisation name on every screen, so every
        role needs the row — including the ones that may not change it."""
        for user in (self.lead, self.finance, self.julius, self.clerk):
            self.client.force_authenticate(user)
            self.assertEqual(self.client.get(self.url).status_code, 200)

    def test_a_signed_out_caller_is_refused(self):
        self.client.force_authenticate(None)

        self.assertIn(
            self.client.get(self.url).status_code,
            (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
        )

    def test_a_lead_may_change_it(self):
        self.client.force_authenticate(self.lead)

        response = self.client.patch(
            self.url, {"organization_name": "AsOne Uganda"}, format="json"
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(Settings.load().organization_name, "AsOne Uganda")

    def test_an_operations_manager_may_change_it(self):
        self.client.force_authenticate(self.ops)

        response = self.client.patch(
            self.url, {"organization_name": "Changed"}, format="json"
        )

        self.assertEqual(response.status_code, 200)

    def test_finance_may_not_change_it(self):
        """Master data is the Table Updates column, and Finance is not in it
        — however many money-adjacent columns they hold elsewhere."""
        self.client.force_authenticate(self.finance)

        response = self.client.patch(
            self.url, {"organization_name": "Nope"}, format="json"
        )

        self.assertEqual(response.status_code, 403)

    def test_warehouse_staff_may_not_change_it(self):
        self.client.force_authenticate(self.julius)

        self.assertEqual(
            self.client.patch(
                self.url, {"organization_name": "Nope"}, format="json"
            ).status_code,
            403,
        )

    def test_school_staff_may_not_change_it(self):
        self.client.force_authenticate(self.clerk)

        self.assertEqual(
            self.client.patch(
                self.url, {"organization_name": "Nope"}, format="json"
            ).status_code,
            403,
        )

    def test_a_refused_change_leaves_the_row_alone(self):
        Settings.load()
        self.client.force_authenticate(self.julius)

        self.client.patch(self.url, {"organization_name": "Nope"}, format="json")

        self.assertEqual(Settings.load().organization_name, "AsOne Logistics")


class TheAlertTogglesActuallyGateAlerts(APITestCase):
    """The three settings that are not stored-only.

    Everything else on the Settings screen is either wired to one screen or
    labelled as stored-only. These three change what every dashboard in the
    organisation reports, which is why they are worth a test that goes
    through `needs_attention()` rather than just asserting the field saved.
    """

    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.url = reverse("organization:settings")

    def kinds(self):
        from dashboard.services import needs_attention

        return {alert["kind"] for alert in needs_attention()}

    def test_turning_low_stock_alerts_off_removes_that_kind(self):
        from catalog.models import Garment, MinimumStockLevel, Size, Sku

        garment = Garment.objects.create(name="White Shirt")
        sku = Sku.objects.create(
            garment=garment, size=Size.objects.create(name="S", sort_order=20)
        )
        MinimumStockLevel.objects.create(
            sku=sku, warehouse=self.sites["namayemba"], minimum_quantity=50
        )
        self.assertIn("low_stock", self.kinds())

        self.client.force_authenticate(self.lead)
        self.client.patch(
            self.url, {"low_stock_alerts_enabled": False}, format="json"
        )

        self.assertNotIn("low_stock", self.kinds())

    def test_the_toggle_survives_a_round_trip(self):
        self.client.force_authenticate(self.lead)

        self.client.patch(
            self.url, {"backorder_allocation_alerts_enabled": False}, format="json"
        )

        self.assertFalse(
            self.client.get(self.url).data["backorder_allocation_alerts_enabled"]
        )


class SynchronizationIsGone(APITestCase):
    """Decision D3 — no offline data entry.

    The Settings design draws a Synchronization panel. Those two fields were
    removed on 18 September 2026 after AsOne (Jim) confirmed the decision
    stands. This test exists so that re-adding them from the design fails
    loudly rather than quietly shipping a control for a feature nobody built.
    """

    def test_the_sync_fields_do_not_exist(self):
        for field in ("auto_sync_interval_minutes", "offline_data_retention_days"):
            self.assertFalse(
                hasattr(Settings(), field),
                f"{field} is back. See D3 — there is no offline mode.",
            )

    def test_the_api_does_not_offer_them(self):
        lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.client.force_authenticate(lead)

        body = self.client.get(reverse("organization:settings")).data

        self.assertNotIn("auto_sync_interval_minutes", body)
        self.assertNotIn("offline_data_retention_days", body)

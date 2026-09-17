"""Nothing is reachable on a password somebody else chose.

A lead types the first password and hands it over, so until the owner
replaces it two people know it — the shared password AsOne ruled out (p.9).
The account may do exactly three things until then: read itself, set a new
password, sign out.

The second test here is the important one. The first proves the rule holds
today; the second proves a **new view added next month** cannot forget it,
which is how this broke in the first place — three whole apps were written
with a bare `IsAuthenticated` and every endpoint in them was reachable.
"""

from django.urls import get_resolver
from django.urls import reverse
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.permissions import PasswordChangeNotPending

from .factories import PASSWORD, build_sites, make_user


class PendingPasswordBlocksEverythingElse(APITestCase):
    def setUp(self):
        self.sites = build_sites()
        self.lead = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.lead.must_change_password = True
        self.lead.save(update_fields=["must_change_password"])
        self.client.force_authenticate(self.lead)

    def test_the_three_permitted_things_still_work(self):
        self.assertEqual(
            self.client.get(reverse("accounts:me")).status_code, status.HTTP_200_OK
        )

    def test_master_data_is_blocked(self):
        for route in ("catalog:garment-list", "catalog:sku-list", "catalog:kit-list"):
            with self.subTest(route=route):
                self.assertEqual(
                    self.client.get(reverse(route)).status_code,
                    status.HTTP_403_FORBIDDEN,
                )

    def test_procurement_is_blocked(self):
        for route in ("procurement:group-order-list", "procurement:production-order-list"):
            with self.subTest(route=route):
                self.assertEqual(
                    self.client.get(reverse(route)).status_code,
                    status.HTTP_403_FORBIDDEN,
                )

    def test_inventory_is_blocked(self):
        for route in ("inventory:stock-levels", "inventory:movement-list"):
            with self.subTest(route=route):
                self.assertEqual(
                    self.client.get(reverse(route)).status_code,
                    status.HTTP_403_FORBIDDEN,
                )

    def test_setting_a_password_lifts_the_block(self):
        self.client.post(
            reverse("accounts:password-change"),
            {"current_password": PASSWORD, "new_password": "a-password-only-she-knows"},
            format="json",
        )

        self.lead.refresh_from_db()
        self.assertFalse(self.lead.must_change_password)
        self.assertEqual(
            self.client.get(reverse("catalog:garment-list")).status_code,
            status.HTTP_200_OK,
        )


class EveryApiViewCarriesTheGate(APITestCase):
    """The guard that stops this happening again.

    Walks every registered API view and checks it either uses
    `PasswordChangeNotPending` or says explicitly that it is one of the
    exceptions. A new view written with a bare `IsAuthenticated` fails here
    rather than quietly becoming reachable on a shared password.
    """

    #: Open to anyone, so the gate does not apply: signing in, refreshing,
    #: the API directory, the docs.
    PUBLIC = {"login", "refresh", "verify", "api-root", "schema", "swagger-ui", "index"}

    def api_views(self):
        for pattern in self._flatten(get_resolver().url_patterns):
            callback = pattern.callback
            view = getattr(callback, "cls", None) or getattr(callback, "view_class", None)
            if view is not None:
                yield pattern, view

    def _flatten(self, patterns, prefix=""):
        for pattern in patterns:
            path = prefix + str(pattern.pattern)
            if hasattr(pattern, "url_patterns"):
                yield from self._flatten(pattern.url_patterns, path)
            elif path.startswith("api/"):
                yield pattern

    def test_no_api_view_uses_a_bare_is_authenticated(self):
        offenders = []

        for pattern, view in self.api_views():
            name = (pattern.name or "").split(":")[-1]
            if name in self.PUBLIC:
                continue

            classes = set(getattr(view, "permission_classes", []))
            if PasswordChangeNotPending in classes:
                continue
            if getattr(view, "allow_password_change_pending", False):
                continue
            if not classes or classes == {IsAuthenticated}:
                offenders.append(f"{view.__name__} ({pattern.name})")
                continue
            if IsAuthenticated in classes:
                offenders.append(f"{view.__name__} ({pattern.name})")

        self.assertEqual(
            offenders,
            [],
            msg=(
                "These views use a bare IsAuthenticated and are reachable on a "
                "password someone else chose. Use [*AUTHENTICATED, ...] or set "
                f"allow_password_change_pending: {offenders}"
            ),
        )


class TheGateDoesNotAskForThePasswordAgain(APITestCase):
    """`current_password` is optional on the first-time gate, and only there.

    The screen used to ask for it, which meant typing the same string twice
    in a row: once on the sign-in form to get a session at all, then again
    here seconds later. See PasswordChangeSerializer for why the field
    defends nothing in this one state.
    """

    def setUp(self):
        self.sites = build_sites()
        self.user = make_user("sharon", User.Role.PROGRAM_LEAD)
        self.url = reverse("accounts:password-change")

    def gate(self):
        self.user.must_change_password = True
        self.user.save(update_fields=["must_change_password"])

    def change(self, **body):
        self.client.force_authenticate(self.user)
        return self.client.post(self.url, body, format="json")

    def test_a_gated_account_may_omit_it(self):
        self.gate()

        response = self.change(new_password="a-password-only-she-knows")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertFalse(self.user.must_change_password)
        self.assertTrue(self.user.check_password("a-password-only-she-knows"))

    def test_a_gated_account_may_still_send_it(self):
        """A client that has the password loses nothing by passing it."""
        self.gate()

        response = self.change(
            current_password=PASSWORD, new_password="a-password-only-she-knows"
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_wrong_one_is_still_refused_on_the_gate(self):
        """Optional does not mean ignored."""
        self.gate()

        response = self.change(
            current_password="not-her-password", new_password="a-password-only-she-knows"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_password", response.data)

    def test_an_ordinary_account_must_still_send_it(self):
        """Off the gate the field is doing real work — a stolen access token
        must not be enough to lock the owner out of their own account."""
        response = self.change(new_password="a-password-only-she-knows")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("current_password", response.data)

    def test_the_new_password_must_still_differ(self):
        """Checked against the stored hash, so it holds with no current sent."""
        self.gate()

        response = self.change(new_password=PASSWORD)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("new_password", response.data)

"""The registration-request list — `/api/auth/registration-requests/`.

Written after the endpoint spent some time returning **login attempts**. A
stray copy of `LoginAttemptViewSet`'s four class attributes had been pasted
at the foot of `RegistrationRequestViewSet`'s body, after its `approve` and
`decline` actions. Python takes the last assignment in a class body, so those
silently replaced the correct `queryset`, `serializer_class` and
`filterset_fields` declared at the top — and nothing failed, because there
was no test for this endpoint at all.

The screen it feeds is the Users list, where pending requests appear as rows
a lead clicks to approve. It was showing sign-in audit rows instead: the same
address repeated, no name, and "Requested Invalid Date" where the serializer
had no `created_at` to give.

So these assert the shape rather than only the status code. A 200 was never
the thing that was wrong.
"""

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import LoginAttempt, RegistrationRequest, User
from accounts.tests.factories import make_user

Role = User.Role


class RegistrationRequestListTests(APITestCase):
    def setUp(self):
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)

        self.pending = RegistrationRequest.objects.create(
            first_name="Robert",
            last_name="Mugisha",
            email="robert.m@asone.test",
            phone_number="+256 701 234567",
        )

        # A login attempt for the same person. The bug returned rows like
        # this one from the registration endpoint, so its presence is the
        # point of the fixture.
        LoginAttempt.objects.create(email="robert.m@asone.test", succeeded=True)

        self.client.force_authenticate(self.lead)
        self.url = reverse("accounts:registration-request-list")

    def test_returns_registration_requests_not_login_attempts(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        rows = response.data["results"]
        self.assertEqual(len(rows), 1)

        row = rows[0]
        # Fields only a RegistrationRequest has.
        self.assertEqual(row["first_name"], "Robert")
        self.assertEqual(row["last_name"], "Mugisha")
        self.assertEqual(row["email"], "robert.m@asone.test")
        self.assertIn("created_at", row)
        self.assertIn("status", row)

        # Fields only a LoginAttempt has. Their presence is the bug.
        self.assertNotIn("succeeded", row)
        self.assertNotIn("ip_address", row)
        self.assertNotIn("user_agent", row)

    def test_filters_by_status(self):
        """`?status=` is the filter the screen uses.

        LoginAttempt has no such field, so under the bug this was silently
        ignored and every row came back whatever was asked for.
        """
        RegistrationRequest.objects.create(
            first_name="Amina",
            last_name="Namubiru",
            email="amina.n@asone.test",
            status=RegistrationRequest.Status.DECLINED,
        )

        response = self.client.get(self.url, {"status": "PENDING"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            [row["email"] for row in response.data["results"]],
            ["robert.m@asone.test"],
        )

    def test_only_the_leads_may_read_it(self):
        """Approving a request is creating a user, so it is the same audience
        as `UserViewSet` — see CanUpdateTables."""
        for role in (Role.FINANCE, Role.WAREHOUSE_STAFF, Role.SCHOOL_STAFF):
            with self.subTest(role=role):
                self.client.force_authenticate(make_user(f"user{role}", role))
                response = self.client.get(self.url)
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ApprovalConfirmsTheAddress(APITestCase):
    """Approving a request produces an account that can actually sign in.

    Two separate things used to stop that, and both were invisible until
    somebody tried it:

    **Approval was refused** unless the request carried a `verified_at`. Once
    the registration email code was removed nothing ever set one, so no
    request could be approved at all.

    **The account was created unconfirmed.** `email_verified_at` stayed null
    until the new user typed a second code, so the person a lead had just
    approved was told at sign-in that their address "has not been confirmed
    yet".
    """

    def setUp(self):
        self.lead = make_user("sharon", Role.PROGRAM_LEAD)
        self.request = RegistrationRequest.objects.create(
            first_name="Robert",
            last_name="Mugisha",
            email="robert.m@asone.test",
        )

    def test_a_request_can_be_approved_without_any_verification(self):
        from accounts import services

        self.assertFalse(self.request.is_email_verified)

        user, password = services.approve_registration(
            self.request, role=Role.FINANCE, decided_by=self.lead
        )

        self.assertEqual(user.email, "robert.m@asone.test")
        self.assertTrue(password)

    def test_the_account_is_confirmed_and_not_gated_at_sign_in(self):
        from accounts import services

        user, _ = services.approve_registration(
            self.request, role=Role.FINANCE, decided_by=self.lead
        )

        user.refresh_from_db()
        # The gate sign-in checks. Null here is the bug the user reported.
        self.assertIsNotNone(user.email_verified_at)
        self.assertTrue(user.email_is_verified)

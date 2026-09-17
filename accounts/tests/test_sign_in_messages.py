"""What somebody is actually told when a sign-in is refused.

Three refusals that used to read the same or read wrongly:

    never added     "ask Central Office to create an account"   — correct
    deactivated     the same sentence                           — wrong, they
                                                                  have one
    wrong password  "No active account found with the given
                     credentials" — simplejwt's default, and untrue by the
                     time it is reached: the view has already established
                     the account exists and is active.

The third is the one people reported. It reads as a system fault rather than
a typo, so they retype the same password expecting a different answer.
"""

from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import PASSWORD, make_user

Role = User.Role


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class WhatTheRefusalSays(APITestCase):
    def setUp(self):
        self.julius = make_user("julius", Role.WAREHOUSE_STAFF)

    def attempt(self, email, password):
        return self.client.post(
            reverse("accounts:login"), {"email": email, "password": password}, format="json"
        )

    def test_a_wrong_password_says_so(self):
        response = self.attempt(self.julius.email, "not-the-right-password")

        said = str(response.data).lower()
        self.assertIn("password", said)
        self.assertNotIn("no active account", said)

    def test_a_wrong_password_does_not_claim_the_account_is_missing(self):
        """The account exists and is active — saying otherwise is untrue and
        sends the person to Central Office for nothing."""
        response = self.attempt(self.julius.email, "not-the-right-password")

        self.assertNotIn("create an account", str(response.data).lower())

    def test_somebody_never_added_is_told_to_ask_for_an_account(self):
        response = self.attempt("stranger@example.com", PASSWORD)

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("create an account", str(response.data).lower())

    def test_a_deactivated_account_is_told_it_was_deactivated(self):
        self.julius.is_active = False
        self.julius.save(update_fields=["is_active"])

        response = self.attempt(self.julius.email, PASSWORD)

        said = str(response.data).lower()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("deactivated", said)

    def test_a_deactivated_account_is_not_told_to_create_one(self):
        """They have worked here for a year. Sending them to ask for an
        account to be created is the wrong person and the wrong question."""
        self.julius.is_active = False
        self.julius.save(update_fields=["is_active"])

        response = self.attempt(self.julius.email, PASSWORD)

        self.assertNotIn("create an account", str(response.data).lower())

    def test_the_three_refusals_do_not_read_alike(self):
        wrong_password = str(self.attempt(self.julius.email, "wrong").data)
        never_added = str(self.attempt("stranger@example.com", PASSWORD).data)

        self.julius.is_active = False
        self.julius.save(update_fields=["is_active"])
        deactivated = str(self.attempt(self.julius.email, PASSWORD).data)

        self.assertNotEqual(wrong_password, never_added)
        self.assertNotEqual(never_added, deactivated)
        self.assertNotEqual(wrong_password, deactivated)

    def test_the_right_password_still_works(self):
        response = self.attempt(self.julius.email, PASSWORD)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("challenge", response.data)

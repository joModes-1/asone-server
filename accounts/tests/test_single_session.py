"""One account, one session.

The rule Bashir asked for on 16 September 2026, in his words:

    "A is logged in and is working. He shares logins to B. The moment B logs
    in, A is logged out. And when A logs in again, B is logged out."

It exists because a shared password is otherwise invisible: two people on one
account look exactly like one person, and every transaction in this system
records who performed it. With this, sharing is not subtle — the other person
is thrown out mid-task and says so.

What these tests can and cannot assert: a retired refresh token is refused
immediately, which is what they check. The other device's *access* token is
stateless and keeps working until it expires — at most 30 minutes. That gap
is documented on `sign_in_tokens_for` and is deliberate.
"""

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import User
from accounts.tests.factories import make_user, sign_in
from accounts.services import sign_in_tokens_for

Role = User.Role


class OneAccountOneSession(APITestCase):
    def setUp(self):
        self.julius = make_user("julius", Role.PROGRAM_LEAD)

    def refresh_works(self, tokens):
        """Can this session get a new access token? That is what 'still
        signed in' means once the current access token runs out."""
        response = self.client.post(
            reverse("accounts:refresh"), {"refresh": tokens["refresh"]}, format="json"
        )
        return response.status_code == status.HTTP_200_OK

    def test_b_signing_in_ends_as_session(self):
        a = sign_in_tokens_for(self.julius)
        self.assertTrue(self.refresh_works(a), "A should start out signed in")

        b = sign_in_tokens_for(self.julius)          # B signs in with A's password

        self.assertFalse(self.refresh_works(a), "A should have been signed out")
        self.assertTrue(self.refresh_works(b), "B should be signed in")

    def test_a_signing_back_in_ends_bs_session(self):
        a = sign_in_tokens_for(self.julius)
        b = sign_in_tokens_for(self.julius)
        self.assertFalse(self.refresh_works(a))

        a_again = sign_in_tokens_for(self.julius)    # A signs in again

        self.assertFalse(self.refresh_works(b), "B should now be signed out")
        self.assertTrue(self.refresh_works(a_again), "A should be signed in")

    def test_it_ping_pongs_indefinitely(self):
        """Two people sharing one password cannot both stay on."""
        previous = sign_in_tokens_for(self.julius)
        for _ in range(4):
            current = sign_in_tokens_for(self.julius)
            self.assertFalse(self.refresh_works(previous))
            self.assertTrue(self.refresh_works(current))
            previous = current

    def test_another_account_is_untouched(self):
        """Signing in as one person does not sign out everybody else."""
        grace = make_user("grace", Role.FINANCE)
        hers = sign_in_tokens_for(grace)

        sign_in_tokens_for(self.julius)

        self.assertTrue(self.refresh_works(hers))


class SigningInOverHttp(APITestCase):
    """The same rule through the real sign-in endpoint, not the service."""

    def setUp(self):
        # The factory sets its own password; reuse it rather than passing one.
        from accounts.tests.factories import PASSWORD

        self.password = PASSWORD
        self.julius = make_user("julius", Role.PROGRAM_LEAD)

    def sign_in(self):
        """The real two-step sign-in, through the factory every other test
        uses — password, then the code from the email."""
        response = sign_in(self.client, self.julius.email, self.password)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.data

    def test_the_second_sign_in_ends_the_first(self):
        first = self.sign_in()
        second = self.sign_in()

        refused = self.client.post(
            reverse("accounts:refresh"), {"refresh": first["refresh"]}, format="json"
        )
        accepted = self.client.post(
            reverse("accounts:refresh"), {"refresh": second["refresh"]}, format="json"
        )

        self.assertEqual(refused.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(accepted.status_code, status.HTTP_200_OK)


class TheOtherDeviceStopsAtOnce(APITestCase):
    """The half of the rule that refresh-token revocation alone cannot do.

    Retiring refresh tokens stops the other device *renewing*. It says nothing
    about the access token already in its hand, which is signed, stateless and
    good for up to thirty minutes. Bashir found this immediately: signed in as
    Sharon in two browsers, working in both.

    `session_epoch` closes it — the token carries the number, and a token
    carrying an older one is refused on the next request.
    """

    def setUp(self):
        self.sharon = make_user("sharon", Role.PROGRAM_LEAD)

    def me(self, tokens):
        """A request the way a browser makes one: access token, no refresh."""
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = self.client.get(reverse("accounts:me"))
        self.client.credentials()
        return response

    def test_the_first_browser_stops_working_at_once(self):
        browser_one = sign_in_tokens_for(self.sharon)
        self.assertEqual(self.me(browser_one).status_code, status.HTTP_200_OK)

        browser_two = sign_in_tokens_for(self.sharon)

        refused = self.me(browser_one)
        self.assertEqual(refused.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.me(browser_two).status_code, status.HTTP_200_OK)

    def test_the_refusal_says_why(self):
        browser_one = sign_in_tokens_for(self.sharon)
        sign_in_tokens_for(self.sharon)

        refused = self.me(browser_one)

        self.assertIn("somewhere else", str(refused.data).lower())

    def test_signing_back_in_reverses_it_immediately(self):
        one = sign_in_tokens_for(self.sharon)
        two = sign_in_tokens_for(self.sharon)
        self.assertEqual(self.me(one).status_code, status.HTTP_401_UNAUTHORIZED)

        three = sign_in_tokens_for(self.sharon)

        self.assertEqual(self.me(two).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.me(three).status_code, status.HTTP_200_OK)

    def test_another_account_is_untouched(self):
        grace = make_user("grace", Role.FINANCE)
        hers = sign_in_tokens_for(grace)

        sign_in_tokens_for(self.sharon)

        self.assertEqual(self.me(hers).status_code, status.HTTP_200_OK)


class SigningSomebodyElseOut(APITestCase):
    """"Sign out everywhere" on the profile you are looking at.

    Bashir, 16 September: "it should be signing out someone else whose profile
    we are in." It did retire their refresh tokens, but they carried on with
    the access token they held — so from the lead's side the button did
    nothing for half an hour.
    """

    def setUp(self):
        self.sharon = make_user("sharon", Role.PROGRAM_LEAD)
        self.julius = make_user("julius", Role.WAREHOUSE_STAFF)

    def me(self, tokens):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = self.client.get(reverse("accounts:me"))
        self.client.credentials()
        return response

    def test_they_are_signed_out_at_once(self):
        from accounts.services import force_sign_out

        his = sign_in_tokens_for(self.julius)
        self.assertEqual(self.me(his).status_code, status.HTTP_200_OK)

        retired = force_sign_out(self.julius)

        self.assertEqual(retired, 1)
        self.assertEqual(self.me(his).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_the_lead_doing_it_is_unaffected(self):
        from accounts.services import force_sign_out

        hers = sign_in_tokens_for(self.sharon)
        sign_in_tokens_for(self.julius)

        force_sign_out(self.julius)

        self.assertEqual(self.me(hers).status_code, status.HTTP_200_OK)

    def test_over_http_from_the_profile_screen(self):
        his = sign_in_tokens_for(self.julius)

        self.client.force_authenticate(self.sharon)
        response = self.client.post(
            reverse("accounts:user-sign-out", args=[self.julius.pk])
        )
        self.client.force_authenticate(None)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["sessions_retired"], 1)
        self.assertEqual(self.me(his).status_code, status.HTTP_401_UNAUTHORIZED)


class AnAdministratorResettingAPassword(APITestCase):
    """The reset is usually because the old password is compromised, so the
    old sessions must not survive it either."""

    def setUp(self):
        self.julius = make_user("julius", Role.WAREHOUSE_STAFF)

    def me(self, tokens):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        response = self.client.get(reverse("accounts:me"))
        self.client.credentials()
        return response

    def test_their_session_dies_at_once(self):
        from accounts.services import set_user_password

        his = sign_in_tokens_for(self.julius)
        self.assertEqual(self.me(his).status_code, status.HTTP_200_OK)

        set_user_password(self.julius)

        self.assertEqual(self.me(his).status_code, status.HTTP_401_UNAUTHORIZED)


class SigningYourselfOut(APITestCase):
    """Your own sign-out must be as final as one done to you.

    Blacklisting the refresh token left the access token working for up to
    thirty minutes. On a shared warehouse machine that is the whole risk:
    somebody signs out, walks away, and the next person at that keyboard
    still holds a live token.
    """

    def setUp(self):
        self.julius = make_user("julius", Role.WAREHOUSE_STAFF)

    def me(self, access):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        response = self.client.get(reverse("accounts:me"))
        self.client.credentials()
        return response.status_code

    def test_the_access_token_dies_with_the_session(self):
        tokens = sign_in_tokens_for(self.julius)
        self.assertEqual(self.me(tokens["access"]), status.HTTP_200_OK)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        out = self.client.post(
            reverse("accounts:logout"), {"refresh": tokens["refresh"]}, format="json"
        )
        self.client.credentials()

        self.assertEqual(out.status_code, status.HTTP_205_RESET_CONTENT)
        self.assertEqual(self.me(tokens["access"]), status.HTTP_401_UNAUTHORIZED)

    def test_a_bad_refresh_token_still_refuses_and_changes_nothing(self):
        tokens = sign_in_tokens_for(self.julius)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        out = self.client.post(
            reverse("accounts:logout"), {"refresh": "not-a-token"}, format="json"
        )
        self.client.credentials()

        self.assertEqual(out.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.me(tokens["access"]), status.HTTP_200_OK)

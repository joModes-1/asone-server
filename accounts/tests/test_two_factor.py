"""Two-factor sign-in, and the closed-door rule — F04-adjacent hardening.

Two things are being protected here.

**A password alone is not a sign-in.** It is enough to read every school's
orders and every warehouse's stock, so it buys a code sent to the mailbox
Central Office chose, and nothing else.

**Only people who were added can get in.** Somebody who was never added, or
who has been deactivated, is told so plainly instead of being left retyping
a password that was never going to work.

That second rule is user enumeration and is accepted on purpose. The
reasoning is in `accounts/services.py::user_with_access`; the tests below
pin the behaviour so that a later change to it has to be deliberate.
"""

import re
from datetime import timedelta

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

from accounts.models import LoginAttempt, LoginChallenge, User
from accounts.tests.factories import PASSWORD, build_sites, make_user

Role = User.Role


def code_from_email(message):
    """The six digits, read out of the message the way a person would."""
    return re.search(r"\b(\d{6})\b", message.body).group(1)


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class TwoFactorSetup(APITestCase):
    def setUp(self):
        # The throttle counts live in the cache, so without this a test
        # inherits the attempts of the one before it. Same reason
        # test_throttling.py does it.
        cache.clear()
        self.sites = build_sites()
        self.user = make_user("julius", Role.WAREHOUSE_STAFF, warehouse=self.sites["namayemba"])
        mail.outbox.clear()

    def password_step(self, email=None, password=PASSWORD):
        return self.client.post(
            reverse("accounts:login"),
            {"email": email or self.user.email, "password": password},
            format="json",
        )

    def code_step(self, challenge, code):
        return self.client.post(
            reverse("accounts:login-verify"),
            {"challenge": str(challenge), "code": code},
            format="json",
        )

    def sign_in(self):
        """The whole two-step dance, as a caller would do it."""
        started = self.password_step()
        code = code_from_email(mail.outbox[-1])
        return self.code_step(started.data["challenge"], code)


class OnlyPeopleWhoWereAddedGetIn(TwoFactorSetup):
    """The closed-door rule. 403, and it says why."""

    def test_an_address_that_is_not_a_user_is_told_so(self):
        response = self.password_step(email="stranger@example.com")

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("do not have access", str(response.data).lower())

    def test_a_deactivated_user_is_told_they_were_deactivated(self):
        """Not the same message as a stranger — reversed 16 September 2026.

        This used to return the stranger's sentence deliberately, on the
        reasoning that a former employee has no use for the difference
        between "never added" and "removed". In practice the person hitting
        it is almost never a former employee: it is somebody deactivated by
        mistake, or during a role change, who is then told to ask Central
        Office to *create* an account they already have. That is the wrong
        question to the right person, and it wasted real time.

        The cost is that an address can now be told apart as "was a user
        here" rather than "never was". That is a smaller leak than the one
        this system already accepts on purpose — see `user_with_access`,
        which answers "you do not have access" instead of "wrong password"
        precisely so somebody who was never added is not left guessing. The
        user list of a closed system with a few dozen named accounts, rate
        limiting and a recorded attempt per try is not the secret worth
        protecting here.
        """
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        response = self.password_step()

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        said = str(response.data).lower()
        self.assertIn("deactivated", said)
        self.assertNotIn("create an account", said)

    def test_no_code_is_emailed_to_somebody_with_no_access(self):
        self.password_step(email="stranger@example.com")

        self.assertEqual(len(mail.outbox), 0)

    def test_a_real_user_with_a_wrong_password_gets_401_not_403(self):
        """Different failure, different answer: the account exists."""
        response = self.password_step(password="not-the-password")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_wrong_password_emails_nothing(self):
        self.password_step(password="not-the-password")

        self.assertEqual(len(mail.outbox), 0)

    def test_both_kinds_of_failure_are_audited(self):
        self.password_step(email="stranger@example.com")
        self.password_step(password="not-the-password")

        self.assertEqual(LoginAttempt.objects.filter(succeeded=False).count(), 2)


class ThePasswordStepIssuesNoTokens(TwoFactorSetup):
    """A password is half a sign-in. This is the half that is not."""

    def test_it_returns_a_challenge(self):
        response = self.password_step()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("challenge", response.data)

    def test_it_returns_no_access_token(self):
        response = self.password_step()

        self.assertNotIn("access", response.data)
        self.assertNotIn("refresh", response.data)

    def test_a_code_is_emailed_to_the_user(self):
        self.password_step()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])

    def test_the_masked_hint_does_not_give_the_address_away(self):
        response = self.password_step()

        hint = response.data["email_hint"]
        self.assertIn("•", hint)
        self.assertNotEqual(hint, self.user.email)

    def test_the_code_is_never_stored_in_plain_text(self):
        """Anybody reading this table — a backup, a support session — must
        not be able to sign in as somebody else."""
        self.password_step()
        code = code_from_email(mail.outbox[0])

        challenge = LoginChallenge.objects.get()
        self.assertNotIn(code, challenge.code_hash)


class TheCodeStepIssuesTokens(TwoFactorSetup):
    def test_the_right_code_signs_you_in(self):
        response = self.sign_in()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.data)
        self.assertIn("refresh", response.data)

    def test_the_user_record_comes_back_with_it(self):
        response = self.sign_in()

        self.assertEqual(response.data["user"]["email"], self.user.email)

    def test_the_token_actually_works(self):
        access = self.sign_in().data["access"]

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {access}")
        self.assertEqual(
            self.client.get(reverse("accounts:me")).status_code, status.HTTP_200_OK
        )


class ACodeIsGoodOnceAndBriefly(TwoFactorSetup):
    def test_a_wrong_code_is_refused(self):
        started = self.password_step()

        response = self.code_step(started.data["challenge"], "000000")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertNotIn("access", response.data)

    def test_a_code_cannot_be_used_twice(self):
        started = self.password_step()
        code = code_from_email(mail.outbox[-1])
        self.code_step(started.data["challenge"], code)

        again = self.code_step(started.data["challenge"], code)

        self.assertEqual(again.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_expired_code_is_refused(self):
        started = self.password_step()
        code = code_from_email(mail.outbox[-1])

        challenge = LoginChallenge.objects.get(pk=started.data["challenge"])
        challenge.expires_at = timezone.now() - timedelta(seconds=1)
        challenge.save(update_fields=["expires_at"])

        self.assertEqual(
            self.code_step(challenge.pk, code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_guessing_runs_out_of_tries(self):
        started = self.password_step()
        challenge = started.data["challenge"]

        for _ in range(settings.LOGIN_CODE_MAX_ATTEMPTS):
            self.code_step(challenge, "000000")

        # Even the right code no longer works.
        code = code_from_email(mail.outbox[-1])
        self.assertEqual(
            self.code_step(challenge, code).status_code, status.HTTP_400_BAD_REQUEST
        )

    def test_an_unknown_challenge_is_refused(self):
        import uuid

        self.assertEqual(
            self.code_step(uuid.uuid4(), "123456").status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_asking_again_retires_the_previous_code(self):
        """Otherwise three requests would leave three working codes, which
        triples the guessing surface."""
        first = self.password_step()
        first_code = code_from_email(mail.outbox[-1])

        self.password_step()

        self.assertEqual(
            self.code_step(first.data["challenge"], first_code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

    def test_deactivating_someone_mid_sign_in_stops_them(self):
        """They passed the password a minute ago. They still must not finish."""
        started = self.password_step()
        code = code_from_email(mail.outbox[-1])

        self.user.is_active = False
        self.user.save(update_fields=["is_active"])

        self.assertEqual(
            self.code_step(started.data["challenge"], code).status_code,
            status.HTTP_400_BAD_REQUEST,
        )

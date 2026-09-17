"""Confirm accounts a lead already approved.

An account created by approving a registration request was left with
`email_verified_at` null, because approval then emailed a *second* code the
new user had to type. Sign-in checks that field, so the person a lead had
just approved was told:

    "Your email address has not been confirmed yet. Check your inbox for the
    confirmation code and enter it before signing in."

`accounts.services.approve_registration` now marks the address confirmed at
the moment of approval — see the comment there for why that is the honest
reading — but that only helps accounts approved from here on. This is the
same correction applied to the ones already made, who would otherwise stay
locked out with no way through: the code they were sent has a seven-day life
and nothing in the UI resends it.

Narrow on purpose. Only accounts that came from an approved registration
request are touched — `created_user` is the link. An account created some
other way and genuinely awaiting confirmation is left alone, because for
those the code is still the intended path.

Reverse is a no-op: there is no record of which of these were null before,
and guessing would lock people out a second time.
"""

from django.db import migrations
from django.utils import timezone


def confirm_approved_accounts(apps, schema_editor):
    RegistrationRequest = apps.get_model("accounts", "RegistrationRequest")
    User = apps.get_model("accounts", "User")

    approved_user_ids = (
        RegistrationRequest.objects.filter(created_user__isnull=False)
        .values_list("created_user_id", flat=True)
    )

    User.objects.filter(
        pk__in=list(approved_user_ids), email_verified_at__isnull=True
    ).update(email_verified_at=timezone.now())


def noop(apps, schema_editor):
    """Deliberately does nothing — see the module docstring."""


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0009_registrationrequest_verified_at_and_more"),
    ]

    operations = [
        migrations.RunPython(confirm_approved_accounts, noop),
    ]

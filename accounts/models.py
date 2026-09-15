import uuid

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from .managers import UserManager


class User(AbstractUser):
    """AsOne staff member. Role and site drive what they can see and do.

    Signs in with **email and password**. AbstractUser's `username` is
    removed rather than left unused: two identifiers on one account invites
    the question of which one is authoritative, and a lead filling in a
    creation form should not have to answer it.
    """

    # Removed, not repurposed. USERNAME_FIELD below takes its place.
    username = None

    # The login credential, so it must be unique and is never optional.
    email = models.EmailField(
        "email address",
        unique=True,
        help_text="Used to sign in. Must be unique across all staff.",
    )

    #: Collected on both the lead-created flow and self-registration.
    #: Contact information only — nothing signs in with this and nothing
    #: checks it for uniqueness.
    phone_number = models.CharField(max_length=32, blank=True)

    USERNAME_FIELD = "email"

    # What `createsuperuser` prompts for in addition to email and password.
    # Role is here because an account without one is refused by every
    # permission class, which is a confusing way to discover the omission.
    REQUIRED_FIELDS = ["role"]

    objects = UserManager()

    class Role(models.TextChoices):
        PROGRAM_LEAD = "PROGRAM_LEAD", "Program Lead"
        OPERATIONS_MANAGER = "OPERATIONS_MANAGER", "Operations Manager"
        WAREHOUSE_STAFF = "WAREHOUSE_STAFF", "Warehouse Staff"
        SCHOOL_STAFF = "SCHOOL_STAFF", "School Staff"
        FINANCE = "FINANCE", "Finance Department"

    role = models.CharField(max_length=32, choices=Role.choices)

    # Set when an administrator creates the account or resets its password.
    # Until the person chooses their own password, the administrator knows it,
    # which is exactly the shared-password situation AsOne asked us to prevent
    # (p.9: "There can be no sharing of Passwords"). Enforced by
    # accounts.permissions.PasswordChangeNotPending.
    #: Set when the person has entered the code emailed to this address.
    #: Null means the address is unproven and sign-in is refused — a
    #: mistyped address must not become a working account.
    email_verified_at = models.DateTimeField(null=True, blank=True)

    must_change_password = models.BooleanField(
        default=False,
        help_text="Blocks everything except viewing your own account and setting a new password.",
    )

    warehouse = models.ForeignKey(
        "catalog.Warehouse",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staff",
    )
    school = models.ForeignKey(
        "catalog.School",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="staff",
    )

    # Roles the access matrix scopes to "All Locations". They are attached to
    # no single site, so a warehouse or school on them would be meaningless.
    ALL_SITE_ROLES = (Role.PROGRAM_LEAD, Role.OPERATIONS_MANAGER, Role.FINANCE)

    #: Which site field each role must carry, if any. The single source for
    #: this rule: clean() enforces it, and the roles endpoint publishes it so
    #: the user-creation form knows which site picker to show.
    SITE_FIELD_BY_ROLE = {
        Role.WAREHOUSE_STAFF: "warehouse",
        Role.SCHOOL_STAFF: "school",
    }

    @classmethod
    def required_site_field(cls, role):
        """"warehouse", "school", or None for the all-locations roles."""
        return cls.SITE_FIELD_BY_ROLE.get(role)

    @property
    def email_is_verified(self) -> bool:
        return self.email_verified_at is not None

    def clean(self):
        """Keep role and site consistent.

        AsOne's user table (p.3) is User Name / Role / Site, with one site per
        user. A school user carrying a warehouse — or a warehouse user with no
        warehouse — would make scope_to_user_site() silently return nothing,
        which reads as "there is no stock" rather than as a broken account.
        Caught here so the admin refuses to save it in the first place.
        """
        super().clean()
        errors = {}

        if self.role == self.Role.WAREHOUSE_STAFF:
            if self.warehouse is None:
                errors["warehouse"] = "Warehouse staff must be assigned a warehouse."
            if self.school is not None:
                errors["school"] = "Warehouse staff cannot be assigned a school."

        elif self.role == self.Role.SCHOOL_STAFF:
            if self.school is None:
                errors["school"] = "School staff must be assigned a school."
            if self.warehouse is not None:
                errors["warehouse"] = "School staff cannot be assigned a warehouse."

        elif self.role in self.ALL_SITE_ROLES:
            if self.warehouse is not None:
                errors["warehouse"] = (
                    f"{self.get_role_display()} covers all locations and takes no warehouse."
                )
            if self.school is not None:
                errors["school"] = (
                    f"{self.get_role_display()} covers all locations and takes no school."
                )

        if errors:
            raise ValidationError(errors)

    @property
    def display_name(self) -> str:
        """What to print where a person is named.

        Falls back to the email, because `get_full_name()` returns an empty
        string for an account created without one — and a blank "Raised by"
        column on an audit trail is worse than an ugly one. AsOne asked for
        user names captured on transactions and available in audit trails
        (p.9); a blank cell does not satisfy that.
        """
        return self.get_full_name() or self.email

    def __str__(self):
        return f"{self.display_name} ({self.get_role_display()})"


class LoginAttempt(models.Model):
    """An audit row for every sign-in attempt, successful or not.

    AsOne asked for user names captured on transactions and available in
    audit trails (p.9). Failed attempts are recorded too: a run of failures
    against one account is the signal that someone is guessing, and it is
    only visible if the failures are kept.

    Append-only. Nothing in the application updates or deletes these rows.
    """

    # Exactly what was typed into the email box, kept even when no such
    # account exists — otherwise an attack on a guessed address leaves no
    # trace at all. A plain CharField, not EmailField: a failed attempt is
    # often not a valid address, and the audit trail should still hold it.
    email = models.CharField(max_length=254, db_index=True)

    # PROTECT so an account cannot be deleted out from under its own audit
    # trail. AsOne deactivates staff rather than deleting them anyway.
    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="login_attempts",
    )

    succeeded = models.BooleanField()
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-at"]
        indexes = [models.Index(fields=["email", "-at"])]

    def __str__(self):
        outcome = "succeeded" if self.succeeded else "failed"
        return f"{self.email} {outcome} at {self.at:%Y-%m-%d %H:%M}"


class OneTimeCode(models.Model):
    """Shared behaviour for a code emailed to somebody, once.

    Two things in this system work this way — the sign-in code (`LoginChallenge`)
    and the invitation that gets a new member of staff their first password
    (`Invitation`). They differ in how long they last and what they unlock,
    and in nothing else, so the rules that make a code a second factor rather
    than a formality live here where they cannot drift apart.

    ## What is stored

    **The code is hashed, never kept in plain text.** Same hashers as a
    password, for the same reason: anybody who can read this table — a
    backup, a support session, a stray query — must not be able to use
    somebody else's code.

    ## Why expiry, attempts and single use all matter

    A six-digit code is a million possibilities, which is plenty against a
    person and nothing at all against a script. Take away the attempt limit
    and it is decoration; take away the expiry and a code left in an inbox
    is a spare key; take away single use and it can be replayed.
    """

    #: The opaque handle the client sends back. A UUID rather than a
    #: sequential id, so one cannot be guessed from the one before it.
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # Hashed. See the class docstring.
    code_hash = models.CharField(max_length=128, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(db_index=True)

    #: Wrong codes tried so far. At the limit the code is dead and the person
    #: starts again.
    attempts = models.PositiveSmallIntegerField(default=0)

    #: Set the moment it is spent. A code that has done its job once must
    #: never do it again, even inside its expiry window.
    consumed_at = models.DateTimeField(null=True, blank=True)

    # Kept for the same reason LoginAttempt keeps them: a code requested from
    # one place and used from another is worth being able to see later.
    ip_address = models.GenericIPAddressField(null=True, blank=True)

    class Meta:
        abstract = True

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    @property
    def is_consumed(self) -> bool:
        return self.consumed_at is not None

    @property
    def attempts_exhausted(self) -> bool:
        return self.attempts >= settings.LOGIN_CODE_MAX_ATTEMPTS

    @property
    def is_usable(self) -> bool:
        """One place that decides, so no two callers can differ."""
        return not (self.is_expired or self.is_consumed or self.attempts_exhausted)


class LoginChallenge(OneTimeCode):
    """A one-time code emailed to someone who has just passed the password step.

    Two-factor authentication, and the reason it is worth the extra step
    here: a stolen or shared AsOne password is otherwise enough to read
    every school's orders and every warehouse's stock. The second factor
    ties a sign-in to the mailbox Central Office chose when they created the
    account.

    The email address is not on this row. It is reachable through ``user``,
    and duplicating it would be one more copy to keep in step.
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="login_challenges"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "consumed_at"])]
        verbose_name = "login verification code"

    def __str__(self):
        return f"Code for {self.user.email} ({self.id})"


class EmailVerification(OneTimeCode):
    """The code that proves a new member of staff holds the mailbox.

    ## Why this exists alongside the password

    A lead creates the account and the system generates a password, which the
    lead reads once and passes on — by WhatsApp, or in person. That proves
    nothing about the email address on the account: it could be mistyped, or
    belong to somebody who left.

    The code is emailed and travels by a different route from the password.
    Holding both is what says "this is the right person, at the right
    address". Emailing the password too would collapse the two routes into
    one and make this step decoration.

    Until it is used, `User.email_verified_at` is null and sign-in is
    refused — an account with an unverified address is not a way in.
    """

    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="email_verifications"
    )
    sent_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "consumed_at"])]
        verbose_name = "email verification code"

    def __str__(self):
        return f"Email verification for {self.user.email}"


class RegistrationRequest(models.Model):
    """Someone asking for an account, before a lead has looked at it.

    Self-registration does not create a `User` — AsOne provisions staff
    deliberately, and every stock movement and order records who performed
    it, so an account nobody vouched for is not something this system wants
    lying around. This is the front door instead: open to anyone, and it
    produces nothing more than a request a lead can approve or decline.

    No `role` field. Choosing a role is a lead's decision, made at approval
    — never the registrant's, and never available before then.

    Kept permanently, approved or declined alike, the same way `User`
    accounts are deactivated rather than deleted: the record of who asked,
    and who decided, is worth keeping.
    """

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        DECLINED = "DECLINED", "Declined"

    first_name = models.CharField(max_length=150)
    last_name = models.CharField(max_length=150)
    email = models.EmailField()
    phone_number = models.CharField(max_length=32, blank=True)

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.PENDING
    )

    created_at = models.DateTimeField(auto_now_add=True)

    #: Set the moment the registrant enters the code emailed to them at
    #: submission. Null means unproven — a lead cannot approve a request
    #: whose address nobody has confirmed holding.
    verified_at = models.DateTimeField(null=True, blank=True)

    #: Set together with `status` moving off PENDING. Null while pending.
    decided_at = models.DateTimeField(null=True, blank=True)
    decided_by = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    #: Set only on approval — the account this request became. Null for a
    #: pending or declined request, since neither has one.
    created_user = models.ForeignKey(
        User, null=True, blank=True, on_delete=models.PROTECT, related_name="+"
    )

    #: Why a request was declined, or any note a lead leaves on approval.
    #: Shown to nobody automatically today — see the open question on
    #: whether a decline should be emailed to the registrant.
    decision_notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["status", "-created_at"])]

    @property
    def is_email_verified(self) -> bool:
        return self.verified_at is not None

    def __str__(self):
        return f"{self.first_name} {self.last_name} <{self.email}> ({self.status})"


class RegistrationVerification(OneTimeCode):
    """The code proving a registrant holds the mailbox they asked with.

    Sent the moment the request is submitted — before any lead has seen
    it — so the person confirms their address right after asking, the same
    beat as `EmailVerification` for a lead-created account. Once spent, the
    request is unlocked for a lead to review; it stays PENDING either way,
    since verifying an address is not the same as being approved.
    """

    registration = models.ForeignKey(
        RegistrationRequest, on_delete=models.CASCADE, related_name="verifications"
    )

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["registration", "consumed_at"])]
        verbose_name = "registration verification code"

    def __str__(self):
        return f"Registration verification for {self.registration.email}"

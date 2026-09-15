"""HTTP layer for authentication.

Views translate between HTTP and the rest of the system and do nothing else:
validate with a serializer, call a service, choose a status code. Any logic
worth testing lives in accounts/services.py, where a test can reach it
without building a request.
"""

from django.conf import settings
from django.db import transaction
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import APIException, PermissionDenied
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
    TokenVerifyView,
)

from . import services
from .models import LoginAttempt, RegistrationRequest, User
from .permissions import AUTHENTICATED, CanUpdateTables
from .throttling import LoginBurstRateThrottle, LoginRateThrottle
from .serializers import (
    ApproveRegistrationSerializer,
    DeclineRegistrationSerializer,
    LoginAttemptSerializer,
    EmailVerificationSerializer,
    LoginChallengeIssuedSerializer,
    LoginSerializer,
    LogoutSerializer,
    MeUpdateSerializer,
    PasswordChangeSerializer,
    RegistrationRequestCreateSerializer,
    RegistrationRequestSerializer,
    RoleSerializer,
    SetPasswordSerializer,
    UserAdminSerializer,
    UserCreateSerializer,
    UserSerializer,
    VerifyLoginCodeSerializer,
    VerifyRegistrationSerializer,
)


# ---------------------------------------------------------------------------
# Getting a token
# ---------------------------------------------------------------------------


class ServiceUnavailable(APIException):
    """503 — the request was fine, something we depend on is not.

    Used when mail cannot be sent. Not a 500: nothing is broken in the
    application and there is no bug to report, the mail server is simply not
    answering. Not a 400 either: the caller did nothing wrong and changing
    their request will not help.
    """

    status_code = 503
    default_detail = "A service this depends on is unavailable. Try again shortly."
    default_code = "service_unavailable"


def _mask_email(email):
    """"julius@asone.test" -> "j••••s@asone.test".

    Enough for the right person to recognise their own mailbox, not enough
    to hand somebody else a full address they did not already have.
    """
    name, _, domain = email.partition("@")
    if len(name) <= 2:
        return f"{name[:1]}•@{domain}"
    return f"{name[0]}{'•' * (len(name) - 2)}{name[-1]}@{domain}"


@extend_schema(
    tags=["Authentication"],
    summary="Sign in — step 1 of 2, password",
    request=LoginSerializer,
    responses={200: LoginChallengeIssuedSerializer},
    description=(
        "Check an email address and password, then **email a one-time code**. "
        "No tokens are returned here — post the code to "
        "`/api/auth/login/verify/` to finish.\n\n"
        "**403 means the address is not a user of this system**, or has been "
        "deactivated. Only people Central Office has added can sign in, and "
        "this says so plainly rather than leaving somebody retyping a "
        "password that was never going to work.\n\n"
        "**401 means the password is wrong** for an address that does exist.\n\n"
        "Both are rate limited per address and per site, and every attempt is "
        "recorded."
    ),
)
class LoginView(TokenObtainPairView):
    """Step one: prove the password, then be sent a code.

    This used to return tokens. It now returns a challenge, because a
    password alone is enough to read every school's orders and every
    warehouse's stock.

    The order of the checks is the interesting part. Access is decided
    **before** the password is looked at, which is what lets a stranger be
    told "you do not have access" instead of "wrong password". That is user
    enumeration, deliberately accepted — see
    `accounts/services.py::user_with_access` for why, and for when to change
    it back.
    """

    serializer_class = LoginSerializer
    permission_classes = [AllowAny]
    # Two limits, both of which must pass: per email address, and a looser one per
    # IP address. See accounts/throttling.py for why a per-IP limit alone
    # would lock out a whole warehouse.
    throttle_classes = [LoginRateThrottle, LoginBurstRateThrottle]

    def post(self, request, *args, **kwargs):
        email = request.data.get("email") if hasattr(request, "data") else None

        # Deliberately first. A person who was never added is told so,
        # rather than being sent round the password loop forever.
        try:
            user = services.user_with_access(email)
        except services.NoAccess as exc:
            services.record_login_attempt(
                email=email, user=None, succeeded=False, request=request
            )
            raise PermissionDenied(str(exc)) from exc

        try:
            serializer = self.get_serializer(data=request.data)
            serializer.is_valid(raise_exception=True)
        except Exception:
            # Recorded before re-raising, so a failure is audited even though
            # the caller only ever sees a 401.
            services.record_login_attempt(
                email=email, user=user, succeeded=False, request=request
            )
            raise

        # The password was right. Before anything else, the address it was
        # sent to has to have been proven — otherwise a mistyped address
        # still makes a working account for whoever holds the password.
        try:
            services.require_verified_email(user)
        except services.EmailNotVerified as exc:
            raise PermissionDenied(str(exc)) from exc

        # Still not a sign-in — it is half of one, and nothing here issues a
        # token.
        challenge = services.start_login_challenge(user, request=request)

        services.record_login_attempt(
            email=email, user=user, succeeded=True, request=request
        )

        return Response(
            LoginChallengeIssuedSerializer(
                {
                    "challenge": challenge.id,
                    "expires_at": challenge.expires_at,
                    "email_hint": _mask_email(user.email),
                    "detail": (
                        "We have emailed you a sign-in code. It expires in "
                        f"{settings.LOGIN_CODE_TTL_MINUTES} minutes."
                    ),
                }
            ).data
        )


@extend_schema(
    tags=["Authentication"],
    summary="Sign in — step 2 of 2, the emailed code",
    request=VerifyLoginCodeSerializer,
    responses={200: LoginSerializer},
    description=(
        "Exchange the challenge and the emailed code for an access token, a "
        "refresh token and the signed-in user's record.\n\n"
        "A code is good **once**, for a few minutes, with a limited number "
        "of tries. Anything else — expired, already used, too many wrong "
        "attempts — is a 400 telling you to sign in again, deliberately "
        "without saying which of those it was."
    ),
)
class VerifyLoginCodeView(APIView):
    """Step two: the code from the email, and only then tokens."""

    permission_classes = [AllowAny]
    serializer_class = VerifyLoginCodeSerializer
    # Its own budget, keyed the same way as login. Without a limit here the
    # second factor would be a six-digit number anybody could work through.
    throttle_classes = [LoginBurstRateThrottle]

    def post(self, request):
        serializer = VerifyLoginCodeSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            user = services.verify_login_code(
                serializer.validated_data["challenge"],
                serializer.validated_data["code"],
            )
        except services.ChallengeUnusable as exc:
            raise DRFValidationError({"code": str(exc)}) from exc

        refresh = RefreshToken.for_user(user)
        return Response(
            {
                "refresh": str(refresh),
                "access": str(refresh.access_token),
                "user": UserSerializer(user).data,
            }
        )


@extend_schema(
    tags=["Authentication"],
    summary="Confirm your email address",
    request=EmailVerificationSerializer,
    responses={200: OpenApiResponse(description="The address is confirmed.")},
    description=(
        "A new member of staff confirms the address their account was "
        "created against, using the code emailed to it.\n\n"
        "**Until this is done, signing in is refused.** An account is created "
        "with a password the lead knows and an address nobody has proven; "
        "this is what proves it. A mistyped address must not become a working "
        "account.\n\n"
        "The password is not part of this step and is never emailed — it "
        "reaches the person through their lead, by a different route. That "
        "separation is the whole point of the code.\n\n"
        "Wrong codes count against a limit, and the code expires."
    ),
)
class EmailVerificationView(APIView):
    """Open, because the caller has no account to authenticate with yet —
    that is exactly what the code is standing in for."""

    permission_classes = [AllowAny]
    serializer_class = EmailVerificationSerializer
    throttle_classes = [LoginRateThrottle, LoginBurstRateThrottle]

    def post(self, request):
        serializer = EmailVerificationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            services.verify_email(
                serializer.validated_data["email"], serializer.validated_data["code"]
            )
        except services.VerificationUnusable as exc:
            raise DRFValidationError({"code": str(exc)}) from exc

        return Response(
            {
                "detail": (
                    "Your email address is confirmed. You can now sign in with "
                    "the password your lead gave you, and you will be asked to "
                    "replace it."
                )
            }
        )


@extend_schema(
    tags=["Authentication"],
    summary="Refresh an access token",
    description=(
        "Exchange a valid refresh token for a new access token. Refresh "
        "tokens rotate: the one sent is blacklisted and a replacement is "
        "returned alongside the new access token."
    ),
)
class RefreshView(TokenRefreshView):
    permission_classes = [AllowAny]
    # Its own budget. Sharing the login scope would mean a site's routine
    # token refreshes ate the allowance its staff need to sign in.
    throttle_scope = "token_refresh"


@extend_schema(
    tags=["Authentication"],
    summary="Verify a token",
    description="Check whether a token is still valid. Returns 200 or 401.",
)
class VerifyView(TokenVerifyView):
    permission_classes = [AllowAny]


# ---------------------------------------------------------------------------
# Giving it back
# ---------------------------------------------------------------------------


@extend_schema(
    tags=["Authentication"],
    summary="Sign out",
    request=LogoutSerializer,
    responses={
        205: OpenApiResponse(description="Refresh token blacklisted."),
        400: OpenApiResponse(description="The token was missing, malformed or already used."),
    },
    description=(
        "Blacklist the refresh token so it cannot be exchanged again. The "
        "access token issued with it stays valid until it expires, which is "
        "why access tokens are short-lived."
    ),
)
class LogoutView(APIView):
    permission_classes = [IsAuthenticated]
    # Signing out must never be blocked. An account that cannot sign out is
    # an account whose token stays live until it expires.
    allow_password_change_pending = True

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            services.blacklist_refresh_token(serializer.validated_data["refresh"])
        except TokenError:
            # Deliberately not specific about why. A caller probing tokens
            # learns only that this one is unusable.
            return Response(
                {"detail": "That refresh token is not valid."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(status=status.HTTP_205_RESET_CONTENT)


# ---------------------------------------------------------------------------
# The signed-in user
# ---------------------------------------------------------------------------


@extend_schema(tags=["Authentication"])
class MeView(RetrieveUpdateAPIView):
    """Read or edit the signed-in user's own record.

    There is no lookup by id and no queryset to filter — the object is always
    ``request.user``, so one user can never address another through this
    endpoint. Writes go through MeUpdateSerializer, whose field list excludes
    role and site.
    """

    permission_classes = [IsAuthenticated]
    # Reachable on a pending password so the React app can read the flag and
    # route to the change-password screen rather than showing a wall of 403s.
    allow_password_change_pending = True
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self):
        return self.request.user

    def get_serializer_class(self):
        return UserSerializer if self.request.method == "GET" else MeUpdateSerializer

    @extend_schema(
        summary="Current user",
        responses=UserSerializer,
        description="The signed-in user with their role, site and access summary.",
    )
    def get(self, request, *args, **kwargs):
        return super().get(request, *args, **kwargs)

    @extend_schema(
        summary="Update your own details",
        request=MeUpdateSerializer,
        responses=UserSerializer,
        description=(
            "Change your name or email address. Role and site are set by an "
            "administrator and cannot be changed here."
        ),
    )
    def patch(self, request, *args, **kwargs):
        super().patch(request, *args, **kwargs)
        # Answer with the full record rather than the three fields that were
        # writable, so the client can refresh its copy of the user in one go.
        return Response(UserSerializer(request.user).data)


@extend_schema(
    tags=["Authentication"],
    summary="Change your password",
    request=PasswordChangeSerializer,
    responses={
        200: OpenApiResponse(description="Password changed. A new token pair is returned."),
        400: OpenApiResponse(description="Current password wrong, or new password rejected."),
    },
    description=(
        "Requires the current password even though the request is already "
        "authenticated. On success every existing session is signed out and a "
        "fresh token pair is returned for the session making the change."
    ),
)
class PasswordChangeView(APIView):
    permission_classes = [IsAuthenticated]
    # The whole point of the pending state is to get here.
    allow_password_change_pending = True
    throttle_scope = "password_change"

    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        tokens = services.change_password(
            request.user, serializer.validated_data["new_password"]
        )
        return Response({"detail": "Password changed.", **tokens}, status=status.HTTP_200_OK)


# ---------------------------------------------------------------------------
# Administering other people's accounts
# ---------------------------------------------------------------------------


@extend_schema(tags=["User administration"])
class UserViewSet(viewsets.ModelViewSet):
    """Create and manage staff accounts.

    Behind ``CanUpdateTables`` — the matrix column that owns the User table
    (p.3), held by Program Lead and Operations Manager. No site scoping is
    applied because those are the only two roles that reach this at all, and
    both are "All Locations". If AsOne ever widens the column, add
    ``scope_to_user_site()`` to ``get_queryset()`` before doing anything else.

    Accounts are never deleted. DELETE is not routed: the stock ledger and the
    audit trail both point at users, and a movement with no user attached
    would break the promise that every transaction records who made it. Use
    ``deactivate`` instead.
    """

    permission_classes = [*AUTHENTICATED, CanUpdateTables]
    queryset = User.objects.select_related("warehouse", "school").order_by("email")
    filterset_fields = ["role", "is_active", "warehouse", "school"]

    # No DELETE. Accounts are deactivated, never removed: the sign-in audit
    # trail points at them, and every stock movement will once the ledger
    # exists. Only LoginAttempt protects a user today, so without this an
    # account that had never signed in could be erased.
    http_method_names = ["get", "post", "patch", "put", "head", "options"]
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_serializer_class(self):
        if self.action == "create":
            return UserCreateSerializer
        if self.action == "set_password":
            return SetPasswordSerializer
        return UserAdminSerializer

    def _guard_self(self, target, what: str):
        """Refuse an administrator changing this aspect of their own account.

        Not paranoia about privilege escalation — a lead already holds the
        highest access. It is about lockout: a lead who deactivates or demotes
        themselves needs another lead to undo it, and there may not be one
        signed in at that site.
        """
        if target.pk == self.request.user.pk:
            raise PermissionDenied(f"You cannot {what} your own account.")

    @extend_schema(
        summary="Add a user",
        request=UserCreateSerializer,
        responses={201: OpenApiResponse(description="Created.")},
        description=(
            "First name, last name, email address, role and password. The "
            "email address is the credential the person signs in with, so it "
            "must be unique.\n\n"
            "Warehouse staff also need a `warehouse`, and school staff a "
            "`school`. The all-locations roles take neither — see "
            "`GET /api/auth/roles/` for which is which.\n\n"
            "By default the account must replace this password at first "
            "sign-in, because until then you know it too."
        ),
    )
    def create(self, request, *args, **kwargs):
        """Add a member of staff, and email them a confirmation code.

        The password is generated unless the lead types one, and shown back
        **once** so they can pass it on themselves. It is never emailed. The
        confirmation code is, and keeping the two on separate routes is what
        makes holding both mean something.

        The account cannot be signed into until that code is entered.
        """
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        fields = dict(serializer.validated_data)
        typed_password = fields.pop("password", "") or None

        # Creating the account and sending the code are one operation or
        # neither.
        #
        # Without the transaction, a mail server that is down left an account
        # nobody could confirm and nobody could recreate: the lead saw an
        # error, retried, and was told the address already existed. The
        # account was then stuck and only a developer could clear it. Found
        # by probing on 5 September 2026, not by a test failing.
        try:
            with transaction.atomic():
                user, password = services.create_staff_user(
                    password=typed_password, **fields
                )
                services.send_email_verification(
                    user, sent_by=request.user, request=request, password=password
                )
        except OSError as exc:
            # Anything the mail library raises for "could not send" —
            # unreachable host, refused credentials, timeout. The account has
            # been rolled back, so the lead can simply try again.
            raise ServiceUnavailable(
                "The account was not created because the confirmation email "
                "could not be sent. Nothing has been saved — try again, and "
                "tell whoever runs the system if it keeps happening."
            ) from exc

        return Response(
            {
                "user": UserAdminSerializer(user).data,
                "password": password,
                "detail": (
                    f"Give this password to {user.get_full_name() or user.email} "
                    "yourself — it is not emailed, and cannot be shown again. A "
                    f"confirmation code has been emailed to {user.email}; they "
                    "must enter it before they can sign in, and they will be "
                    "asked to replace this password once they do."
                ),
            },
            status=status.HTTP_201_CREATED,
        )

    @extend_schema(
        summary="Send the confirmation code again",
        request=None,
        responses={200: OpenApiResponse(description="A fresh code was emailed.")},
        description=(
            "Emails a **new** confirmation code and retires the old one.\n\n"
            "Needed more often than it sounds: a code lasts seven days, mail "
            "goes astray, and people start a new job a fortnight after being "
            "added. Without this the only fix is a developer.\n\n"
            "Refused for somebody whose address is already confirmed — there "
            "is nothing left to prove. If they cannot get in, use **set "
            "password** instead."
        ),
    )
    @action(detail=True, methods=["post"], url_path="resend-verification")
    def resend_verification(self, request, pk=None):
        user = self.get_object()

        if user.email_is_verified:
            raise DRFValidationError(
                {
                    "detail": (
                        f"{user.get_full_name() or user.email} has already "
                        "confirmed their address. If they cannot get in, set "
                        "them a new password instead."
                    )
                }
            )
        if not user.is_active:
            raise DRFValidationError(
                {"detail": "That account is deactivated. Reactivate it first."}
            )

        try:
            services.send_email_verification(
                user, sent_by=request.user, request=request
            )
        except OSError as exc:
            raise ServiceUnavailable(
                "The code could not be sent. Nothing has changed — try again, "
                "and tell whoever runs the system if it keeps happening."
            ) from exc

        return Response(
            {
                "detail": (
                    f"A new confirmation code has been emailed to {user.email}. "
                    "Any earlier code no longer works."
                )
            }
        )

    def update(self, request, *args, **kwargs):
        # Checked before the serializer runs. A lead patching their own role
        # should be told plainly that they cannot, not handed a validation
        # error about some other field in the same payload.
        target = self.get_object()
        if "role" in request.data:
            self._guard_self(target, "change the role of")
        if request.data.get("is_active") is False:
            self._guard_self(target, "deactivate")

        return super().update(request, *args, **kwargs)

    @extend_schema(
        summary="Set a user's password",
        request=SetPasswordSerializer,
        responses={200: OpenApiResponse(description="Password set.")},
        description=(
            "Changes another person's password — for someone who has "
            "forgotten theirs, or when an account needs a new one. The "
            "current password is not required: the point is that nobody has "
            "it.\n\n"
            "Signs that account out of every session. Send `new_password` to "
            "choose one, or omit it to have one generated.\n\n"
            "There is no self-service email reset — AsOne's sites are rural "
            "and mail delivery is not something the system can rely on."
        ),
    )
    @action(detail=True, methods=["post"], url_path="set-password")
    def set_password(self, request, pk=None):
        user = self.get_object()

        serializer = SetPasswordSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        password = services.set_user_password(
            user,
            new_password=serializer.validated_data.get("new_password"),
            must_change_password=serializer.validated_data["must_change_password"],
        )

        return Response(
            {
                "password": password,
                "detail": "Signed out of every session. Cannot be shown again.",
            }
        )

    @extend_schema(
        summary="Deactivate a user",
        request=None,
        responses={200: UserAdminSerializer},
        description=(
            "Removes access and signs the account out everywhere. The account "
            "and its history remain, because the ledger and audit trail point "
            "at it."
        ),
    )
    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        user = self.get_object()
        self._guard_self(user, "deactivate")
        services.set_active(user, is_active=False)
        return Response(UserAdminSerializer(user).data)

    @extend_schema(summary="Reactivate a user", request=None, responses={200: UserAdminSerializer})
    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        user = self.get_object()
        services.set_active(user, is_active=True)
        return Response(UserAdminSerializer(user).data)

    @extend_schema(
        summary="Sign a user out everywhere",
        request=None,
        responses={200: OpenApiResponse(description="Number of sessions retired.")},
        description=(
            "For a lost or stolen device, where the person still works here. "
            "The account stays usable; only its current sessions are retired."
        ),
    )
    @action(detail=True, methods=["post"], url_path="sign-out")
    def sign_out(self, request, pk=None):
        user = self.get_object()
        retired = services.force_sign_out(user)
        return Response({"sessions_retired": retired})


@extend_schema(
    tags=["User administration"],
    summary="Sign-in audit trail",
    description=(
        "Every sign-in attempt, successful or not, newest first. Failures are "
        "included: a run of them against one account is the visible sign that "
        "someone is guessing at it.\n\n"
        "Filter with `?email=`, `?succeeded=`, or `?user=`."
    ),
)
class LoginAttemptViewSet(viewsets.ReadOnlyModelViewSet):
    """The audit trail. Read-only at every level — these rows are never edited."""

    queryset = LoginAttempt.objects.select_related("user").all()
    serializer_class = LoginAttemptSerializer
    permission_classes = [*AUTHENTICATED, CanUpdateTables]
    filterset_fields = ["email", "succeeded", "user"]


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------


@extend_schema(
    tags=["Authentication"],
    summary="Ask for an account",
    request=RegistrationRequestCreateSerializer,
    responses={201: RegistrationRequestSerializer},
    description=(
        "Open to anyone — there is no account yet to authenticate as. Name, "
        "email and phone number only: **not** a role, which only a lead may "
        "assign, and only once reviewing this request.\n\n"
        "Creates nothing more than a request. Nobody can sign in from this "
        "alone — a lead must approve it first, at `POST "
        "/api/auth/registration-requests/{id}/approve/`, the same way "
        "`POST /api/auth/users/` creates an account today."
    ),
)
class RegistrationRequestCreateView(APIView):
    """POST /api/auth/register/."""

    permission_classes = [AllowAny]
    serializer_class = RegistrationRequestCreateSerializer
    throttle_classes = [LoginRateThrottle, LoginBurstRateThrottle]

    def post(self, request):
        serializer = RegistrationRequestCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                registration = services.request_registration(
                    **serializer.validated_data, http_request=request
                )
        except OSError as exc:
            raise ServiceUnavailable(
                "The request was not saved because the confirmation email "
                "could not be sent. Nothing has been saved — try again, and "
                "tell whoever runs the system if it keeps happening."
            ) from exc

        return Response(
            RegistrationRequestSerializer(registration).data,
            status=status.HTTP_201_CREATED,
        )


@extend_schema(
    tags=["Authentication"],
    summary="Confirm a registration request's address",
    request=VerifyRegistrationSerializer,
    responses={200: OpenApiResponse(description="Confirmed.")},
    description=(
        "Confirms the code emailed immediately after `POST /auth/register/`. "
        "Open, the same way `/auth/verify-email/` is — the registrant has no "
        "account to authenticate with yet.\n\n"
        "This does not create an account and does not sign anyone in. It "
        "unlocks the request for a lead to review: `approve` on "
        "`/auth/registration-requests/{id}/` is refused until this is done."
    ),
)
class VerifyRegistrationView(APIView):
    """POST /api/auth/register/verify/."""

    permission_classes = [AllowAny]
    serializer_class = VerifyRegistrationSerializer
    throttle_classes = [LoginRateThrottle, LoginBurstRateThrottle]

    def post(self, request):
        serializer = VerifyRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            services.verify_registration_email(
                serializer.validated_data["email"], serializer.validated_data["code"]
            )
        except services.VerificationUnusable as exc:
            raise DRFValidationError({"code": str(exc)}) from exc

        return Response(
            {
                "detail": (
                    "Your email address is confirmed. AsOne's team will review "
                    "your request and assign you a role — you will hear from "
                    "them by email."
                )
            }
        )


@extend_schema(tags=["User administration"])
class RegistrationRequestViewSet(viewsets.ReadOnlyModelViewSet):
    """Review requests and decide them. Program Lead and Operations Manager
    only, same as `UserViewSet` — approving one *is* creating a user.

    Read-only at the ModelViewSet level: a request is never edited, only
    approved or declined through the actions below.
    """

    permission_classes = [*AUTHENTICATED, CanUpdateTables]
    queryset = RegistrationRequest.objects.select_related(
        "decided_by", "created_user"
    ).order_by("-created_at")
    serializer_class = RegistrationRequestSerializer
    filterset_fields = ["status"]

    @extend_schema(
        summary="Approve a registration request",
        request=ApproveRegistrationSerializer,
        responses={201: OpenApiResponse(description="Account created.")},
        description=(
            "Supplies the one thing a registrant could not: the role, and the "
            "warehouse or school it requires. Creates the account and emails "
            "the confirmation code exactly as `POST /api/auth/users/` does — "
            "this is that same operation, reached from a request instead of a "
            "blank form.\n\n"
            "409 if the request was already approved or declined."
        ),
    )
    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        registration = self.get_object()

        serializer = ApproveRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            with transaction.atomic():
                user, password = services.approve_registration(
                    registration,
                    decided_by=request.user,
                    http_request=request,
                    **serializer.validated_data,
                )
        except services.RegistrationAlreadyDecided as exc:
            raise self._conflict(exc) from exc
        except services.RegistrationEmailNotVerified as exc:
            raise DRFValidationError({"detail": str(exc)}) from exc
        except OSError as exc:
            raise ServiceUnavailable(
                "The account was not created because the confirmation email "
                "could not be sent. Nothing has been saved — try again, and "
                "tell whoever runs the system if it keeps happening."
            ) from exc

        return Response(
            {
                "user": UserAdminSerializer(user).data,
                "password": password,
                "detail": (
                    f"Give this password to {user.get_full_name() or user.email} "
                    "yourself — it is not emailed, and cannot be shown again. A "
                    f"confirmation code has been emailed to {user.email}; they "
                    "must enter it before they can sign in, and they will be "
                    "asked to replace this password once they do."
                ),
            },
            status=status.HTTP_201_CREATED,
        )

    @staticmethod
    def _conflict(exc):
        """Route RegistrationAlreadyDecided to a 409 rather than the 500 a
        bare exception would raise — the request is fine, the state is not."""
        conflict = APIException(str(exc), code="already_decided")
        conflict.status_code = status.HTTP_409_CONFLICT
        return conflict

    @extend_schema(
        summary="Decline a registration request",
        request=DeclineRegistrationSerializer,
        responses={200: RegistrationRequestSerializer},
        description=(
            "Refuses the request. No account is created and nothing is "
            "emailed to the registrant — they simply see no reply and may "
            "submit the form again.\n\n"
            "409 if the request was already approved or declined."
        ),
    )
    @action(detail=True, methods=["post"])
    def decline(self, request, pk=None):
        registration = self.get_object()

        serializer = DeclineRegistrationSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        try:
            services.decline_registration(
                registration,
                decided_by=request.user,
                notes=serializer.validated_data.get("notes", ""),
            )
        except services.RegistrationAlreadyDecided as exc:
            raise self._conflict(exc) from exc

        return Response(RegistrationRequestSerializer(registration).data)


@extend_schema(
    tags=["User administration"],
    summary="The roles, and what each one may do",
    responses=RoleSerializer(many=True),
    description=(
        "AsOne's five roles with the seven columns of their access matrix, "
        "the scope each reaches, and which site each requires.\n\n"
        "Built from the same permission classes the API enforces, so the "
        "navigation a client draws and the answer the server gives cannot "
        "drift apart. Use `requires_site` to decide whether a user-creation "
        "form shows a warehouse picker, a school picker, or neither."
    ),
)
class RoleListView(APIView):
    """The access matrix, published.

    Readable by any signed-in user. It describes AsOne's own access policy
    and contains no data about any person or site — a school clerk learning
    that Finance posts adjustments is not a disclosure.
    """

    permission_classes = AUTHENTICATED

    def get(self, request):
        return Response(RoleSerializer(services.role_catalogue(), many=True).data)

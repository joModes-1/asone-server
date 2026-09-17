"""Making "one account, one session" take effect immediately.

Retiring refresh tokens at sign-in is half the rule. It ends the other
device's ability to *renew*, but an access token is signed and self-contained,
so the other device keeps working until its current one expires — up to
ACCESS_TOKEN_LIFETIME, thirty minutes. Two people sharing a password both keep
working for that half hour, which is exactly the situation the rule exists to
make visible.

This closes it. Every sign-in bumps `User.session_epoch`, the number rides in
the token, and a token carrying an older one is refused on the next request.

## Why this is nearly free

The usual objection to checking anything per-request is the database read. But
`JWTAuthentication.get_user` **already** loads the user on every authenticated
request — it has to, to return `request.user` and to check `is_active`.
Comparing one integer on a row that is already in memory costs nothing
measurable. There is no extra query.

## Why an integer and not a timestamp

Two sign-ins inside the same clock tick would compare equal on a timestamp,
and the loser would keep its session. A counter cannot tie.

## What the other device sees

A 401 with `code: "session_superseded"` — distinct from an expired token,
because the client should treat them differently. An expired access token is
refreshed silently; this one means somebody else signed in and the person
needs telling that, not a login form appearing for no reason.
"""

from rest_framework_simplejwt.authentication import JWTAuthentication
from rest_framework_simplejwt.exceptions import AuthenticationFailed

#: The claim carrying the epoch. Short because it is in every token.
SESSION_EPOCH_CLAIM = "se"

#: Sent to the client so it can tell this apart from an ordinary expiry.
SESSION_SUPERSEDED = "session_superseded"


def stamp_session_epoch(token, user):
    """Copy the account's current epoch into ``token``.

    Applied to the refresh token; simplejwt copies every claim from a refresh
    token onto the access tokens minted from it, so this rides through
    refreshes without anything further.
    """
    token[SESSION_EPOCH_CLAIM] = user.session_epoch
    return token


class SingleSessionJWTAuthentication(JWTAuthentication):
    """Refuse a token minted before this account's most recent sign-in."""

    def get_user(self, validated_token):
        # The parent does the real work — loads the user, refuses an inactive
        # one. Everything below reads the row it has already fetched.
        user = super().get_user(validated_token)

        presented = validated_token.get(SESSION_EPOCH_CLAIM)

        # A token with no epoch at all was minted before this feature existed.
        # Refusing those would sign out everybody the moment it deploys, on a
        # system whose users are in warehouses without an IT desk. They are
        # allowed through and die naturally at their own expiry, by which time
        # every token in circulation carries one.
        if presented is None:
            return user

        if presented != user.session_epoch:
            raise AuthenticationFailed(
                "Somebody signed in to this account somewhere else, so this "
                "session was ended. Sign in again to continue.",
                code=SESSION_SUPERSEDED,
            )

        return user

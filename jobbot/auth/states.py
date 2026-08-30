"""Authentication states and outcomes (spec §25, §30, §31).

Authentication is first-class here: it has its own state set, its own
verification step, and its own boundary. Two of those states are not
problems to be solved but lines not to cross.
"""
from __future__ import annotations

from enum import Enum


class AuthState(str, Enum):
    """What the page is asking for. §25."""

    ALREADY_AUTHENTICATED = "ALREADY_AUTHENTICATED"
    LOGIN_REQUIRED = "LOGIN_REQUIRED"
    SIGNUP_REQUIRED = "SIGNUP_REQUIRED"
    SSO_REQUIRED = "SSO_REQUIRED"
    OTP_REQUIRED = "OTP_REQUIRED"
    EMAIL_VERIFICATION_REQUIRED = "EMAIL_VERIFICATION_REQUIRED"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    ACCOUNT_LOCKED = "ACCOUNT_LOCKED"
    SECURITY_CHALLENGE = "SECURITY_CHALLENGE"
    CAPTCHA = "CAPTCHA"
    BOT_DETECTION = "BOT_DETECTION"
    UNSUPPORTED_AUTH = "UNSUPPORTED_AUTH"
    NOT_AUTH = "NOT_AUTH"          # this page is not asking us to sign in


#: States we never attempt to satisfy automatically (§31). These are
#: boundaries, not obstacles: a system that works around them is one that
#: gets the user's account flagged, and defeating a security control on
#: the user's behalf is not something this project does.
NEVER_AUTOMATED: frozenset[AuthState] = frozenset({
    AuthState.CAPTCHA,
    AuthState.BOT_DETECTION,
    AuthState.SECURITY_CHALLENGE,
    AuthState.ACCOUNT_LOCKED,
    # Third-party SSO means driving a Google/Microsoft/Apple sign-in with
    # the user's primary identity. The blast radius of getting that wrong
    # is their whole account, not one job application.
    AuthState.SSO_REQUIRED,
})

#: States a human can clear, after which the agent picks the flow back up
#: (§32). Distinct from NEVER_AUTOMATED: those are refused permanently,
#: these are handed over and resumed.
HUMAN_CAN_RESOLVE: frozenset[AuthState] = frozenset({
    AuthState.OTP_REQUIRED,
    AuthState.EMAIL_VERIFICATION_REQUIRED,
    AuthState.SSO_REQUIRED,
    AuthState.SECURITY_CHALLENGE,
    AuthState.CAPTCHA,
})


class AuthOutcome(str, Enum):
    """How an authentication attempt ended."""

    AUTHENTICATED = "AUTHENTICATED"        # verified, not merely attempted
    ALREADY_AUTHENTICATED = "ALREADY_AUTHENTICATED"
    #: The page never asked us to sign in. The caller may proceed, but this
    #: is *not* evidence of a session and must never be recorded as one:
    #: an error page, a 404 and a half-loaded page all fail to ask.
    NOT_REQUIRED = "NOT_REQUIRED"
    #: Perception failed, so nothing can be concluded either way.
    INDETERMINATE = "INDETERMINATE"
    HUMAN_REQUIRED = "HUMAN_REQUIRED"      # a person must do this step
    NO_CREDENTIAL = "NO_CREDENTIAL"        # nothing stored for this domain
    REFUSED = "REFUSED"                    # policy says we do not do this
    FAILED = "FAILED"                      # tried, and verification said no
    BLOCKED = "BLOCKED"                    # a wall


class VerificationChannelState(str, Enum):
    """§30. Whether a second factor can be supplied at all."""

    AVAILABLE = "AVAILABLE"      # a human is present and can provide it
    REQUIRED = "REQUIRED"        # the page wants one
    UNAVAILABLE = "UNAVAILABLE"  # unattended run; nobody to ask
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"

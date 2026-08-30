"""Deterministic detection of what a page is asking for (spec §25).

Kept separate from `page_classify.py` on purpose. That module answers
"what kind of page is this" for the agent loop; this one answers the
narrower, higher-stakes question "is this asking me to authenticate, and
in a way I am permitted to satisfy". The two disagree usefully: a page can
classify as `APPLICATION_FORM` while carrying an expired-session banner.

All signal-based, no LLM. An authentication decision made by a model
reading page text is a decision an injected page can change.
"""
from __future__ import annotations

import re

from jobbot.agent.observation import BrowserObservation
from jobbot.auth.states import AuthState

#: Ordered most-specific first. The first match wins, so a page that is
#: both a login form and a CAPTCHA is reported as the CAPTCHA — the wall
#: is the thing that stops us, and mislabelling it as LOGIN_REQUIRED would
#: send us to type a password into a challenge page.
_SIGNALS: tuple[tuple[AuthState, re.Pattern[str]], ...] = (
    (AuthState.BOT_DETECTION, re.compile(
        r"\b(unusual traffic|automated (?:queries|traffic|access)|are you a (?:human|robot)|"
        r"suspicious activity detected|access denied by security)\b", re.I)),
    (AuthState.CAPTCHA, re.compile(
        r"\b(captcha|recaptcha|hcaptcha|verify you are human|i'?m not a robot|"
        r"cloudflare (?:security )?check)\b", re.I)),
    (AuthState.ACCOUNT_LOCKED, re.compile(
        r"\b(account (?:has been )?(?:locked|disabled|suspended)|too many (?:failed )?"
        r"(?:login )?attempts|temporarily blocked)\b", re.I)),
    (AuthState.SECURITY_CHALLENGE, re.compile(
        r"\b(security (?:question|challenge|check)|verify your identity|"
        r"confirm it'?s you|unusual sign[- ]?in)\b", re.I)),
    (AuthState.OTP_REQUIRED, re.compile(
        r"\b(one[- ]time (?:code|password|passcode)|verification code|2fa|two[- ]factor|"
        r"authenticator app|enter the (?:6|four|six)[- ]digit)\b", re.I)),
    (AuthState.EMAIL_VERIFICATION_REQUIRED, re.compile(
        r"\b(verify your email|confirm your email|check your (?:inbox|email) "
        r"(?:for|to)|we (?:have )?sent (?:you )?(?:a|an) (?:verification|confirmation) "
        r"(?:e-?mail|link))\b", re.I)),
    (AuthState.SESSION_EXPIRED, re.compile(
        r"\b(session (?:has )?expired|you (?:have been|were) (?:signed|logged) out|"
        r"please (?:sign|log) in again|your session timed out)\b", re.I)),
    (AuthState.SIGNUP_REQUIRED, re.compile(
        r"\b(create (?:an )?account|sign up (?:to|and) (?:apply|continue)|"
        r"register to (?:apply|continue)|new (?:candidate|applicant) account)\b", re.I)),
)

#: Buttons that mean "sign in with someone else's identity provider".
_SSO_CONTROL = re.compile(
    r"\b(?:continue|sign ?in|log ?in) with (google|microsoft|apple|github|linkedin|okta|sso)\b"
    r"|\bsingle sign[- ]on\b|\bsaml\b", re.I)

_PASSWORD_FIELD = re.compile(r"\bpassword\b", re.I)
_IDENTIFIER_FIELD = re.compile(r"\b(email|e-?mail address|username|user name|user id)\b", re.I)
_SUBMIT_CONTROL = re.compile(r"\b(sign ?in|log ?in|continue|next|submit)\b", re.I)

#: Evidence we are already through the door.
_AUTHENTICATED_SIGNAL = re.compile(
    r"\b(sign ?out|log ?out|my (?:account|profile|applications)|"
    r"welcome back|you are (?:signed|logged) in)\b", re.I)


def _text_of(observation: BrowserObservation) -> str:
    return " \n ".join(filter(None, (
        observation.visible_text, observation.aria_snapshot, observation.title,
    )))


def _controls_text(observation: BrowserObservation) -> str:
    return " \n ".join(c.semantic_label for c in observation.controls)


def detect_auth_state(observation: BrowserObservation) -> tuple[AuthState, list[str]]:
    """What this page wants, plus the evidence for it.

    Returns `NOT_AUTH` when nothing suggests authentication — which must
    be the common case, since most application pages do not ask.
    """
    text = _text_of(observation)
    controls = _controls_text(observation)
    evidence: list[str] = []

    for state, pattern in _SIGNALS:
        match = pattern.search(text) or pattern.search(controls)
        if match:
            evidence.append(f"{state.value}: {match.group(0)!r}")
            return state, evidence

    has_password = any(
        c.role in ("textbox", "password") and _PASSWORD_FIELD.search(c.semantic_label)
        for c in observation.controls
    )
    has_identifier = any(
        c.role in ("textbox", "combobox") and _IDENTIFIER_FIELD.search(c.semantic_label)
        for c in observation.controls
    )
    sso_match = _SSO_CONTROL.search(controls) or _SSO_CONTROL.search(text)

    if has_password and has_identifier:
        evidence.append("identifier + password fields present")
        if sso_match:
            evidence.append(f"also offers SSO: {sso_match.group(0)!r}")
        return AuthState.LOGIN_REQUIRED, evidence

    if sso_match and not has_password:
        # SSO with no local password form: the only way in is through a
        # third-party identity provider, which we do not drive.
        evidence.append(f"SSO only: {sso_match.group(0)!r}")
        return AuthState.SSO_REQUIRED, evidence

    if has_password and not has_identifier:
        # A password field with no identifier is a second step of a
        # multi-step login, or something we do not recognise.
        evidence.append("password field with no identifier field")
        return AuthState.LOGIN_REQUIRED, evidence

    if _AUTHENTICATED_SIGNAL.search(text):
        evidence.append("sign-out / account link present")
        return AuthState.ALREADY_AUTHENTICATED, evidence

    return AuthState.NOT_AUTH, evidence


def is_authenticated(observation: BrowserObservation) -> bool:
    """Positive evidence of a live session.

    Deliberately requires a *signal*, not merely the absence of a login
    form: "no login form here" is equally true of an error page, a 404,
    and a page that has not finished loading. Absence is not evidence
    (browser-agent-failures.md §14).
    """
    state, _ = detect_auth_state(observation)
    if state is AuthState.ALREADY_AUTHENTICATED:
        return True
    return False

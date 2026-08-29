"""Classify what kind of page the agent is currently looking at.

The agent must not assume the job URL opens an application form. A real
journey is routinely:

    job page -> Apply -> careers page -> Apply -> external ATS -> login
    -> redirect -> multi-page form -> review -> confirmation

so every step has to be re-grounded in what the page actually *is*.

Two rules govern this module:

1. **UNKNOWN is a real answer.** The failure mode being designed out is an
   agent that, finding nothing it recognizes, decides "probably the
   application form" and starts typing into an unrelated page. `classify()`
   returns UNKNOWN with its evidence, and the caller is expected to gather
   more observation or escalate — never to coerce it into a guess.

2. **Deterministic signals first, model second.** Classification runs on
   URL/controls/text with no LLM call, because it happens on every step and
   §6 (context rot) plus cost both argue against a model call per step. The
   LLM is an escalation path for UNKNOWN, not the default classifier.

Blocking states (CAPTCHA/bot detection) are checked before anything else:
a page showing both a CAPTCHA and a "thank you for applying" banner is
blocked, not a success — the same precedence rule as submit/verify.py.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from jobbot.agent.observation import BrowserObservation


class PageState(str, Enum):
    JOB_PAGE = "JOB_PAGE"
    CAREERS_PAGE = "CAREERS_PAGE"
    APPLICATION_ENTRY = "APPLICATION_ENTRY"
    LOGIN = "LOGIN"
    SIGNUP = "SIGNUP"
    SSO = "SSO"
    OTP = "OTP"
    EMAIL_VERIFICATION = "EMAIL_VERIFICATION"
    ACCOUNT_SETUP = "ACCOUNT_SETUP"
    APPLICATION_FORM = "APPLICATION_FORM"
    APPLICATION_REVIEW = "APPLICATION_REVIEW"
    SUBMISSION_CONFIRMATION = "SUBMISSION_CONFIRMATION"
    ALREADY_APPLIED = "ALREADY_APPLIED"
    DRAFT_APPLICATION = "DRAFT_APPLICATION"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    CAPTCHA = "CAPTCHA"
    BOT_DETECTION = "BOT_DETECTION"
    ACCESS_DENIED = "ACCESS_DENIED"
    ERROR = "ERROR"
    UNKNOWN = "UNKNOWN"


#: States where the agent must stop rather than act.
TERMINAL_BLOCKING: frozenset[PageState] = frozenset(
    {PageState.CAPTCHA, PageState.BOT_DETECTION, PageState.ACCESS_DENIED}
)

#: States that mean "authenticate before continuing".
AUTH_STATES: frozenset[PageState] = frozenset(
    {PageState.LOGIN, PageState.SIGNUP, PageState.SSO, PageState.OTP,
     PageState.EMAIL_VERIFICATION, PageState.ACCOUNT_SETUP, PageState.SESSION_EXPIRED}
)


@dataclass
class Classification:
    state: PageState
    confidence: float           # 0.0 - 1.0
    evidence: list[str] = field(default_factory=list)
    #: Runner-up states, so an UNKNOWN escalation can say what it was torn
    #: between rather than starting from nothing.
    alternatives: list[tuple[PageState, float]] = field(default_factory=list)

    @property
    def is_confident(self) -> bool:
        return self.confidence >= 0.6

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "confidence": round(self.confidence, 2),
            "evidence": self.evidence,
            "alternatives": [(s.value, round(c, 2)) for s, c in self.alternatives],
        }


# --- signal vocabularies ---------------------------------------------------

_RX = lambda p: re.compile(p, re.IGNORECASE)  # noqa: E731

_BLOCKING_TEXT = _RX(r"\b(captcha|recaptcha|hcaptcha|verify you(?:'| a)?re (?:a )?human|are you a robot)\b")
_BOT_TEXT = _RX(r"\b(unusual traffic|automated (?:requests|traffic)|access denied|forbidden|rate limit(?:ed)?|cloudflare)\b")
_DENIED_TEXT = _RX(r"\b(403 forbidden|access denied|not authorized|permission denied)\b")

_CONFIRMATION_TEXT = _RX(
    r"\b(thank you for (?:your )?appl|application (?:has been )?(?:received|submitted|complete)"
    r"|we(?:'| ha)?ve received your application|successfully submitted|thanks for applying)\b"
)
_ALREADY_APPLIED_TEXT = _RX(
    r"\b(you(?:'| ha)?ve already applied|already applied (?:to|for)|application already (?:exists|submitted)"
    r"|you applied on)\b"
)
_DRAFT_TEXT = _RX(r"\b(continue (?:your )?application|resume (?:your )?application|saved (?:application|draft)|finish your application)\b")
_REVIEW_TEXT = _RX(r"\b(review your application|please review|review and submit|confirm your (?:details|application))\b")

_LOGIN_TEXT = _RX(r"\b(sign in|log ?in|welcome back|enter your password)\b")
_SIGNUP_TEXT = _RX(r"\b(create (?:an )?account|sign ?up|register|get started|new user)\b")
_SSO_TEXT = _RX(r"\b(continue with (?:google|linkedin|apple|microsoft|github)|single sign|sso|use your \w+ account)\b")
_OTP_TEXT = _RX(r"\b(one[- ]time (?:code|password)|verification code|enter the (?:6|four|six)[- ]digit|authenticator|\botp\b|2fa|two[- ]factor)\b")
_EMAIL_VERIFY_TEXT = _RX(r"\b(verify your email|check your (?:email|inbox)|confirmation (?:email|link) (?:has been )?sent|we(?:'| ha)?ve sent (?:you )?an email)\b")
_SESSION_EXPIRED_TEXT = _RX(r"\b(session (?:has )?expired|session timed out|please (?:log ?in|sign in) again|your session ended)\b")
_ERROR_TEXT = _RX(r"\b(something went wrong|internal server error|500 error|503|service unavailable|page not found|404)\b")

_APPLY_CONTROL = _RX(r"\b(apply|apply now|apply for this job|start (?:your )?application|submit (?:an )?application|i'?m interested)\b")
_SUBMIT_CONTROL = _RX(r"\b(submit application|submit|send application)\b")

_URL_LOGIN = _RX(r"/(login|signin|sign-in|auth|session/new)\b")
_URL_SIGNUP = _RX(r"/(signup|sign-up|register|join|create-account)\b")
_URL_SSO = _RX(r"(accounts\.google|login\.microsoftonline|linkedin\.com/oauth|okta\.com|auth0\.com|/sso)")
_URL_CONFIRM = _RX(r"/(confirmation|thanks|thank[-_]?you|submitted|success|complete)\b")
_URL_CAREERS = _RX(r"/(careers|jobs|opportunities|openings|work-with-us)\b")

#: Field names that only appear on a real application form.
_APPLICATION_FIELD = _RX(r"\b(resume|cv|cover letter|first name|last name|phone|linkedin|portfolio|why do you want)\b")
#: Field names typical of credential entry.
_CREDENTIAL_FIELD = _RX(r"\b(password|email address|username|e-?mail)\b")


def _control_names(obs: BrowserObservation) -> str:
    return " \n ".join(f"{c.role} {c.name}" for c in obs.controls).lower()


def _has_password_field(obs: BrowserObservation) -> bool:
    return any(
        c.role == "textbox" and re.search(r"password", c.name, re.IGNORECASE)
        for c in obs.controls
    )


def _score(hits: list[tuple[bool, float, str]]) -> tuple[float, list[str]]:
    total = 0.0
    evidence: list[str] = []
    for matched, weight, label in hits:
        if matched:
            total += weight
            evidence.append(label)
    return total, evidence


def classify(obs: BrowserObservation) -> Classification:
    """Classify `obs` using deterministic signals only. Never raises."""
    url = (obs.url or "").lower()
    text = (obs.visible_text or "")
    controls = _control_names(obs)
    haystack = f"{obs.title}\n{text}"

    # --- blocking takes absolute precedence ---------------------------
    # A page that shows a CAPTCHA *and* a confirmation banner is blocked.
    # Reading it as success is how an agent records a submission that
    # never happened (see submit/verify.py, same precedence).
    if _BLOCKING_TEXT.search(haystack) or _has_captcha_control(obs):
        return Classification(PageState.CAPTCHA, 0.95, ["captcha signal present"])
    if _BOT_TEXT.search(haystack):
        return Classification(PageState.BOT_DETECTION, 0.85, ["bot-detection wording"])
    if _DENIED_TEXT.search(haystack):
        return Classification(PageState.ACCESS_DENIED, 0.8, ["access-denied wording"])

    scores: dict[PageState, tuple[float, list[str]]] = {}

    def put(
        state: PageState,
        hits: list[tuple[bool, float, str]],
        *,
        corroborating: list[tuple[bool, float, str]] | None = None,
    ) -> None:
        """`hits` are primary signals — something is affirmatively present.

        `corroborating` signals only count once at least one primary signal
        fired. They are typically *absences* ("no form on the page"), and an
        absence is not evidence on its own: a blank page has no form either,
        and letting that accumulate score is how an empty observation ends
        up looking like a confirmation page.
        """
        total, ev = _score(hits)
        if total <= 0:
            return
        if corroborating:
            extra, extra_ev = _score(corroborating)
            total += extra
            ev = ev + extra_ev
        scores[state] = (total, ev)

    put(
        PageState.SUBMISSION_CONFIRMATION,
        [
            (bool(_CONFIRMATION_TEXT.search(haystack)), 0.7, "confirmation wording"),
            (bool(_URL_CONFIRM.search(url)), 0.4, "confirmation URL"),
        ],
        corroborating=[(not obs.has_form, 0.15, "no form present")],
    )
    put(PageState.ALREADY_APPLIED, [
        (bool(_ALREADY_APPLIED_TEXT.search(haystack)), 0.85, "already-applied wording"),
    ])
    put(PageState.DRAFT_APPLICATION, [
        (bool(_DRAFT_TEXT.search(haystack)), 0.65, "resume-draft wording"),
    ])
    put(PageState.SESSION_EXPIRED, [
        (bool(_SESSION_EXPIRED_TEXT.search(haystack)), 0.85, "session-expired wording"),
    ])
    put(PageState.OTP, [
        (bool(_OTP_TEXT.search(haystack)), 0.75, "one-time-code wording"),
        (bool(_OTP_TEXT.search(controls)), 0.2, "otp field present"),
    ])
    put(PageState.EMAIL_VERIFICATION, [
        (bool(_EMAIL_VERIFY_TEXT.search(haystack)), 0.75, "email-verification wording"),
    ])
    put(PageState.SSO, [
        (bool(_URL_SSO.search(url)), 0.6, "SSO provider URL"),
        (bool(_SSO_TEXT.search(controls)), 0.35, "SSO button present"),
    ])
    put(PageState.LOGIN, [
        (_has_password_field(obs), 0.5, "password field present"),
        (bool(_URL_LOGIN.search(url)), 0.3, "login URL"),
        (bool(_LOGIN_TEXT.search(haystack)), 0.25, "sign-in wording"),
        (bool(_SIGNUP_TEXT.search(haystack)), -0.2, "signup wording (counter-signal)"),
    ])
    put(PageState.SIGNUP, [
        (bool(_URL_SIGNUP.search(url)), 0.45, "signup URL"),
        (bool(_SIGNUP_TEXT.search(haystack)), 0.4, "create-account wording"),
        (_has_password_field(obs), 0.2, "password field present"),
    ])
    put(
        PageState.APPLICATION_REVIEW,
        [(bool(_REVIEW_TEXT.search(haystack)), 0.6, "review wording")],
        corroborating=[(obs.has_form, 0.1, "form present")],
    )
    put(PageState.APPLICATION_FORM, [
        (obs.has_form and bool(_APPLICATION_FIELD.search(controls)), 0.6, "application fields present"),
        (any(c.role == "file" for c in obs.controls), 0.25, "file upload present"),
        (bool(_SUBMIT_CONTROL.search(controls)), 0.15, "submit control present"),
        (_has_password_field(obs), -0.35, "password field (counter-signal)"),
    ])
    put(
        PageState.APPLICATION_ENTRY,
        [(bool(_APPLY_CONTROL.search(controls)), 0.5, "apply control present")],
        corroborating=[(not obs.has_form, 0.1, "no form yet")],
    )
    put(PageState.CAREERS_PAGE, [
        (bool(_URL_CAREERS.search(url)), 0.3, "careers URL"),
        (sum(1 for c in obs.controls if c.role == "link") > 20, 0.2, "many job links"),
    ])
    put(PageState.ERROR, [
        (bool(_ERROR_TEXT.search(haystack)), 0.6, "error wording"),
    ])

    if not scores:
        return Classification(
            PageState.UNKNOWN, 0.0,
            ["no recognized signals"] + (["observation degraded"] if obs.degraded else []),
        )

    ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
    best_state, (best_score, best_evidence) = ranked[0]
    confidence = max(0.0, min(best_score, 1.0))

    # An observation that failed to gather its own inputs cannot support a
    # confident conclusion, however well the little it saw scored.
    if obs.degraded:
        confidence *= 0.6
        best_evidence = best_evidence + ["observation degraded"]

    alternatives = [(s, min(v[0], 1.0)) for s, v in ranked[1:4]]

    # Genuinely ambiguous: the top two are within noise of each other.
    if len(ranked) > 1:
        runner_up = ranked[1][1][0]
        if best_score - runner_up < 0.15 and best_score < 0.7:
            return Classification(
                PageState.UNKNOWN, confidence,
                best_evidence + [f"ambiguous: {best_state.value} vs {ranked[1][0].value}"],
                alternatives=[(best_state, min(best_score, 1.0))] + alternatives,
            )

    if confidence < 0.35:
        return Classification(
            PageState.UNKNOWN, confidence,
            best_evidence + [f"weak signals; best guess was {best_state.value}"],
            alternatives=[(best_state, min(best_score, 1.0))] + alternatives,
        )

    return Classification(best_state, confidence, best_evidence, alternatives)


def _has_captcha_control(obs: BrowserObservation) -> bool:
    blob = f"{obs.aria_snapshot}\n{_control_names(obs)}"
    return bool(_RX(r"recaptcha|hcaptcha|g-recaptcha|captcha").search(blob))


def requires_stop(state: PageState) -> bool:
    """Whether the agent must halt this application rather than act."""
    return state in TERMINAL_BLOCKING


def requires_auth(state: PageState) -> bool:
    return state in AUTH_STATES

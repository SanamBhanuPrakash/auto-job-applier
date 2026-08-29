"""Evidence-based submission verification.

The rule this module exists to enforce:

    A submit click that did not raise is NOT evidence of submission.

Previously `click_submit()` returning normally set status="submitted"
outright, which records success for a validation failure, a multi-step
form whose "Continue" button matched the submit selector, a CAPTCHA
interstitial, or a silently-failed XHR. That produces the worst possible
outcome for a job seeker: believing you applied when you did not.

Verification here is deliberately conservative and asymmetric:
  - positive evidence is required to return SUBMITTED
  - contradicting evidence (visible validation errors, the form still
    sitting there fully intact) returns NOT_SUBMITTED
  - a blocking wall returns BLOCKED
  - anything else returns UNKNOWN

UNKNOWN is never upgraded to SUBMITTED. It routes to human review, because
guessing "probably fine" is how a duplicate application gets made and
guessing "probably failed" is how a real application gets lost.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum

from jobbot.submit.form_scan import FrameLike

log = logging.getLogger(__name__)


class SubmissionVerdict(str, Enum):
    SUBMITTED = "SUBMITTED"
    NOT_SUBMITTED = "NOT_SUBMITTED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass
class VerificationResult:
    verdict: SubmissionVerdict
    #: Human-readable, machine-storable reasons behind the verdict. This is
    #: what makes a wrong verdict diagnosable after the fact without
    #: re-running the application.
    evidence: list[str] = field(default_factory=list)
    confirmation_text: str = ""
    final_url: str = ""

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "evidence": self.evidence,
            "confirmation_text": self.confirmation_text[:500],
            "final_url": self.final_url,
        }


# --- signal vocabularies ---------------------------------------------------

_CONFIRMATION_TEXT = re.compile(
    r"\b("
    r"thank you for (your )?(applying|application|your interest)"
    r"|application (has been )?(received|submitted|complete)"
    r"|your application was submitted"
    r"|we('| ha)?ve received your application"
    r"|successfully submitted"
    r"|thanks for applying"
    r")\b",
    re.IGNORECASE,
)

_CONFIRMATION_URL = re.compile(
    r"(/confirmation|/thanks|/thank[-_]?you|/submitted|/success|application_confirmation)",
    re.IGNORECASE,
)

#: Text indicating the submit was rejected and the form is still live.
_VALIDATION_ERROR = re.compile(
    r"\b("
    r"(this )?field is required"
    r"|please (complete|fill|correct|enter|provide)"
    r"|required field"
    r"|is required\b"
    r"|invalid (email|phone|input|format)"
    r"|there (was|were) (an? )?(error|problem)"
    r"|please fix"
    r")\b",
    re.IGNORECASE,
)

#: Walls we must never attempt to defeat. Detecting these is the point;
#: bypassing them is exactly the behaviour that gets accounts flagged.
_BLOCKING_SIGNAL = re.compile(
    r"\b("
    r"captcha|recaptcha|hcaptcha|cloudflare"
    r"|verify (you are|you're) (a )?human"
    r"|are you a robot"
    r"|unusual traffic|access denied|rate limit"
    r"|two[- ]factor|verification code|one[- ]time (code|password)"
    r"|please (log ?in|sign ?in) to (continue|apply)"
    r")\b",
    re.IGNORECASE,
)

_BLOCKING_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[src*='hcaptcha']",
    ".g-recaptcha",
    "#challenge-form",
    "[data-sitekey]",
)


def _safe_text(page) -> str:
    try:
        return page.inner_text("body", timeout=3000) or ""
    except Exception:  # noqa: BLE001 - page may be closed/navigating
        return ""


def _safe_url(page) -> str:
    try:
        return page.url or ""
    except Exception:  # noqa: BLE001
        return ""


def _count(ctx: FrameLike, selector: str) -> int:
    try:
        return ctx.locator(selector).count()
    except Exception:  # noqa: BLE001
        return 0


def detect_blocking(page, ctx: FrameLike | None = None) -> str | None:
    """Return a description of any CAPTCHA/MFA/bot wall present, else None."""
    for selector in _BLOCKING_SELECTORS:
        if _count(page, selector) > 0:
            return f"blocking element present: {selector}"
    if ctx is not None and ctx is not page:
        for selector in _BLOCKING_SELECTORS:
            if _count(ctx, selector) > 0:
                return f"blocking element present in form frame: {selector}"

    match = _BLOCKING_SIGNAL.search(_safe_text(page))
    if match:
        return f"blocking text present: {match.group(0)!r}"
    return None


def verify_submission(
    page,
    form_ctx: FrameLike,
    *,
    url_before: str,
    submit_selector: str = "",
    settle_ms: int = 3000,
) -> VerificationResult:
    """Inspect the post-click page and classify what actually happened."""
    try:
        page.wait_for_timeout(settle_ms)
    except Exception:  # noqa: BLE001
        pass

    url_after = _safe_url(page)
    body_text = _safe_text(page)
    evidence: list[str] = []

    # 1. A wall takes precedence: we are neither submitted nor free to retry.
    blocking = detect_blocking(page, form_ctx)
    if blocking:
        evidence.append(blocking)
        return VerificationResult(SubmissionVerdict.BLOCKED, evidence, final_url=url_after)

    # 2. Signals are graded, and the grades are not interchangeable.
    #
    #    STRONG  = the page affirmatively says the application was received
    #              (confirmation wording, or a confirmation URL).
    #    WEAK    = the page merely changed (navigation, form gone, button
    #              gone). Every one of these is equally consistent with a
    #              failed submit that redirected to a careers homepage, so
    #              no amount of weak evidence is allowed to add up to
    #              proof. Only a STRONG signal can return SUBMITTED.
    strong: list[str] = []
    weak: list[str] = []
    confirmation_text = ""

    text_match = _CONFIRMATION_TEXT.search(body_text)
    if text_match:
        confirmation_text = text_match.group(0)
        strong.append(f"confirmation text: {confirmation_text!r}")

    if _CONFIRMATION_URL.search(url_after):
        strong.append(f"confirmation URL pattern: {url_after}")

    navigated = bool(url_before) and bool(url_after) and url_after != url_before
    if navigated:
        weak.append(f"navigated {url_before} -> {url_after}")

    form_gone = _count(form_ctx, "form") == 0
    if form_gone:
        weak.append("application form no longer present")

    if submit_selector and _count(form_ctx, submit_selector) == 0:
        weak.append("submit control no longer present")

    evidence.extend(strong)
    evidence.extend(weak)

    # 3. Negative signals.
    error_match = _VALIDATION_ERROR.search(body_text)
    if error_match:
        evidence.append(f"validation error text: {error_match.group(0)!r}")
        # A visible validation error alongside a still-present form is
        # conclusive: the employer rejected the submit, nothing was sent.
        if not form_gone:
            return VerificationResult(
                SubmissionVerdict.NOT_SUBMITTED, evidence, final_url=url_after
            )

    if strong:
        return VerificationResult(
            SubmissionVerdict.SUBMITTED, evidence,
            confirmation_text=confirmation_text, final_url=url_after,
        )

    if not weak:
        # Nothing moved at all: the click did not take effect.
        evidence.append("page unchanged after submit click")
        return VerificationResult(SubmissionVerdict.NOT_SUBMITTED, evidence, final_url=url_after)

    # The page changed but never confirmed anything. Do not guess upward:
    # this routes to human review, which is recoverable, whereas a wrong
    # SUBMITTED is not.
    evidence.append(
        f"page changed but no confirmation signal ({len(weak)} weak signal(s), 0 strong)"
    )
    return VerificationResult(SubmissionVerdict.UNKNOWN, evidence, final_url=url_after)


#: How a verdict maps onto the state machine. SUBMITTED is the only verdict
#: that may record an application as sent.
VERDICT_IS_CONCLUSIVE_SUCCESS = {
    SubmissionVerdict.SUBMITTED: True,
    SubmissionVerdict.NOT_SUBMITTED: False,
    SubmissionVerdict.BLOCKED: False,
    SubmissionVerdict.FAILED: False,
    SubmissionVerdict.UNKNOWN: False,
}

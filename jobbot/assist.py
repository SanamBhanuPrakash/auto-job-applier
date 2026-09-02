"""Assisted apply — you drive, the agent fills.

This is a different product from `jobbot apply`, and a much more widely
useful one. `apply` runs the whole flow unattended, which requires a
submission handler written per ATS: today Greenhouse, Lever and Ashby, and
nothing else. That is why most Indian postings — Workday, Darwinbox, Keka,
Zoho Recruit, Naukri, or a company's own careers page — could not be
touched at all.

Assisted mode inverts the division of labour:

    you        open the posting, sign in if it asks, review, press Submit
    the agent  finds the form, fills everything it can, explains the rest

That inversion removes the per-ATS requirement entirely. **Filling a form
is generic** — `form_scan` reads whatever inputs exist through the
accessibility tree, and `filler` writes to them. Only *submitting*
unattended needed site-specific code, and in this mode you do that. So
assisted mode works on any form-shaped page, including every ATS this
project has no handler for.

Three properties are deliberate.

**The browser stays open and stays yours.** It runs headed, on your
persistent profile, so a session you already signed into is reused. The
agent never presses Submit here — not as a safety fig leaf, but because
that is the division of labour that makes the mode work on sites where
nobody has written a submit selector.

**Sensitive answers follow the same rules as everywhere else.** Work
authorization, sponsorship, EEO and legal attestations are filled only
from answers you gave in `jobbot setup`. A model guess never fills one,
here or anywhere.

**What it could not fill is reported, with the reason.** A list of
untouched fields is not useful; "this is a sensitive question and you have
not answered it in setup" and "this control is a custom widget the filler
could not drive" need different responses from you.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from jobbot.agent.observation import Detail, observe
from jobbot.agent.page_classify import PageState, classify
from jobbot.learning.provenance import may_autofill_sensitive
from jobbot.submit.fill_planner import build_fill_plan, is_sensitive
from jobbot.submit.form_scan import FieldSpec, find_target_frame, scan_form
from jobbot.submit.memory_plan import build_memory_plan

log = logging.getLogger(__name__)


@dataclass
class AssistResult:
    url: str = ""
    form_found: bool = False
    fields_seen: int = 0
    filled: list[tuple[str, str]] = field(default_factory=list)
    #: (label, why) for everything left for the human.
    left_for_you: list[tuple[str, str]] = field(default_factory=list)
    resume_uploaded: bool = False
    needs_login: bool = False
    blocked: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        total = len(self.filled) + len(self.left_for_you)
        return len(self.filled) / total if total else 0.0


#: Why a field was not filled. Each maps to a different action from you.
REASON_SENSITIVE_UNANSWERED = (
    "sensitive question with no answer from you — run `jobbot setup` to answer "
    "it once and it will fill automatically from then on"
)
REASON_SENSITIVE_LOCKED = (
    "you have answered this, but JOBBOT_AUTOFILL_SENSITIVE=false so saved "
    "answers are not reused"
)
REASON_NO_VALUE = "nothing in your profile answers this"
REASON_FILL_FAILED = "tried to fill it but the value did not stick — custom widget"
REASON_CIRCUIT_BROKEN = "this question has failed to fill before, so it is left to you"


def assist(
    page,
    url: str,
    profile,
    resume_path: Path | None,
    *,
    autofill_sensitive: bool,
    job_context: str = "",
    use_llm: bool = True,
) -> AssistResult:
    """Fill as much of the form at `url` as can be filled honestly.

    Assumes `page` is already navigated (the caller handles login pauses),
    so this function is pure form work and is testable against a fixture.
    """
    from jobbot.db import session_scope
    from jobbot.learning import store as learning_store
    from jobbot.submit.filler import apply_fill_plan, upload_resume

    result = AssistResult(url=url)

    observation = observe(page, None, detail=Detail.ARIA)
    classification = classify(observation)
    if classification.state in (PageState.CAPTCHA, PageState.BOT_DETECTION,
                                PageState.ACCESS_DENIED):
        result.blocked = classification.state.value
        result.notes.append(
            "This page is showing a CAPTCHA or bot check. Nothing here tries to get "
            "around one — solve it yourself and run assist again."
        )
        return result
    if classification.state in (PageState.LOGIN, PageState.SIGNUP, PageState.SSO,
                                PageState.OTP, PageState.SESSION_EXPIRED):
        result.needs_login = True
        result.notes.append(
            f"This looks like a {classification.state.value} page. Sign in in the "
            "browser window, get to the application form, then press Enter here."
        )
        return result

    try:
        form_ctx = find_target_frame(page, timeout_ms=12000)
        result.form_found = True
    except Exception:  # noqa: BLE001
        result.notes.append(
            "No form found on this page. If the application is behind an 'Apply' "
            "button, click it yourself and press Enter here to try again."
        )
        return result

    fields = scan_form(form_ctx)
    result.fields_seen = len(fields)
    if not fields:
        result.notes.append("Found a form but no fillable fields in it.")
        return result

    # --- resolve values, cheapest and most trustworthy first -------------
    with session_scope() as session:
        matches = learning_store.match_fields(session, fields)
        circuit_broken = {
            f.field_id for f in fields if learning_store.is_circuit_broken(session, f.label)
        }

    memory = build_memory_plan(
        fields, matches,
        autofill_sensitive=autofill_sensitive,
        value_still_offerable=learning_store.value_still_offerable,
    )

    from jobbot.submit.static_answers import resolve_static_fields

    static_plan = {
        fid: value for fid, value in resolve_static_fields(profile, fields).items()
        if fid not in memory.remembered and fid not in circuit_broken
    }

    remaining = [
        f for f in fields
        if f.field_id not in memory.remembered
        and f.field_id not in static_plan
        and f.field_id not in circuit_broken
        and f.field_type != "file"
    ]

    llm_plan: dict[int, dict] = {}
    if use_llm and remaining:
        try:
            llm_plan = build_fill_plan(profile, remaining, job_context)
        except Exception as exc:  # noqa: BLE001 - assist must survive an LLM outage
            log.warning("LLM planning failed, filling what we can without it: %s", exc)
            result.notes.append(f"LLM unavailable ({type(exc).__name__}); filled only "
                                "from your profile and remembered answers.")

    plan: dict[int, dict] = dict(static_plan)
    plan.update(llm_plan)
    plan.update(memory.remembered)  # your own answers outrank a model's

    # --- act --------------------------------------------------------------
    if resume_path and resume_path.exists():
        try:
            result.resume_uploaded = upload_resume(form_ctx, fields, resume_path)
        except Exception as exc:  # noqa: BLE001
            result.notes.append(f"Resume upload failed: {type(exc).__name__}")

    needs_human = apply_fill_plan(form_ctx, fields, plan)
    failed_ids = {f.field_id for f in needs_human}

    by_id = {f.field_id: f for f in fields}
    for field_id, entry in plan.items():
        spec = by_id.get(field_id)
        if spec is None or entry.get("needs_human") or not entry.get("value"):
            continue
        if field_id in failed_ids:
            continue
        result.filled.append((spec.label, str(entry["value"])))

    filled_labels = {label for label, _ in result.filled}

    for spec in fields:
        if spec.label in filled_labels or spec.field_type == "file":
            continue
        result.left_for_you.append((spec.label, _why(
            spec, memory, plan, circuit_broken, failed_ids, autofill_sensitive)))

    return result


def page_signature(page) -> str:
    """A cheap fingerprint of "which step of the application am I on".

    Workday, Darwinbox and Keka run the application as several steps —
    My Information, My Experience, Application Questions, Voluntary
    Disclosures, Review — and many of them do it without changing the URL.
    Watching the URL alone would therefore miss every step transition, so
    the signature includes the set of field labels on screen.
    """
    try:
        # Playwright keeps returning the last URL after a page closes, so
        # asking for it is not enough to notice the browser is gone — the
        # watch loop would then keep hashing a dead page forever. Check
        # explicitly, and return "" to mean "no signature available".
        if page.is_closed():
            return ""
        url = page.url or ""
    except Exception:  # noqa: BLE001
        return ""
    try:
        labels = page.evaluate(
            """() => [...document.querySelectorAll('label, [role=combobox], [aria-label]')]
                   .slice(0, 60)
                   .map(e => (e.getAttribute('aria-label') || e.textContent || '').trim())
                   .filter(Boolean).join('|')"""
        )
    except Exception:  # noqa: BLE001
        labels = ""
    import hashlib
    return hashlib.sha256(f"{url}||{labels}".encode("utf-8")).hexdigest()[:16]


def _why(spec: FieldSpec, memory, plan: dict, circuit_broken: set,
         failed_ids: set, autofill_sensitive: bool) -> str:
    if spec.field_id in failed_ids:
        return REASON_FILL_FAILED
    if spec.field_id in circuit_broken:
        return REASON_CIRCUIT_BROKEN
    if is_sensitive(spec.label):
        if spec.field_id in memory.hints:
            return REASON_SENSITIVE_LOCKED if not autofill_sensitive else REASON_NO_VALUE
        return REASON_SENSITIVE_UNANSWERED
    entry = plan.get(spec.field_id)
    if entry and entry.get("needs_human"):
        return entry.get("reasoning") or REASON_NO_VALUE
    return REASON_NO_VALUE

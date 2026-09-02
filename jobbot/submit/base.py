"""Orchestrates one application attempt as an explicit, crash-safe state machine.

  claim (race-free) -> OPENING_APPLICATION -> INSPECTING_FORM -> FILLING
  -> VERIFYING_FIELDS -> READY_TO_SUBMIT -> SUBMITTING -> VERIFYING_SUBMISSION
  -> SUBMITTED | HUMAN_REVIEW | BLOCKED | UNKNOWN

Field values are resolved cheapest-first: static profile mappings, then
remembered answers, then (only for what's left) the LLM.

Two orderings in here are load-bearing and should not be "tidied up":

1. In the unattended path, `SUBMITTING` is written to the database *before*
   the submit button is clicked. If the process dies mid-click, the row
   proves we were about to act, so recovery escalates to a human instead
   of blindly re-applying. (In the interactive path the click is the
   human's, at a time we don't control, so we instead enter SUBMITTING the
   moment we detect it — see `_await_human_submit`.)

2. The outcome comes from inspecting the resulting page
   (jobbot/submit/verify.py), never from "the click didn't raise" or "the
   page navigated". Navigation and the submit button disappearing are
   equally consistent with a validation failure that re-rendered, so they
   are treated as a trigger to verify, not as proof. UNKNOWN is never
   upgraded to SUBMITTED.

Uses a persistent browser profile so cookies and any manual login survive
between runs; a fresh profile per run re-triggers 2FA/CAPTCHA every time.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright

from jobbot.agent import statestore
from jobbot.agent.states import ApplicationState, FailureCategory
from jobbot.config import get_settings, load_profile_raw
from jobbot.db import session_scope
from jobbot.learning import store as learning_store
from jobbot.learning.provenance import may_autofill_sensitive
from jobbot.models import Application, Job
from jobbot.resume import multi as multi_resume
from jobbot.resume.schema import Profile
from jobbot.submit import ashby, greenhouse, lever
from jobbot.submit.ats_detect import detect_ats
from jobbot.submit.fill_planner import build_fill_plan
from jobbot.submit.filler import apply_fill_plan, upload_resume
from jobbot.submit.form_scan import find_target_frame, scan_form
from jobbot.submit.memory_plan import build_memory_plan
from jobbot.submit.review import show_review, wait_for_submit_or_close
from jobbot.submit.static_answers import resolve_static_fields
from jobbot.submit.takeover_bridge import reach_application_form
from jobbot.submit.verify import SubmissionVerdict, detect_blocking, verify_submission

log = logging.getLogger(__name__)

_ATS_MODULES = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby}


class UnsupportedATS(ValueError):
    """No submission handler exists for this job's ATS.

    Typed (rather than a bare ValueError raised mid-batch) so a batch run
    can skip one unsupported posting instead of aborting every remaining
    application queued behind it.
    """


def _user_data_dir(worker_id: str = "") -> Path:
    """The Chromium profile directory for this worker (spec §84).

    One shared directory across concurrent workers is a real corruption
    risk, not a theoretical one: Chromium takes a `SingletonLock` on a
    user-data dir, so a second `launch_persistent_context` against the
    same path either fails or silently attaches to the first browser's
    session — sharing cookies, and with them the logged-in identity and
    any rate-limit state. Each worker gets its own directory, and the
    default worker keeps the original path so existing profiles (and any
    manual login already saved there) still work.
    """
    settings = get_settings()
    name = "browser_profile" if not worker_id else f"browser_profile_{_safe_worker(worker_id)}"
    d = settings.data_dir / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_worker(worker_id: str) -> str:
    """Worker ids reach the filesystem, so they are constrained here rather
    than trusted. Anything outside [A-Za-z0-9_-] is replaced."""
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "_", worker_id).strip("_")
    return (cleaned or "worker")[:40]


def _resolve_profile_and_resume(job: Job) -> tuple[Profile, Path]:
    """Uses the resume `jobbot match` decided fits this job best, falling
    back to the single global config/profile.yaml + JOBBOT_RESUME_PATH."""
    settings = get_settings()
    if job.matched_profile_tag:
        row = multi_resume.get_profile(job.matched_profile_tag)
        if row is not None:
            return Profile.model_validate(row.profile_json), Path(row.resume_path)
        log.warning(
            "Job %d was matched to profile %r but it no longer exists; falling back to the default profile",
            job.id, job.matched_profile_tag,
        )
    return Profile.model_validate(load_profile_raw()), settings.jobbot_resume_path


def _park(app_id: int, state: ApplicationState, *, reason: str = "",
          category: FailureCategory | None = None, run_id: str = "",
          detail: dict | None = None) -> None:
    with session_scope() as session:
        app = session.get(Application, app_id)
        statestore.transition(
            session, app, state, reason=reason, run_id=run_id,
            failure_category=category, detail=detail,
        )


def _reload(app_id: int) -> Application:
    with session_scope() as session:
        return session.get(Application, app_id)


def apply_to_job(
    job: Job,
    *,
    run_id: str = "",
    auto_submit_override: bool | None = None,
    autofill_sensitive_override: bool | None = None,
    worker_id: str = "",
) -> Application | None:
    """Attempt one application. Returns None if the job could not be
    claimed (already done, held by another worker, or parked awaiting a
    human) — never a silent re-application."""
    settings = get_settings()
    ats = job.ats or detect_ats(job.url)
    if ats not in _ATS_MODULES:
        raise UnsupportedATS(
            f"No submission handler for ATS {ats!r} (job {job.id}). "
            f"Supported: {sorted(_ATS_MODULES)}. Apply manually for this one."
        )
    ats_module = _ATS_MODULES[ats]
    profile, resume_path = _resolve_profile_and_resume(job)
    autofill_sensitive = (
        settings.jobbot_autofill_sensitive
        if autofill_sensitive_override is None
        else autofill_sensitive_override
    )

    # --- claim: the database picks one winner, so two concurrent runs
    # cannot both proceed to apply to the same posting -------------------
    with session_scope() as session:
        claim = statestore.claim(session, job, run_id=run_id)
    if not claim.acquired:
        log.info(
            "Skipping job %d (%s @ %s): %s — %s",
            job.id, job.title, job.company, claim.outcome.value, claim.reason,
        )
        return None
    app_id = claim.application.id

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(_user_data_dir(worker_id)), headless=settings.jobbot_headless
        )
        page = context.new_page()
        try:
            _park(app_id, ApplicationState.ANALYZING, reason="resolved ATS + resume", run_id=run_id)
            _park(app_id, ApplicationState.ELIGIBILITY_CHECK, reason="pre-open checks", run_id=run_id)
            _park(app_id, ApplicationState.SELECTING_RESUME,
                  reason=f"resume={job.matched_profile_tag or 'default'}", run_id=run_id)
            _park(app_id, ApplicationState.OPENING_APPLICATION, reason=job.url, run_id=run_id)

            # Ashby serves the posting and the application form at
            # different URLs; opening the posting would find no form.
            target_url = getattr(ats_module, "apply_url", lambda u: u)(job.url)
            page.goto(target_url, wait_until="domcontentloaded", timeout=30000)

            # A wall here means we never even reach the form. Never try to
            # work around it — that is what gets accounts flagged.
            blocking = detect_blocking(page)
            if blocking:
                _park(app_id, ApplicationState.BLOCKED, reason=blocking,
                      category=FailureCategory.BLOCKED, run_id=run_id)
                return _reload(app_id)

            # --- authentication (§25) -----------------------------------
            # Plenty of postings put the form behind a sign-in. The auth
            # subsystem reads the credential, types it, and *verifies*; a
            # password never passes through the agent, a prompt or a trace.
            # Anything it will not do — CAPTCHA, SSO, a second factor with
            # nobody to ask — parks for a person rather than improvising.
            auth = _ensure_authenticated(page, job.url, app_id=app_id, run_id=run_id)
            if auth is not None and not auth.ok:
                _park(
                    app_id,
                    ApplicationState.BLOCKED if auth.outcome.value == "BLOCKED"
                    else ApplicationState.HUMAN_REVIEW,
                    reason=f"authentication: {auth.reason}"[:500],
                    category=(FailureCategory.BLOCKED if auth.outcome.value == "BLOCKED"
                              else FailureCategory.RECOVERABLE),
                    run_id=run_id, detail=auth.to_dict(),
                )
                return _reload(app_id)

            # A form may genuinely not be here yet: many postings put it
            # behind an "Apply" button, and some employers render it in an
            # iframe that hydrates late. A timeout is a signal to look
            # harder (agent takeover, below), not a reason to fail the
            # attempt outright.
            try:
                form_ctx = find_target_frame(page, ats_module.ATS_HINT)
                form_located = True
            except PlaywrightTimeoutError:
                log.info("No form found on %s within the timeout; will try agent takeover", job.url)
                form_ctx, form_located = page, False
            ats_module.settle(page)

            _park(app_id, ApplicationState.INSPECTING_FORM,
                  reason="form located" if form_located else "no form found yet", run_id=run_id)
            fields = scan_form(form_ctx) if form_located else []

            # --- agent takeover (§16) -----------------------------------
            # No fields means the deterministic path is not looking at the
            # application: the form is behind an Apply button, in another
            # frame, or under a consent overlay. Continuing from here would
            # walk an empty form all the way to READY_TO_SUBMIT, so this is
            # exactly the "unfamiliar situation before a fatal failure"
            # takeover exists for. The agent runs at NAVIGATE autonomy, so
            # it can move around but can neither fill nor submit.
            if not fields:
                handoff, form_ctx, fields = reach_application_form(
                    page, form_ctx, app_id=app_id, run_id=run_id,
                    ats_hint=ats_module.ATS_HINT,
                )
                if handoff is not None and not handoff.resolved:
                    _park(
                        app_id,
                        ApplicationState.BLOCKED if handoff.outcome.value == "BLOCKED"
                        else ApplicationState.HUMAN_REVIEW,
                        reason=f"agent takeover did not reach a form: {handoff.reason}"[:500],
                        category=handoff.failure_category or FailureCategory.RECOVERABLE,
                        run_id=run_id, detail=handoff.to_dict(),
                    )
                    return _reload(app_id)
            if not fields:
                # Takeover disabled, or it resolved but the re-scan still
                # found nothing. Never proceed to submit an empty form.
                _park(app_id, ApplicationState.HUMAN_REVIEW,
                      reason="no form fields found on the application page",
                      category=FailureCategory.RECOVERABLE, run_id=run_id)
                return _reload(app_id)
            # Kept short for the same reason matching/score.py truncates
            # descriptions — Groq's free-tier 6,000 tokens/minute cap.
            job_context = f"{job.title} at {job.company}\n\n{(job.description or '')[:800]}"

            # --- resolve fields from memory / circuit breaker ------------
            with session_scope() as session:
                matches = learning_store.match_fields(session, fields)
                circuit_broken_ids = {
                    f.field_id for f in fields if learning_store.is_circuit_broken(session, f.label)
                }

            memory = build_memory_plan(
                fields, matches,
                autofill_sensitive=autofill_sensitive,
                value_still_offerable=learning_store.value_still_offerable,
            )
            memory_hints = memory.hints
            auto_filled_sensitive = memory.auto_filled_sensitive
            remembered_plan = memory.remembered
            for label, why in memory.withheld:
                log.info("Not auto-filling %r: %s", label, why)

            circuit_broken_plan = {
                fid: {
                    "value": None,
                    "needs_human": True,
                    "reasoning": "This question has failed to auto-fill before; needs your input.",
                }
                for fid in circuit_broken_ids
                if fid not in remembered_plan
            }

            # Resolve the handful of near-universal fields (name, email,
            # phone, links, current company/title, school, ...) directly
            # from the profile before anything goes to the LLM at all — see
            # static_answers.py. Never overrides a remembered answer
            # (something you actually typed beats a generic mapping) or a
            # circuit-broken field.
            static_plan = {
                fid: v for fid, v in resolve_static_fields(profile, fields).items()
                if fid not in remembered_plan and fid not in circuit_broken_plan
            }

            llm_fields = [
                f for f in fields
                if f.field_id not in remembered_plan
                and f.field_id not in circuit_broken_plan
                and f.field_id not in static_plan
            ]

            _park(app_id, ApplicationState.FILLING,
                  reason=(f"{len(static_plan)} static, {len(remembered_plan)} from memory, "
                          f"{len(llm_fields)} to plan"),
                  run_id=run_id)

            injection_reports: list[dict] = []
            llm_plan = build_fill_plan(
                profile, llm_fields, job_context, injection_report=injection_reports,
            )
            # Only genuine model output counts as a guess for provenance;
            # static answers come straight from the candidate's own profile.
            model_filled_ids = {
                fid for fid, d in llm_plan.items() if not d["needs_human"] and d.get("value")
            }

            plan = dict(static_plan)
            plan.update(llm_plan)
            plan.update(remembered_plan)
            plan.update(circuit_broken_plan)

            if resume_path.exists():
                upload_resume(form_ctx, fields, resume_path)
            else:
                log.warning("Resume file %s not found — resume upload skipped", resume_path)

            needs_human = apply_fill_plan(form_ctx, fields, plan)
            _park(app_id, ApplicationState.VERIFYING_FIELDS,
                  reason=f"{len(needs_human)} field(s) need a human", run_id=run_id)

            # Fields we intended to auto-fill that still need a human are
            # genuine fill failures; feed the circuit breaker.
            attempted_ids = {fid for fid, d in plan.items() if not d["needs_human"] and d.get("value")}
            failed_specs = [f for f in needs_human if f.field_id in attempted_ids]
            if failed_specs:
                with session_scope() as session:
                    for f in failed_specs:
                        learning_store.record_failure(session, f.label, "fill did not verify after retry")

            screenshot_path = settings.data_dir / "screenshots" / f"application_{app_id}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)

            with session_scope() as session:
                app = session.get(Application, app_id)
                app.fill_plan = {str(k): v for k, v in plan.items()}
                app.fields_needing_human = {f.field_id: f.label for f in needs_human}
                app.screenshot_path = str(screenshot_path)
                session.commit()

            # Injection attempts are recorded on the transition, so they
            # are a measurable rate per run rather than a log line nobody
            # reads (§39). They never block on their own: a description
            # legitimately containing "ignore" must not fail an
            # application, and the actual defence is that page text has no
            # authority in the first place.
            suspicious = [r for r in injection_reports if r.get("suspicious")]
            ready_detail = {"screenshot": str(screenshot_path)}
            if suspicious:
                ready_detail["injection_signals"] = suspicious
                log.warning(
                    "Application %d: %d injection-shaped block(s) in this posting",
                    app_id, len(suspicious),
                )

            _park(app_id, ApplicationState.READY_TO_SUBMIT,
                  reason="form filled and verified", run_id=run_id,
                  detail=ready_detail)

            show_review(job, screenshot_path, needs_human, memory_hints, auto_filled_sensitive)

            auto_submit = settings.jobbot_auto_submit if auto_submit_override is None else auto_submit_override
            if auto_submit and needs_human:
                log.warning(
                    "auto_submit is on but %d field(s) still need human input; forcing manual review",
                    len(needs_human),
                )
                auto_submit = False

            if auto_submit:
                submitted_attempt = _auto_submit(page, form_ctx, ats_module, job, app_id, run_id=run_id)
            else:
                submitted_attempt = _await_human_submit(
                    page, form_ctx, ats_module, job, fields, app_id,
                    run_id=run_id, model_filled_ids=model_filled_ids,
                )

            if not submitted_attempt:
                _capture_learning(page, form_ctx, fields, verified=False,
                                  model_filled_ids=model_filled_ids)
                _park(app_id, ApplicationState.SKIPPED,
                      reason="closed without submitting", run_id=run_id)
                return _reload(app_id)

            # --- verify what actually happened --------------------------
            _park(app_id, ApplicationState.VERIFYING_SUBMISSION,
                  reason="inspecting page for submission evidence", run_id=run_id)

            result = verify_submission(
                page, form_ctx,
                url_before=submitted_attempt,
                submit_selector=ats_module.SUBMIT_SELECTOR,
                # We got here by clicking that control, so it was there.
                # Saying so stops a later absence being read as a
                # selector mismatch instead of as progress.
                submit_present_before=True,
            )
            post_shot = settings.data_dir / "screenshots" / f"application_{app_id}_post.png"
            try:
                page.screenshot(path=str(post_shot), full_page=True)
            except Exception:  # noqa: BLE001 - page may be closed
                post_shot = Path("")

            _finalize_verdict(app_id, result, post_shot, run_id=run_id)
            _capture_learning(
                page, form_ctx, fields,
                verified=result.verdict is SubmissionVerdict.SUBMITTED,
                model_filled_ids=model_filled_ids,
            )
            return _reload(app_id)

        except Exception as exc:  # noqa: BLE001
            log.exception("Application attempt failed for job %d", job.id)
            with session_scope() as session:
                app = session.get(Application, app_id)
                current = ApplicationState(app.state)
                # A failure at or past SUBMITTING cannot be called a clean
                # failure — the employer may hold the application.
                in_danger = current in (ApplicationState.SUBMITTING,
                                        ApplicationState.VERIFYING_SUBMISSION)
                target = ApplicationState.UNKNOWN if in_danger else ApplicationState.FAILED
                category = (
                    FailureCategory.AMBIGUOUS if in_danger else FailureCategory.RECOVERABLE
                )
                app.error = str(exc)
                statestore.transition(
                    session, app, target, reason=str(exc)[:500],
                    run_id=run_id, failure_category=category,
                )
            return _reload(app_id)
        finally:
            # If the browser/context already died (crashed, or Playwright's
            # own action timeouts cascaded into it becoming unresponsive),
            # closing it can itself raise. That must never mask the real
            # outcome above: the except block already recorded the state and
            # returned cleanly, and a second exception from cleanup here
            # would otherwise replace that return and crash the whole call.
            try:
                context.close()
            except Exception:  # noqa: BLE001
                log.debug("context.close() failed (already closed) — ignoring", exc_info=True)


def _ensure_authenticated(page, url: str, *, app_id: int, run_id: str):
    """Run the auth subsystem against the current page.

    Returns None when authentication is switched off, so the caller
    behaves exactly as it did before this existed.
    """
    settings = get_settings()
    if not settings.jobbot_auth_enabled:
        return None

    from jobbot.auth.orchestrator import AuthOrchestrator, VerificationChannel
    from jobbot.auth.session import record_attempt, should_attempt

    allowed, why = should_attempt(url)
    if not allowed:
        from jobbot.auth.states import AuthOutcome, AuthState
        from jobbot.auth.orchestrator import AuthResult
        return AuthResult(AuthOutcome.HUMAN_REQUIRED, AuthState.LOGIN_REQUIRED,
                          reason=why, resumable_by_human=True)

    orchestrator = AuthOrchestrator(
        # An unattended run has nobody to ask for a one-time code, and
        # there is no legitimate way to obtain one otherwise. Attended
        # runs pass a prompt in through the CLI.
        verification=VerificationChannel(prompt=_OTP_PROMPT),
        allow_signup=settings.jobbot_allow_signup,
        allowed_signup_domains=tuple(
            d.strip() for d in (settings.jobbot_signup_domains or "").split(",") if d.strip()
        ),
    )
    result = orchestrator.ensure_authenticated(page, url=url)
    record_attempt(url, result, profile_dir=str(_user_data_dir()))
    log.info("Application %d: authentication %s (%s)", app_id, result.outcome.value,
             result.reason[:120])
    return result


#: Set by the CLI for attended runs. Left None otherwise, which makes the
#: verification channel report UNAVAILABLE rather than inventing a code.
_OTP_PROMPT = None


def set_otp_prompt(prompt) -> None:
    """Install a callable that asks the human for a one-time code."""
    global _OTP_PROMPT
    _OTP_PROMPT = prompt


def _auto_submit(page, form_ctx, ats_module, job: Job, app_id: int, *, run_id: str) -> str | None:
    """Unattended submit. Returns the pre-click URL (to verify against) or
    None if no click was made.

    SUBMITTING is persisted *before* the click: nobody is watching an
    unattended run, so a crash here must be recoverable from the database
    alone.
    """
    if page.is_closed():
        _park(app_id, ApplicationState.FAILED,
              reason="Browser window closed before Submit could be clicked.",
              category=FailureCategory.RECOVERABLE, run_id=run_id)
        return None

    url_before = page.url
    _park(app_id, ApplicationState.SUBMITTING, reason="about to click submit",
          run_id=run_id, detail={"url_before": url_before})
    try:
        ats_module.click_submit(form_ctx)
    except Exception as exc:  # noqa: BLE001
        # The click raised, but we cannot assume nothing was sent — fall
        # through to verification rather than declaring failure.
        log.warning("Submit click raised for job %d: %s", job.id, exc)
    return url_before


def _await_human_submit(
    page, form_ctx, ats_module, job: Job, fields, app_id: int, *,
    run_id: str, model_filled_ids: set[int],
) -> str | None:
    """Watch the browser for the candidate submitting (or closing) it.

    Returns the pre-submit URL if a submit appears to have happened, else
    None. Note the return of `wait_for_submit_or_close` is only a *trigger
    to verify*: it concludes "submitted" from navigation or the submit
    button disappearing, and both are equally consistent with a validation
    error that re-rendered the form. The verdict comes from verify.py.

    Unlike the unattended path, SUBMITTING is entered on *detection* rather
    than before the click, because the click is the human's and happens at
    a time we do not control.
    """
    url_before = form_ctx.url if not page.is_closed() else ""
    outcome = wait_for_submit_or_close(
        page, form_ctx, ats_module, job, fields, model_filled_ids=model_filled_ids
    )
    if outcome != "submitted":
        return None
    _park(app_id, ApplicationState.SUBMITTING,
          reason="detected your submit in the browser", run_id=run_id,
          detail={"url_before": url_before})
    return url_before


def _capture_learning(page, form_ctx, fields, *, verified: bool, model_filled_ids: set[int]) -> None:
    try:
        if page.is_closed():
            return  # nothing to read; reading would just raise
        with session_scope() as session:
            learning_store.capture_from_page(
                session, form_ctx, fields,
                verified_submission=verified,
                model_filled_ids=model_filled_ids,
            )
    except Exception:  # noqa: BLE001 - learning must never break an application
        log.debug("Failed to capture learned answers (continuing)", exc_info=True)


#: How a verification verdict lands in the state machine. Only a positively
#: verified submission may be recorded as SUBMITTED.
_VERDICT_ROUTING: dict[SubmissionVerdict, tuple[ApplicationState, FailureCategory | None]] = {
    SubmissionVerdict.SUBMITTED: (ApplicationState.SUBMITTED, None),
    SubmissionVerdict.NOT_SUBMITTED: (ApplicationState.HUMAN_REVIEW, FailureCategory.RECOVERABLE),
    SubmissionVerdict.BLOCKED: (ApplicationState.BLOCKED, FailureCategory.BLOCKED),
    SubmissionVerdict.FAILED: (ApplicationState.HUMAN_REVIEW, FailureCategory.RECOVERABLE),
    SubmissionVerdict.UNKNOWN: (ApplicationState.UNKNOWN, FailureCategory.AMBIGUOUS),
}


def _finalize_verdict(app_id: int, result, post_shot: Path, *, run_id: str) -> None:
    target, category = _VERDICT_ROUTING[result.verdict]
    with session_scope() as session:
        app = session.get(Application, app_id)
        app.verification_verdict = result.verdict.value
        app.verification_evidence = result.as_dict()
        app.post_submit_screenshot = str(post_shot) if post_shot else ""
        statestore.transition(
            session, app, target,
            reason=f"verdict={result.verdict.value}: " + "; ".join(result.evidence)[:400],
            run_id=run_id, failure_category=category, detail=result.as_dict(),
        )
    if result.verdict is not SubmissionVerdict.SUBMITTED:
        log.warning(
            "Application %d NOT recorded as submitted (verdict=%s). Evidence: %s",
            app_id, result.verdict.value, "; ".join(result.evidence),
        )


def apply_to_jobs(
    jobs: list[Job],
    *,
    pacing_min: float,
    pacing_max: float,
    run_id: str = "",
    auto_submit_override: bool | None = None,
    autofill_sensitive_override: bool | None = None,
    worker_id: str = "",
) -> list[Application]:
    from jobbot.utils.ratelimit import human_pause

    results: list[Application] = []
    for i, job in enumerate(jobs):
        log.info("Applying to job %d/%d: %s @ %s", i + 1, len(jobs), job.title, job.company)
        try:
            outcome = apply_to_job(
                job,
                run_id=run_id,
                auto_submit_override=auto_submit_override,
                autofill_sensitive_override=autofill_sensitive_override,
                worker_id=worker_id,
            )
        except UnsupportedATS as exc:
            # One unsupported posting must not abort every application
            # queued behind it.
            log.warning("Skipping job %d: %s", job.id, exc)
            continue
        if outcome is not None:
            results.append(outcome)
        if i < len(jobs) - 1:
            human_pause(pacing_min, pacing_max)
    return results

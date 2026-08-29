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
from pathlib import Path

from playwright.sync_api import sync_playwright

from jobbot.agent import statestore
from jobbot.agent.states import ApplicationState, FailureCategory
from jobbot.config import get_settings, load_profile_raw
from jobbot.db import session_scope
from jobbot.learning import store as learning_store
from jobbot.learning.provenance import may_autofill_sensitive
from jobbot.models import Application, Job
from jobbot.resume import multi as multi_resume
from jobbot.resume.schema import Profile
from jobbot.submit import greenhouse, lever
from jobbot.submit.ats_detect import detect_ats
from jobbot.submit.fill_planner import build_fill_plan
from jobbot.submit.filler import apply_fill_plan, upload_resume
from jobbot.submit.form_scan import find_target_frame, scan_form
from jobbot.submit.review import show_review, wait_for_submit_or_close
from jobbot.submit.static_answers import resolve_static_fields
from jobbot.submit.verify import SubmissionVerdict, detect_blocking, verify_submission

log = logging.getLogger(__name__)

_ATS_MODULES = {"greenhouse": greenhouse, "lever": lever}


class UnsupportedATS(ValueError):
    """No submission handler exists for this job's ATS.

    Typed (rather than a bare ValueError raised mid-batch) so a batch run
    can skip one unsupported posting instead of aborting every remaining
    application queued behind it.
    """


def _user_data_dir() -> Path:
    settings = get_settings()
    d = settings.data_dir / "browser_profile"
    d.mkdir(exist_ok=True)
    return d


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
            str(_user_data_dir()), headless=settings.jobbot_headless
        )
        page = context.new_page()
        try:
            _park(app_id, ApplicationState.ANALYZING, reason="resolved ATS + resume", run_id=run_id)
            _park(app_id, ApplicationState.ELIGIBILITY_CHECK, reason="pre-open checks", run_id=run_id)
            _park(app_id, ApplicationState.SELECTING_RESUME,
                  reason=f"resume={job.matched_profile_tag or 'default'}", run_id=run_id)
            _park(app_id, ApplicationState.OPENING_APPLICATION, reason=job.url, run_id=run_id)

            page.goto(job.url, wait_until="domcontentloaded", timeout=30000)

            # A wall here means we never even reach the form. Never try to
            # work around it — that is what gets accounts flagged.
            blocking = detect_blocking(page)
            if blocking:
                _park(app_id, ApplicationState.BLOCKED, reason=blocking,
                      category=FailureCategory.BLOCKED, run_id=run_id)
                return _reload(app_id)

            form_ctx = find_target_frame(page, ats_module.ATS_HINT)
            ats_module.settle(page)

            _park(app_id, ApplicationState.INSPECTING_FORM, reason="form located", run_id=run_id)
            fields = scan_form(form_ctx)
            # Kept short for the same reason matching/score.py truncates
            # descriptions — Groq's free-tier 6,000 tokens/minute cap.
            job_context = f"{job.title} at {job.company}\n\n{(job.description or '')[:800]}"

            # --- resolve fields from memory / circuit breaker ------------
            with session_scope() as session:
                matches = learning_store.match_fields(session, fields)
                by_id = {f.field_id: f for f in fields}
                learned: dict[int, dict] = {}
                for fid, m in matches.items():
                    field = by_id[fid]
                    if not learning_store.value_still_offerable(field, m.value):
                        continue  # this posting doesn't offer the remembered option
                    learned[fid] = {
                        "value": m.value,
                        "sensitive": m.sensitive,
                        "times_used": m.times_used,
                        "trusted": may_autofill_sensitive(m.provenance or "", bool(m.human_confirmed)),
                    }
                circuit_broken_ids = {
                    f.field_id for f in fields if learning_store.is_circuit_broken(session, f.label)
                }

            memory_hints = {fid: h["value"] for fid, h in learned.items() if h["sensitive"]}
            by_field_id = {f.field_id: f for f in fields}

            def _may_autofill(hint: dict) -> bool:
                if not hint["sensitive"]:
                    return True
                # A sensitive question is only auto-answered when the run
                # authorized it AND the remembered value is actually
                # trustworthy — a model guess never qualifies, however
                # many times it has been reused.
                return autofill_sensitive and hint["trusted"]

            auto_filled_sensitive = [
                (by_field_id[fid].label, h["value"])
                for fid, h in learned.items()
                if h["sensitive"] and _may_autofill(h)
            ]

            remembered_plan: dict[int, dict] = {}
            for fid, h in learned.items():
                if not _may_autofill(h):
                    continue
                remembered_plan[fid] = {
                    "value": h["value"],
                    "needs_human": False,
                    "reasoning": (
                        f"Auto-filled from your confirmed answer (used {h['times_used']} time(s) before)"
                        + (" — sensitive-field autofill is enabled." if h["sensitive"] else ".")
                    ),
                }

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

            llm_plan = build_fill_plan(profile, llm_fields, job_context)
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

            _park(app_id, ApplicationState.READY_TO_SUBMIT,
                  reason="form filled and verified", run_id=run_id,
                  detail={"screenshot": str(screenshot_path)})

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

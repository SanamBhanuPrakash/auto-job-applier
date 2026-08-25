"""Orchestrates one application attempt end to end:

  pick profile (per-job matched resume, or the global default) -> open
  browser -> scan form -> resolve learned/circuit-broken fields -> LLM fill
  plan for the rest -> fill+verify -> screenshot -> human review -> (only on
  explicit "yes", or auto_submit) -> submit -> capture what was learned ->
  log

Uses a persistent browser profile (launch_persistent_context) so cookies and
any manual login you do survive between runs, per the research's note that a
fresh profile per run causes repeated 2FA/CAPTCHA challenges.
"""
from __future__ import annotations

import logging
from pathlib import Path

from playwright.sync_api import sync_playwright

from jobbot.config import get_settings, load_profile_raw
from jobbot.db import session_scope
from jobbot.learning import store as learning_store
from jobbot.models import Application, Job
from jobbot.resume import multi as multi_resume
from jobbot.resume.schema import Profile
from jobbot.submit import greenhouse, lever
from jobbot.submit.ats_detect import detect_ats
from jobbot.submit.fill_planner import build_fill_plan
from jobbot.submit.filler import apply_fill_plan, upload_resume
from jobbot.submit.form_scan import find_target_frame, scan_form
from jobbot.submit.review import confirm_submit, show_review

log = logging.getLogger(__name__)

_ATS_MODULES = {"greenhouse": greenhouse, "lever": lever}


def _user_data_dir() -> Path:
    settings = get_settings()
    d = settings.data_dir / "browser_profile"
    d.mkdir(exist_ok=True)
    return d


def _resolve_profile_and_resume(job: Job) -> tuple[Profile, Path]:
    """Uses the resume/profile `jobbot match` decided fits this job best
    (config/resumes/<tag>), falling back to the single global
    config/profile.yaml + JOBBOT_RESUME_PATH when no resumes/ folder has
    been imported or nothing was matched for this job."""
    settings = get_settings()
    if job.matched_profile_tag:
        row = multi_resume.get_profile(job.matched_profile_tag)
        if row is not None:
            return Profile.model_validate(row.profile_json), Path(row.resume_path)
        log.warning(
            "Job %d was matched to profile %r but it no longer exists; falling back to the default profile",
            job.id,
            job.matched_profile_tag,
        )
    return Profile.model_validate(load_profile_raw()), settings.jobbot_resume_path


def apply_to_job(
    job: Job,
    *,
    auto_submit_override: bool | None = None,
    autofill_sensitive_override: bool | None = None,
) -> Application:
    settings = get_settings()
    ats = job.ats or detect_ats(job.url)
    if ats not in _ATS_MODULES:
        raise ValueError(
            f"No submission handler for ATS {ats!r} (job {job.id}). "
            f"Supported: {list(_ATS_MODULES)}. Apply manually for this one."
        )
    ats_module = _ATS_MODULES[ats]
    profile, resume_path = _resolve_profile_and_resume(job)
    autofill_sensitive = (
        settings.jobbot_autofill_sensitive if autofill_sensitive_override is None else autofill_sensitive_override
    )

    with session_scope() as session:
        application = Application(job_id=job.id, status="attempted")
        session.add(application)
        session.flush()
        app_id = application.id

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            str(_user_data_dir()), headless=settings.jobbot_headless
        )
        page = context.new_page()
        try:
            page.goto(job.url, wait_until="domcontentloaded", timeout=30000)
            # Most hosted-apply pages have the form at the top level; some
            # employers embed it in an iframe on their own branded careers
            # page instead — this finds whichever one actually has it.
            form_ctx = find_target_frame(page, ats_module.ATS_HINT)
            ats_module.settle(page)

            fields = scan_form(form_ctx)
            # Kept short for the same reason matching/score.py truncates
            # descriptions — Groq's free-tier 6,000 tokens/minute cap.
            job_context = f"{job.title} at {job.company}\n\n{(job.description or '')[:800]}"

            with session_scope() as session:
                matches = learning_store.match_fields(session, fields)
                by_id = {f.field_id: f for f in fields}
                learned = {}
                for fid, m in matches.items():
                    field = by_id[fid]
                    if not learning_store.value_still_offerable(field, m.value):
                        continue  # this posting's options don't include the remembered value — don't guess
                    learned[fid] = {"value": m.value, "sensitive": m.sensitive, "times_used": m.times_used}

                circuit_broken_ids = {
                    f.field_id for f in fields if learning_store.is_circuit_broken(session, f.label)
                }

            memory_hints = {fid: h["value"] for fid, h in learned.items() if h["sensitive"]}
            by_field_id = {f.field_id: f for f in fields}
            auto_filled_sensitive = [
                (by_field_id[fid].label, h["value"])
                for fid, h in learned.items()
                if h["sensitive"] and autofill_sensitive
            ]

            remembered_plan: dict[int, dict] = {}
            for fid, h in learned.items():
                if h["sensitive"] and not autofill_sensitive:
                    continue  # shown as a hint only — see memory_hints above
                remembered_plan[fid] = {
                    "value": h["value"],
                    "needs_human": False,
                    "reasoning": (
                        f"Auto-filled from your confirmed answer (used {h['times_used']} time(s) before)"
                        + (" — sensitive-field autofill is enabled." if h["sensitive"] else ".")
                    ),
                }

            circuit_broken_plan = {
                fid: {"value": None, "needs_human": True, "reasoning": "This question has failed to auto-fill before; needs your input."}
                for fid in circuit_broken_ids
                if fid not in remembered_plan
            }

            llm_fields = [
                f for f in fields
                if f.field_id not in remembered_plan and f.field_id not in circuit_broken_plan
            ]

            plan = build_fill_plan(profile, llm_fields, job_context)
            plan.update(remembered_plan)
            plan.update(circuit_broken_plan)

            if resume_path.exists():
                upload_resume(form_ctx, fields, resume_path)
            else:
                log.warning("Resume file %s not found — resume upload skipped", resume_path)

            needs_human = apply_fill_plan(form_ctx, fields, plan)

            # Fields we planned to auto-fill (not flagged, had a value) that
            # still ended up needing a human are genuine fill failures —
            # feed the circuit breaker so repeats of this exact question
            # stop being retried automatically.
            attempted_ids = {fid for fid, d in plan.items() if not d["needs_human"] and d.get("value")}
            failed_specs = [f for f in needs_human if f.field_id in attempted_ids]
            if failed_specs:
                with session_scope() as session:
                    for f in failed_specs:
                        learning_store.record_failure(session, f.label, "fill did not verify after retry")

            screenshot_path = settings.data_dir / "screenshots" / f"application_{app_id}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)

            show_review(job, screenshot_path, needs_human, memory_hints, auto_filled_sensitive)

            auto_submit = settings.jobbot_auto_submit if auto_submit_override is None else auto_submit_override
            if auto_submit and needs_human:
                log.warning(
                    "auto_submit is on but %d field(s) still need human input; forcing manual review",
                    len(needs_human),
                )
                auto_submit = False

            should_submit = auto_submit or confirm_submit(job)

            # Capture whatever ended up in the form (auto-filled or typed by
            # you) before the submit click potentially navigates the page
            # away, so the next application to ask the same question already
            # knows the answer.
            with session_scope() as session:
                learning_store.capture_from_page(session, form_ctx, fields)

            status = "skipped"
            error = ""
            if should_submit:
                try:
                    ats_module.click_submit(form_ctx)
                    page.wait_for_timeout(2000)
                    status = "submitted"
                except Exception as exc:  # noqa: BLE001
                    status = "error"
                    error = str(exc)
                    log.exception("Submit click failed for job %d", job.id)
            else:
                status = "filled_pending_review"

            with session_scope() as session:
                app = session.get(Application, app_id)
                app.status = status
                app.error = error
                app.screenshot_path = str(screenshot_path)
                app.fill_plan = {str(k): v for k, v in plan.items()}
                app.fields_needing_human = {f.field_id: f.label for f in needs_human}
                session.add(app)

            return app

        except Exception as exc:  # noqa: BLE001
            log.exception("Application attempt failed for job %d", job.id)
            with session_scope() as session:
                app = session.get(Application, app_id)
                app.status = "error"
                app.error = str(exc)
                session.add(app)
            return app
        finally:
            # If the browser/context already died (crashed, or Playwright's
            # own action timeouts cascaded into it becoming unresponsive —
            # see the caller for why that can happen), closing it can itself
            # raise. That must never mask the real outcome above: the
            # `except` block already recorded status="error" and returned
            # cleanly, and a second exception from cleanup here would
            # otherwise replace that return and crash the whole call.
            try:
                context.close()
            except Exception:  # noqa: BLE001
                log.debug("context.close() failed (already closed) — ignoring", exc_info=True)


def apply_to_jobs(
    jobs: list[Job],
    *,
    pacing_min: float,
    pacing_max: float,
    auto_submit_override: bool | None = None,
    autofill_sensitive_override: bool | None = None,
) -> list[Application]:
    from jobbot.utils.ratelimit import human_pause

    results = []
    for i, job in enumerate(jobs):
        log.info("Applying to job %d/%d: %s @ %s", i + 1, len(jobs), job.title, job.company)
        results.append(
            apply_to_job(
                job,
                auto_submit_override=auto_submit_override,
                autofill_sensitive_override=autofill_sensitive_override,
            )
        )
        if i < len(jobs) - 1:
            human_pause(pacing_min, pacing_max)
    return results

"""Orchestrates one application attempt end to end:

  detect ATS -> open browser -> scan form -> LLM fill plan -> fill+verify
  -> screenshot -> human review -> (only on explicit "yes") submit -> log

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
from jobbot.models import Application, Job
from jobbot.resume.schema import Profile
from jobbot.submit import greenhouse, lever
from jobbot.submit.ats_detect import detect_ats
from jobbot.submit.fill_planner import build_fill_plan
from jobbot.submit.filler import apply_fill_plan, upload_resume
from jobbot.submit.form_scan import scan_form
from jobbot.submit.review import confirm_submit, show_review

log = logging.getLogger(__name__)

_ATS_MODULES = {"greenhouse": greenhouse, "lever": lever}


def _user_data_dir() -> Path:
    settings = get_settings()
    d = settings.data_dir / "browser_profile"
    d.mkdir(exist_ok=True)
    return d


def apply_to_job(job: Job, *, auto_submit_override: bool | None = None) -> Application:
    settings = get_settings()
    ats = job.ats or detect_ats(job.url)
    if ats not in _ATS_MODULES:
        raise ValueError(
            f"No submission handler for ATS {ats!r} (job {job.id}). "
            f"Supported: {list(_ATS_MODULES)}. Apply manually for this one."
        )
    ats_module = _ATS_MODULES[ats]
    profile = Profile.model_validate(load_profile_raw())

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
            ats_module.wait_for_form(page)

            fields = scan_form(page)
            job_context = f"{job.title} at {job.company}\n\n{(job.description or '')[:2000]}"
            plan = build_fill_plan(profile, fields, job_context)

            if settings.jobbot_resume_path.exists():
                upload_resume(page, fields, settings.jobbot_resume_path)

            needs_human = apply_fill_plan(page, fields, plan)

            screenshot_path = settings.data_dir / "screenshots" / f"application_{app_id}.png"
            page.screenshot(path=str(screenshot_path), full_page=True)

            show_review(job, screenshot_path, needs_human)

            auto_submit = settings.jobbot_auto_submit if auto_submit_override is None else auto_submit_override
            if auto_submit and needs_human:
                log.warning(
                    "auto_submit is on but %d field(s) still need human input; forcing manual review",
                    len(needs_human),
                )
                auto_submit = False

            should_submit = auto_submit or confirm_submit(job)

            status = "skipped"
            error = ""
            if should_submit:
                try:
                    ats_module.click_submit(page)
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
            context.close()


def apply_to_jobs(jobs: list[Job], *, pacing_min: float, pacing_max: float) -> list[Application]:
    from jobbot.utils.ratelimit import human_pause

    results = []
    for i, job in enumerate(jobs):
        log.info("Applying to job %d/%d: %s @ %s", i + 1, len(jobs), job.title, job.company)
        results.append(apply_to_job(job))
        if i < len(jobs) - 1:
            human_pause(pacing_min, pacing_max)
    return results

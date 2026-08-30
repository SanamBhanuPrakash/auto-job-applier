"""Preflight checks — everything that has to be true before a real run.

This exists because the failure mode it prevents is expensive and silent:
you set the tool up, kick off a batch, walk away, and come back to a run
that did nothing because the resume path was wrong, or one that did
something you did not intend because auto-submit was on.

Each check answers one question, reports what it found rather than a bare
pass/fail, and says what to do about it. Nothing here touches the network
or a job board.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Status(str, Enum):
    OK = "OK"
    WARN = "WARN"      # usable, but you probably do not want it this way
    FAIL = "FAIL"      # a real run cannot work until this is fixed


@dataclass
class Check:
    name: str
    status: Status
    detail: str
    fix: str = ""


def _check_profile() -> Check:
    from jobbot.config import load_profile_raw

    try:
        raw = load_profile_raw()
    except Exception as exc:  # noqa: BLE001
        return Check("candidate profile", Status.FAIL, f"could not load: {exc}",
                     "copy config/profile.example.yaml to config/profile.yaml and fill it in")
    missing = [k for k in ("full_name", "email") if not raw.get(k)]
    if missing:
        return Check("candidate profile", Status.FAIL,
                     f"missing required field(s): {', '.join(missing)}",
                     "edit config/profile.yaml")
    return Check("candidate profile", Status.OK,
                 f"{raw.get('full_name')} <{raw.get('email')}>")


def _check_resume() -> Check:
    from jobbot.config import get_settings

    path = get_settings().jobbot_resume_path
    if not path:
        return Check("resume file", Status.FAIL, "JOBBOT_RESUME_PATH is not set",
                     "set JOBBOT_RESUME_PATH in .env, or import a folder with "
                     "`jobbot resume import-folder <dir>`")
    p = Path(path)
    if not p.exists():
        return Check("resume file", Status.FAIL, f"{p} does not exist",
                     "point JOBBOT_RESUME_PATH at a real PDF/DOCX")
    size_kb = p.stat().st_size // 1024
    if size_kb == 0:
        return Check("resume file", Status.FAIL, f"{p} is empty", "check the file")
    return Check("resume file", Status.OK, f"{p.name} ({size_kb} KB)")


def _check_resume_folder() -> Check:
    from jobbot.resume import multi

    try:
        profiles = multi.list_profiles()
    except Exception as exc:  # noqa: BLE001
        return Check("resume variants", Status.WARN, f"could not read: {exc}")
    if not profiles:
        return Check("resume variants", Status.WARN,
                     "no per-role resumes imported; every job uses the default resume",
                     "`jobbot resume import-folder ~/resumes` to let the tool pick per job")
    return Check("resume variants", Status.OK,
                 f"{len(profiles)} profile(s): " + ", ".join(p.tag for p in profiles[:6]))


def _check_llm() -> Check:
    from jobbot.config import get_settings
    from jobbot.llm import _provider_has_key

    settings = get_settings()
    available = [p for p in ("groq", "gemini", "anthropic") if _provider_has_key(settings, p)]
    if not available:
        return Check("LLM provider", Status.FAIL, "no API key configured",
                     "set GROQ_API_KEY, GEMINI_API_KEY or ANTHROPIC_API_KEY in .env")
    return Check("LLM provider", Status.OK, f"key(s) present: {', '.join(available)}")


def _check_browser() -> Check:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return Check("browser", Status.FAIL, "playwright is not installed",
                     "pip install -e '.[dev]'")
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            version = b.version
            b.close()
        return Check("browser", Status.OK, f"Chromium {version}")
    except Exception as exc:  # noqa: BLE001
        return Check("browser", Status.FAIL, f"Chromium will not launch: {str(exc)[:120]}",
                     "run `playwright install chromium`")


def _check_database() -> Check:
    from jobbot.config import get_settings
    from jobbot.db import session_scope
    from jobbot.models import Application, Job
    from sqlalchemy import func, select

    try:
        with session_scope() as session:
            jobs = session.execute(select(func.count()).select_from(Job)).scalar_one()
            apps = session.execute(select(func.count()).select_from(Application)).scalar_one()
    except Exception as exc:  # noqa: BLE001
        return Check("database", Status.FAIL, f"unusable: {str(exc)[:120]}",
                     "run `jobbot migrate`")
    return Check("database", Status.OK,
                 f"{get_settings().data_dir} — {jobs} job(s), {apps} application(s)")


def _check_submission_mode() -> Check:
    """The one setting whose default matters most.

    Auto-submit sends applications with nobody watching. That is a real
    capability and it is off by default; this check makes sure you know
    which mode you are in before a batch, rather than after.
    """
    from jobbot.config import get_settings

    settings = get_settings()
    if not settings.jobbot_auto_submit:
        return Check("submission mode", Status.OK,
                     "review mode — every application waits for you to press Submit")
    return Check("submission mode", Status.WARN,
                 "AUTO-SUBMIT IS ON: applications will be sent without review",
                 "set JOBBOT_AUTO_SUBMIT=false in .env to review each one first")


def _check_sensitive_autofill() -> Check:
    from jobbot.config import get_settings

    if not get_settings().jobbot_autofill_sensitive:
        return Check("sensitive answers", Status.OK,
                     "work authorization / EEOC / legal questions always ask you")
    return Check("sensitive answers", Status.WARN,
                 "sensitive autofill is enabled; previously *confirmed* answers "
                 "will be reused (model guesses never are)",
                 "set JOBBOT_AUTOFILL_SENSITIVE=false to be asked every time")


def _check_agent() -> Check:
    from jobbot.config import get_settings

    settings = get_settings()
    if not settings.jobbot_agent_takeover:
        return Check("agent takeover", Status.WARN,
                     "off — a posting whose form is behind an Apply button will be skipped",
                     "set JOBBOT_AGENT_TAKEOVER=true")
    return Check("agent takeover", Status.OK,
                 f"on, max {settings.jobbot_agent_max_steps} steps, navigation only "
                 "(the agent can never fill or submit)")


def _check_auth() -> Check:
    """Sign-in capability, and where credentials would come from."""
    from jobbot.auth.credentials import _keyring
    from jobbot.auth.session import MAX_CONSECUTIVE_FAILURES, list_records
    from jobbot.config import get_settings

    settings = get_settings()
    if not settings.jobbot_auth_enabled:
        return Check("sign-in", Status.WARN,
                     "off — postings behind a login will stop for you",
                     "set JOBBOT_AUTH_ENABLED=true")

    keyring_available = _keyring() is not None
    try:
        records = list_records()
    except Exception:  # noqa: BLE001
        records = []
    stuck = [r for r in records if (r.consecutive_failures or 0) >= MAX_CONSECUTIVE_FAILURES]

    if stuck:
        return Check(
            "sign-in", Status.WARN,
            f"{len(stuck)} domain(s) not being retried after repeated failures: "
            + ", ".join(r.domain for r in stuck[:4]),
            "fix the credential with `jobbot auth add <domain>`, or sign in yourself once",
        )
    detail = (f"on; credentials from the OS keyring"
              if keyring_available else
              "on; no OS keyring, so credentials must come from "
              "JOBBOT_CRED_<DOMAIN>_USER / _PASSWORD")
    if records:
        detail += f"; {len(records)} domain(s) seen"
    return Check("sign-in", Status.OK, detail,
                 "" if keyring_available else "pip install keyring for encrypted storage")


def _check_signup() -> Check:
    """Account creation is the setting with the widest blast radius."""
    from jobbot.config import get_settings

    settings = get_settings()
    if not settings.jobbot_allow_signup:
        return Check("account creation", Status.OK,
                     "off — postings needing a new account stop for you")
    domains = [d.strip() for d in (settings.jobbot_signup_domains or "").split(",") if d.strip()]
    if not domains:
        return Check("account creation", Status.WARN,
                     "enabled but no allowed domains listed, so nothing can use it",
                     "set JOBBOT_SIGNUP_DOMAINS, or JOBBOT_ALLOW_SIGNUP=false")
    return Check("account creation", Status.WARN,
                 f"enabled for: {', '.join(domains)}",
                 "set JOBBOT_ALLOW_SIGNUP=false unless you meant this")


CHECKS = (
    _check_profile, _check_resume, _check_resume_folder, _check_llm,
    _check_browser, _check_database, _check_submission_mode,
    _check_sensitive_autofill, _check_agent, _check_auth, _check_signup,
)


def run_preflight() -> list[Check]:
    results: list[Check] = []
    for check in CHECKS:
        try:
            results.append(check())
        except Exception as exc:  # noqa: BLE001 - a broken check is a WARN, not a crash
            results.append(Check(check.__name__, Status.WARN,
                                 f"check itself failed: {type(exc).__name__}: {exc}"))
    return results


def ready(results: list[Check]) -> bool:
    return not any(c.status is Status.FAIL for c in results)

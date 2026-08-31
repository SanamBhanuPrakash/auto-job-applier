"""Imports every resume in a folder as its own tagged profile, so jobbot can
pick whichever one actually fits a given posting instead of using one
resume for everything — e.g.:

  config/resumes/python-developer.pdf
  config/resumes/ai-engineer.pdf
  config/resumes/frontend.pdf
  config/resumes/backend.pdf
  config/resumes/full-stack.pdf
  config/resumes/cloud-engineer.pdf

The filename (minus extension) becomes the tag, normalized to
lowercase-with-hyphens. Re-importing the same filename updates that tag's
profile in place rather than creating a duplicate.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select

from jobbot.db import session_scope
from jobbot.llm import DailyQuotaExceeded
from jobbot.models import ResumeProfile
from jobbot.resume.parser import parse_resume

log = logging.getLogger(__name__)

RESUME_EXTENSIONS = {".pdf", ".docx", ".doc", ".txt", ".md"}


def tag_from_filename(path: Path) -> str:
    return path.stem.strip().lower().replace(" ", "-").replace("_", "-")


def import_folder(folder: Path) -> list[str]:
    """Parses every resume file in `folder` (non-recursive) and upserts a
    ResumeProfile per file. Returns the tags imported, in filename order.

    A single resume that fails to parse — confirmed live: the model's
    tool-call generation kept coming back malformed/empty even after
    llm.py's retries were exhausted, on one unusually dense resume — is
    logged and skipped rather than losing every resume queued behind it in
    the same folder. Before this, one bad file aborted the whole
    `import-folder` run and any files after it in sorted order were never
    even attempted.
    """
    tags: list[str] = []
    for path in sorted(folder.iterdir()):
        if not path.is_file() or path.suffix.lower() not in RESUME_EXTENSIONS:
            continue
        tag = tag_from_filename(path)
        log.info("Parsing resume %s as profile %r", path.name, tag)
        try:
            profile = parse_resume(path)
        except DailyQuotaExceeded:
            raise  # every remaining resume would fail the same way today — stop, don't waste calls
        except Exception:  # noqa: BLE001
            log.exception("Failed to parse %s; skipping (re-run `jobbot resume import-folder` later to retry it)", path.name)
            continue

        with session_scope() as session:
            existing = session.execute(
                select(ResumeProfile).where(ResumeProfile.tag == tag)
            ).scalar_one_or_none()
            if existing is None:
                existing = ResumeProfile(tag=tag)
                session.add(existing)
            existing.resume_path = str(path)
            existing.profile_json = profile.model_dump(exclude_none=False)

        tags.append(tag)
    return tags


def list_profiles() -> list[ResumeProfile]:
    with session_scope() as session:
        return list(session.execute(select(ResumeProfile)).scalars().all())


def get_profile(tag: str) -> ResumeProfile | None:
    with session_scope() as session:
        return session.execute(
            select(ResumeProfile).where(ResumeProfile.tag == tag)
        ).scalar_one_or_none()

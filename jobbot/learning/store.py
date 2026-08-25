"""Persists and looks up remembered answers to recurring application
questions. Every function takes an explicit Session so it's easy to test
against an in-memory DB and easy to call from within submit/base.py's
session_scope().
"""
from __future__ import annotations

import datetime as dt
import logging

from rapidfuzz import fuzz
from sqlalchemy import select
from sqlalchemy.orm import Session

from jobbot.learning.normalize import normalize_label
from jobbot.models import LearnedAnswer
from jobbot.submit.form_scan import FieldSpec, FrameLike

log = logging.getLogger(__name__)

FUZZY_MATCH_THRESHOLD = 90
MAX_LEARNABLE_VALUE_LEN = 500
# Long free-text answers (cover letters, "why do you want to work here")
# are company-specific — reusing them verbatim elsewhere reads as spam and
# they're excluded from learning even if short enough by length alone.
MAX_LEARNABLE_TEXTAREA_LEN = 200


def eligible_for_learning(field_type: str, value: str) -> bool:
    if field_type == "file":
        return False
    value = (value or "").strip()
    if not value:
        return False
    if len(value) > MAX_LEARNABLE_VALUE_LEN:
        return False
    if field_type == "textarea" and len(value) > MAX_LEARNABLE_TEXTAREA_LEN:
        return False
    return True


def find_match(session: Session, label: str, field_type: str) -> LearnedAnswer | None:
    key = normalize_label(label)
    if not key:
        return None

    exact = session.execute(
        select(LearnedAnswer).where(LearnedAnswer.question_key == key)
    ).scalar_one_or_none()
    if exact is not None:
        return exact

    best: LearnedAnswer | None = None
    best_score = 0.0
    for candidate in session.execute(select(LearnedAnswer)).scalars():
        # token_set_ratio: robust to reordering and to one label containing
        # extra words the other doesn't ("legally", "United States" vs "US").
        score = fuzz.token_set_ratio(key, candidate.question_key)
        if score > best_score:
            best_score = score
            best = candidate

    if best is not None and best_score >= FUZZY_MATCH_THRESHOLD:
        return best
    return None


def match_fields(session: Session, fields: list[FieldSpec]) -> dict[int, LearnedAnswer]:
    matches: dict[int, LearnedAnswer] = {}
    for field in fields:
        if field.field_type == "file":
            continue
        match = find_match(session, field.label, field.field_type)
        if match is not None:
            matches[field.field_id] = match
    return matches


def upsert(session: Session, label: str, field_type: str, value: str, sensitive: bool) -> LearnedAnswer:
    key = normalize_label(label)
    now = dt.datetime.now(dt.timezone.utc)
    existing = session.execute(
        select(LearnedAnswer).where(LearnedAnswer.question_key == key)
    ).scalar_one_or_none()

    if existing is not None:
        existing.value = value
        existing.label_raw = label
        existing.field_type = field_type
        existing.sensitive = sensitive
        existing.times_used += 1
        existing.last_used_at = now
        return existing

    row = LearnedAnswer(
        question_key=key,
        label_raw=label,
        field_type=field_type,
        value=value,
        sensitive=sensitive,
        times_used=1,
        last_used_at=now,
    )
    session.add(row)
    return row


def capture_from_page(session: Session, page: FrameLike, fields: list[FieldSpec]) -> int:
    """Reads whatever ended up in each field (auto-filled or typed by the
    human) right before submit and remembers anything eligible. Called
    regardless of whether the application is actually submitted, so even a
    field you filled in on a skipped application gets remembered.
    """
    from jobbot.submit.fill_planner import _ALWAYS_HUMAN_RE  # local import: avoid a submit<->learning cycle at module load
    from jobbot.submit.values import read_field_value

    count = 0
    for field in fields:
        if field.field_type == "file":
            continue
        value = read_field_value(page, field)
        if not eligible_for_learning(field.field_type, value):
            continue
        sensitive = bool(_ALWAYS_HUMAN_RE.search(field.label))
        upsert(session, field.label, field.field_type, value, sensitive)
        count += 1

    if count:
        log.info("Learned/updated %d field answer(s) from this application", count)
    return count

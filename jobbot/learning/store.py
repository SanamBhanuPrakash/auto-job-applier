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
from jobbot.learning.provenance import Provenance, strongest
from jobbot.models import FieldIssue, LearnedAnswer
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


def _fuzzy_find(session: Session, model, label: str):
    """Exact normalized-key match first, falling back to the closest
    fuzzy match (token_set_ratio, robust to reordering and to one label
    containing extra words the other doesn't — "legally", "United States"
    vs "US"). Shared by LearnedAnswer lookups and FieldIssue/circuit-breaker
    lookups so a reworded question resolves to the same row either way.
    """
    key = normalize_label(label)
    if not key:
        return None

    exact = session.execute(select(model).where(model.question_key == key)).scalar_one_or_none()
    if exact is not None:
        return exact

    best = None
    best_score = 0.0
    for candidate in session.execute(select(model)).scalars():
        score = fuzz.token_set_ratio(key, candidate.question_key)
        if score > best_score:
            best_score = score
            best = candidate

    if best is not None and best_score >= FUZZY_MATCH_THRESHOLD:
        return best
    return None


def find_match(session: Session, label: str, field_type: str) -> LearnedAnswer | None:
    return _fuzzy_find(session, LearnedAnswer, label)


def match_fields(session: Session, fields: list[FieldSpec]) -> dict[int, LearnedAnswer]:
    matches: dict[int, LearnedAnswer] = {}
    for field in fields:
        if field.field_type == "file":
            continue
        match = find_match(session, field.label, field.field_type)
        if match is not None:
            matches[field.field_id] = match
    return matches


def upsert(
    session: Session,
    label: str,
    field_type: str,
    value: str,
    sensitive: bool,
    *,
    provenance: str = Provenance.UNVERIFIED.value,
    human_confirmed: bool = False,
    verified: bool = False,
) -> LearnedAnswer:
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
        # Trust only ever ratchets up: re-seeing a value on an unverified
        # form must not erase the fact that it was previously confirmed.
        existing.provenance = strongest(existing.provenance or Provenance.UNVERIFIED.value, provenance)
        existing.human_confirmed = bool(existing.human_confirmed or human_confirmed)
        if verified:
            existing.verified_submission_count = (existing.verified_submission_count or 0) + 1
            existing.last_validated_at = now
        return existing

    row = LearnedAnswer(
        question_key=key,
        label_raw=label,
        field_type=field_type,
        value=value,
        sensitive=sensitive,
        times_used=1,
        last_used_at=now,
        provenance=provenance,
        human_confirmed=human_confirmed,
        verified_submission_count=1 if verified else 0,
        last_validated_at=now if verified else None,
    )
    session.add(row)
    return row


def value_still_offerable(field: FieldSpec, value: str) -> bool:
    """A learned value for a select/radio field is only safe to reuse if
    the current form still actually offers it as an option — the option
    list can differ per employer/posting even for a similarly-worded
    question. Text/textarea/combobox/checkbox fields have no fixed option
    list to check against.
    """
    if field.field_type in ("select", "radio"):
        return any(value.strip().lower() == opt.strip().lower() for opt in field.options)
    return True


# --- fill-failure circuit breaker -------------------------------------------
# A question that has failed to auto-fill this many times in a row stops
# being retried automatically and routes straight to human review instead —
# see is_circuit_broken / record_failure / clear_failure, wired in
# submit/base.py around the fill step.
CIRCUIT_BREAKER_THRESHOLD = 2


def is_circuit_broken(session: Session, label: str) -> bool:
    row = _fuzzy_find(session, FieldIssue, label)
    return row is not None and row.failure_count >= CIRCUIT_BREAKER_THRESHOLD


def record_failure(session: Session, label: str, error: str) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    row = _fuzzy_find(session, FieldIssue, label)
    if row is None:
        row = FieldIssue(question_key=normalize_label(label), label_raw=label, failure_count=0)
        session.add(row)
    row.failure_count += 1
    row.label_raw = label
    row.last_error = (error or "")[:500]
    row.last_seen_at = now
    if row.failure_count >= CIRCUIT_BREAKER_THRESHOLD:
        log.warning(
            "Field %r has now failed to auto-fill %d time(s) — will route straight to human review from now on",
            label,
            row.failure_count,
        )


def clear_failure(session: Session, label: str) -> None:
    """Called whenever a value is successfully captured for a question —
    evidence it's fillable again, so let it be attempted automatically."""
    row = _fuzzy_find(session, FieldIssue, label)
    if row is not None:
        session.delete(row)


def capture_from_page(
    session: Session,
    page: FrameLike,
    fields: list[FieldSpec],
    *,
    verified_submission: bool = False,
    model_filled_ids: set[int] | None = None,
) -> int:
    """Read back whatever ended up in each field and remember it, tagged
    with how much that value can be trusted.

    `model_filled_ids` are fields the LLM populated on this run. Those are
    recorded as MODEL_GUESS unless the submission was positively verified;
    everything else was put there by the candidate and counts as
    HUMAN_ENTERED. Only the latter is ever eligible to auto-fill a
    sensitive question later (see learning/provenance.py) — which is what
    stops a hallucinated work-authorization answer from being replayed onto
    future applications as though it had been confirmed.
    """
    from jobbot.submit.fill_planner import _ALWAYS_HUMAN_RE  # local import: avoid a submit<->learning cycle at module load
    from jobbot.submit.values import read_field_value

    model_filled_ids = model_filled_ids or set()
    count = 0
    for field in fields:
        if field.field_type == "file":
            continue
        value = read_field_value(page, field)
        if not eligible_for_learning(field.field_type, value):
            continue

        was_model_filled = field.field_id in model_filled_ids
        if verified_submission:
            provenance = Provenance.VERIFIED.value
        elif was_model_filled:
            provenance = Provenance.MODEL_GUESS.value
        else:
            provenance = Provenance.HUMAN_ENTERED.value

        sensitive = bool(_ALWAYS_HUMAN_RE.search(field.label))
        upsert(
            session, field.label, field.field_type, value, sensitive,
            provenance=provenance,
            human_confirmed=not was_model_filled,
            verified=verified_submission,
        )
        clear_failure(session, field.label)  # it clearly can be filled now
        count += 1

    if count:
        log.info(
            "Learned/updated %d field answer(s) from this application (verified=%s)",
            count, verified_submission,
        )
    return count

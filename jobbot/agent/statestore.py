"""Crash-safe, race-free ownership of application attempts.

Two problems this closes, both of which could produce a real duplicate
application to a real employer:

1. **The crash window.** Previously a row was written as "attempted"
   before the browser opened and only became "submitted" after a
   successful click, so a crash between the click and the write left the
   database claiming the job was never applied to — and the next run
   applied again. Now every state change is persisted *before* the action
   it describes, and anything found at or past SUBMITTING is never
   auto-resumed (see states.is_safe_to_auto_resume).

2. **The concurrency window.** Previously two processes both read "which
   jobs are already submitted" before either wrote, so both applied. Now
   claiming an attempt is an INSERT against a UNIQUE index, so the
   database picks exactly one winner; the loser observes the conflict and
   backs off.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import socket
import uuid
from dataclasses import dataclass
from enum import Enum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from jobbot.agent.identity import application_idempotency_key, canonical_url, job_identity
from jobbot.agent.states import (
    ApplicationState,
    FailureCategory,
    assert_transition,
    is_safe_to_auto_resume,
)
from jobbot.models import Application, Job, Run, StateTransition

log = logging.getLogger(__name__)

DEFAULT_LEASE_SECONDS = 1800  # 30 min: longer than any sane single application


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_aware(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite round-trips datetimes without tzinfo; treat naive as UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.timezone.utc)
    return value


def new_run_id() -> str:
    return f"run_{_utcnow():%Y%m%dT%H%M%S}_{uuid.uuid4().hex[:8]}"


def worker_identity() -> str:
    """Identifies this process for lease ownership."""
    return f"{socket.gethostname()}:{os.getpid()}"


class ClaimOutcome(str, Enum):
    CLAIMED = "CLAIMED"           # fresh attempt, we own it
    RESUMED = "RESUMED"           # prior attempt died safely pre-submit, we took over
    ALREADY_DONE = "ALREADY_DONE" # terminal; nothing to do
    HELD_BY_OTHER = "HELD_BY_OTHER"  # another live worker owns it
    NEEDS_HUMAN = "NEEDS_HUMAN"   # cannot be auto-resumed (may already be submitted)


@dataclass
class ClaimResult:
    outcome: ClaimOutcome
    application: Application | None
    reason: str = ""

    @property
    def acquired(self) -> bool:
        return self.outcome in (ClaimOutcome.CLAIMED, ClaimOutcome.RESUMED)


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def start_run(session: Session, command: str) -> str:
    run_id = new_run_id()
    session.add(Run(run_id=run_id, command=command, status="RUNNING"))
    session.commit()
    return run_id


def finish_run(session: Session, run_id: str, status: str = "COMPLETED", notes: str = "") -> None:
    run = session.execute(select(Run).where(Run.run_id == run_id)).scalar_one_or_none()
    if run is None:
        return
    run.status = status
    run.ended_at = _utcnow()
    if notes:
        run.notes = notes
    session.commit()


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------


def claim(
    session: Session,
    job: Job,
    *,
    run_id: str,
    owner: str | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> ClaimResult:
    """Acquire exclusive ownership of "applying to this job", or explain why not.

    The uniqueness of `Application.idempotency_key` (a UNIQUE index, see
    jobbot/migrate.py) is what makes this safe across processes: the
    INSERT either wins or raises IntegrityError. We never decide ownership
    by reading first and writing later, because that is precisely the race
    that produced duplicate applications.
    """
    owner = owner or worker_identity()
    identity = job.job_identity or job_identity(
        url=job.url or "",
        company=job.company or "",
        title=job.title or "",
        source=job.source or "",
        external_id=job.external_id or "",
    )
    key = application_idempotency_key(identity)
    now = _utcnow()

    application = Application(
        job_id=job.id,
        run_id=run_id,
        status="attempted",
        state=ApplicationState.DISCOVERED.value,
        previous_state="",
        attempt_no=1,
        idempotency_key=key,
        job_identity=identity,
        canonical_url=canonical_url(job.url or ""),
        lease_owner=owner,
        lease_expires_at=now + dt.timedelta(seconds=lease_seconds),
        updated_at=now,
    )
    session.add(application)
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        return _handle_existing(
            session, key, run_id=run_id, owner=owner, lease_seconds=lease_seconds
        )

    _record_transition(
        session, application, from_state=None,
        to_state=ApplicationState.DISCOVERED, reason="claimed", run_id=run_id,
    )
    session.commit()
    return ClaimResult(ClaimOutcome.CLAIMED, application)


def _handle_existing(
    session: Session, key: str, *, run_id: str, owner: str, lease_seconds: int
) -> ClaimResult:
    """Decide what to do about an attempt that already exists for this job."""
    existing = session.execute(
        select(Application).where(Application.idempotency_key == key)
    ).scalar_one_or_none()

    if existing is None:  # deleted between our INSERT failing and this read
        return ClaimResult(ClaimOutcome.HELD_BY_OTHER, None, "row vanished during claim")

    try:
        state = ApplicationState(existing.state)
    except ValueError:
        # Unrecognized/legacy state: treat as unsafe rather than guessing.
        return ClaimResult(
            ClaimOutcome.NEEDS_HUMAN, existing,
            f"unrecognized state {existing.state!r}; not auto-resuming",
        )

    if state in (ApplicationState.SUBMITTED, ApplicationState.SKIPPED, ApplicationState.COMPLETED):
        return ClaimResult(ClaimOutcome.ALREADY_DONE, existing, f"already {state.value}")

    if not is_safe_to_auto_resume(state):
        # Covers SUBMITTING / VERIFYING_SUBMISSION / UNKNOWN / BLOCKED /
        # HUMAN_REVIEW / FAILED. The employer may already hold this
        # application; a human decides, not us.
        return ClaimResult(
            ClaimOutcome.NEEDS_HUMAN, existing,
            f"state {state.value} cannot be auto-resumed",
        )

    lease_expiry = _as_aware(existing.lease_expires_at)
    if lease_expiry is not None and lease_expiry > _utcnow() and existing.lease_owner != owner:
        return ClaimResult(
            ClaimOutcome.HELD_BY_OTHER, existing,
            f"leased by {existing.lease_owner} until {lease_expiry.isoformat()}",
        )

    # Safe pre-submit state with a dead/expired lease: take it over.
    now = _utcnow()
    existing.lease_owner = owner
    existing.lease_expires_at = now + dt.timedelta(seconds=lease_seconds)
    existing.attempt_no = (existing.attempt_no or 1) + 1
    existing.run_id = run_id
    existing.updated_at = now
    session.commit()

    log.info(
        "Resumed application %s (attempt %d) from state %s after expired lease",
        existing.id, existing.attempt_no, state.value,
    )
    return ClaimResult(ClaimOutcome.RESUMED, existing, f"took over expired lease at {state.value}")


def release(session: Session, application: Application) -> None:
    """Drop our lease without changing state (e.g. run ended cleanly)."""
    application.lease_owner = ""
    application.lease_expires_at = None
    application.updated_at = _utcnow()
    session.commit()


def heartbeat(session: Session, application: Application, lease_seconds: int = DEFAULT_LEASE_SECONDS) -> None:
    """Extend our lease during a long application so another worker doesn't
    conclude we died and take over mid-fill."""
    application.lease_expires_at = _utcnow() + dt.timedelta(seconds=lease_seconds)
    application.updated_at = _utcnow()
    session.commit()


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def _record_transition(
    session: Session,
    application: Application,
    *,
    from_state: ApplicationState | None,
    to_state: ApplicationState,
    reason: str,
    run_id: str,
    failure_category: FailureCategory | None = None,
    detail: dict | None = None,
) -> None:
    session.add(
        StateTransition(
            application_id=application.id,
            run_id=run_id,
            from_state=from_state.value if from_state else "",
            to_state=to_state.value,
            reason=reason,
            failure_category=failure_category.value if failure_category else "",
            detail=detail or {},
        )
    )


def transition(
    session: Session,
    application: Application,
    to_state: ApplicationState,
    *,
    reason: str = "",
    run_id: str = "",
    failure_category: FailureCategory | None = None,
    detail: dict | None = None,
    checkpoint: dict | None = None,
) -> Application:
    """Move an attempt to a new state, validating the transition and
    persisting it *before* the action it authorizes is performed.

    Ordering matters: writing SUBMITTING to disk before clicking submit is
    what makes a crash during the click recoverable — the row proves we
    were about to act, so recovery escalates instead of retrying.
    """
    current = ApplicationState(application.state) if application.state else ApplicationState.DISCOVERED
    assert_transition(current, to_state)

    application.previous_state = current.value
    application.state = to_state.value
    application.updated_at = _utcnow()
    if failure_category is not None:
        application.failure_category = failure_category.value
    if checkpoint is not None:
        application.checkpoint = checkpoint

    application.status = _legacy_status_for(to_state)

    _record_transition(
        session, application,
        from_state=current, to_state=to_state, reason=reason,
        run_id=run_id or application.run_id or "",
        failure_category=failure_category, detail=detail,
    )
    session.commit()
    return application


#: Keeps the pre-existing free-text `status` column (and everything reading
#: it, including `jobbot ledger`) meaningful while `state` becomes the
#: authoritative value.
_STATE_TO_LEGACY_STATUS = {
    ApplicationState.SUBMITTED: "submitted",
    ApplicationState.SKIPPED: "skipped",
    ApplicationState.FAILED: "error",
    ApplicationState.BLOCKED: "error",
    ApplicationState.HUMAN_REVIEW: "filled_pending_review",
    ApplicationState.READY_TO_SUBMIT: "filled_pending_review",
    ApplicationState.UNKNOWN: "error",
    ApplicationState.COMPLETED: "submitted",
}


def _legacy_status_for(state: ApplicationState) -> str:
    return _STATE_TO_LEGACY_STATUS.get(state, "attempted")


def history(session: Session, application_id: int) -> list[StateTransition]:
    return list(
        session.execute(
            select(StateTransition)
            .where(StateTransition.application_id == application_id)
            .order_by(StateTransition.id)
        ).scalars()
    )


def consumed_job_identities(session: Session) -> set[str]:
    """Job identities that must not be attempted again.

    Deliberately broader than "submitted": an attempt parked in the danger
    zone, or awaiting human review, also consumes its job — treating an
    unprovable outcome as "not applied" is what causes duplicates.
    """
    from jobbot.agent.states import CONSUMES_JOB

    states = [s.value for s in CONSUMES_JOB]
    rows = session.execute(
        select(Application.job_identity).where(Application.state.in_(states)).distinct()
    ).scalars().all()
    return {r for r in rows if r}

"""Crash-safety and concurrency tests for attempt ownership.

These cover the two scenarios that could previously produce a real
duplicate application to a real employer:

  * the process dying between the submit click and the database write
  * two runs racing to apply to the same posting
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import sessionmaker

from jobbot.agent import statestore
from jobbot.agent.identity import application_idempotency_key, job_identity
from jobbot.agent.states import ApplicationState, FailureCategory, IllegalTransition
from jobbot.models import Base, Job

S = ApplicationState


@pytest.fixture
def engine(tmp_path):
    """A real on-disk SQLite DB with the same unique index the migration
    creates — the constraint IS the concurrency guarantee, so testing
    against a schema without it would prove nothing."""
    db = tmp_path / "test.sqlite3"
    eng = create_engine(f"sqlite:///{db}", future=True)

    @event.listens_for(eng, "connect")
    def _pragmas(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    Base.metadata.create_all(eng)
    with eng.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_idempotency_key "
                "ON applications (idempotency_key)"
            )
        )
    return eng


@pytest.fixture
def Session(engine):
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture
def session(Session):
    s = Session()
    yield s
    s.close()


def _make_job(session, external_id="1", url="https://boards.greenhouse.io/acme/jobs/1") -> Job:
    job = Job(
        source="greenhouse", external_id=external_id, company="Acme", title="Engineer",
        url=url, job_identity=job_identity(url=url, company="Acme", title="Engineer"),
    )
    session.add(job)
    session.commit()
    return job


# --- basic claiming --------------------------------------------------------


def test_first_claim_succeeds_and_records_a_transition(session):
    job = _make_job(session)
    result = statestore.claim(session, job, run_id="run_1")

    assert result.outcome is statestore.ClaimOutcome.CLAIMED
    assert result.acquired
    assert result.application.state == S.DISCOVERED.value
    assert result.application.idempotency_key == application_idempotency_key(job.job_identity)
    assert len(statestore.history(session, result.application.id)) == 1


def test_second_claim_by_another_worker_loses(session):
    """The race: process B must not also get to apply."""
    job = _make_job(session)
    first = statestore.claim(session, job, run_id="run_1", owner="worker-a")
    assert first.acquired

    second = statestore.claim(session, job, run_id="run_2", owner="worker-b")
    assert not second.acquired
    assert second.outcome is statestore.ClaimOutcome.HELD_BY_OTHER


def test_claim_after_submission_is_refused(session):
    job = _make_job(session)
    claimed = statestore.claim(session, job, run_id="run_1")
    app = claimed.application
    for state in (S.ANALYZING, S.ELIGIBILITY_CHECK, S.SELECTING_RESUME, S.OPENING_APPLICATION,
                  S.INSPECTING_FORM, S.FILLING, S.VERIFYING_FIELDS, S.READY_TO_SUBMIT,
                  S.SUBMITTING, S.VERIFYING_SUBMISSION, S.SUBMITTED):
        statestore.transition(session, app, state, run_id="run_1")

    again = statestore.claim(session, job, run_id="run_2")
    assert again.outcome is statestore.ClaimOutcome.ALREADY_DONE
    assert not again.acquired


# --- the crash window ------------------------------------------------------


def _expire_lease(session, app):
    app.lease_expires_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)
    session.commit()


def test_crash_during_submit_is_never_auto_retried(session):
    """THE regression test for the duplicate-application bug.

    Simulates: we wrote SUBMITTING, clicked submit, then the process died
    before recording the outcome. The employer may hold the application.
    A later run must refuse to re-apply, even though the lease is dead.
    """
    job = _make_job(session)
    app = statestore.claim(session, job, run_id="run_1").application
    for state in (S.ANALYZING, S.ELIGIBILITY_CHECK, S.SELECTING_RESUME, S.OPENING_APPLICATION,
                  S.INSPECTING_FORM, S.FILLING, S.VERIFYING_FIELDS, S.READY_TO_SUBMIT, S.SUBMITTING):
        statestore.transition(session, app, state, run_id="run_1")

    _expire_lease(session, app)  # the crashed process is long gone

    retry = statestore.claim(session, job, run_id="run_2")
    assert retry.outcome is statestore.ClaimOutcome.NEEDS_HUMAN
    assert not retry.acquired
    assert "SUBMITTING" in retry.reason


def test_crash_before_submit_is_safely_resumed(session):
    """The complement: dying while still filling the form is harmless, so
    the work should be picked back up rather than abandoned."""
    job = _make_job(session)
    app = statestore.claim(session, job, run_id="run_1").application
    for state in (S.ANALYZING, S.ELIGIBILITY_CHECK, S.SELECTING_RESUME,
                  S.OPENING_APPLICATION, S.INSPECTING_FORM, S.FILLING):
        statestore.transition(session, app, state, run_id="run_1")

    _expire_lease(session, app)

    resumed = statestore.claim(session, job, run_id="run_2", owner="worker-b")
    assert resumed.outcome is statestore.ClaimOutcome.RESUMED
    assert resumed.acquired
    assert resumed.application.attempt_no == 2


def test_unknown_state_is_never_auto_retried(session):
    job = _make_job(session)
    app = statestore.claim(session, job, run_id="run_1").application
    for state in (S.ANALYZING, S.ELIGIBILITY_CHECK, S.SELECTING_RESUME, S.OPENING_APPLICATION,
                  S.INSPECTING_FORM, S.FILLING, S.VERIFYING_FIELDS, S.READY_TO_SUBMIT,
                  S.SUBMITTING, S.VERIFYING_SUBMISSION, S.UNKNOWN):
        statestore.transition(session, app, state, run_id="run_1")
    _expire_lease(session, app)

    assert statestore.claim(session, job, run_id="run_2").outcome is statestore.ClaimOutcome.NEEDS_HUMAN


def test_live_lease_blocks_takeover_even_pre_submit(session):
    job = _make_job(session)
    app = statestore.claim(session, job, run_id="run_1", owner="worker-a").application
    statestore.transition(session, app, S.ANALYZING, run_id="run_1")

    other = statestore.claim(session, job, run_id="run_2", owner="worker-b")
    assert other.outcome is statestore.ClaimOutcome.HELD_BY_OTHER


def test_heartbeat_extends_the_lease(session):
    job = _make_job(session)
    app = statestore.claim(session, job, run_id="run_1").application
    original = app.lease_expires_at
    statestore.heartbeat(session, app, lease_seconds=7200)
    assert app.lease_expires_at > original


# --- transitions -----------------------------------------------------------


def test_illegal_transition_is_rejected(session):
    job = _make_job(session)
    app = statestore.claim(session, job, run_id="run_1").application
    with pytest.raises(IllegalTransition):
        statestore.transition(session, app, S.SUBMITTED, run_id="run_1")


def test_transition_records_history_and_failure_category(session):
    job = _make_job(session)
    app = statestore.claim(session, job, run_id="run_1").application
    statestore.transition(
        session, app, S.BLOCKED, reason="captcha detected",
        run_id="run_1", failure_category=FailureCategory.BLOCKED,
    )
    rows = statestore.history(session, app.id)
    assert rows[-1].to_state == S.BLOCKED.value
    assert rows[-1].failure_category == FailureCategory.BLOCKED.value
    assert app.failure_category == FailureCategory.BLOCKED.value


def test_legacy_status_column_stays_meaningful(session):
    """`jobbot ledger` and older queries read `status`; it must keep
    tracking the machine state rather than going stale."""
    job = _make_job(session)
    app = statestore.claim(session, job, run_id="run_1").application
    for state in (S.ANALYZING, S.ELIGIBILITY_CHECK, S.SELECTING_RESUME, S.OPENING_APPLICATION,
                  S.INSPECTING_FORM, S.FILLING, S.VERIFYING_FIELDS, S.READY_TO_SUBMIT,
                  S.SUBMITTING, S.VERIFYING_SUBMISSION, S.SUBMITTED):
        statestore.transition(session, app, state, run_id="run_1")
    assert app.status == "submitted"


# --- consumed identities ---------------------------------------------------


def test_consumed_identities_include_unprovable_outcomes(session):
    job = _make_job(session)
    app = statestore.claim(session, job, run_id="run_1").application
    for state in (S.ANALYZING, S.ELIGIBILITY_CHECK, S.SELECTING_RESUME, S.OPENING_APPLICATION,
                  S.INSPECTING_FORM, S.FILLING, S.VERIFYING_FIELDS, S.READY_TO_SUBMIT, S.SUBMITTING):
        statestore.transition(session, app, state, run_id="run_1")

    assert job.job_identity in statestore.consumed_job_identities(session)


def test_in_flight_pre_submit_attempt_does_not_permanently_consume_job(session):
    """A job still being worked on shouldn't look permanently spent —
    otherwise a crash pre-submit would lose the application forever."""
    job = _make_job(session)
    statestore.claim(session, job, run_id="run_1")
    assert job.job_identity not in statestore.consumed_job_identities(session)


# --- runs ------------------------------------------------------------------


def test_run_lifecycle(session):
    run_id = statestore.start_run(session, command="batch")
    assert run_id.startswith("run_")
    statestore.finish_run(session, run_id, status="COMPLETED")

    from jobbot.models import Run
    from sqlalchemy import select

    run = session.execute(select(Run).where(Run.run_id == run_id)).scalar_one()
    assert run.status == "COMPLETED"
    assert run.ended_at is not None

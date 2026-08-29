"""The migration runs against databases users already have data in, so
losing or mis-classifying an existing row is not an acceptable failure
mode. These tests build a genuine *old-schema* database and migrate it.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from jobbot.migrate import migrate
from jobbot.models import Application, Base, Job


def _legacy_db(tmp_path):
    """A database with the pre-agent schema: no state, no identity, no
    idempotency key, no runs/transitions tables."""
    db = tmp_path / "legacy.sqlite3"
    engine = create_engine(f"sqlite:///{db}", future=True)
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY, source VARCHAR(32), external_id VARCHAR(256),
                company VARCHAR(256), title VARCHAR(512), location VARCHAR(256),
                remote BOOLEAN DEFAULT 0, url VARCHAR(1024), description TEXT,
                posted_at VARCHAR(64), ats VARCHAR(32), discovered_at DATETIME,
                raw JSON, matched_profile_tag VARCHAR(128) DEFAULT ''
            )"""))
        conn.execute(text("""
            CREATE TABLE applications (
                id INTEGER PRIMARY KEY, job_id INTEGER, status VARCHAR(32),
                fill_plan JSON, fields_needing_human JSON,
                screenshot_path VARCHAR(1024), error TEXT, created_at DATETIME
            )"""))
        conn.execute(text("""
            CREATE TABLE learned_answers (
                id INTEGER PRIMARY KEY, question_key VARCHAR(512), label_raw VARCHAR(512),
                field_type VARCHAR(32), value TEXT, sensitive BOOLEAN DEFAULT 0,
                times_used INTEGER DEFAULT 1, last_used_at DATETIME, created_at DATETIME
            )"""))
        conn.execute(text(
            "INSERT INTO jobs (id, source, external_id, company, title, url) VALUES "
            "(1, 'greenhouse', '12345', 'Acme', 'Engineer', "
            "'https://boards.greenhouse.io/acme/jobs/12345?gh_src=abc')"
        ))
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for app_id, status in [(1, "submitted"), (2, "attempted"), (3, "skipped"),
                               (4, "error"), (5, "filled_pending_review")]:
            conn.execute(
                text("INSERT INTO applications (id, job_id, status, created_at) "
                     "VALUES (:i, 1, :s, :t)"),
                {"i": app_id, "s": status, "t": now},
            )
        conn.execute(text(
            "INSERT INTO learned_answers (id, question_key, label_raw, field_type, value, "
            "sensitive, times_used) VALUES (1, 'first name', 'First name', 'text', 'Ada', 0, 3)"
        ))
    return engine


def test_migration_adds_missing_columns_and_tables(tmp_path):
    engine = _legacy_db(tmp_path)
    migrate(engine)

    inspector = inspect(engine)
    app_cols = {c["name"] for c in inspector.get_columns("applications")}
    for expected in ("state", "previous_state", "attempt_no", "idempotency_key",
                     "job_identity", "lease_owner", "lease_expires_at",
                     "verification_verdict", "run_id", "checkpoint"):
        assert expected in app_cols, f"missing applications.{expected}"

    job_cols = {c["name"] for c in inspector.get_columns("jobs")}
    assert "canonical_url" in job_cols and "job_identity" in job_cols

    learned_cols = {c["name"] for c in inspector.get_columns("learned_answers")}
    assert "provenance" in learned_cols and "human_confirmed" in learned_cols

    assert "runs" in inspector.get_table_names()
    assert "state_transitions" in inspector.get_table_names()


def test_migration_preserves_existing_rows(tmp_path):
    engine = _legacy_db(tmp_path)
    migrate(engine)

    session = sessionmaker(bind=engine, future=True)()
    assert session.query(Job).count() == 1
    assert session.query(Application).count() == 5
    session.close()


def test_legacy_attempted_rows_are_treated_as_unknown_not_retryable(tmp_path):
    """The critical backfill decision. A row left at "attempted" by the old
    code may have died mid-submit, so it must NOT be classified as safe to
    re-apply — that is the exact duplicate-application bug."""
    from jobbot.agent.states import ApplicationState, is_safe_to_auto_resume

    engine = _legacy_db(tmp_path)
    migrate(engine)

    session = sessionmaker(bind=engine, future=True)()
    attempted = session.get(Application, 2)
    assert attempted.state == ApplicationState.UNKNOWN.value
    assert not is_safe_to_auto_resume(ApplicationState(attempted.state))
    session.close()


@pytest.mark.parametrize(
    "app_id,expected_state",
    [(1, "SUBMITTED"), (3, "SKIPPED"), (4, "FAILED"), (5, "HUMAN_REVIEW")],
)
def test_legacy_statuses_map_to_correct_states(tmp_path, app_id, expected_state):
    engine = _legacy_db(tmp_path)
    migrate(engine)
    session = sessionmaker(bind=engine, future=True)()
    assert session.get(Application, app_id).state == expected_state
    session.close()


def test_backfills_job_identity_from_existing_url(tmp_path):
    engine = _legacy_db(tmp_path)
    migrate(engine)
    session = sessionmaker(bind=engine, future=True)()
    job = session.get(Job, 1)
    assert job.job_identity == "ats:greenhouse:12345"
    assert "gh_src" not in job.canonical_url  # tracking param stripped
    session.close()


def test_migration_is_idempotent(tmp_path):
    """Runs on every engine creation, so re-running must be a clean no-op."""
    engine = _legacy_db(tmp_path)
    first = migrate(engine)
    second = migrate(engine)

    assert first["columns_added"], "first migration should have added columns"
    assert second["columns_added"] == []
    assert second["states_backfilled"] == 0
    assert second["identities_backfilled"] == 0


def test_unique_index_on_idempotency_key_exists_and_allows_many_nulls(tmp_path):
    """Legacy rows have no key; SQLite treats NULLs as distinct, so they
    must not collide with each other."""
    engine = _legacy_db(tmp_path)
    migrate(engine)

    indexes = {ix["name"] for ix in inspect(engine).get_indexes("applications")}
    assert "uq_applications_idempotency_key" in indexes

    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT COUNT(*) FROM applications WHERE idempotency_key IS NULL")
        ).scalar()
    assert rows == 5  # all five legacy rows coexist


def test_fresh_database_gets_full_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.sqlite3'}", future=True)
    migrate(engine)
    tables = set(inspect(engine).get_table_names())
    assert {"jobs", "applications", "runs", "state_transitions",
            "learned_answers", "field_issues", "resume_profiles", "job_scores"} <= tables


def test_legacy_learned_answers_default_to_untrusted(tmp_path):
    """Pre-provenance rows must not be grandfathered into autofilling
    sensitive questions."""
    from jobbot.learning.provenance import may_autofill_sensitive
    from jobbot.models import LearnedAnswer

    engine = _legacy_db(tmp_path)
    migrate(engine)
    session = sessionmaker(bind=engine, future=True)()
    row = session.get(LearnedAnswer, 1)
    assert may_autofill_sensitive(row.provenance or "", bool(row.human_confirmed)) is False
    session.close()

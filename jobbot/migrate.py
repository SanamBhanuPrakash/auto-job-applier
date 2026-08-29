"""Additive, idempotent schema migration for the local SQLite database.

This project ships no Alembic setup and the database is a single local file
that users already have data in, so `Base.metadata.create_all` (which only
ever creates *missing tables*, never missing columns) is not enough once
columns are added to an existing model.

The rules here are deliberately conservative:
  - only ever ADD tables, columns and indexes; never drop or retype
  - every step is guarded, so running it repeatedly is a no-op
  - existing rows are backfilled, never deleted

Called automatically from db.get_engine(), so no user action is needed.
"""
from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from jobbot.models import Base

log = logging.getLogger(__name__)


def _sqlite_literal(value: object) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return str(value)
    return "'" + str(value).replace("'", "''") + "'"


def _default_literal(column) -> str:
    """A constant DEFAULT for ALTER TABLE ADD COLUMN.

    SQLite only accepts constant defaults here, so callables (e.g. _utcnow)
    and server-side defaults fall back to NULL; the ORM still applies the
    real default on every subsequent insert.
    """
    default = column.default
    if default is None or getattr(default, "is_callable", False):
        return "NULL"
    arg = getattr(default, "arg", None)
    if callable(arg):
        return "NULL"
    if arg is None:
        return "NULL"
    if isinstance(arg, (dict, list)):
        # JSON columns: store the empty container as text, matching what
        # SQLAlchemy's JSON type would serialize.
        return _sqlite_literal("{}" if isinstance(arg, dict) else "[]")
    return _sqlite_literal(arg)


def _add_missing_columns(engine: Engine) -> list[str]:
    """ALTER TABLE ADD COLUMN for every model column absent from the DB."""
    applied: list[str] = []
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue  # create_all handles brand-new tables
            present = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                col_type = column.type.compile(dialect=engine.dialect)
                ddl = (
                    f"ALTER TABLE {table.name} "
                    f"ADD COLUMN {column.name} {col_type} DEFAULT {_default_literal(column)}"
                )
                conn.execute(text(ddl))
                applied.append(f"{table.name}.{column.name}")
                log.info("Migration: added column %s.%s", table.name, column.name)
    return applied


def _ensure_indexes(engine: Engine) -> None:
    """Indexes that back correctness, not just speed.

    The unique index on applications.idempotency_key is the concurrency
    guarantee: two processes racing to start an attempt for the same
    posting cannot both win the INSERT. SQLite treats NULLs as distinct in
    a unique index, so legacy rows (which have no key) are unaffected.
    """
    statements = [
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_applications_idempotency_key "
        "ON applications (idempotency_key)",
        "CREATE INDEX IF NOT EXISTS ix_applications_job_identity "
        "ON applications (job_identity)",
        "CREATE INDEX IF NOT EXISTS ix_applications_state ON applications (state)",
        "CREATE INDEX IF NOT EXISTS ix_jobs_job_identity ON jobs (job_identity)",
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


#: Legacy free-text status -> the state-machine state it corresponds to.
#: "attempted" is deliberately mapped to UNKNOWN rather than to a pre-submit
#: state: a row left at "attempted" by the old code may have been killed
#: mid-submit, and treating it as safe-to-retry is exactly the bug this
#: work exists to close.
_LEGACY_STATUS_TO_STATE = {
    "submitted": "SUBMITTED",
    "skipped": "SKIPPED",
    "filled_pending_review": "HUMAN_REVIEW",
    "error": "FAILED",
    "attempted": "UNKNOWN",
}


def _backfill_states(engine: Engine) -> int:
    """Give pre-existing application rows a state-machine state."""
    updated = 0
    with engine.begin() as conn:
        for status, state in _LEGACY_STATUS_TO_STATE.items():
            result = conn.execute(
                text(
                    "UPDATE applications SET state = :state "
                    "WHERE (state IS NULL OR state = '') AND status = :status"
                ),
                {"state": state, "status": status},
            )
            updated += result.rowcount or 0
    if updated:
        log.info("Migration: backfilled state for %d existing application row(s)", updated)
    return updated


def _backfill_job_identity(engine: Engine) -> int:
    """Compute canonical_url/job_identity for jobs discovered before the
    identity layer existed, so duplicate protection covers them too."""
    from jobbot.agent.identity import canonical_url, job_identity

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, url, company, title, source, external_id FROM jobs "
                "WHERE job_identity IS NULL OR job_identity = ''"
            )
        ).fetchall()

        for row in rows:
            conn.execute(
                text("UPDATE jobs SET canonical_url = :cu, job_identity = :ji WHERE id = :id"),
                {
                    "cu": canonical_url(row.url or ""),
                    "ji": job_identity(
                        url=row.url or "",
                        company=row.company or "",
                        title=row.title or "",
                        source=row.source or "",
                        external_id=row.external_id or "",
                    ),
                    "id": row.id,
                },
            )
    if rows:
        log.info("Migration: backfilled identity for %d existing job row(s)", len(rows))
    return len(rows)


def migrate(engine: Engine) -> dict:
    """Bring an existing database up to the current schema. Safe to re-run."""
    Base.metadata.create_all(engine)
    added = _add_missing_columns(engine)
    _ensure_indexes(engine)
    states = _backfill_states(engine)
    identities = _backfill_job_identity(engine)
    return {
        "columns_added": added,
        "states_backfilled": states,
        "identities_backfilled": identities,
    }

from __future__ import annotations

from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from jobbot.config import get_settings

_engine = None
_SessionLocal = None


def _configure_sqlite(dbapi_connection, _connection_record) -> None:
    """Pragmas that make concurrent access survivable.

    Without WAL, a second `jobbot run` blocks the first out of the database
    entirely; without busy_timeout it fails instantly with "database is
    locked" instead of waiting for the in-flight write. Both matter now
    that attempts are claimed via a unique-constraint race.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=10000")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def get_engine():
    global _engine
    if _engine is None:
        from jobbot.migrate import migrate

        settings = get_settings()
        _engine = create_engine(f"sqlite:///{settings.db_path}", future=True)
        event.listen(_engine, "connect", _configure_sqlite)
        migrate(_engine)
    return _engine


def get_sessionmaker() -> sessionmaker:
    global _SessionLocal
    if _SessionLocal is None:
        _SessionLocal = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _SessionLocal


@contextmanager
def session_scope() -> Session:
    Session_ = get_sessionmaker()
    session = Session_()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

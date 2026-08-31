"""The dashboard is read-only — it never drives a browser or clicks
anything. These lock in that its three columns categorize applications
correctly: a job mid-review must show as "current" and never appear in the
"up next" stack (it's already claimed), a submitted/skipped/failed one must
land in "recent" and also never reappear in the stack, and an untouched
scored job must appear in the stack ordered by score.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import jobbot.dashboard as dashboard
from jobbot.models import Application, Base, Job, JobScore


@pytest.fixture
def isolated_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def fake_session_scope():
        session = Session()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(dashboard, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        dashboard, "load_search_settings",
        lambda: {"submission": {"supported_ats": ["greenhouse", "lever"]}},
    )
    yield Session


_next_external_id = iter(range(1_000_000))


def _job(session_factory, *, ats="greenhouse", company="Acme", title="Engineer") -> Job:
    session = session_factory()
    job = Job(source=ats, external_id=str(next(_next_external_id)), company=company, title=title, url="https://x", ats=ats)
    session.add(job)
    session.commit()
    session.refresh(job)
    session.close()
    return job


def _score(session_factory, job_id: int, score: float) -> None:
    session = session_factory()
    session.add(JobScore(job_id=job_id, llm_score=score))
    session.commit()
    session.close()


def _application(session_factory, job_id: int, state: str) -> None:
    session = session_factory()
    session.add(Application(job_id=job_id, status="", state=state))
    session.commit()
    session.close()


def test_untouched_scored_job_appears_in_the_stack(isolated_db):
    job = _job(isolated_db)
    _score(isolated_db, job.id, 80)

    data = dashboard._queue_data(min_score=60, limit=10)

    assert data["current"] is None
    assert [row["id"] for row in data["stack"]] == [job.id]
    assert data["recent"] == []


def test_stack_is_ordered_by_score_descending(isolated_db):
    low = _job(isolated_db, company="Low")
    high = _job(isolated_db, company="High")
    _score(isolated_db, low.id, 61)
    _score(isolated_db, high.id, 95)

    data = dashboard._queue_data(min_score=60, limit=10)

    assert [row["company"] for row in data["stack"]] == ["High", "Low"]


def test_job_below_threshold_is_excluded_from_the_stack(isolated_db):
    job = _job(isolated_db)
    _score(isolated_db, job.id, 40)

    data = dashboard._queue_data(min_score=60, limit=10)

    assert data["stack"] == []


def test_job_with_unsupported_ats_is_excluded_from_the_stack(isolated_db):
    job = _job(isolated_db, ats="smartrecruiters")
    _score(isolated_db, job.id, 90)

    data = dashboard._queue_data(min_score=60, limit=10)

    assert data["stack"] == []


def test_job_mid_review_shows_as_current_and_not_in_the_stack(isolated_db):
    job = _job(isolated_db, company="MidReview")
    _score(isolated_db, job.id, 90)
    _application(isolated_db, job.id, "HUMAN_REVIEW")

    data = dashboard._queue_data(min_score=60, limit=10)

    assert data["current"] is not None
    assert data["current"]["company"] == "MidReview"
    assert data["stack"] == []


def test_submitted_job_appears_in_recent_and_not_in_the_stack(isolated_db):
    job = _job(isolated_db, company="Submitted")
    _score(isolated_db, job.id, 90)
    _application(isolated_db, job.id, "SUBMITTED")

    data = dashboard._queue_data(min_score=60, limit=10)

    assert data["stack"] == []
    assert [row["company"] for row in data["recent"]] == ["Submitted"]
    assert data["recent"][0]["state"] == "SUBMITTED"


def test_stack_respects_the_limit(isolated_db):
    for i in range(5):
        job = _job(isolated_db, company=f"Co{i}")
        _score(isolated_db, job.id, 70 + i)

    data = dashboard._queue_data(min_score=60, limit=2)

    assert len(data["stack"]) == 2

"""score_shortlist scores potentially thousands of jobs against a free-tier
LLM in small batches over a run that can take hours. A single unrecoverable
batch — confirmed live: a malformed-JSON generation that exhausted its
retries in jobbot/llm.py — must not crash the whole run and strand every
other batch unscored. This exercises the catch-and-continue behavior in
jobbot/matching/score.py directly, against an isolated in-memory DB.
"""
import re
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from jobbot.llm import DailyQuotaExceeded
from jobbot.matching import score as score_module
from jobbot.models import Base, Job, JobScore
from jobbot.resume import multi as multi_module


@pytest.fixture
def isolated_db(monkeypatch):
    """Points both score.py's and resume/multi.py's session_scope at a
    throwaway in-memory DB — score_shortlist calls into both modules."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def fake_session_scope():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(score_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(multi_module, "session_scope", fake_session_scope)
    # score_shortlist always loads the single-resume profile as a fallback
    # even when config/resumes/ profiles exist — real config/profile.yaml
    # isn't guaranteed to exist wherever this test runs, so stub it out.
    monkeypatch.setattr(score_module, "load_profile_raw", lambda: {"name": "Test Candidate", "email": "test@example.com"})
    yield Session


def _make_jobs(session_factory, n: int) -> list[Job]:
    session = session_factory()
    jobs = [
        Job(source="greenhouse", external_id=str(i), company="Acme", title="Engineer", url="https://x", description="")
        for i in range(n)
    ]
    session.add_all(jobs)
    session.commit()
    for job in jobs:
        session.refresh(job)
    session.close()
    return jobs


def test_one_failing_batch_does_not_abort_the_rest(isolated_db, monkeypatch):
    jobs = _make_jobs(isolated_db, 8)
    monkeypatch.setattr(score_module, "BATCH_SIZE", 4)

    calls = {"n": 0}

    def fake_call_tool(*, user_message, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Error code: 400 - ... 'code': 'tool_use_failed' ... (retries exhausted)")
        ids = [int(m) for m in re.findall(r"job_id: (\d+)", user_message)]
        return {"scores": [{"job_id": i, "score": 70, "reasoning": "ok"} for i in ids]}

    monkeypatch.setattr(score_module, "call_tool", fake_call_tool)

    score_module.score_shortlist([(job, 50.0) for job in jobs])

    assert calls["n"] == 2  # first batch failed, second still ran
    session = isolated_db()
    scored_ids = {row.job_id for row in session.execute(select(JobScore)).scalars()}
    session.close()
    # the first batch (4 jobs) was lost; the second batch (4 jobs) was scored
    assert len(scored_ids) == 4


def test_daily_quota_exhaustion_stops_the_whole_run_instead_of_wasting_calls(isolated_db, monkeypatch):
    jobs = _make_jobs(isolated_db, 8)
    monkeypatch.setattr(score_module, "BATCH_SIZE", 4)

    calls = {"n": 0}

    def fake_call_tool(**_kwargs):
        calls["n"] += 1
        raise DailyQuotaExceeded("daily quota exhausted for today")

    monkeypatch.setattr(score_module, "call_tool", fake_call_tool)

    with pytest.raises(DailyQuotaExceeded):
        score_module.score_shortlist([(job, 50.0) for job in jobs])

    assert calls["n"] == 1  # stopped immediately, didn't burn through every remaining batch

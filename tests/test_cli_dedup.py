"""Tests the already-applied dedup logic directly: `jobbot batch` must
never re-attempt a job with a submitted Application on record, and
`jobbot apply` must refuse (without --force) on one too. Both funnel
through cli._already_submitted_job_ids, which takes an explicit session so
it's testable without touching the real DB.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.cli import _already_submitted_job_ids
from jobbot.models import Application, Base, Job


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    yield s
    s.close()


def _job(session, external_id: str) -> Job:
    job = Job(source="greenhouse", external_id=external_id, company="Acme", title="Engineer", url="https://example.com")
    session.add(job)
    session.commit()
    return job


def test_no_applications_means_nothing_excluded(session):
    job = _job(session, "1")
    assert _already_submitted_job_ids(session) == set()


def test_submitted_application_excludes_job(session):
    job = _job(session, "1")
    session.add(Application(job_id=job.id, status="submitted"))
    session.commit()
    assert _already_submitted_job_ids(session) == {job.id}


def test_skipped_or_error_application_does_not_exclude_job(session):
    job1 = _job(session, "1")
    job2 = _job(session, "2")
    session.add(Application(job_id=job1.id, status="skipped"))
    session.add(Application(job_id=job2.id, status="error"))
    session.commit()
    assert _already_submitted_job_ids(session) == set()


def test_multiple_submitted_applications_for_same_job_counted_once(session):
    job = _job(session, "1")
    session.add(Application(job_id=job.id, status="error"))
    session.add(Application(job_id=job.id, status="submitted"))
    session.commit()
    assert _already_submitted_job_ids(session) == {job.id}


def test_mixed_jobs_only_submitted_ones_excluded(session):
    submitted = _job(session, "1")
    skipped = _job(session, "2")
    untouched = _job(session, "3")
    session.add(Application(job_id=submitted.id, status="submitted"))
    session.add(Application(job_id=skipped.id, status="skipped"))
    session.commit()

    excluded = _already_submitted_job_ids(session)
    assert excluded == {submitted.id}
    assert skipped.id not in excluded
    assert untouched.id not in excluded

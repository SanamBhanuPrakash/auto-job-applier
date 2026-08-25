from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, DateTime, Float, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_job_source_external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)  # e.g. "greenhouse"
    external_id: Mapped[str] = mapped_column(String(256))
    company: Mapped[str] = mapped_column(String(256), index=True)
    title: Mapped[str] = mapped_column(String(512))
    location: Mapped[str] = mapped_column(String(256), default="")
    remote: Mapped[bool] = mapped_column(default=False)
    url: Mapped[str] = mapped_column(String(1024))
    description: Mapped[str] = mapped_column(Text, default="")
    posted_at: Mapped[str] = mapped_column(String(64), default="")
    ats: Mapped[str] = mapped_column(String(32), default="")  # normalized ATS type for submission routing
    discovered_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    # Tag of the ResumeProfile jobbot decided fits this job best (set by
    # `jobbot match` when multiple resumes are configured). Empty means
    # either no resumes/ folder was imported, or nothing matched — falls
    # back to the single config/profile.yaml.
    matched_profile_tag: Mapped[str] = mapped_column(String(128), default="")

    score: Mapped["JobScore"] = relationship(back_populates="job", uselist=False, cascade="all, delete-orphan")
    applications: Mapped[list["Application"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class JobScore(Base):
    __tablename__ = "job_scores"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), unique=True)
    lexical_score: Mapped[float] = mapped_column(Float, default=0.0)
    llm_score: Mapped[float] = mapped_column(Float, default=0.0)
    llm_reasoning: Mapped[str] = mapped_column(Text, default="")
    scored_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="score")


class Application(Base):
    __tablename__ = "applications"

    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"))
    status: Mapped[str] = mapped_column(String(32), default="attempted")
    # attempted | filled_pending_review | submitted | skipped | error
    fill_plan: Mapped[dict] = mapped_column(JSON, default=dict)
    fields_needing_human: Mapped[dict] = mapped_column(JSON, default=dict)
    screenshot_path: Mapped[str] = mapped_column(String(1024), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    job: Mapped[Job] = relationship(back_populates="applications")


class LearnedAnswer(Base):
    """A remembered answer to a recurring application question, keyed by a
    normalized version of its label so future postings that ask the same
    thing in slightly different wording still match. See jobbot/learning/.
    """

    __tablename__ = "learned_answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    label_raw: Mapped[str] = mapped_column(String(512))
    field_type: Mapped[str] = mapped_column(String(32))
    value: Mapped[str] = mapped_column(Text)
    # Sensitive answers (work auth, EEOC, legal attestations, ...) are only
    # auto-filled when JOBBOT_AUTOFILL_SENSITIVE is explicitly enabled AND
    # the run's one-time confirmation has been accepted — see
    # jobbot/submit/base.py and cli.py's _confirm_sensitive_autofill.
    sensitive: Mapped[bool] = mapped_column(default=False)
    times_used: Mapped[int] = mapped_column(default=1)
    last_used_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class ResumeProfile(Base):
    """One parsed+tagged resume from config/resumes/. When this table has
    rows, `jobbot match` picks the best-fitting one per job instead of the
    single global config/profile.yaml — see jobbot/resume/multi.py.
    """

    __tablename__ = "resume_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    tag: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    resume_path: Mapped[str] = mapped_column(String(1024))
    profile_json: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class FieldIssue(Base):
    """Tracks repeated auto-fill failures for a question, keyed the same way
    as LearnedAnswer. Once failure_count crosses the circuit-breaker
    threshold, jobbot stops attempting to auto-fill that question and routes
    straight to human review instead of retrying the same failure forever.
    See jobbot/learning/store.py record_failure/is_circuit_broken.
    """

    __tablename__ = "field_issues"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_key: Mapped[str] = mapped_column(String(512), unique=True, index=True)
    label_raw: Mapped[str] = mapped_column(String(512), default="")
    failure_count: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

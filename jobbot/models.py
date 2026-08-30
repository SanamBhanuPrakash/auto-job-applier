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
    # Cross-source identity (jobbot/agent/identity.py). (source, external_id)
    # below is unique per *source*, so the same posting found via a company
    # board and via an aggregator yields two rows; job_identity collapses
    # those for duplicate-application protection.
    canonical_url: Mapped[str] = mapped_column(String(1024), default="")
    job_identity: Mapped[str] = mapped_column(String(128), default="", index=True)
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
    job_id: Mapped[int] = mapped_column(ForeignKey("jobs.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="attempted")
    # Legacy free-text status, kept so existing ledgers/queries keep working.
    # attempted | filled_pending_review | submitted | skipped | error
    # `state` below is the authoritative machine-checked value.
    fill_plan: Mapped[dict] = mapped_column(JSON, default=dict)
    fields_needing_human: Mapped[dict] = mapped_column(JSON, default=dict)
    screenshot_path: Mapped[str] = mapped_column(String(1024), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # --- agent state machine (jobbot/agent/states.py) --------------------
    state: Mapped[str] = mapped_column(String(32), default="", index=True)
    previous_state: Mapped[str] = mapped_column(String(32), default="")
    attempt_no: Mapped[int] = mapped_column(default=1)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    # --- idempotency (jobbot/agent/identity.py) --------------------------
    # UNIQUE: a second process trying to start an attempt for the same
    # posting loses the INSERT race in the database rather than racing us
    # into a duplicate submission. NULL for pre-existing legacy rows, and
    # SQLite permits many NULLs in a unique column, so old data is fine.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), unique=True, nullable=True)
    job_identity: Mapped[str] = mapped_column(String(128), default="", index=True)
    canonical_url: Mapped[str] = mapped_column(String(1024), default="")
    submission_fingerprint: Mapped[str] = mapped_column(String(64), default="")

    # --- crash-safe leasing ---------------------------------------------
    lease_owner: Mapped[str] = mapped_column(String(64), default="")
    lease_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # --- submission verification (jobbot/submit/verify.py) ---------------
    verification_verdict: Mapped[str] = mapped_column(String(32), default="")
    verification_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    post_submit_screenshot: Mapped[str] = mapped_column(String(1024), default="")

    # --- failure handling ------------------------------------------------
    failure_category: Mapped[str] = mapped_column(String(32), default="")
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    run_id: Mapped[str] = mapped_column(String(64), default="", index=True)

    job: Mapped[Job] = relationship(back_populates="applications")


class Run(Base):
    """One invocation of the agent. Gives every application attempt a
    traceable parent ("what did last night's run do?") and carries the
    budget counters that bound the run."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    command: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="RUNNING")  # RUNNING|COMPLETED|ABORTED|FAILED
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    jobs_considered: Mapped[int] = mapped_column(default=0)
    applications_attempted: Mapped[int] = mapped_column(default=0)
    applications_submitted: Mapped[int] = mapped_column(default=0)
    llm_calls: Mapped[int] = mapped_column(default=0)
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    notes: Mapped[str] = mapped_column(Text, default="")


class StateTransition(Base):
    """Append-only audit of every state change. This is what makes a crash
    recoverable: the last row for an application tells you exactly where it
    was, and `to_state` being in the danger zone is what stops a blind retry."""

    __tablename__ = "state_transitions"

    id: Mapped[int] = mapped_column(primary_key=True)
    application_id: Mapped[int] = mapped_column(ForeignKey("applications.id"), index=True)
    run_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    from_state: Mapped[str] = mapped_column(String(32), default="")
    to_state: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    failure_category: Mapped[str] = mapped_column(String(32), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


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

    # --- provenance ------------------------------------------------------
    # Without this, a value the *model guessed* on a form that was never
    # submitted is indistinguishable from one the *candidate typed* on an
    # application that succeeded — and the sensitive-autofill flow would
    # then present a hallucination back as "your saved answer" for a legal
    # question. See jobbot/learning/provenance.py.
    provenance: Mapped[str] = mapped_column(String(32), default="unverified")
    human_confirmed: Mapped[bool] = mapped_column(default=False)
    verified_submission_count: Mapped[int] = mapped_column(default=0)
    last_validated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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


class AccountRecord(Base):
    """What we know about our session on one domain (spec §25, §27).

    Deliberately contains **no credential material** — no password, no
    cookie, no token. Cookies live in the Chromium profile directory on
    disk, where the browser manages them; this table only records that a
    session existed, when it was last seen working, and how the last
    authentication attempt ended, so a run can decide whether to bother
    trying before it opens a page.

    Keeping secrets out of the database is not incidental. This file is
    read by report commands, dumped in bug reports, and synced by whatever
    backs up the data directory.
    """

    __tablename__ = "account_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain: Mapped[str] = mapped_column(String(256), unique=True, index=True)
    #: The identifier used to sign in. Not secret, and needed to tell two
    #: accounts on one domain apart.
    username: Mapped[str] = mapped_column(String(256), default="")
    #: Chromium profile directory holding this session's cookies.
    profile_dir: Mapped[str] = mapped_column(String(1024), default="")
    #: Last AuthOutcome, as a plain string.
    last_outcome: Mapped[str] = mapped_column(String(64), default="")
    last_auth_state: Mapped[str] = mapped_column(String(64), default="")
    #: When we last saw positive evidence of being signed in.
    last_verified_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    last_attempt_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    #: Consecutive failures. A domain that keeps failing is not retried
    #: automatically — repeated attempts are how accounts get locked.
    consecutive_failures: Mapped[int] = mapped_column(default=0)
    #: Redacted, human-readable note about the last attempt.
    note: Mapped[str] = mapped_column(Text, default="")

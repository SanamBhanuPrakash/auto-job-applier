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

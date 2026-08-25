"""LLM reranking of the lexical shortlist against the candidate's profile.

Only the shortlist (not every discovered job) goes through the LLM, and jobs
are batched to keep API usage low.
"""
from __future__ import annotations

import logging
from collections import defaultdict

from sqlalchemy import select

from jobbot.config import load_profile_raw
from jobbot.db import session_scope
from jobbot.llm import call_tool
from jobbot.matching.profile_select import best_profile_for_job
from jobbot.models import Job, JobScore
from jobbot.resume import multi as multi_resume
from jobbot.resume.schema import Profile

log = logging.getLogger(__name__)

# Kept small on purpose: Groq's free tier (the default provider) caps at
# 6,000 tokens/minute, and a batch's job descriptions dominate the prompt
# size. This size + the 800-char description truncation below comfortably
# fits one batch call in that budget; jobbot/llm.py retries with backoff if
# a call still gets rate-limited. Raise it freely if you're on
# LLM_PROVIDER=anthropic, which has no such per-minute ceiling at this scale.
BATCH_SIZE = 4

_SCORE_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "scores": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                    "score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "reasoning": {"type": "string"},
                },
                "required": ["job_id", "score", "reasoning"],
            },
        }
    },
    "required": ["scores"],
}

_SYSTEM = """You score how well each job posting fits a candidate's profile, 0-100.
Consider title/seniority match, required skills the candidate has or lacks,
location/remote fit, and desired titles/locations. Be honest about mediocre
fits — most jobs should not score above 80. Give one sentence of reasoning
per job. You are not a recruiter for the employer; you work for the
candidate, so weight the candidate's stated preferences heavily."""


def _profile_summary(profile: Profile) -> str:
    return profile.facts_json_for_llm()


def _batch_prompt(profile: Profile, jobs: list[Job]) -> str:
    job_blocks = []
    for job in jobs:
        desc = (job.description or "")[:800]
        job_blocks.append(
            f"job_id: {job.id}\ncompany: {job.company}\ntitle: {job.title}\n"
            f"location: {job.location}\nremote: {job.remote}\ndescription: {desc}"
        )
    return (
        f"Candidate profile:\n{_profile_summary(profile)}\n\n"
        f"Jobs to score:\n\n" + "\n\n---\n\n".join(job_blocks)
    )


def _score_batch(profile: Profile, batch: list[Job], lex_by_id: dict[int, float], tag: str) -> None:
    log.info("Scoring batch of %d job(s) against profile %r", len(batch), tag or "(default)")
    result = call_tool(
        system=_SYSTEM,
        user_message=_batch_prompt(profile, batch),
        tool_name="record_scores",
        tool_description="Record the fit score and reasoning for each job in this batch.",
        input_schema=_SCORE_TOOL_SCHEMA,
        max_tokens=1024,
    )
    by_id = {row["job_id"]: row for row in result.get("scores", [])}

    with session_scope() as session:
        for job in batch:
            row = by_id.get(job.id)
            if row is None:
                continue
            existing = session.execute(
                select(JobScore).where(JobScore.job_id == job.id)
            ).scalar_one_or_none()
            if existing is None:
                existing = JobScore(job_id=job.id)
                session.add(existing)
            existing.lexical_score = lex_by_id.get(job.id, 0.0)
            existing.llm_score = float(row["score"])
            existing.llm_reasoning = row["reasoning"]

            job_row = session.get(Job, job.id)
            if job_row is not None:
                job_row.matched_profile_tag = tag


def score_shortlist(shortlisted: list[tuple[Job, float]]) -> None:
    jobs = [job for job, _lex in shortlisted]
    lex_by_id = {job.id: lex for job, lex in shortlisted}

    profiles = multi_resume.list_profiles()
    default_profile = Profile.model_validate(load_profile_raw())

    if profiles:
        # Group jobs by whichever configured resume fits them best, so each
        # LLM batch call is scored against the right profile — a job matched
        # to "python-developer" and one matched to "frontend" shouldn't
        # share a scoring call meant for only one candidate profile.
        jobs_by_tag: dict[str, list[Job]] = defaultdict(list)
        for job in jobs:
            tag, _fit = best_profile_for_job(job, profiles)
            jobs_by_tag[tag].append(job)
        profile_by_tag = {p.tag: Profile.model_validate(p.profile_json) for p in profiles}
    else:
        jobs_by_tag = {"": jobs}
        profile_by_tag = {}

    for tag, tag_jobs in jobs_by_tag.items():
        profile = profile_by_tag.get(tag, default_profile)
        for i in range(0, len(tag_jobs), BATCH_SIZE):
            batch = tag_jobs[i : i + BATCH_SIZE]
            _score_batch(profile, batch, lex_by_id, tag)

"""Picks which resume/profile best fits a job posting, when multiple are
configured via config/resumes/. Pure lexical scoring, no LLM call — this
has to run before jobbot/matching/score.py's LLM reranking, to know which
profile's facts to hand the model, and doesn't need to be exact: it's
choosing between resumes you already wrote for different roles (SDE vs.
frontend vs. cloud engineer), not judging fit precisely.
"""
from __future__ import annotations

from rapidfuzz import fuzz

from jobbot.models import Job, ResumeProfile
from jobbot.resume.schema import Profile


def _profile_keywords(profile_row: ResumeProfile) -> str:
    profile = Profile.model_validate(profile_row.profile_json)
    parts = [profile_row.tag.replace("-", " ")]
    parts.extend(profile.desired_titles)
    parts.extend(profile.skills[:30])
    if profile.experience:
        parts.append(profile.experience[0].title)
    return " ".join(parts).lower()


def best_profile_for_job(job: Job, profiles: list[ResumeProfile]) -> tuple[str, float]:
    """Returns (tag, score) for the best-matching profile among `profiles`,
    or ("", 0.0) if `profiles` is empty — callers should fall back to the
    single global config/profile.yaml in that case."""
    if not profiles:
        return "", 0.0

    haystack = f"{job.title} {(job.description or '')[:1000]}".lower()
    title_words = {w for w in job.title.lower().split() if len(w) > 3}

    best_tag, best_score = "", -1.0
    for row in profiles:
        keywords = _profile_keywords(row)
        score = fuzz.token_set_ratio(haystack, keywords)
        # Nudge by direct keyword-in-title hits — a resume tagged
        # "python-developer" whose keywords include "python" and the job
        # title literally says "Python" should win over token-set noise.
        score += 3 * sum(1 for w in keywords.split() if w in title_words)
        if score > best_score:
            best_score = score
            best_tag = row.tag

    return best_tag, best_score

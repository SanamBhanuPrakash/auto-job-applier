"""Picks which resume/profile best fits a job posting, when multiple are
configured via config/resumes/. Pure lexical scoring, no LLM call — this
has to run before jobbot/matching/score.py's LLM reranking, to know which
profile's facts to hand the model, and doesn't need to be exact: it's
choosing between resumes you already wrote for different roles (SDE vs.
frontend vs. cloud engineer), not judging fit precisely.

Real bug found running this against 9 real resumes: skills lists across
resumes written by the same person for different role variants overlap
heavily (the same core stack — FastAPI, Docker, PostgreSQL, React, ... —
listed on nearly every one of them). Comparing raw token_set_ratio against
the full skills list meant that shared boilerplate dominated the score and
one profile won almost every job regardless of actual fit (verified: the
winning margin between the top match and 5 runner-ups was often just 2-5
points out of 100 — noise, not signal). Fixed by only counting each
profile's *distinctive* skills — the ones a minority of your other resumes
also list — and weighting the tag name (the cleanest, least-overlapping
signal there is) much more heavily than any of the skill soup.
"""
from __future__ import annotations

from collections import Counter

from rapidfuzz import fuzz

from jobbot.models import Job, ResumeProfile
from jobbot.resume.schema import Profile


def _skill_frequency(profiles: list[ResumeProfile]) -> Counter:
    """How many of the given profiles list each skill (case-insensitive) —
    used to tell "the ML resume's PyTorch" (rare, distinctive) from "every
    resume's Docker" (common, not distinctive) apart."""
    counter: Counter[str] = Counter()
    for row in profiles:
        profile = Profile.model_validate(row.profile_json)
        for skill in {s.lower() for s in profile.skills}:
            counter[skill] += 1
    return counter


def _distinctive_keywords(profile_row: ResumeProfile, skill_freq: Counter, total_profiles: int) -> str:
    profile = Profile.model_validate(profile_row.profile_json)
    parts = [profile_row.tag.replace("-", " ")]
    parts.extend(profile.desired_titles)
    if profile.experience:
        parts.append(profile.experience[0].title)

    # A skill shared by most of your OTHER resumes carries ~no discriminating
    # signal for THIS one — only skills held by a minority are distinctive.
    majority = max(2, (total_profiles + 1) // 2)
    for skill in profile.skills:
        if skill_freq.get(skill.lower(), 0) <= majority:
            parts.append(skill)

    return " ".join(parts).lower()


def best_profile_for_job(job: Job, profiles: list[ResumeProfile]) -> tuple[str, float]:
    """Returns (tag, score) for the best-matching profile among `profiles`,
    or ("", 0.0) if `profiles` is empty — callers should fall back to the
    single global config/profile.yaml in that case."""
    if not profiles:
        return "", 0.0

    haystack = f"{job.title} {(job.description or '')[:1000]}".lower()
    title_words = {w for w in job.title.lower().split() if len(w) > 3}
    skill_freq = _skill_frequency(profiles)

    best_tag, best_score = "", -1.0
    for row in profiles:
        keywords = _distinctive_keywords(row, skill_freq, len(profiles))
        score = fuzz.token_set_ratio(haystack, keywords)

        # Tag-word overlap with the job title is the single cleanest signal
        # here — it can't suffer the cross-resume skill overlap this
        # module's docstring describes, so it's weighted well above the
        # general keyword-in-title bonus below. (token_set_ratio itself
        # can't be "weighted by repeating the tag" — verified it operates
        # on a de-duplicated token set, so repetition has zero effect;
        # this explicit bonus is the actual mechanism.)
        tag_words = {w for w in row.tag.replace("-", " ").split() if len(w) > 3}
        score += 15 * len(tag_words & title_words)
        # Nudge by direct keyword-in-title hits — a resume tagged
        # "python-developer" whose keywords include "python" and the job
        # title literally says "Python" should win over token-set noise.
        score += 3 * sum(1 for w in set(keywords.split()) if w in title_words)

        if score > best_score:
            best_score = score
            best_tag = row.tag

    return best_tag, best_score

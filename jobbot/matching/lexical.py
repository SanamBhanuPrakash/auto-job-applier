"""Cheap first-pass filter/scorer, no API calls. Runs over every discovered
job before the (rate-limited, costlier) LLM reranker sees a shortlist.
"""
from __future__ import annotations

from rapidfuzz import fuzz

from jobbot.models import Job


def lexical_score(job: Job, settings: dict) -> float:
    """0-100. Combines keyword presence, exclusion keywords, and location match."""
    search_cfg = settings.get("search", {}) or {}
    keywords = [k.lower() for k in search_cfg.get("keywords", [])]
    exclude = [k.lower() for k in search_cfg.get("exclude_keywords", [])]
    locations = [l.lower() for l in search_cfg.get("locations", [])]
    remote_only = bool(search_cfg.get("remote_only", False))

    haystack = f"{job.title}\n{job.description}".lower()

    if any(bad in haystack for bad in exclude):
        return 0.0
    if remote_only and not job.remote:
        return 0.0

    score = 0.0
    if keywords:
        hits = sum(1 for kw in keywords if kw in haystack)
        score += 60.0 * (hits / len(keywords))
    else:
        score += 30.0

    if locations:
        loc_hit = job.remote or any(
            fuzz.partial_ratio(loc, job.location.lower()) > 80 for loc in locations
        )
        score += 40.0 if loc_hit else 0.0
    else:
        score += 20.0

    return min(score, 100.0)


def shortlist(jobs: list[Job], settings: dict, top_n: int = 50) -> list[tuple[Job, float]]:
    scored = [(job, lexical_score(job, settings)) for job in jobs]
    scored = [pair for pair in scored if pair[1] > 0]
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:top_n]

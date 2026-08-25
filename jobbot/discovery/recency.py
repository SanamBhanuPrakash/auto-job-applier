"""Parses each discovery source's own `posted_at` format into a real
datetime, and filters postings to only the recent ones. Every source uses a
different shape (ISO8601 with different precisions, epoch milliseconds,
date-only strings), so this is deliberately permissive: anything it can't
parse is kept rather than silently dropped — a parsing gap should never be
the reason a real posting goes missing.
"""
from __future__ import annotations

import datetime as dt
import logging

from jobbot.discovery.base import NormalizedJob

log = logging.getLogger(__name__)


def parse_posted_at(raw: str) -> dt.datetime | None:
    if not raw:
        return None
    raw = raw.strip()

    # Lever ships createdAt as epoch milliseconds (as a string, via str(int)).
    if raw.isdigit():
        try:
            return dt.datetime.fromtimestamp(int(raw) / 1000, tz=dt.timezone.utc)
        except (ValueError, OSError, OverflowError):
            return None

    # Everything else (Greenhouse, Ashby, SmartRecruiters, Recruitee, Adzuna,
    # RemoteOK, Remotive: ISO8601 variants; USAJOBS: date-only "YYYY-MM-DD").
    candidate = raw.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(candidate)
    except ValueError:
        try:
            parsed = dt.datetime.strptime(raw[:10], "%Y-%m-%d")
        except ValueError:
            return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def filter_recent(jobs: list[NormalizedJob], max_age_days: float) -> list[NormalizedJob]:
    if not max_age_days or max_age_days <= 0:
        return jobs

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=max_age_days)
    kept = []
    unparsed = 0
    dropped = 0
    for job in jobs:
        posted = parse_posted_at(job.posted_at)
        if posted is None:
            unparsed += 1
            kept.append(job)  # can't tell how old it is — keep it rather than guess
            continue
        if posted >= cutoff:
            kept.append(job)
        else:
            dropped += 1

    log.info(
        "Recency filter (<=%.1f days): kept %d, dropped %d as stale, %d had no parseable date (kept anyway)",
        max_age_days, len(kept) - unparsed, dropped, unparsed,
    )
    return kept

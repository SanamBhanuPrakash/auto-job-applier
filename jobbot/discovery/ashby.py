"""Ashby public job-board API. No auth required."""
from __future__ import annotations

import logging

import httpx

from jobbot.discovery.base import NormalizedJob
from jobbot.utils.ratelimit import http_retry
from jobbot.utils.textclean import strip_html

log = logging.getLogger(__name__)

BASE_URL = "https://api.ashbyhq.com/posting-api/job-board/{board}"


@http_retry
def _fetch(client: httpx.Client, board: str) -> dict:
    resp = client.get(BASE_URL.format(board=board), params={"includeCompensation": "true"}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_jobs(company_slug: str, client: httpx.Client | None = None) -> list[NormalizedJob]:
    owns_client = client is None
    client = client or httpx.Client()
    try:
        data = _fetch(client, company_slug)
    except httpx.HTTPStatusError as exc:
        log.warning("Ashby fetch failed for %s: %s", company_slug, exc)
        return []
    finally:
        if owns_client:
            client.close()

    jobs: list[NormalizedJob] = []
    for item in data.get("jobs", []):
        location = item.get("location", "") or ""
        jobs.append(
            NormalizedJob(
                source="ashby",
                external_id=str(item.get("id", "")),
                company=company_slug,
                title=item.get("title", ""),
                url=item.get("jobUrl", item.get("applyUrl", "")),
                location=location,
                remote=bool(item.get("isRemote")) or "remote" in location.lower(),
                description=strip_html(item.get("descriptionPlain", "") or item.get("description", "") or ""),
                posted_at=item.get("publishedAt", ""),
                # Ashby now has a submission handler (jobbot/submit/ashby.py).
                # Its apply form is a React SPA at <jobUrl>/application, which
                # is why that module waits for hydration and rewrites the URL.
                ats="ashby",
                raw=item,
            )
        )
    return jobs

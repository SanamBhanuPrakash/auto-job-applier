"""Remotive public JSON API. No auth required.

Docs: https://remotive.com/api-documentation
"""
from __future__ import annotations

import logging

import httpx

from jobbot.discovery.base import NormalizedJob
from jobbot.utils.ratelimit import http_retry
from jobbot.utils.textclean import strip_html

log = logging.getLogger(__name__)

BASE_URL = "https://remotive.com/api/remote-jobs"


@http_retry
def _fetch(client: httpx.Client, category: str) -> dict:
    params = {"category": category} if category else {}
    resp = client.get(BASE_URL, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_jobs(category: str = "", client: httpx.Client | None = None) -> list[NormalizedJob]:
    owns_client = client is None
    client = client or httpx.Client()
    try:
        data = _fetch(client, category)
    except httpx.HTTPStatusError as exc:
        log.warning("Remotive fetch failed: %s", exc)
        return []
    finally:
        if owns_client:
            client.close()

    jobs: list[NormalizedJob] = []
    for item in data.get("jobs", []):
        jobs.append(
            NormalizedJob(
                source="remotive",
                external_id=str(item.get("id", "")),
                company=item.get("company_name", ""),
                title=item.get("title", ""),
                url=item.get("url", ""),
                location=item.get("candidate_required_location", "") or "Remote",
                remote=True,
                description=strip_html(item.get("description", "") or ""),
                posted_at=item.get("publication_date", ""),
                ats="",
                raw=item,
            )
        )
    return jobs

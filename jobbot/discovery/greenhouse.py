"""Greenhouse public job-board API. No auth required.

Docs: https://developers.greenhouse.io/job-board.html
"""
from __future__ import annotations

import logging

import httpx

from jobbot.discovery.base import NormalizedJob
from jobbot.utils.ratelimit import http_retry
from jobbot.utils.textclean import strip_html

log = logging.getLogger(__name__)

BASE_URL = "https://boards-api.greenhouse.io/v1/boards/{token}/jobs"


@http_retry
def _fetch(client: httpx.Client, token: str) -> dict:
    resp = client.get(BASE_URL.format(token=token), params={"content": "true"}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_jobs(company_slug: str, client: httpx.Client | None = None) -> list[NormalizedJob]:
    owns_client = client is None
    client = client or httpx.Client()
    try:
        data = _fetch(client, company_slug)
    except httpx.HTTPStatusError as exc:
        log.warning("Greenhouse fetch failed for %s: %s", company_slug, exc)
        return []
    finally:
        if owns_client:
            client.close()

    jobs: list[NormalizedJob] = []
    for item in data.get("jobs", []):
        location = (item.get("location") or {}).get("name", "")
        jobs.append(
            NormalizedJob(
                source="greenhouse",
                external_id=str(item["id"]),
                company=company_slug,
                title=item.get("title", ""),
                url=item.get("absolute_url", ""),
                location=location,
                remote="remote" in location.lower(),
                # Greenhouse's `content` field is raw HTML, confirmed live
                # (e.g. `<div class="content-intro">...`) — see textclean.py.
                description=strip_html(item.get("content", "") or ""),
                posted_at=item.get("updated_at", ""),
                ats="greenhouse",
                raw=item,
            )
        )
    return jobs

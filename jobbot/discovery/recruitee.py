"""Recruitee public offers API. No auth required."""
from __future__ import annotations

import logging

import httpx

from jobbot.discovery.base import NormalizedJob
from jobbot.utils.ratelimit import http_retry

log = logging.getLogger(__name__)

BASE_URL = "https://{company}.recruitee.com/api/offers/"


@http_retry
def _fetch(client: httpx.Client, company: str) -> dict:
    resp = client.get(BASE_URL.format(company=company), timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_jobs(company_slug: str, client: httpx.Client | None = None) -> list[NormalizedJob]:
    owns_client = client is None
    client = client or httpx.Client()
    try:
        data = _fetch(client, company_slug)
    except httpx.HTTPStatusError as exc:
        log.warning("Recruitee fetch failed for %s: %s", company_slug, exc)
        return []
    finally:
        if owns_client:
            client.close()

    jobs: list[NormalizedJob] = []
    for item in data.get("offers", []):
        location = item.get("location", "") or ""
        jobs.append(
            NormalizedJob(
                source="recruitee",
                external_id=str(item.get("id", "")),
                company=company_slug,
                title=item.get("title", ""),
                url=item.get("careers_url", ""),
                location=location,
                remote=bool(item.get("remote")),
                description=item.get("description", "") or "",
                posted_at=item.get("created_at", ""),
                ats="",
                raw=item,
            )
        )
    return jobs

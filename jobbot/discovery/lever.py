"""Lever public postings API. No auth required.

Docs: https://github.com/lever/postings-api
"""
from __future__ import annotations

import logging

import httpx

from jobbot.discovery.base import NormalizedJob
from jobbot.utils.ratelimit import http_retry

log = logging.getLogger(__name__)

BASE_URL = "https://api.lever.co/v0/postings/{company}"


@http_retry
def _fetch(client: httpx.Client, company: str) -> list[dict]:
    resp = client.get(BASE_URL.format(company=company), params={"mode": "json"}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_jobs(company_slug: str, client: httpx.Client | None = None) -> list[NormalizedJob]:
    owns_client = client is None
    client = client or httpx.Client()
    try:
        data = _fetch(client, company_slug)
    except httpx.HTTPStatusError as exc:
        log.warning("Lever fetch failed for %s: %s", company_slug, exc)
        return []
    finally:
        if owns_client:
            client.close()

    jobs: list[NormalizedJob] = []
    for item in data:
        categories = item.get("categories", {}) or {}
        location = categories.get("location", "") or ""
        description = "\n".join(
            filter(None, [item.get("descriptionPlain", ""), item.get("additionalPlain", "")])
        )
        jobs.append(
            NormalizedJob(
                source="lever",
                external_id=str(item.get("id", "")),
                company=company_slug,
                title=item.get("text", ""),
                url=item.get("hostedUrl", item.get("applyUrl", "")),
                location=location,
                remote="remote" in location.lower(),
                description=description,
                posted_at=str(item.get("createdAt", "")),
                ats="lever",
                raw=item,
            )
        )
    return jobs

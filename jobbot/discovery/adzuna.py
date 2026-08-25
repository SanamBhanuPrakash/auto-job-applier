"""Adzuna aggregator API. Requires ADZUNA_APP_ID / ADZUNA_APP_KEY (free tier: 1000 calls/month).

Docs: https://developer.adzuna.com/
"""
from __future__ import annotations

import logging

import httpx

from jobbot.config import get_settings
from jobbot.discovery.base import NormalizedJob
from jobbot.utils.ratelimit import http_retry

log = logging.getLogger(__name__)

BASE_URL = "https://api.adzuna.com/v1/api/jobs/{country}/search/1"


@http_retry
def _fetch(client: httpx.Client, country: str, params: dict) -> dict:
    resp = client.get(BASE_URL.format(country=country), params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_jobs(
    what: str, where: str = "", country: str = "", client: httpx.Client | None = None
) -> list[NormalizedJob]:
    """`country` is an Adzuna market code (e.g. "in", "us", "gb") — defaults
    to ADZUNA_COUNTRY when not given. Adzuna covers 18 countries (as of
    checking: US/UK/Canada/Australia/Germany/France/Spain/Italy/Netherlands/
    Austria/Belgium/Brazil/India/Mexico/New Zealand/Poland/Singapore/South
    Africa) — call this once per market/city you want covered (see
    aggregate.py, which fans out over settings.yaml's aggregators.adzuna.queries)."""
    settings = get_settings()
    if not settings.adzuna_app_id or not settings.adzuna_app_key:
        log.info("Adzuna skipped: ADZUNA_APP_ID/ADZUNA_APP_KEY not set")
        return []

    owns_client = client is None
    client = client or httpx.Client()
    params = {
        "app_id": settings.adzuna_app_id,
        "app_key": settings.adzuna_app_key,
        "what": what,
        "results_per_page": 50,
    }
    if where:
        params["where"] = where

    try:
        data = _fetch(client, country or settings.adzuna_country, params)
    except httpx.HTTPStatusError as exc:
        log.warning("Adzuna fetch failed (what=%r where=%r country=%r): %s", what, where, country, exc)
        return []
    finally:
        if owns_client:
            client.close()

    jobs: list[NormalizedJob] = []
    for item in data.get("results", []):
        location = (item.get("location") or {}).get("display_name", "")
        jobs.append(
            NormalizedJob(
                source="adzuna",
                external_id=str(item.get("id", "")),
                company=(item.get("company") or {}).get("display_name", ""),
                title=item.get("title", ""),
                url=item.get("redirect_url", ""),
                location=location,
                remote="remote" in location.lower(),
                description=item.get("description", "") or "",
                posted_at=item.get("created", ""),
                ats="",
                raw=item,
            )
        )
    return jobs

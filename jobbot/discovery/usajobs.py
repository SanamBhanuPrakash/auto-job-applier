"""USAJOBS API. Requires a free API key + registered user-agent email.

Docs: https://developer.usajobs.gov/
"""
from __future__ import annotations

import logging

import httpx

from jobbot.config import get_settings
from jobbot.discovery.base import NormalizedJob
from jobbot.utils.ratelimit import http_retry

log = logging.getLogger(__name__)

BASE_URL = "https://data.usajobs.gov/api/search"


@http_retry
def _fetch(client: httpx.Client, headers: dict, params: dict) -> dict:
    resp = client.get(BASE_URL, headers=headers, params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_jobs(keyword: str, client: httpx.Client | None = None) -> list[NormalizedJob]:
    settings = get_settings()
    if not settings.usajobs_api_key or not settings.usajobs_user_agent_email:
        log.info("USAJOBS skipped: USAJOBS_API_KEY/USAJOBS_USER_AGENT_EMAIL not set")
        return []

    owns_client = client is None
    client = client or httpx.Client()
    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": settings.usajobs_user_agent_email,
        "Authorization-Key": settings.usajobs_api_key,
    }
    try:
        data = _fetch(client, headers, {"Keyword": keyword, "ResultsPerPage": 100})
    except httpx.HTTPStatusError as exc:
        log.warning("USAJOBS fetch failed: %s", exc)
        return []
    finally:
        if owns_client:
            client.close()

    jobs: list[NormalizedJob] = []
    for wrapper in data.get("SearchResult", {}).get("SearchResultItems", []):
        item = wrapper.get("MatchedObjectDescriptor", {})
        locations = item.get("PositionLocation", []) or []
        location = ", ".join(loc.get("LocationName", "") for loc in locations)
        jobs.append(
            NormalizedJob(
                source="usajobs",
                external_id=str(item.get("PositionID", "")),
                company=item.get("OrganizationName", ""),
                title=item.get("PositionTitle", ""),
                url=item.get("PositionURI", ""),
                location=location,
                remote=False,
                description=item.get("UserArea", {}).get("Details", {}).get("JobSummary", "") or "",
                posted_at=item.get("PublicationStartDate", ""),
                ats="",
                raw=item,
            )
        )
    return jobs

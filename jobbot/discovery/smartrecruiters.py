"""SmartRecruiters public postings API. No auth required."""
from __future__ import annotations

import logging

import httpx

from jobbot.discovery.base import NormalizedJob
from jobbot.utils.ratelimit import http_retry

log = logging.getLogger(__name__)

BASE_URL = "https://api.smartrecruiters.com/v1/companies/{company}/postings"


@http_retry
def _fetch_page(client: httpx.Client, company: str, offset: int) -> dict:
    resp = client.get(
        BASE_URL.format(company=company),
        params={"limit": 100, "offset": offset},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_jobs(company_slug: str, client: httpx.Client | None = None) -> list[NormalizedJob]:
    owns_client = client is None
    client = client or httpx.Client()
    jobs: list[NormalizedJob] = []
    try:
        offset = 0
        while True:
            try:
                data = _fetch_page(client, company_slug, offset)
            except httpx.HTTPStatusError as exc:
                log.warning("SmartRecruiters fetch failed for %s: %s", company_slug, exc)
                break

            content = data.get("content", [])
            if not content:
                break

            for item in content:
                location_obj = item.get("location", {}) or {}
                location = ", ".join(
                    filter(None, [location_obj.get("city"), location_obj.get("country")])
                )
                jobs.append(
                    NormalizedJob(
                        source="smartrecruiters",
                        external_id=str(item.get("id", "")),
                        company=company_slug,
                        title=item.get("name", ""),
                        url=item.get("applyUrl", item.get("ref", "")),
                        location=location,
                        remote=bool(location_obj.get("remote")),
                        description="",  # requires a per-posting detail call; kept empty to stay cheap
                        posted_at=item.get("releasedDate", ""),
                        ats="",
                        raw=item,
                    )
                )

            offset += len(content)
            if offset >= data.get("totalFound", 0):
                break
    finally:
        if owns_client:
            client.close()
    return jobs

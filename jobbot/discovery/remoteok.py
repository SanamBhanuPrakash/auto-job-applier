"""RemoteOK public JSON feed. No auth required."""
from __future__ import annotations

import logging

import httpx

from jobbot.discovery.base import NormalizedJob
from jobbot.utils.ratelimit import http_retry

log = logging.getLogger(__name__)

BASE_URL = "https://remoteok.com/api"


@http_retry
def _fetch(client: httpx.Client, tag: str) -> list[dict]:
    params = {"tag": tag} if tag else {}
    resp = client.get(BASE_URL, params=params, headers={"User-Agent": "jobbot/0.1"}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_jobs(tag: str = "", client: httpx.Client | None = None) -> list[NormalizedJob]:
    owns_client = client is None
    client = client or httpx.Client()
    try:
        data = _fetch(client, tag)
    except httpx.HTTPStatusError as exc:
        log.warning("RemoteOK fetch failed: %s", exc)
        return []
    finally:
        if owns_client:
            client.close()

    jobs: list[NormalizedJob] = []
    for item in data:
        if "id" not in item:  # first element is a legal/metadata blob, not a job
            continue
        jobs.append(
            NormalizedJob(
                source="remoteok",
                external_id=str(item.get("id", "")),
                company=item.get("company", ""),
                title=item.get("position", ""),
                url=item.get("url", ""),
                location=item.get("location", "") or "Remote",
                remote=True,
                description=item.get("description", "") or "",
                posted_at=item.get("date", ""),
                ats="",
                raw=item,
            )
        )
    return jobs

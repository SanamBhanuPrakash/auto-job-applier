"""Workday public job-listing API. No auth required for discovery — verified
live (POST to /wday/cxs/{tenant}/{site}/jobs returns real postings with no
credentials) against four different employers' Workday sites.

Discovery-only. Submission is NOT implemented: clicking "Apply" on a real
posting leads straight to a "Sign In" wall — Workday requires a candidate
account per employer tenant, the same guest-apply-vs-account-required
distinction that already separates Greenhouse/Lever (implemented) from
Wellfound/Cutshort (not implemented). See README "Extending to more ATSes /
sites".

Workday career sites don't have one global slug the way Greenhouse/Lever
do — each employer's URL encodes a tenant, a numbered pod ("wd5", "wd1",
...), and a site name, e.g.:
  https://automationanywhere.wd5.myworkdayjobs.com/en-US/automationanywherejobs
config/companies.yaml's `workday` list takes exactly that URL per employer
(copy it straight from the browser's address bar) — parse_workday_url()
below extracts what the API call needs from it.
"""
from __future__ import annotations

import logging
import re

import httpx

from jobbot.discovery.base import NormalizedJob
from jobbot.utils.ratelimit import http_retry

log = logging.getLogger(__name__)

_URL_RE = re.compile(
    r"https?://(?P<tenant>[\w-]+)\.(?P<pod>wd\d+)\.myworkdayjobs\.com/(?:(?P<locale>[\w-]+)/)?(?P<site>[\w-]+)/?$"
)

_PAGE_SIZE = 20


def parse_workday_url(careers_url: str) -> dict | None:
    m = _URL_RE.match(careers_url.strip())
    return m.groupdict() if m else None


@http_retry
def _fetch_page(client: httpx.Client, tenant: str, pod: str, site: str, offset: int) -> dict:
    api_url = f"https://{tenant}.{pod}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    resp = client.post(api_url, json={"limit": _PAGE_SIZE, "offset": offset, "searchText": ""}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def fetch_jobs(careers_url: str, client: httpx.Client | None = None) -> list[NormalizedJob]:
    parsed = parse_workday_url(careers_url)
    if not parsed:
        log.warning(
            "Could not parse Workday careers URL %r — expected "
            "https://<tenant>.wd<N>.myworkdayjobs.com/<locale>/<site>",
            careers_url,
        )
        return []

    tenant, pod = parsed["tenant"], parsed["pod"]
    locale, site = parsed.get("locale") or "en-US", parsed["site"]

    owns_client = client is None
    client = client or httpx.Client()
    jobs: list[NormalizedJob] = []
    try:
        offset = 0
        while True:
            try:
                data = _fetch_page(client, tenant, pod, site, offset)
            except httpx.HTTPStatusError as exc:
                log.warning("Workday fetch failed for %s: %s", tenant, exc)
                break

            postings = data.get("jobPostings", []) or []
            if not postings:
                break

            for item in postings:
                path = item.get("externalPath", "")
                location = item.get("locationsText", "") or ""
                jobs.append(
                    NormalizedJob(
                        source="workday",
                        external_id=f"{tenant}:{path}",
                        company=tenant,
                        title=item.get("title", ""),
                        url=f"https://{tenant}.{pod}.myworkdayjobs.com/{locale}/{site}{path}",
                        location=location,
                        remote="remote" in location.lower(),
                        # The listing endpoint doesn't include the full JD
                        # (only a per-posting detail call would) — left
                        # empty rather than adding an extra API call per
                        # posting; scoring still works off title/location.
                        description="",
                        # postedOn here is a relative string ("Posted 3 Days
                        # Ago"), not a parseable date — recency.py already
                        # keeps postings it can't parse a date for, so this
                        # degrades gracefully rather than filtering them out.
                        posted_at=item.get("postedOn", "") or "",
                        ats="",  # discovery only, see module docstring
                        raw=item,
                    )
                )

            offset += len(postings)
            if offset >= data.get("total", 0):
                break
    finally:
        if owns_client:
            client.close()
    return jobs

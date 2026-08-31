"""Reads schema.org JobPosting structured data straight off a company's own
career page. No API, no login, no third party — this is the exact markup a
company publishes on its own public page specifically so search engines can
index it (the same mechanism "Google for Jobs" itself runs on). Reading a
page's own declared metadata is a different thing from scraping a search
engine's results or a job board that doesn't want automated traffic — it's
the same legitimacy tier as Greenhouse/Lever's public JSON APIs, just for
companies that don't use one of those ATSes at all.

Real, live-checked limitation, not a guess: this only finds jobs where the
structured data is embedded on the page it's given — usually the individual
job posting page, sometimes the listing page if the company (or its ATS)
went out of its way to also mark up an ItemList of JobPostings there. A
company's plain listing page with no structured data at all yields nothing
here; that's expected, not a bug, and is exactly why this is one more
source alongside the others rather than a replacement for any of them.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from jobbot.discovery.base import NormalizedJob
from jobbot.utils.ratelimit import http_retry
from jobbot.utils.textclean import strip_html

log = logging.getLogger(__name__)

_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def _iter_job_postings(node) -> list[dict]:
    """A page's JSON-LD can nest a JobPosting inside a bare object, a list,
    an @graph, or an ItemList of ListItems each wrapping one — walk
    whatever shape shows up rather than assuming one."""
    found: list[dict] = []
    if isinstance(node, dict):
        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if "JobPosting" in types:
            found.append(node)
        for key in ("@graph", "itemListElement"):
            if key in node:
                found.extend(_iter_job_postings(node[key]))
        if "item" in node:
            found.extend(_iter_job_postings(node["item"]))
    elif isinstance(node, list):
        for item in node:
            found.extend(_iter_job_postings(item))
    return found


def _location_text(job: dict) -> str:
    loc = job.get("jobLocation")
    if isinstance(loc, list):
        loc = loc[0] if loc else None
    if not isinstance(loc, dict):
        return ""
    address = loc.get("address")
    if isinstance(address, dict):
        parts = [address.get("addressLocality", ""), address.get("addressRegion", ""), address.get("addressCountry", "")]
        return ", ".join(p for p in parts if p)
    return str(address or "")


def _is_remote(job: dict) -> bool:
    job_type = job.get("jobLocationType", "")
    if isinstance(job_type, str) and "telecommute" in job_type.lower():
        return True
    return "remote" in _location_text(job).lower()


def parse_job_postings(html_text: str, page_url: str) -> list[NormalizedJob]:
    jobs: list[NormalizedJob] = []
    for block in _LD_JSON_RE.findall(html_text):
        try:
            data = json.loads(block.strip())
        except (json.JSONDecodeError, ValueError):
            continue
        for posting in _iter_job_postings(data):
            title = posting.get("title", "")
            if not title:
                continue
            org = posting.get("hiringOrganization")
            company = org.get("name", "") if isinstance(org, dict) else (org or "")
            url = posting.get("url") or page_url
            external_id = str(posting.get("identifier", {}).get("value", "")) if isinstance(posting.get("identifier"), dict) else str(posting.get("identifier") or url)
            jobs.append(
                NormalizedJob(
                    source="structured_data",
                    external_id=external_id or url,
                    company=company,
                    title=title,
                    url=url,
                    location=_location_text(posting),
                    remote=_is_remote(posting),
                    description=strip_html(posting.get("description", "") or ""),
                    posted_at=posting.get("datePosted", ""),
                    ats="",  # discovery only — no submission handler for arbitrary company sites
                    raw=posting,
                )
            )
    return jobs


@http_retry
def _fetch(client: httpx.Client, url: str) -> str:
    resp = client.get(url, timeout=20, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def fetch_jobs(url: str, client: httpx.Client | None = None) -> list[NormalizedJob]:
    owns_client = client is None
    client = client or httpx.Client(headers={"User-Agent": "Mozilla/5.0 (compatible; jobbot/1.0)"})
    try:
        html_text = _fetch(client, url)
    except httpx.HTTPError as exc:
        log.warning("Structured-data fetch failed for %s: %s", url, exc)
        return []
    finally:
        if owns_client:
            client.close()

    return parse_job_postings(html_text, url)

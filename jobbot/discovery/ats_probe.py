"""Given a company name, find out whether it has a board we can apply through.

This is what turns Indeed from "a list of links you have to click" into
part of the automatic pipeline. Indeed's connector supplies no employer
application URL, so nothing discovered there can be submitted directly.
But if the company runs a Greenhouse, Lever or Ashby board, the same
posting is reachable through that board with a real, stable URL — and the
existing submission engine handles those already.

Proven live while this was written: an Indeed search surfaced Bounteous,
which was not in `config/companies.yaml`; probing found a Lever board with
36 open postings at `jobs.lever.co/bounteous/...`. That is the whole loop,
and it is why this module exists rather than leaving the user to guess
slugs by hand.

ATS boards have no "find company by name" endpoint, so this works by
generating plausible slugs from the name and asking each API. That is a
handful of cheap, unauthenticated GETs against public endpoints those
providers publish for exactly this purpose — the same tier of access the
existing discovery connectors already use.

A 404 means "not this slug", not "no board": a company can use a slug
nobody would guess. So a negative result is reported as *not found*, never
as *does not exist*.
"""
from __future__ import annotations

import logging
import re

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 12.0

#: Corporate suffixes that never appear in a board slug.
_SUFFIXES = re.compile(
    r"\b(pvt|private|ltd|limited|llc|llp|inc|incorporated|corp|corporation|"
    r"gmbh|plc|co|company|technologies|technology|solutions|systems|labs|"
    r"software|consulting|infotech|services|group|holdings)\b",
    re.IGNORECASE,
)

_BOARDS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
}


def slug_candidates(company: str, *, limit: int = 4) -> list[str]:
    """Plausible board slugs for a company name, most likely first.

    "Motorola Solutions" -> motorolasolutions, motorola-solutions, motorola
    """
    name = re.sub(r"[^\w\s&-]", " ", (company or "")).strip()
    if not name:
        return []
    words = [w for w in re.split(r"[\s&_-]+", name) if w]

    candidates: list[str] = []

    def add(value: str) -> None:
        value = value.lower().strip("-")
        if value and value not in candidates:
            candidates.append(value)

    add("".join(words))
    add("-".join(words))

    # Same again with corporate suffixes removed — "Astrro Creations &
    # Impex Pvt Ltd" is never a slug, "astrro" might be.
    trimmed = [w for w in words if not _SUFFIXES.fullmatch(w)]
    if trimmed and trimmed != words:
        add("".join(trimmed))
        add("-".join(trimmed))
    if trimmed:
        add(trimmed[0])
    elif words:
        add(words[0])

    return candidates[:limit]


def probe_company(company: str, *, client: httpx.Client | None = None,
                  limit: int = 4) -> dict | None:
    """Find a live board for `company`, or None.

    Returns `{"company", "ats", "slug", "open_roles", "url"}`.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=TIMEOUT, follow_redirects=True)
    try:
        for slug in slug_candidates(company, limit=limit):
            for ats, template in _BOARDS.items():
                found = _try(client, ats, template, slug)
                if found is not None:
                    found["company"] = company
                    return found
        return None
    finally:
        if owns_client:
            client.close()


def _try(client: httpx.Client, ats: str, template: str, slug: str) -> dict | None:
    url = template.format(slug=slug)
    try:
        response = client.get(url)
    except Exception as exc:  # noqa: BLE001 - a probe failing is not an error
        log.debug("probe %s/%s failed: %s", ats, slug, exc)
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None

    count = _count_roles(ats, payload)
    if count is None:
        return None
    # A board that exists but lists nothing is not useful to apply through,
    # and is often a stale slug that still resolves.
    if count == 0:
        log.debug("probe %s/%s: board exists but has no open roles", ats, slug)
        return None
    return {"ats": ats, "slug": slug, "open_roles": count,
            "url": _board_url(ats, slug)}


def _count_roles(ats: str, payload) -> int | None:
    if ats == "greenhouse":
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        return len(jobs) if isinstance(jobs, list) else None
    if ats == "lever":
        return len(payload) if isinstance(payload, list) else None
    if ats == "ashby":
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        return len(jobs) if isinstance(jobs, list) else None
    return None


def _board_url(ats: str, slug: str) -> str:
    return {
        "greenhouse": f"https://boards.greenhouse.io/{slug}",
        "lever": f"https://jobs.lever.co/{slug}",
        "ashby": f"https://jobs.ashbyhq.com/{slug}",
    }[ats]


def probe_all(companies: list[str], *, limit: int = 4) -> list[dict]:
    """Probe several companies, skipping quietly over the ones with no board."""
    found: list[dict] = []
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        for company in companies:
            result = probe_company(company, client=client, limit=limit)
            if result is not None:
                log.info("%s: %s board '%s' with %d open role(s)",
                         company, result["ats"], result["slug"], result["open_roles"])
                found.append(result)
    return found

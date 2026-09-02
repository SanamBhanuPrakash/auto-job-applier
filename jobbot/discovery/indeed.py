"""Indeed as a discovery source, via the official Indeed MCP connector.

Why this is a different thing from "automating Indeed". This project has
always refused to drive indeed.com with a browser: their terms prohibit
automated access and it is what gets accounts banned. An MCP connector the
user has deliberately connected is the opposite — a first-party API Indeed
publishes for exactly this purpose. Reading it is sanctioned. That makes
Indeed usable for *discovery* while the refusal to automate their site
stands unchanged.

What the connector does NOT provide, verified live against it rather than
assumed:

1. **No apply capability.** The connector exposes search, job details,
   company data and resume. There is no apply tool, so nothing here can
   submit an application to Indeed. "Easily apply" postings are out of
   reach and are reported as such rather than half-attempted.

2. **No stable job identifier.** Job ids come back as `JOBSEARCH_1`,
   `JOBSEARCH_2`, ... — they are the *position in the result list*, not
   the job. Asking for details on `JOBSEARCH_3` twice returned two
   different apply URLs pointing at two different Indeed hosts. Every
   `to.indeed.com/...` link is a per-call click-tracking redirect.

   This matters more than it sounds. The whole duplicate-application
   guarantee in this project rests on stable job identity, and if
   `JOBSEARCH_3` were used as `external_id` then tomorrow's third search
   result would collide with today's and the system would believe it had
   already applied to a job it has never seen. So identity here is a
   **content fingerprint** over (company, title, location, posted date),
   never the supplied id and never the URL.

3. **No employer application URL.** The links resolve to
   `indeed.com/uie/clk?...` tracking redirects (which return 403 to a
   non-browser client), not to the employer's Greenhouse/Lever/Workday
   posting. So an Indeed record cannot be handed to the submission engine
   directly.

Given all three, what Indeed is genuinely good for here is **finding
companies and roles we did not already know about**. A posting discovered
on Indeed is cross-referenced against the ATS boards this project can
actually apply through; when the company has one, the real posting (with a
real, stable, appliable URL) comes in through that connector and the
existing pipeline applies to it normally. When it does not, the job is
kept and surfaced for you to apply by hand, with the Indeed link.

Result quality is also worth defending against, again from a live run: a
single search for "Python Developer" in Bengaluru returned ten results,
all ten from one advertiser posting part-time gig work, dated four months
earlier. `MAX_PER_COMPANY` and the shared recency filter exist for that.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re

from jobbot.discovery.base import NormalizedJob

log = logging.getLogger(__name__)

SOURCE = "indeed"

#: One advertiser flooding a search is the normal case, not the exception.
MAX_PER_COMPANY = 3

#: Titles that are gig/annotation work rather than employment. These
#: dominate developer searches and are almost never what someone running
#: this tool is looking for. Matched case-insensitively against the title.
_GIG_TITLE = re.compile(
    r"\bai (?:trainer|tutor)\b|\bdata annotat|\bfreelance\b|\bgig\b|\bsurvey\b",
    re.IGNORECASE,
)

#: The markdown the MCP tools emit, field by field. Parsed rather than
#: hand-copied so the same code works whether records arrive from an
#: export file or straight from an MCP client later.
_FIELD = re.compile(r"\*\*(?P<key>[A-Za-z ]+?):\*\*\s*(?P<value>.*?)\s*$", re.MULTILINE)
_TITLE_HEADING = re.compile(r"^#{2,3}\s+(?P<title>.+?)\s*$", re.MULTILINE)


def parse_mcp_markdown(text: str) -> list[dict]:
    """Turn the connector's markdown into records.

    Both `search_jobs` and `get_job_details` return markdown, with search
    results separated by blank lines and each carrying a `**Job Title:**`
    field, while a details payload leads with a `###` heading and has the
    description as loose prose after the fields.
    """
    if not text or not text.strip():
        return []

    blocks = [b for b in re.split(r"\n\s*\n(?=\s*\*\*Job Title:\*\*)", text) if b.strip()]
    records: list[dict] = []
    for block in blocks:
        record: dict = {}
        for match in _FIELD.finditer(block):
            key = match.group("key").strip().lower().replace(" ", "_")
            record[key] = match.group("value").strip()
        if not record.get("job_title"):
            heading = _TITLE_HEADING.search(block)
            if heading:
                record["job_title"] = heading.group("title").strip()
        if not record.get("job_title"):
            continue
        record["description"] = _description_after_fields(block)
        records.append(record)
    return records


def _description_after_fields(block: str) -> str:
    """Everything following the last `**Field:**` line is the body."""
    last = None
    for match in _FIELD.finditer(block):
        last = match
    if last is None:
        return ""
    return block[last.end():].strip()


def content_fingerprint(company: str, title: str, location: str, posted_at: str) -> str:
    """Stable identity for a posting the source gives no stable id for.

    Deliberately excludes the supplied job id and the URL, both of which
    change between calls for the same job. Includes the posted date so a
    genuinely re-listed role is treated as new rather than silently
    matching an application sent months ago.
    """
    parts = [_norm(company), _norm(title), _norm(location), (posted_at or "").strip().lower()]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def _parse_posted(value: str) -> dt.date | None:
    """`Posted on: May 08, 2026` and a few near neighbours."""
    raw = (value or "").strip()
    if not raw or raw.lower() in ("n/a", "none"):
        return None
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y"):
        try:
            return dt.datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _is_remote(location: str, description: str) -> bool:
    blob = f"{location} {description[:400]}".lower()
    return "remote" in blob or "work from home" in blob


def normalize(
    records: list[dict],
    *,
    max_per_company: int = MAX_PER_COMPANY,
    posted_within_days: int | None = None,
    drop_gig_work: bool = True,
    today: dt.date | None = None,
) -> list[NormalizedJob]:
    """Records -> NormalizedJob, deduped, capped and filtered."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    seen: set[str] = set()
    per_company: dict[str, int] = {}
    out: list[NormalizedJob] = []
    dropped = {"duplicate": 0, "company_cap": 0, "stale": 0, "gig": 0, "incomplete": 0}

    for record in records:
        title = (record.get("job_title") or "").strip()
        company = (record.get("company") or "").strip()
        if not title or not company:
            dropped["incomplete"] += 1
            continue

        if drop_gig_work and _GIG_TITLE.search(title):
            dropped["gig"] += 1
            continue

        location = (record.get("location") or "").strip()
        posted_raw = (record.get("posted_on") or "").strip()

        if posted_within_days is not None:
            posted = _parse_posted(posted_raw)
            # An unparseable date is kept, not dropped: dropping on a
            # format we failed to read would silently lose real jobs, and
            # the recency filter is a convenience, not a safety control.
            if posted is not None and (today - posted).days > posted_within_days:
                dropped["stale"] += 1
                continue

        fingerprint = content_fingerprint(company, title, location, posted_raw)
        if fingerprint in seen:
            dropped["duplicate"] += 1
            continue

        key = _norm(company)
        if per_company.get(key, 0) >= max_per_company:
            dropped["company_cap"] += 1
            continue

        seen.add(fingerprint)
        per_company[key] = per_company.get(key, 0) + 1
        description = (record.get("description") or "").strip()

        out.append(NormalizedJob(
            source=SOURCE,
            external_id=fingerprint,
            company=company,
            title=title,
            # The tracking link is per-call, so it is stored for a human to
            # click and never used for identity or deduplication.
            url=(record.get("view_job_url") or "").strip(),
            location=location,
            remote=_is_remote(location, description),
            description=description,
            posted_at=posted_raw,
            # No ATS: an Indeed record carries no employer application URL,
            # so the submission engine must never be pointed at it.
            ats="",
            raw={
                "indeed_job_id": record.get("job_id", ""),
                "job_type": record.get("job_type", ""),
                "compensation": record.get("compensation", ""),
                "apply_route": "INDEED_HOSTED",
                "note": (
                    "Indeed supplies no employer application URL and no apply API. "
                    "Apply by hand via the link, or let the ATS connectors pick this "
                    "company up if it has a supported board."
                ),
            },
        ))

    if any(dropped.values()):
        log.info("Indeed: kept %d, dropped %s", len(out),
                 ", ".join(f"{v} {k}" for k, v in dropped.items() if v))
    return out


def companies_worth_adding(jobs: list[NormalizedJob], known: set[str]) -> list[str]:
    """Companies seen on Indeed that are not yet in `config/companies.yaml`.

    This is the honest payoff of the Indeed connector. We cannot apply
    through Indeed, but a company it surfaces may well have a Greenhouse or
    Lever board — and through that board the posting arrives with a real,
    stable, appliable URL that the existing pipeline handles normally.
    """
    known_norm = {_norm(k) for k in known}
    found: dict[str, str] = {}
    for job in jobs:
        key = _norm(job.company)
        if key and key not in known_norm and key not in found:
            found[key] = job.company
    return sorted(found.values())

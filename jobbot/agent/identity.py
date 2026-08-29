"""Canonical identity for jobs and application attempts.

The original dedupe keyed off `(source, external_id)`, which means the same
posting discovered through a company's own ATS board *and* through an
aggregator produced two independent Job rows with two independent ids — so
"don't apply twice" silently failed for exactly the postings most likely to
be found twice.

This module derives a stable identity that survives:
  - tracking/attribution query parameters (?gh_src=, ?utm_*, ?ref=)
  - http vs https, host casing, trailing slashes, fragments
  - the same job being seen via different discovery sources

It deliberately does NOT strip parameters that *identify* the posting
(Greenhouse serves many jobs from one path, distinguished only by
?gh_jid=), because over-normalizing collapses distinct jobs into one, which
would silently skip real applications.
"""
from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Query parameters that carry attribution/analytics only. Anything not on
#: this list is preserved, so an unrecognized identifying parameter can
#: never be dropped (over-keeping splits an identity, which is safe;
#: over-stripping merges two jobs, which is not).
TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "gh_src", "ref", "referrer", "referer", "source", "src", "trk",
        "trackingid", "lever-source", "lever-origin", "gclid", "fbclid",
        "mc_cid", "mc_eid", "_ga", "t",
    }
)

_WHITESPACE_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

#: Path patterns that expose an ATS's own job id, which is the strongest
#: identity signal available when two sources disagree about everything else.
_ATS_ID_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("greenhouse", re.compile(r"greenhouse\.io/(?:embed/job_app\?for=)?[^/]+/jobs/(\d+)", re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/[^/]+/([0-9a-f-]{16,})", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/[^/]+/([0-9a-f-]{16,})", re.I)),
)

_GREENHOUSE_JID_RE = re.compile(r"[?&]gh_jid=(\d+)", re.I)


def canonical_url(url: str) -> str:
    """Normalize a job/application URL for comparison and storage.

    Lowercases scheme+host, forces https, drops the fragment, removes
    tracking parameters, sorts the rest, and strips a trailing slash.
    """
    if not url:
        return ""
    raw = url.strip()
    if not raw:
        return ""
    if "//" not in raw:  # tolerate "boards.greenhouse.io/acme/jobs/1"
        raw = "https://" + raw

    parts = urlsplit(raw)
    scheme = "https"
    netloc = (parts.netloc or "").lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    path = parts.path or ""
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")

    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=False)
        if k.lower() not in TRACKING_PARAMS
    ]
    kept.sort()
    query = urlencode(kept)

    return urlunsplit((scheme, netloc, path, query, ""))


def _slug(text: str) -> str:
    text = _WHITESPACE_RE.sub(" ", (text or "").strip().lower())
    return _NON_ALNUM_RE.sub("-", text).strip("-")


def ats_job_reference(url: str) -> tuple[str, str] | None:
    """Extract (ats, ats_job_id) from a URL when the ATS exposes one.

    This is what lets a posting found at `stripe.com/jobs/search?gh_jid=123`
    and the same posting at `boards.greenhouse.io/stripe/jobs/123` resolve to
    one identity.
    """
    if not url:
        return None

    jid = _GREENHOUSE_JID_RE.search(url)
    if jid:
        return ("greenhouse", jid.group(1))

    for ats, pattern in _ATS_ID_PATTERNS:
        match = pattern.search(url)
        if match:
            return (ats, match.group(1).lower())
    return None


def job_identity(
    *,
    url: str = "",
    company: str = "",
    title: str = "",
    source: str = "",
    external_id: str = "",
) -> str:
    """A stable identity string for a posting, best-signal-first.

    1. The ATS's own job id parsed out of the URL (survives the posting
       being surfaced by any number of aggregators).
    2. The ATS name + the source's external id, when the source *is* the ATS.
    3. Company + title + canonical URL, hashed.
    """
    ref = ats_job_reference(url)
    if ref is not None:
        return f"ats:{ref[0]}:{ref[1]}"

    if source and external_id and source.lower() in {"greenhouse", "lever", "ashby", "smartrecruiters", "recruitee"}:
        return f"ats:{source.lower()}:{external_id.strip().lower()}"

    basis = "|".join([_slug(company), _slug(title), canonical_url(url)])
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:32]
    return f"fuzzy:{digest}"


def application_idempotency_key(job_identity_value: str) -> str:
    """The uniqueness key for "an application to this posting".

    Stored under a UNIQUE constraint, so a second concurrent process trying
    to start an attempt for the same posting loses the insert race at the
    database rather than racing us into a duplicate submission.

    Deliberately keyed on the *job*, not on the attempt: retries reuse the
    same row (bumping attempt_no) instead of creating a second row that
    would defeat the constraint.
    """
    return hashlib.sha256(f"apply:{job_identity_value}".encode("utf-8")).hexdigest()


def submission_fingerprint(*, job_identity_value: str, canonical_application_url: str) -> str:
    """Identifies the specific submission that was (or may have been) made,
    so a post-crash reconciliation can match a recorded attempt against
    external evidence (a confirmation email, an ATS reference number)."""
    basis = f"{job_identity_value}|{canonical_url(canonical_application_url)}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:40]

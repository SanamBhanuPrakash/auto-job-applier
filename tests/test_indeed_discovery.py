"""The Indeed connector, and the trap it exists to avoid.

Every fact asserted here about the connector's behaviour was verified
against the live Indeed MCP tools, not assumed: the positional job ids, the
per-call tracking URLs, the single-advertiser flooding, and the absence of
any employer application URL.
"""
from __future__ import annotations

import datetime as dt

import pytest

from jobbot.discovery.indeed import (
    MAX_PER_COMPANY,
    companies_worth_adding,
    content_fingerprint,
    normalize,
    parse_mcp_markdown,
)

TODAY = dt.date(2026, 9, 2)

#: Copied verbatim from a live `search_jobs` response.
LIVE_SEARCH = """**Job Title:** Backend Developer - AI Trainer
            **Job Id:** JOBSEARCH_1
            **Company:** DataAnnotation
            **Location:** Bengaluru, Karnataka
            **Posted on:** May 08, 2026
            **Job Type:** Part-time
            **Compensation:** N/A
            **View Job URL:** https://to.indeed.com/aab9tqhy7qpy

**Job Title:** Software Developer - AI Trainer
            **Job Id:** JOBSEARCH_2
            **Company:** DataAnnotation
            **Location:** Bengaluru, Karnataka
            **Posted on:** May 08, 2026
            **Job Type:** Part-time
            **Compensation:** N/A
            **View Job URL:** https://to.indeed.com/aaxl9jcnc88q
"""


def _record(**kw):
    base = {
        "job_title": "Senior Python Engineer",
        "job_id": "JOBSEARCH_1",
        "company": "Acme Labs",
        "location": "Bengaluru, Karnataka",
        "posted_on": "September 01, 2026",
        "view_job_url": "https://to.indeed.com/abc",
        "description": "Build backend services.",
    }
    base.update(kw)
    return base


# --- parsing the connector's actual output --------------------------------


def test_a_live_search_response_parses():
    records = parse_mcp_markdown(LIVE_SEARCH)
    assert len(records) == 2
    assert records[0]["company"] == "DataAnnotation"
    assert records[0]["view_job_url"] == "https://to.indeed.com/aab9tqhy7qpy"


def test_empty_or_junk_input_yields_nothing_rather_than_raising():
    assert parse_mcp_markdown("") == []
    assert parse_mcp_markdown("no jobs found") == []


# --- the identity trap ----------------------------------------------------


def test_identity_ignores_the_job_id_the_connector_supplies():
    """`JOBSEARCH_3` is the *position in the result list*, not the job.

    Verified live: asking for details on JOBSEARCH_3 twice returned two
    different apply URLs on two different Indeed hosts. Using it as
    external_id would make tomorrow's third search result collide with
    today's, and the duplicate-application guarantee would then believe an
    application had already been sent for a job never seen.
    """
    a = normalize([_record(job_id="JOBSEARCH_1")], today=TODAY)[0]
    b = normalize([_record(job_id="JOBSEARCH_9")], today=TODAY)[0]
    assert a.external_id == b.external_id, "the supplied id must not affect identity"


def test_identity_ignores_the_tracking_url():
    """Every to.indeed.com link is minted per call."""
    a = normalize([_record(view_job_url="https://to.indeed.com/aaa")], today=TODAY)[0]
    b = normalize([_record(view_job_url="https://to.indeed.com/zzz")], today=TODAY)[0]
    assert a.external_id == b.external_id


def test_two_different_jobs_do_not_share_an_identity():
    a = normalize([_record()], today=TODAY)[0]
    b = normalize([_record(job_title="Staff Python Engineer")], today=TODAY)[0]
    assert a.external_id != b.external_id


def test_the_same_role_relisted_later_is_treated_as_new():
    """A re-listed posting is a fresh opportunity, not a match against an
    application sent months ago."""
    old = normalize([_record(posted_on="March 01, 2026")], today=TODAY,
                    posted_within_days=None)[0]
    new = normalize([_record(posted_on="September 01, 2026")], today=TODAY)[0]
    assert old.external_id != new.external_id


def test_identity_is_insensitive_to_whitespace_and_case():
    a = content_fingerprint("Acme Labs", "Python Engineer", "Bengaluru", "May 08, 2026")
    b = content_fingerprint("  acme   labs ", "PYTHON ENGINEER", "bengaluru", "May 08, 2026")
    assert a == b


# --- nothing here is ever handed to the submission engine -----------------


def test_an_indeed_job_never_carries_an_ats():
    """There is no employer application URL in the connector's output, so
    pointing the submission engine at one of these would drive indeed.com —
    which this project does not do."""
    job = normalize([_record()], today=TODAY)[0]
    assert job.ats == ""
    assert job.raw["apply_route"] == "INDEED_HOSTED"


def test_the_record_says_why_it_cannot_be_auto_applied():
    job = normalize([_record()], today=TODAY)[0]
    assert "no employer application URL" in job.raw["note"]


# --- result-quality defences ----------------------------------------------


def test_one_advertiser_cannot_flood_the_queue():
    """Live: a single 'Python Developer' search in Bengaluru returned ten
    results, all ten from one advertiser."""
    records = [_record(company="DataAnnotation", job_title=f"Role {i}") for i in range(10)]
    jobs = normalize(records, today=TODAY)
    assert len(jobs) == MAX_PER_COMPANY


def test_the_cap_is_per_company_not_global():
    records = ([_record(company="A", job_title=f"A{i}") for i in range(5)]
               + [_record(company="B", job_title=f"B{i}") for i in range(5)])
    jobs = normalize(records, today=TODAY)
    assert len({j.company for j in jobs}) == 2
    assert len(jobs) == MAX_PER_COMPANY * 2


def test_gig_work_is_filtered_out_by_default():
    jobs = normalize([_record(job_title="Software Engineer - AI Trainer")], today=TODAY)
    assert jobs == []


def test_gig_filtering_can_be_switched_off():
    jobs = normalize([_record(job_title="Software Engineer - AI Trainer")],
                     today=TODAY, drop_gig_work=False)
    assert len(jobs) == 1


def test_stale_postings_are_dropped_when_a_window_is_given():
    """Live results were about four months old."""
    jobs = normalize([_record(posted_on="May 08, 2026")],
                     posted_within_days=30, today=TODAY)
    assert jobs == []


def test_an_unparseable_date_is_kept_rather_than_silently_lost():
    """The recency filter is a convenience, not a safety control. Dropping
    on a date format we simply failed to read would lose real jobs."""
    jobs = normalize([_record(posted_on="sometime last week")],
                     posted_within_days=1, today=TODAY)
    assert len(jobs) == 1


def test_duplicate_records_collapse():
    jobs = normalize([_record(), _record(job_id="JOBSEARCH_7")], today=TODAY)
    assert len(jobs) == 1


def test_a_record_missing_a_company_or_title_is_dropped():
    assert normalize([_record(company="")], today=TODAY) == []
    assert normalize([_record(job_title="")], today=TODAY) == []


@pytest.mark.parametrize("location,expected", [
    ("Remote", True), ("Bengaluru, Karnataka", False), ("Remote in India", True),
])
def test_remote_is_detected(location, expected):
    job = normalize([_record(location=location)], today=TODAY)[0]
    assert job.remote is expected


# --- the actual payoff ----------------------------------------------------


def test_new_companies_are_surfaced_for_the_ats_connectors():
    """Indeed cannot apply, but a company it surfaces may have a
    Greenhouse or Lever board — and through that board the posting arrives
    with a real, stable, appliable URL."""
    jobs = normalize([_record(company="Acme Labs"), _record(company="Zephyr AI",
                                                           job_title="ML Engineer")],
                     today=TODAY)
    suggestions = companies_worth_adding(jobs, known={"acme labs"})
    assert suggestions == ["Zephyr AI"]


def test_a_company_already_configured_is_not_suggested_again():
    jobs = normalize([_record(company="Acme Labs")], today=TODAY)
    assert companies_worth_adding(jobs, known={"ACME  labs"}) == []

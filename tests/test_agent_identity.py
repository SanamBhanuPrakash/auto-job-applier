"""Identity is what makes duplicate protection work across discovery
sources. Over-normalizing is dangerous in the opposite direction (two
distinct jobs collapsing into one silently skips a real application), so
both directions are tested.
"""
from jobbot.agent.identity import (
    application_idempotency_key,
    ats_job_reference,
    canonical_url,
    job_identity,
    submission_fingerprint,
)


# --- canonical_url ---------------------------------------------------------


def test_strips_tracking_params_but_keeps_identifying_ones():
    # Real Greenhouse URL shape observed live: ?t=gh_src=&gh_jid=8114351
    url = "https://boards.greenhouse.io/robinhood/jobs/8114351?t=gh_src=&gh_jid=8114351"
    out = canonical_url(url)
    assert "gh_jid=8114351" in out, "gh_jid identifies the posting and must survive"
    assert "t=" not in out


def test_strips_utm_and_referral_noise():
    out = canonical_url("https://jobs.lever.co/acme/abc-123?utm_source=x&utm_campaign=y&ref=z")
    assert out == "https://jobs.lever.co/acme/abc-123"


def test_normalizes_scheme_host_case_www_and_trailing_slash():
    a = canonical_url("HTTP://WWW.Example.com/Jobs/1/")
    b = canonical_url("https://example.com/Jobs/1")
    assert a == b


def test_drops_fragment():
    assert canonical_url("https://example.com/jobs/1#apply") == "https://example.com/jobs/1"


def test_query_param_order_does_not_change_identity():
    a = canonical_url("https://example.com/j?b=2&a=1")
    b = canonical_url("https://example.com/j?a=1&b=2")
    assert a == b


def test_distinct_jobs_do_not_collapse():
    """The dangerous failure direction: two real postings must not merge."""
    a = canonical_url("https://stripe.com/jobs/search?gh_jid=111")
    b = canonical_url("https://stripe.com/jobs/search?gh_jid=222")
    assert a != b


def test_unknown_query_params_are_preserved():
    """Anything not on the tracking denylist might identify the posting."""
    out = canonical_url("https://example.com/apply?requisitionId=A42")
    assert "requisitionId=A42" in out


def test_empty_and_scheme_less_input():
    assert canonical_url("") == ""
    assert canonical_url("boards.greenhouse.io/acme/jobs/1").startswith("https://")


# --- ats_job_reference -----------------------------------------------------


def test_extracts_greenhouse_id_from_hosted_url():
    assert ats_job_reference("https://boards.greenhouse.io/acme/jobs/12345") == ("greenhouse", "12345")


def test_extracts_greenhouse_id_from_employer_branded_url():
    # This shape is the common one in practice (Stripe/Airbnb/Asana etc.)
    assert ats_job_reference("https://stripe.com/jobs/search?gh_jid=8130725") == ("greenhouse", "8130725")


def test_returns_none_for_unknown_host():
    assert ats_job_reference("https://example.com/careers/42") is None


# --- job_identity ----------------------------------------------------------


def test_same_posting_via_two_sources_gets_one_identity():
    """The bug this exists to fix: a posting found on the company's own
    Greenhouse board and via an aggregator previously produced two rows
    with two ids, so 'already applied' missed it."""
    via_board = job_identity(
        url="https://boards.greenhouse.io/stripe/jobs/8130725",
        company="Stripe", title="Backend Engineer", source="greenhouse", external_id="8130725",
    )
    via_aggregator = job_identity(
        url="https://stripe.com/jobs/search?gh_jid=8130725&utm_source=aggregator",
        company="stripe inc.", title="Backend Engineer (Remote)", source="adzuna", external_id="99999",
    )
    assert via_board == via_aggregator


def test_different_postings_get_different_identities():
    a = job_identity(url="https://boards.greenhouse.io/stripe/jobs/1", company="Stripe", title="A")
    b = job_identity(url="https://boards.greenhouse.io/stripe/jobs/2", company="Stripe", title="B")
    assert a != b


def test_falls_back_to_company_title_url_hash_for_unknown_ats():
    identity = job_identity(url="https://example.com/careers/42", company="Acme", title="Engineer")
    assert identity.startswith("fuzzy:")


def test_fallback_identity_is_stable_across_cosmetic_differences():
    a = job_identity(url="https://example.com/careers/42?utm_source=x", company="Acme Inc", title="Engineer")
    b = job_identity(url="http://www.example.com/careers/42/", company="  acme  inc ", title="ENGINEER")
    assert a == b


# --- idempotency -----------------------------------------------------------


def test_idempotency_key_is_stable_and_identity_derived():
    identity = "ats:greenhouse:12345"
    assert application_idempotency_key(identity) == application_idempotency_key(identity)
    assert application_idempotency_key(identity) != application_idempotency_key("ats:greenhouse:12346")


def test_two_sources_of_one_posting_share_an_idempotency_key():
    """This is what makes the UNIQUE constraint block a duplicate apply."""
    a = job_identity(url="https://boards.greenhouse.io/stripe/jobs/777")
    b = job_identity(url="https://stripe.com/jobs/search?gh_jid=777")
    assert application_idempotency_key(a) == application_idempotency_key(b)


def test_submission_fingerprint_varies_with_application_url():
    identity = "ats:lever:abc"
    assert submission_fingerprint(job_identity_value=identity, canonical_application_url="https://a/1") != \
           submission_fingerprint(job_identity_value=identity, canonical_application_url="https://a/2")

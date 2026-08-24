from jobbot.matching.lexical import lexical_score, shortlist
from jobbot.models import Job

SETTINGS = {
    "search": {
        "keywords": ["python", "backend"],
        "exclude_keywords": ["staff", "principal"],
        "locations": ["remote"],
        "remote_only": False,
    }
}


def _job(**overrides) -> Job:
    defaults = dict(
        source="greenhouse",
        external_id="1",
        company="Acme",
        title="Backend Engineer",
        location="Remote - US",
        remote=True,
        url="https://example.com",
        description="We need someone strong in Python and backend systems.",
    )
    defaults.update(overrides)
    return Job(**defaults)


def test_matching_job_scores_high():
    job = _job()
    score = lexical_score(job, SETTINGS)
    assert score > 80


def test_excluded_keyword_zeroes_score():
    job = _job(title="Staff Backend Engineer")
    score = lexical_score(job, SETTINGS)
    assert score == 0.0


def test_no_keyword_hits_scores_low():
    job = _job(title="Sales Manager", description="Manage the sales team.", location="Remote")
    score = lexical_score(job, SETTINGS)
    assert score < 50


def test_shortlist_sorts_descending_and_drops_zero():
    good = _job(external_id="1")
    bad = _job(external_id="2", title="Staff Backend Engineer")
    ranked = shortlist([good, bad], SETTINGS)
    assert len(ranked) == 1
    assert ranked[0][0].external_id == "1"

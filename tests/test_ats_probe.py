"""Finding an appliable board for a company discovered elsewhere.

This is the step that turns an Indeed result — which carries no employer
application URL and cannot be submitted — into a posting the existing
engine applies to automatically. Verified live while it was written:
Bounteous came out of an Indeed search, was not in companies.yaml, and
probing found a Lever board with 36 open roles.

No network here: every test drives a stub client, so the suite stays
offline and deterministic.
"""
from __future__ import annotations

import httpx
import pytest

from jobbot.discovery.ats_probe import probe_company, slug_candidates


class _Stub:
    """Answers only the URLs it is given; 404s everything else."""

    def __init__(self, routes: dict, *, fail: set[str] = frozenset()):
        self.routes = routes
        self.fail = fail
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)
        if any(f in url for f in self.fail):
            raise httpx.ConnectError("boom")
        for fragment, payload in self.routes.items():
            if fragment in url:
                return httpx.Response(200, json=payload)
        return httpx.Response(404, text="not found")


# --- slug generation -------------------------------------------------------


@pytest.mark.parametrize("company,expected_first", [
    ("Bounteous", "bounteous"),
    ("Motorola Solutions", "motorolasolutions"),
    ("WebSenor InfoTech", "websenorinfotech"),
])
def test_the_most_likely_slug_comes_first(company, expected_first):
    assert slug_candidates(company)[0] == expected_first


def test_corporate_suffixes_are_stripped_in_a_later_candidate():
    """"Astrro Creations & Impex Pvt Ltd" is never a board slug."""
    candidates = slug_candidates("Astrro Creations & Impex Pvt Ltd")
    assert any("pvt" not in c and "ltd" not in c for c in candidates)
    assert candidates[0] == "astrrocreationsimpexpvtltd"


def test_punctuation_and_ampersands_do_not_leak_into_a_slug():
    for candidate in slug_candidates("Foo & Bar, Inc."):
        assert "&" not in candidate and "," not in candidate and "." not in candidate


def test_an_empty_name_yields_no_candidates():
    assert slug_candidates("") == []
    assert slug_candidates("   ") == []


def test_candidates_are_bounded():
    assert len(slug_candidates("One Two Three Four Five Pvt Ltd", limit=3)) <= 3


# --- probing ---------------------------------------------------------------


def test_a_live_lever_board_is_found():
    client = _Stub({"api.lever.co/v0/postings/bounteous": [{"text": "Engineer"}] * 36})
    result = probe_company("Bounteous", client=client)
    assert result == {
        "ats": "lever", "slug": "bounteous", "open_roles": 36,
        "url": "https://jobs.lever.co/bounteous", "company": "Bounteous",
    }


def test_a_greenhouse_board_is_found():
    client = _Stub({"boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [{"id": 1}]}})
    result = probe_company("Acme", client=client)
    assert result["ats"] == "greenhouse"
    assert result["url"] == "https://boards.greenhouse.io/acme"


def test_a_company_with_no_board_reports_nothing():
    assert probe_company("Nowhere Ltd", client=_Stub({})) is None


def test_a_board_that_exists_but_lists_nothing_is_not_offered():
    """Often a stale slug that still resolves. Adding it would put a dead
    entry in the config and yield no applications."""
    client = _Stub({"api.lever.co/v0/postings/ghost": []})
    assert probe_company("Ghost", client=client) is None


def test_a_malformed_response_does_not_count_as_a_board():
    client = _Stub({"api.lever.co/v0/postings/weird": {"unexpected": "shape"}})
    assert probe_company("Weird", client=client) is None


def test_a_network_failure_on_one_provider_does_not_abort_the_probe():
    """A probe failing is not an error — the next provider still gets a
    chance, and a company is only reported as having no board once every
    candidate has actually been asked."""
    client = _Stub({"api.lever.co/v0/postings/acme": [{"text": "Role"}]},
                   fail={"greenhouse"})
    result = probe_company("Acme", client=client)
    assert result is not None
    assert result["ats"] == "lever"


def test_later_slug_candidates_are_tried_when_the_first_misses():
    client = _Stub({"api.lever.co/v0/postings/motorola?": [{"text": "Role"}]})
    result = probe_company("Motorola Solutions", client=client)
    assert result is not None
    assert result["slug"] == "motorola"


def test_probing_stops_as_soon_as_a_board_is_found():
    """Politeness toward public endpoints: no reason to keep asking."""
    client = _Stub({"boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [{"id": 1}]}})
    probe_company("Acme", client=client)
    assert not any("lever" in c for c in client.calls)


def test_a_company_with_no_usable_name_is_not_probed_at_all():
    client = _Stub({})
    assert probe_company("", client=client) is None
    assert client.calls == []

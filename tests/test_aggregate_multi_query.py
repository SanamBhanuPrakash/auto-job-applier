"""discover_aggregator_jobs() needs to fan out over a LIST of queries per
aggregator (one Adzuna search per city, one RemoteOK/Remotive tag per
resume domain) while still working for anyone with an old settings.yaml
that only has a single what/where/tag/category. These test that fan-out
and the backward-compat path directly, with fetch_jobs mocked so no real
HTTP happens.
"""
from unittest.mock import patch

from jobbot.discovery import aggregate
from jobbot.discovery.base import NormalizedJob


def _job(source: str, external_id: str) -> NormalizedJob:
    return NormalizedJob(source=source, external_id=external_id, company="Acme", title="Engineer", url="https://example.com")


def test_adzuna_fans_out_over_multiple_queries():
    settings_yaml = {
        "aggregators": {
            "adzuna": {
                "enabled": True,
                "queries": [
                    {"what": "engineer", "where": "Bangalore", "country": "in"},
                    {"what": "engineer", "where": "remote", "country": "us"},
                ],
            }
        }
    }
    with patch("jobbot.discovery.aggregate.load_search_settings", return_value=settings_yaml), \
         patch("jobbot.discovery.aggregate.adzuna.fetch_jobs") as mock_fetch:
        mock_fetch.return_value = [_job("adzuna", "1")]
        jobs = aggregate.discover_aggregator_jobs()

    assert mock_fetch.call_count == 2
    called_with = [(c.args[0], c.args[1], c.args[2]) for c in mock_fetch.call_args_list]
    assert ("engineer", "Bangalore", "in") in called_with
    assert ("engineer", "remote", "us") in called_with
    assert len(jobs) == 2  # one NormalizedJob returned per query


def test_adzuna_backward_compatible_single_query():
    settings_yaml = {
        "aggregators": {
            "adzuna": {"enabled": True, "what": "engineer", "where": "remote"},
        }
    }
    with patch("jobbot.discovery.aggregate.load_search_settings", return_value=settings_yaml), \
         patch("jobbot.discovery.aggregate.adzuna.fetch_jobs") as mock_fetch:
        mock_fetch.return_value = [_job("adzuna", "1")]
        aggregate.discover_aggregator_jobs()

    assert mock_fetch.call_count == 1
    args, kwargs = mock_fetch.call_args
    assert args[:2] == ("engineer", "remote")


def test_adzuna_disabled_makes_no_calls():
    settings_yaml = {"aggregators": {"adzuna": {"enabled": False, "what": "engineer"}}}
    with patch("jobbot.discovery.aggregate.load_search_settings", return_value=settings_yaml), \
         patch("jobbot.discovery.aggregate.adzuna.fetch_jobs") as mock_fetch:
        aggregate.discover_aggregator_jobs()

    mock_fetch.assert_not_called()


def test_remoteok_fans_out_over_multiple_tags():
    settings_yaml = {
        "aggregators": {"remoteok": {"enabled": True, "tags": ["python", "frontend", "cloud"]}}
    }
    with patch("jobbot.discovery.aggregate.load_search_settings", return_value=settings_yaml), \
         patch("jobbot.discovery.aggregate.remoteok.fetch_jobs") as mock_fetch:
        mock_fetch.return_value = []
        aggregate.discover_aggregator_jobs()

    assert mock_fetch.call_count == 3
    called_tags = [c.args[0] for c in mock_fetch.call_args_list]
    assert called_tags == ["python", "frontend", "cloud"]


def test_remoteok_backward_compatible_single_tag():
    settings_yaml = {"aggregators": {"remoteok": {"enabled": True, "tag": "python"}}}
    with patch("jobbot.discovery.aggregate.load_search_settings", return_value=settings_yaml), \
         patch("jobbot.discovery.aggregate.remoteok.fetch_jobs") as mock_fetch:
        mock_fetch.return_value = []
        aggregate.discover_aggregator_jobs()

    assert mock_fetch.call_count == 1
    assert mock_fetch.call_args.args[0] == "python"


def test_remotive_fans_out_over_multiple_categories():
    settings_yaml = {
        "aggregators": {"remotive": {"enabled": True, "categories": ["software-dev", "data"]}}
    }
    with patch("jobbot.discovery.aggregate.load_search_settings", return_value=settings_yaml), \
         patch("jobbot.discovery.aggregate.remotive.fetch_jobs") as mock_fetch:
        mock_fetch.return_value = []
        aggregate.discover_aggregator_jobs()

    assert mock_fetch.call_count == 2

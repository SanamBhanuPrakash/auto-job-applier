import json
from pathlib import Path

import httpx
import respx

from jobbot.discovery import workday

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "workday_sample.json").read_text())

URL = "https://automationanywhere.wd5.myworkdayjobs.com/en-US/automationanywherejobs"


def test_parse_workday_url_extracts_tenant_pod_locale_site():
    parsed = workday.parse_workday_url(URL)
    assert parsed == {"tenant": "automationanywhere", "pod": "wd5", "locale": "en-US", "site": "automationanywherejobs"}


def test_parse_workday_url_defaults_missing_locale():
    parsed = workday.parse_workday_url("https://workday.wd5.myworkdayjobs.com/Workday")
    assert parsed["tenant"] == "workday"
    assert parsed["pod"] == "wd5"
    assert parsed["site"] == "Workday"


def test_parse_workday_url_rejects_non_workday_url():
    assert workday.parse_workday_url("https://boards.greenhouse.io/stripe") is None


@respx.mock
def test_fetch_jobs_normalizes_fields():
    respx.post("https://automationanywhere.wd5.myworkdayjobs.com/wday/cxs/automationanywhere/automationanywherejobs/jobs").mock(
        return_value=httpx.Response(200, json=FIXTURE)
    )

    with httpx.Client() as client:
        jobs = workday.fetch_jobs(URL, client=client)

    assert len(jobs) == 2
    first = jobs[0]
    assert first.source == "workday"
    assert first.company == "automationanywhere"
    assert first.title == "Sr. AI Engineer"
    assert first.location == "Bengaluru, India"
    assert first.url == (
        "https://automationanywhere.wd5.myworkdayjobs.com/en-US/automationanywherejobs"
        "/job/Bengaluru-India/Sr-AI-Engineer_JR1262"
    )
    assert first.remote is False

    second = jobs[1]
    assert second.remote is True  # "Brazil - Remote" contains "remote"


@respx.mock
def test_fetch_jobs_handles_http_error_gracefully():
    respx.post("https://ghost.wd5.myworkdayjobs.com/wday/cxs/ghost/careers/jobs").mock(
        return_value=httpx.Response(404)
    )

    with httpx.Client() as client:
        jobs = workday.fetch_jobs("https://ghost.wd5.myworkdayjobs.com/careers", client=client)

    assert jobs == []


def test_fetch_jobs_returns_empty_for_unparseable_url():
    assert workday.fetch_jobs("https://not-a-workday-url.example.com/jobs") == []

import json
from pathlib import Path

import httpx
import respx

from jobbot.discovery import greenhouse

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "greenhouse_sample.json").read_text())


@respx.mock
def test_fetch_jobs_normalizes_fields():
    respx.get("https://boards-api.greenhouse.io/v1/boards/acme/jobs").mock(
        return_value=httpx.Response(200, json=FIXTURE)
    )

    with httpx.Client() as client:
        jobs = greenhouse.fetch_jobs("acme", client=client)

    assert len(jobs) == 2
    first = jobs[0]
    assert first.source == "greenhouse"
    assert first.external_id == "12345"
    assert first.ats == "greenhouse"
    assert first.remote is True
    assert first.url == "https://boards.greenhouse.io/acme/jobs/12345"

    second = jobs[1]
    assert second.remote is False


@respx.mock
def test_fetch_jobs_handles_http_error_gracefully():
    respx.get("https://boards-api.greenhouse.io/v1/boards/ghost-co/jobs").mock(
        return_value=httpx.Response(404)
    )

    with httpx.Client() as client:
        jobs = greenhouse.fetch_jobs("ghost-co", client=client)

    assert jobs == []

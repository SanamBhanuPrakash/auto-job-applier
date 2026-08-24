import json
from pathlib import Path

import httpx
import respx

from jobbot.discovery import lever

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "lever_sample.json").read_text())


@respx.mock
def test_fetch_jobs_normalizes_fields():
    respx.get("https://api.lever.co/v0/postings/acme").mock(
        return_value=httpx.Response(200, json=FIXTURE)
    )

    with httpx.Client() as client:
        jobs = lever.fetch_jobs("acme", client=client)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "lever"
    assert job.external_id == "abc-123"
    assert job.ats == "lever"
    assert job.remote is True
    assert "Kubernetes" in job.description

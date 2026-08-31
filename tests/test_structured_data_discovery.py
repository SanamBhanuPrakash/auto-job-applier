import httpx
import respx

from jobbot.discovery import structured_data


def _html(json_ld: str) -> str:
    return f"""<!doctype html><html><head>
    <script type="application/ld+json">{json_ld}</script>
    </head><body>irrelevant page content</body></html>"""


def test_parses_a_bare_job_posting_object():
    html = _html("""{
        "@context": "https://schema.org/",
        "@type": "JobPosting",
        "title": "Backend Engineer",
        "hiringOrganization": {"name": "Acme"},
        "url": "https://acme.example/jobs/1",
        "identifier": "job-1",
        "datePosted": "2026-08-20",
        "description": "<p>Build things.</p>",
        "jobLocation": {"address": {"addressLocality": "Bangalore", "addressCountry": "IN"}}
    }""")

    jobs = structured_data.parse_job_postings(html, "https://acme.example/jobs/1")

    assert len(jobs) == 1
    job = jobs[0]
    assert job.source == "structured_data"
    assert job.company == "Acme"
    assert job.title == "Backend Engineer"
    assert job.location == "Bangalore, IN"
    assert job.remote is False
    assert job.description == "Build things."
    assert job.ats == ""


def test_parses_a_list_of_job_postings():
    html = _html("""[
        {"@type": "JobPosting", "title": "Engineer A", "hiringOrganization": {"name": "Acme"}},
        {"@type": "JobPosting", "title": "Engineer B", "hiringOrganization": {"name": "Acme"}}
    ]""")

    jobs = structured_data.parse_job_postings(html, "https://acme.example/careers")

    assert {j.title for j in jobs} == {"Engineer A", "Engineer B"}


def test_parses_an_at_graph_wrapper():
    html = _html("""{
        "@context": "https://schema.org",
        "@graph": [
            {"@type": "WebPage", "name": "ignored"},
            {"@type": "JobPosting", "title": "Data Scientist", "hiringOrganization": {"name": "Acme"}}
        ]
    }""")

    jobs = structured_data.parse_job_postings(html, "https://acme.example/careers")

    assert [j.title for j in jobs] == ["Data Scientist"]


def test_parses_an_item_list_of_job_postings():
    html = _html("""{
        "@type": "ItemList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "item": {"@type": "JobPosting", "title": "SRE", "hiringOrganization": {"name": "Acme"}}},
            {"@type": "ListItem", "position": 2, "item": {"@type": "JobPosting", "title": "QA Engineer", "hiringOrganization": {"name": "Acme"}}}
        ]
    }""")

    jobs = structured_data.parse_job_postings(html, "https://acme.example/careers")

    assert {j.title for j in jobs} == {"SRE", "QA Engineer"}


def test_remote_detected_from_job_location_type():
    html = _html("""{
        "@type": "JobPosting", "title": "Remote Engineer",
        "hiringOrganization": {"name": "Acme"}, "jobLocationType": "TELECOMMUTE"
    }""")

    jobs = structured_data.parse_job_postings(html, "https://acme.example/jobs/2")

    assert jobs[0].remote is True


def test_remote_detected_from_location_text_fallback():
    html = _html("""{
        "@type": "JobPosting", "title": "Remote Engineer",
        "hiringOrganization": {"name": "Acme"},
        "jobLocation": {"address": {"addressLocality": "Remote"}}
    }""")

    jobs = structured_data.parse_job_postings(html, "https://acme.example/jobs/3")

    assert jobs[0].remote is True


def test_malformed_json_ld_block_is_skipped_not_fatal():
    html = """<!doctype html><html><head>
    <script type="application/ld+json">{ this is not valid json </script>
    <script type="application/ld+json">{"@type": "JobPosting", "title": "Still Works", "hiringOrganization": {"name": "Acme"}}</script>
    </head><body></body></html>"""

    jobs = structured_data.parse_job_postings(html, "https://acme.example/jobs/4")

    assert [j.title for j in jobs] == ["Still Works"]


def test_page_with_no_job_posting_markup_yields_nothing():
    html = """<!doctype html><html><head>
    <script type="application/ld+json">{"@type": "Organization", "name": "Acme"}</script>
    </head><body>a careers page with no structured job data</body></html>"""

    jobs = structured_data.parse_job_postings(html, "https://acme.example/careers")

    assert jobs == []


def test_missing_title_is_skipped():
    html = _html('{"@type": "JobPosting", "hiringOrganization": {"name": "Acme"}}')

    jobs = structured_data.parse_job_postings(html, "https://acme.example/jobs/5")

    assert jobs == []


@respx.mock
def test_fetch_jobs_end_to_end():
    respx.get("https://acme.example/careers").mock(
        return_value=httpx.Response(200, text=_html(
            '{"@type": "JobPosting", "title": "Platform Engineer", "hiringOrganization": {"name": "Acme"}, "url": "https://acme.example/jobs/9"}'
        ))
    )

    with httpx.Client() as client:
        jobs = structured_data.fetch_jobs("https://acme.example/careers", client=client)

    assert len(jobs) == 1
    assert jobs[0].title == "Platform Engineer"
    assert jobs[0].url == "https://acme.example/jobs/9"


@respx.mock
def test_fetch_jobs_handles_http_error_gracefully():
    respx.get("https://gone.example/careers").mock(return_value=httpx.Response(404))

    with httpx.Client() as client:
        jobs = structured_data.fetch_jobs("https://gone.example/careers", client=client)

    assert jobs == []

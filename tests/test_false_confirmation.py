"""Contradictory page content must never read as a submission (spec §33).

Found by the fault-injection harness, not by hand: `jobbot/eval` scenario
`false_confirmation` prepended confirmation wording to a page whose form
was still sitting there unsent, and verification returned SUBMITTED — the
worst outcome this project has, because it is unrecoverable and it stops
us ever applying to that posting again.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.eval.faults import fake_confirmation
from jobbot.submit.verify import SubmissionVerdict, verify_submission

FIXTURES = Path(__file__).parent / "fixtures"
_CHROME = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None
SUBMIT = "#submit_app"


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True, executable_path=_CHROME)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not installed: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    p = context.new_page()
    yield p
    context.close()


def _form(page):
    page.goto((FIXTURES / "application_form.html").as_uri(), wait_until="domcontentloaded")
    return page


def test_confirmation_text_over_a_live_form_is_unknown_not_submitted(page):
    _form(page)
    fake_confirmation(page)
    result = verify_submission(page, page, url_before=page.url,
                               submit_selector=SUBMIT, submit_present_before=True,
                               settle_ms=100)
    assert result.verdict is not SubmissionVerdict.SUBMITTED
    assert result.verdict is SubmissionVerdict.UNKNOWN
    assert any("contradiction" in e for e in result.evidence)


def test_a_real_confirmation_page_is_still_accepted(page):
    """The fix must not make every submission unverifiable."""
    page.goto((FIXTURES / "confirmation_page.html").as_uri(), wait_until="domcontentloaded")
    result = verify_submission(page, page, url_before="https://acme.example/apply",
                               submit_selector=SUBMIT, submit_present_before=True,
                               settle_ms=100)
    assert result.verdict is SubmissionVerdict.SUBMITTED


def test_a_hidden_form_left_behind_after_an_xhr_submit_still_verifies(page):
    """Single-page apps submit over XHR and hide the form rather than
    removing it. Presence would read as contradiction; visibility does not."""
    _form(page)
    fake_confirmation(page)
    page.evaluate("document.getElementById('application-form').style.display = 'none'")
    result = verify_submission(page, page, url_before=page.url,
                               submit_selector=SUBMIT, submit_present_before=True,
                               settle_ms=100)
    assert result.verdict is SubmissionVerdict.SUBMITTED


def test_a_selector_that_never_matched_is_not_evidence_of_progress(page):
    """Regression: `submit control no longer present` was emitted for a
    selector that matched nothing to begin with — an absence that was
    always there, counted as a change (failures §14)."""
    _form(page)
    result = verify_submission(page, page, url_before=page.url,
                               submit_selector="#this-never-existed",
                               settle_ms=100)
    assert not any("no longer present" in e for e in result.evidence)
    assert any("selector mismatch" in e for e in result.evidence)
    assert result.verdict is SubmissionVerdict.NOT_SUBMITTED


def test_a_caller_that_clicked_the_button_is_believed(page):
    """When the caller says the control was there, its absence is real
    evidence again — the heuristic must not override known truth."""
    _form(page)
    page.evaluate("document.getElementById('submit_app').remove()")
    result = verify_submission(page, page, url_before=page.url,
                               submit_selector=SUBMIT, submit_present_before=True,
                               settle_ms=100)
    assert any("no longer present" in e for e in result.evidence)

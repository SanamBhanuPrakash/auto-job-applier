"""Submission verification, driven against real pages in real Chromium.

The behaviour under test replaces the previous rule ("the submit click did
not raise, therefore we applied"), which recorded success for validation
failures, CAPTCHA walls, and multi-step forms. The asymmetry matters most:
a false SUBMITTED means the candidate believes they applied when they did
not, and it permanently blocks a retry.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.submit.form_scan import find_target_frame
from jobbot.submit.verify import SubmissionVerdict, detect_blocking, verify_submission

FIXTURES = Path(__file__).parent / "fixtures"
_CHROME_OVERRIDE = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None

GREENHOUSE_SUBMIT = 'button#submit_app, button[type="submit"]:has-text("Submit Application")'


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True, executable_path=_CHROME_OVERRIDE)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not installed (`playwright install chromium`): {exc}")
        yield b
        b.close()


def _verify(browser, fixture: str, *, url_before: str, selector: str = GREENHOUSE_SUBMIT):
    page = browser.new_page()
    page.goto((FIXTURES / fixture).as_uri())
    try:
        ctx = find_target_frame(page, "", timeout_ms=1000)
    except TimeoutError:
        ctx = page  # confirmation pages legitimately have no form
    result = verify_submission(
        page, ctx, url_before=url_before, submit_selector=selector, settle_ms=50
    )
    page.close()
    return result


def test_confirmation_page_verifies_as_submitted(browser):
    result = _verify(browser, "confirmation_page.html", url_before="https://acme.example/apply")
    assert result.verdict is SubmissionVerdict.SUBMITTED
    assert "thank you for applying" in result.confirmation_text.lower()
    assert result.evidence


def test_validation_error_with_live_form_is_not_submitted(browser):
    """The single most dangerous false positive: the employer rejected the
    form and the old code would have recorded it as applied."""
    result = _verify(browser, "validation_error_page.html", url_before="https://acme.example/apply")
    assert result.verdict is SubmissionVerdict.NOT_SUBMITTED
    assert any("validation error" in e for e in result.evidence)


def test_unchanged_form_page_is_not_submitted(browser):
    """Click had no effect at all: nothing navigated, form still there."""
    url = (FIXTURES / "application_form.html").as_uri()
    result = _verify(browser, "application_form.html", url_before=url)
    assert result.verdict is SubmissionVerdict.NOT_SUBMITTED
    assert any("unchanged" in e for e in result.evidence)


def test_captcha_page_is_blocked_not_submitted(browser):
    result = _verify(browser, "captcha_page.html", url_before="https://acme.example/apply")
    assert result.verdict is SubmissionVerdict.BLOCKED
    assert any("blocking" in e for e in result.evidence)


def test_ambiguous_page_is_unknown_never_submitted(browser):
    """Form gone and URL changed, but no confirmation wording. One weak
    signal must not be promoted to success."""
    result = _verify(browser, "silent_success_page.html", url_before="https://acme.example/apply")
    assert result.verdict is SubmissionVerdict.UNKNOWN
    assert result.verdict is not SubmissionVerdict.SUBMITTED


def test_unknown_is_never_upgraded_to_submitted(browser):
    from jobbot.submit.verify import VERDICT_IS_CONCLUSIVE_SUCCESS

    assert VERDICT_IS_CONCLUSIVE_SUCCESS[SubmissionVerdict.UNKNOWN] is False
    assert VERDICT_IS_CONCLUSIVE_SUCCESS[SubmissionVerdict.BLOCKED] is False
    assert VERDICT_IS_CONCLUSIVE_SUCCESS[SubmissionVerdict.NOT_SUBMITTED] is False
    assert VERDICT_IS_CONCLUSIVE_SUCCESS[SubmissionVerdict.SUBMITTED] is True


# --- blocking detection ----------------------------------------------------


def test_detect_blocking_finds_recaptcha_element(browser):
    page = browser.new_page()
    page.goto((FIXTURES / "captcha_page.html").as_uri())
    assert detect_blocking(page) is not None
    page.close()


def test_detect_blocking_is_quiet_on_a_normal_form(browser):
    page = browser.new_page()
    page.goto((FIXTURES / "application_form.html").as_uri())
    assert detect_blocking(page) is None
    page.close()


def test_blocking_takes_precedence_over_confirmation_text(browser):
    """A page that says 'thank you for applying' *and* shows a CAPTCHA is
    not a submission — never bypass or assume past a wall."""
    page = browser.new_page()
    page.set_content(
        "<body><h1>Thank you for applying</h1>"
        "<div class='g-recaptcha' data-sitekey='x'></div></body>"
    )
    result = verify_submission(page, page, url_before="https://a/apply", settle_ms=50)
    assert result.verdict is SubmissionVerdict.BLOCKED
    page.close()

"""Regression tests for a real bug hit live running `jobbot apply-all`: the
review step leaves the browser open so the applicant can fill remaining
fields (or just click the site's own real Submit button) themselves. If
they then close the window, `_resolve_submit_status` used to let Playwright's
raw "Target page, context or browser has been closed" exception crash the
attempt with status="error" — which `apply-all` retries every future run,
so it kept reopening a browser for a job already handled. These exercise
the fix directly against a real (closed) Playwright page, not mocks.
"""
from __future__ import annotations

import os

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

import jobbot.submit.base as base_module
from jobbot.models import Job

_CHROME_OVERRIDE = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True, executable_path=_CHROME_OVERRIDE)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not installed (`playwright install chromium`): {exc}")
        yield b
        b.close()


class _FakeAtsModule:
    """Stand-in for jobbot.submit.greenhouse/lever — click_submit is the
    only thing _resolve_submit_status calls on it."""

    def __init__(self, click_submit_fn):
        self.click_submit = click_submit_fn


def _job() -> Job:
    job = Job(source="greenhouse", external_id="1", company="Acme", title="Engineer", url="https://example.com")
    job.id = 999  # unpersisted; log.exception's "%d" formatting needs a real int
    return job


def test_closed_page_with_auto_submit_records_error_without_prompting(browser, monkeypatch):
    def must_not_be_called(_job):
        raise AssertionError("an unattended auto_submit run has no one to answer a prompt")

    monkeypatch.setattr(base_module, "confirm_already_closed_browser", must_not_be_called)

    page = browser.new_page()
    page.close()
    ats = _FakeAtsModule(lambda _ctx: pytest.fail("click_submit should never be attempted on a closed page"))

    status, error = base_module._resolve_submit_status(page, page, ats, _job(), auto_submit=True)

    assert status == "error"
    assert "closed" in error.lower()


def test_closed_page_without_auto_submit_asks_and_records_submitted_on_yes(browser, monkeypatch):
    monkeypatch.setattr(base_module, "confirm_already_closed_browser", lambda _job: True)

    page = browser.new_page()
    page.close()
    ats = _FakeAtsModule(lambda _ctx: pytest.fail("click_submit should never be attempted on a closed page"))

    status, error = base_module._resolve_submit_status(page, page, ats, _job(), auto_submit=False)

    assert status == "submitted"
    assert error == ""


def test_closed_page_without_auto_submit_records_skipped_on_no(browser, monkeypatch):
    monkeypatch.setattr(base_module, "confirm_already_closed_browser", lambda _job: False)

    page = browser.new_page()
    page.close()
    ats = _FakeAtsModule(lambda _ctx: pytest.fail("click_submit should never be attempted on a closed page"))

    status, error = base_module._resolve_submit_status(page, page, ats, _job(), auto_submit=False)

    assert status == "skipped"
    assert error == ""


def test_click_raising_closed_target_error_prompts_instead_of_crashing(browser, monkeypatch):
    """page.is_closed() can still read False for a moment while the click
    itself fails with Playwright's own closed-target error — the fix must
    catch that at the click site too, not just via the is_closed() check."""
    monkeypatch.setattr(base_module, "confirm_already_closed_browser", lambda _job: True)

    page = browser.new_page()

    def raise_closed(_ctx):
        raise PlaywrightError("Locator.click: Target page, context or browser has been closed")

    status, error = base_module._resolve_submit_status(page, page, _FakeAtsModule(raise_closed), _job(), auto_submit=False)

    assert status == "submitted"
    assert error == ""
    page.close()


def test_genuine_unrelated_click_failure_still_records_error(browser, monkeypatch):
    """A real, unrelated fill/click failure (browser never closed) must
    still be recorded as an error exactly as before — this fix only
    changes behavior for the specific closed-browser case."""
    def must_not_be_called(_job):
        raise AssertionError("an unrelated click failure should not be treated as a closed browser")

    monkeypatch.setattr(base_module, "confirm_already_closed_browser", must_not_be_called)

    page = browser.new_page()

    def raise_unrelated(_ctx):
        raise PlaywrightError("Locator.click: Timeout 5000ms exceeded")

    status, error = base_module._resolve_submit_status(page, page, _FakeAtsModule(raise_unrelated), _job(), auto_submit=False)

    assert status == "error"
    assert "Timeout" in error
    page.close()

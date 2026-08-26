"""_resolve_auto_submit_status is only used for unattended `--auto-submit`
runs (the interactive path now uses review.wait_for_submit_or_close
instead — see test_review_wait_for_submit.py). A browser that's already
gone by the time this runs must be recorded as a clean error, never a
crash, since there's no one watching an unattended run to ask about it.
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
    def __init__(self, click_submit_fn):
        self.click_submit = click_submit_fn


def _job() -> Job:
    job = Job(source="greenhouse", external_id="1", company="Acme", title="Engineer", url="https://example.com")
    job.id = 999  # unpersisted; log.exception's "%d" formatting needs a real int
    return job


def test_closed_page_records_error_without_attempting_a_click(browser):
    page = browser.new_page()
    page.close()
    ats = _FakeAtsModule(lambda _ctx: pytest.fail("click_submit should never be attempted on a closed page"))

    status, error = base_module._resolve_auto_submit_status(page, page, ats, _job())

    assert status == "error"
    assert "closed" in error.lower()


def test_successful_click_records_submitted():
    calls = []

    class FakePage:
        def is_closed(self):
            return False

        def wait_for_timeout(self, _ms):
            pass

    status, error = base_module._resolve_auto_submit_status(
        FakePage(), "form_ctx", _FakeAtsModule(lambda ctx: calls.append(ctx)), _job()
    )

    assert status == "submitted"
    assert error == ""
    assert calls == ["form_ctx"]


def test_click_failure_records_error_with_the_real_message():
    class FakePage:
        def is_closed(self):
            return False

    def raise_unrelated(_ctx):
        raise PlaywrightError("Locator.click: Timeout 5000ms exceeded")

    status, error = base_module._resolve_auto_submit_status(FakePage(), "form_ctx", _FakeAtsModule(raise_unrelated), _job())

    assert status == "error"
    assert "Timeout" in error

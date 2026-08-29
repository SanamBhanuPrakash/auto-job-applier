"""`_auto_submit` is only used for unattended `--auto-submit` runs (the
interactive path uses review.wait_for_submit_or_close — see
test_review_wait_for_submit.py). A browser that's already gone by the time
this runs must be recorded as a clean failure, never a crash, since there's
no one watching an unattended run to ask about it.

This replaces the earlier `_resolve_auto_submit_status`, which returned a
final status directly. It no longer can: a click that returns normally is
not proof of submission (it returns normally for validation failures,
CAPTCHA interstitials and multi-step forms), so `_auto_submit` now returns
the pre-click URL for jobbot/submit/verify.py to check the page against,
and the *verdict* decides the outcome. The guarantees the original tests
protected are all still asserted here.
"""
from __future__ import annotations

import os

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

import jobbot.submit.base as base_module
from jobbot.agent.states import ApplicationState
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
    SUBMIT_SELECTOR = "button[type=submit]"

    def __init__(self, click_submit_fn):
        self.click_submit = click_submit_fn


def _job() -> Job:
    job = Job(source="greenhouse", external_id="1", company="Acme", title="Engineer", url="https://example.com")
    job.id = 999  # unpersisted; log.exception's "%d" formatting needs a real int
    return job


@pytest.fixture
def parked(monkeypatch):
    """Capture state transitions without needing a database."""
    seen: list[tuple[int, ApplicationState, str]] = []
    monkeypatch.setattr(
        base_module, "_park",
        lambda app_id, state, **kw: seen.append((app_id, state, kw.get("reason", ""))),
    )
    return seen


def test_closed_page_records_failure_without_attempting_a_click(browser, parked):
    page = browser.new_page()
    page.close()
    ats = _FakeAtsModule(lambda _ctx: pytest.fail("click_submit should never be attempted on a closed page"))

    result = base_module._auto_submit(page, page, ats, _job(), 1, run_id="r")

    assert result is None, "no click was made, so there is nothing to verify"
    assert parked, "the failure must be recorded"
    _app_id, state, reason = parked[-1]
    assert state is ApplicationState.FAILED
    assert "closed" in reason.lower()


def test_successful_click_returns_url_for_verification(parked):
    calls = []

    class FakePage:
        url = "https://acme.example/apply"

        def is_closed(self):
            return False

    result = base_module._auto_submit(
        FakePage(), "form_ctx", _FakeAtsModule(lambda ctx: calls.append(ctx)), _job(), 1, run_id="r",
    )

    assert calls == ["form_ctx"], "the submit must actually be clicked"
    assert result == "https://acme.example/apply"
    # The click alone is NOT recorded as submitted; the run enters
    # SUBMITTING and the verdict comes from verification.
    assert [s for _, s, _ in parked] == [ApplicationState.SUBMITTING]


def test_submitting_is_persisted_before_the_click_is_attempted(parked):
    """Crash-safety ordering: if the process dies mid-click, the database
    must already say we were about to submit, so recovery escalates
    instead of blindly re-applying."""
    order: list[str] = []

    class FakePage:
        url = "https://acme.example/apply"

        def is_closed(self):
            return False

    def record_click(_ctx):
        order.append("click")

    def record_park(app_id, state, **kw):
        order.append(f"park:{state.value}")

    original = base_module._park
    base_module._park = record_park
    try:
        base_module._auto_submit(FakePage(), "ctx", _FakeAtsModule(record_click), _job(), 1, run_id="r")
    finally:
        base_module._park = original

    assert order == ["park:SUBMITTING", "click"], f"wrong ordering: {order}"


def test_click_failure_still_returns_url_so_the_page_gets_verified(parked):
    """A raising click is NOT proof that nothing was sent — the request may
    have gone through before the timeout. Verifying is the only safe move;
    declaring failure here could hide a real submission and let a later run
    apply a second time."""
    class FakePage:
        url = "https://acme.example/apply"

        def is_closed(self):
            return False

    def raise_unrelated(_ctx):
        raise PlaywrightError("Locator.click: Timeout 5000ms exceeded")

    result = base_module._auto_submit(
        FakePage(), "form_ctx", _FakeAtsModule(raise_unrelated), _job(), 1, run_id="r",
    )

    assert result == "https://acme.example/apply"
    assert [s for _, s, _ in parked] == [ApplicationState.SUBMITTING]

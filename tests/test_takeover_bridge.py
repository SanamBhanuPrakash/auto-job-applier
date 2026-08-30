"""The seam between the deterministic path and the agent.

The behaviour that matters: a posting whose form sits behind an "Apply"
button used to walk an *empty* form all the way to READY_TO_SUBMIT.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.agent.controller import Outcome
from jobbot.submit.takeover_bridge import reach_application_form

FIXTURES = Path(__file__).parent / "fixtures"
_CHROME = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None


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


def _goto(page, name):
    page.goto((FIXTURES / name).as_uri(), wait_until="domcontentloaded")


def test_the_bridge_reaches_the_form_and_returns_grounded_fields(page):
    _goto(page, "apply_entry_page.html")
    handoff, form_ctx, fields = reach_application_form(page, page)
    assert handoff is not None
    assert handoff.outcome is Outcome.RESOLVED
    # The caller's old handles are worthless after takeover; the bridge
    # must hand back freshly scanned ones.
    assert len(fields) >= 3
    assert form_ctx is not None


def test_fields_come_back_bound_to_the_page_the_agent_ended_on(page):
    """Re-grounding is the point: a field id scanned before takeover would
    refer to a document the agent has since navigated away from."""
    _goto(page, "apply_entry_page.html")
    _, form_ctx, fields = reach_application_form(page, page)
    labels = " ".join(f.label.lower() for f in fields)
    assert "first name" in labels or "email" in labels
    assert "application_form" in page.url


def test_the_bridge_is_a_no_op_when_takeover_is_switched_off(page, monkeypatch):
    """Zero-regression: with the setting off, the caller sees exactly what
    it saw before this existed."""
    from jobbot import config

    settings = config.get_settings()
    monkeypatch.setattr(settings, "jobbot_agent_takeover", False)
    _goto(page, "apply_entry_page.html")
    handoff, form_ctx, fields = reach_application_form(page, page)
    assert handoff is None
    assert fields == []
    assert "apply_entry_page" in page.url, "nothing should have navigated"


def test_an_unresolvable_page_reports_rather_than_inventing_a_form(page):
    _goto(page, "unknown_page.html")
    handoff, _, fields = reach_application_form(page, page)
    assert handoff is not None
    assert handoff.resolved is False
    assert fields == []


def test_a_wall_comes_back_as_blocked_not_as_a_missing_form(page):
    _goto(page, "captcha_page.html")
    handoff, _, _ = reach_application_form(page, page)
    assert handoff.outcome is Outcome.BLOCKED

"""The three perception pieces working together on real pages.

This is the loop the agent controller will run — observe, classify, record,
check for loops — exercised end to end against Chromium without any LLM
call, so it stays fast and deterministic.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.agent.observation import Detail, observe
from jobbot.agent.page_classify import PageState, classify, requires_stop
from jobbot.agent.trajectory import Budget, StopReason, Trajectory

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
    p = browser.new_page()
    yield p
    if not p.is_closed():
        p.close()


def _step(page, traj: Trajectory, action: str, do):
    """One OBSERVE -> ACT -> OBSERVE -> record cycle."""
    before = observe(page, detail=Detail.CONTROLS)
    cls = classify(before)
    step = traj.begin_step(state_digest=before.state_digest(), action=action, page_state=cls.state.value)
    do()
    after = observe(page, detail=Detail.CONTROLS)
    traj.complete_step(step, result_digest=after.state_digest())
    return after, classify(after)


def test_navigating_from_entry_page_to_form_is_recognized_as_progress(page):
    """The journey the agent must handle: job page -> Apply -> form."""
    page.goto((FIXTURES / "apply_entry_page.html").as_uri())
    traj = Trajectory()

    entry = classify(observe(page, detail=Detail.CONTROLS))
    assert entry.state is PageState.APPLICATION_ENTRY

    after, cls = _step(page, traj, "click apply", lambda: page.locator("#apply").click())

    assert cls.state is PageState.APPLICATION_FORM
    assert traj.steps[-1].made_progress is True
    assert traj.should_stop() is None


def test_clicking_a_dead_control_is_caught_as_no_progress(page):
    """A control that does nothing must terminate rather than repeat — on a
    submit button, repeating is how duplicates happen."""
    page.set_content(
        "<body><form><input id='a'><button id='dead' type='button'>Submit</button></form></body>"
    )
    traj = Trajectory(budget=Budget(max_repeat_same_state_action=2))

    for _ in range(2):
        _step(page, traj, "click submit", lambda: page.locator("#dead").click())

    assert traj.should_stop() is StopReason.NO_PROGRESS
    assert "click submit" in traj.forbidden_actions()


def test_filling_a_field_counts_as_progress_not_a_loop(page):
    """Guards the opposite error: aborting work that is actually advancing."""
    page.goto((FIXTURES / "application_form.html").as_uri())
    traj = Trajectory(budget=Budget(max_repeat_same_state_action=2))

    _step(page, traj, "fill first name", lambda: page.locator("#first_name").fill("Ada"))
    _step(page, traj, "fill last name", lambda: page.locator("#last_name").fill("Lovelace"))
    _step(page, traj, "fill email", lambda: page.locator("#email").fill("ada@example.com"))

    assert all(s.made_progress for s in traj.steps)
    assert traj.should_stop() is None
    assert traj.summary()["progressing_steps"] == 3


def test_a_blocking_page_stops_the_run_before_any_action(page):
    page.goto((FIXTURES / "captcha_page.html").as_uri())
    cls = classify(observe(page, detail=Detail.ARIA))
    assert requires_stop(cls.state)


def test_unknown_page_does_not_get_treated_as_a_form(page):
    page.goto((FIXTURES / "unknown_page.html").as_uri())
    cls = classify(observe(page, detail=Detail.ARIA))
    assert cls.state is PageState.UNKNOWN
    assert cls.state is not PageState.APPLICATION_FORM


def test_validation_error_after_submit_is_visible_as_a_state_change(page):
    """The agent must be able to see that its submit was rejected."""
    page.set_content("<body><form><input id='a' required><button id='go'>Submit</button></form></body>")
    before = observe(page, detail=Detail.CONTROLS)

    page.set_content(
        "<body><form><input id='a' required><button id='go'>Submit</button></form>"
        "<div role='alert'>This field is required.</div></body>"
    )
    after = observe(page, detail=Detail.CONTROLS)

    assert before.state_digest() != after.state_digest()
    assert any("required" in m.lower() for m in after.validation_messages)


def test_whole_perception_pass_stays_within_a_sane_token_budget(page):
    """Guards the context-rot rule: perception must stay cheap enough to
    run on every single step."""
    page.goto((FIXTURES / "application_form.html").as_uri())
    obs = observe(page, detail=Detail.ARIA)
    payload = obs.to_agent_dict()

    assert obs.approx_tokens() < 4000
    assert len(str(payload)) < 20000

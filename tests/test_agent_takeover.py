"""Agent takeover and hand-back (§16-§18), end to end in a real browser.

The interesting assertions are the negative ones: takeover cannot fill,
cannot submit, cannot run inside the danger zone, and cannot hand back on
its own say-so.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.agent.controller import Decision, Outcome
from jobbot.agent.deciders import HeuristicDecider
from jobbot.agent.observation import Detail, observe
from jobbot.agent.page_classify import PageState, classify
from jobbot.agent.policy import Autonomy, PolicyContext
from jobbot.agent.recovery import RecoveryTrigger
from jobbot.agent.states import ApplicationState
from jobbot.agent.takeover import (
    NextAction,
    detect_trigger,
    form_is_present,
    take_over,
)
from jobbot.agent.tools import ToolContext, ToolRegistry
from jobbot.agent.trajectory import Budget

FIXTURES = Path(__file__).parent / "fixtures"
_CHROME = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None
S = ApplicationState


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


def _goto(page, name: str):
    page.goto((FIXTURES / name).as_uri(), wait_until="domcontentloaded")
    return page


def _ctx(page, state=S.INSPECTING_FORM) -> ToolContext:
    return ToolContext(page=page, application_state=state, observe_detail=Detail.CONTROLS)


def _policy(page, autonomy=Autonomy.NAVIGATE, state=S.INSPECTING_FORM) -> PolicyContext:
    obs = observe(page, None, detail=Detail.CONTROLS)
    return PolicyContext(application_state=state, page_state=classify(obs).state,
                         autonomy=autonomy)


def _run(page, **kw):
    return take_over(
        "Reach the application form for this job posting.",
        _ctx(page, kw.pop("state", S.INSPECTING_FORM)),
        kw.pop("policy", None) or _policy(page),
        kw.pop("decider", None) or HeuristicDecider(),
        goal_reached=kw.pop("goal_reached", form_is_present),
        registry=ToolRegistry(),
        budget=kw.pop("budget", Budget(max_steps=8)),
        observe_detail=Detail.CONTROLS,
        **kw,
    )


# --- the happy path: takeover reaches a form and hands back ---------------


def test_takeover_gets_from_a_job_page_to_the_application_form(page):
    """The case that motivated this: the deterministic scan finds nothing
    because the form is behind an "Apply for this job" link."""
    _goto(page, "apply_entry_page.html")
    result = _run(page)
    assert result.outcome is Outcome.RESOLVED
    assert result.resolved is True
    assert result.next_action is NextAction.RESUME_AFTER_REGROUND
    assert "application_form" in page.url


def test_takeover_clears_a_consent_overlay_before_looking_for_apply(page):
    """A full-viewport consent overlay intercepts every click aimed at the
    Apply link underneath it. Clicking Apply first would fail forever."""
    _goto(page, "consent_overlay_page.html")
    result = _run(page)
    assert result.outcome is Outcome.RESOLVED
    assert "application_form" in page.url


def test_the_decider_prefers_rejecting_tracking_over_accepting_it(page):
    """Both buttons unblock the page. Accepting tracking on the
    candidate's behalf, unattended, is a choice we should not make for
    them when the site offers the other one."""
    _goto(page, "consent_overlay_page.html")
    obs = observe(page, None, detail=Detail.CONTROLS)
    from jobbot.agent.controller import DecisionContext

    decision = HeuristicDecider().decide(DecisionContext(
        goal="reach the form", observation=obs, classification=classify(obs),
        application_state=S.INSPECTING_FORM, available_tools=[],
    ))
    assert decision.action == "click"
    ref = decision.args["ref"]
    name = next(c.name for c in obs.controls if c.ref == ref)
    assert "reject" in name.lower()


def test_hand_back_carries_a_checkpoint_and_the_page_state(page):
    _goto(page, "apply_entry_page.html")
    result = _run(page)
    assert result.checkpoint["goal"]
    assert result.checkpoint["agent_run"]["steps"] >= 1
    assert result.new_state is not PageState.UNKNOWN


# --- the negative assertions ----------------------------------------------


def test_takeover_cannot_fill_a_field_even_when_the_decider_asks(page):
    """Takeover runs at NAVIGATE autonomy: reaching the form is the job,
    and filling it is the deterministic path's job."""
    _goto(page, "application_form.html")

    class FillingDecider:
        def decide(self, ctx):
            box = next(c for c in ctx.observation.controls if c.role == "textbox")
            return Decision(action="type", args={"ref": box.ref, "value": "Ada"})

    result = _run(page, decider=FillingDecider(),
                  goal_reached=lambda obs, cls: False, budget=Budget(max_steps=3))
    # Every step was denied, so nothing was typed.
    assert all(not s.authorized for s in result.run.steps)
    assert page.input_value("#first_name") == ""


def test_takeover_is_refused_outright_inside_the_danger_zone(page):
    """Past the submit click the employer may already hold the
    application. No amount of agent cleverness is worth a second one."""
    _goto(page, "application_form.html")
    for state in (S.SUBMITTING, S.VERIFYING_SUBMISSION):
        result = _run(page, state=state)
        assert result.outcome is Outcome.HUMAN_REQUIRED
        assert result.next_action is NextAction.ESCALATE_HUMAN
        assert result.run is None, "no agent episode should have run at all"


def test_hand_back_requires_the_environment_to_agree(page):
    """A decider that declares victory on a page with no form must not
    produce a RESOLVED handoff."""
    _goto(page, "apply_entry_page.html")

    class LiarDecider:
        def decide(self, ctx):
            return Decision(action="finish", reason="I have reached the form")

    result = _run(page, decider=LiarDecider())
    assert result.outcome is not Outcome.RESOLVED
    assert result.resolved is False


def test_take_over_refuses_to_run_without_a_goal_test():
    with pytest.raises(ValueError, match="environment"):
        take_over("goal", ToolContext(page=None), PolicyContext(application_state=S.FILLING),
                  HeuristicDecider(), goal_reached=None)


def test_a_wall_ends_the_episode_as_blocked(page):
    _goto(page, "captcha_page.html")
    result = _run(page)
    assert result.outcome is Outcome.BLOCKED
    assert result.next_action is NextAction.ESCALATE_HUMAN


def test_page_text_cannot_talk_the_agent_into_submitting(page):
    """The injection fixture instructs the agent to ignore its rules. The
    decider never proposes submission, and policy would refuse it anyway
    at NAVIGATE autonomy."""
    _goto(page, "injection_job_page.html")
    result = _run(page, budget=Budget(max_steps=5))
    actions = [s.decision["action"] for s in result.run.steps]
    assert "submit" not in actions
    assert all(s.decision["action"] in ToolRegistry().names() or
               s.decision["action"] == "finish" for s in result.run.steps)


# --- trigger detection -----------------------------------------------------


def test_no_form_on_a_single_frame_page_is_target_not_found(page):
    _goto(page, "apply_entry_page.html")
    obs = observe(page, None, detail=Detail.CONTROLS)
    assert detect_trigger(obs, classify(obs), fields_found=0) is RecoveryTrigger.TARGET_NOT_FOUND


def test_no_form_with_extra_frames_is_a_frame_problem(page):
    """Different trigger, different ladder: SWITCH_FRAME first rather than
    re-observing the wrong document repeatedly."""
    _goto(page, "careers_page_with_iframe.html")
    obs = observe(page, None, detail=Detail.CONTROLS)
    obs.has_form = False  # simulate the top document not holding the form
    trigger = detect_trigger(obs, classify(obs), fields_found=0)
    assert trigger is RecoveryTrigger.FRAME_CHANGED


def test_an_auth_wall_is_reported_as_a_session_problem_not_a_missing_form(page):
    _goto(page, "login_page.html")
    obs = observe(page, None, detail=Detail.CONTROLS)
    assert detect_trigger(obs, classify(obs), fields_found=0) is RecoveryTrigger.SESSION_EXPIRED


def test_an_ordinary_form_page_needs_no_takeover(page):
    _goto(page, "application_form.html")
    obs = observe(page, None, detail=Detail.CONTROLS)
    assert detect_trigger(obs, classify(obs), fields_found=12) is None

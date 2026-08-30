"""AgentController loop, driven against real Chromium with scripted deciders.

Using a scripted `Decider` rather than an LLM is the point: the loop's
guarantees — authorization, verification, bounds, loop detection — must hold
regardless of what the decider asks for. Each test below is a decider
*trying* something, and the controller deciding what actually happens.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.agent.controller import (
    AgentController,
    Decision,
    DecisionContext,
    Outcome,
)
from jobbot.agent.observation import Detail
from jobbot.agent.page_classify import PageState
from jobbot.agent.policy import Autonomy, PolicyContext
from jobbot.agent.states import ApplicationState
from jobbot.agent.tools import ToolContext, ToolRegistry
from jobbot.agent.trajectory import Budget, StopReason

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


class ScriptedDecider:
    """Returns a fixed sequence of decisions, then 'finish'. Records every
    context it was given so tests can assert on what the agent was told."""

    def __init__(self, decisions: list[Decision]):
        self._decisions = list(decisions)
        self.seen: list[DecisionContext] = []

    def decide(self, ctx: DecisionContext) -> Decision:
        self.seen.append(ctx)
        if self._decisions:
            return self._decisions.pop(0)
        return Decision(action="finish", reason="script exhausted")


class RepeatingDecider:
    """Always asks for the same action — the shape that must be bounded."""

    def __init__(self, decision: Decision):
        self.decision = decision
        self.calls = 0
        self.seen: list[DecisionContext] = []

    def decide(self, ctx: DecisionContext) -> Decision:
        self.calls += 1
        self.seen.append(ctx)
        return self.decision


def _controller(decider, **budget_kw) -> AgentController:
    return AgentController(
        ToolRegistry(), decider,
        budget=Budget(**budget_kw) if budget_kw else Budget(max_steps=12),
        observe_detail=Detail.CONTROLS,
    )


def _ctx(page, **kw) -> ToolContext:
    kw.setdefault("application_state", ApplicationState.FILLING)
    return ToolContext(page=page, **kw)


def _policy(**kw) -> PolicyContext:
    kw.setdefault("application_state", ApplicationState.FILLING)
    kw.setdefault("autonomy", Autonomy.FILL)
    return PolicyContext(**kw)


def _ref(controller_ctx, page, fragment: str) -> str:
    from jobbot.agent.observation import observe

    for c in observe(page, detail=Detail.CONTROLS).controls:
        if fragment.lower() in c.name.lower():
            return c.ref
    raise AssertionError(f"no control matching {fragment!r}")


# --- the happy loop --------------------------------------------------------


def test_loop_fills_a_form_and_finishes(page):
    page.goto((FIXTURES / "application_form.html").as_uri())
    first = _ref(None, page, "first name")
    email = _ref(None, page, "email")

    decider = ScriptedDecider([
        Decision(action="type", args={"ref": first, "value": "Ada"}, reason="fill first name"),
        Decision(action="type", args={"ref": email, "value": "ada@example.com"}, reason="fill email"),
        Decision(action="finish", reason="required fields done"),
    ])
    run = _controller(decider).run("fill the application", _ctx(page), _policy())

    assert run.outcome is Outcome.RESOLVED
    assert page.locator("#first_name").input_value() == "Ada"
    assert all(s.authorized for s in run.steps if s.decision["action"] != "finish")
    assert all(s.verified for s in run.steps if s.tool_result)


def test_every_step_is_traced(page):
    """§59: the trace must explain what happened without a rerun."""
    page.goto((FIXTURES / "application_form.html").as_uri())
    ref = _ref(None, page, "first name")
    decider = ScriptedDecider([Decision(action="type", args={"ref": ref, "value": "Ada"},
                                        objective="fill", reason="needed", expected_outcome="field set")])
    run = _controller(decider).run("fill", _ctx(page), _policy())

    step = run.steps[0]
    assert step.index == 0
    assert step.page_state == PageState.APPLICATION_FORM.value
    assert step.decision["action"] == "type"
    assert step.decision["reason"] == "needed"
    assert step.tool_result["ok"] is True
    assert step.verified is True
    assert "steps" in run.summary()


# --- authorization is enforced by the loop, not the decider ---------------


def test_controller_denies_an_unauthorized_action_and_keeps_going(page):
    """The decider asks to type outside a form state; policy refuses, and
    the episode continues rather than crashing."""
    page.goto((FIXTURES / "application_form.html").as_uri())
    ref = _ref(None, page, "first name")

    decider = ScriptedDecider([
        Decision(action="type", args={"ref": ref, "value": "Ada"}, reason="premature fill"),
        Decision(action="finish", reason="done"),
    ])
    ctx = _ctx(page, application_state=ApplicationState.OPENING_APPLICATION)
    run = _controller(decider).run(
        "fill", ctx, _policy(application_state=ApplicationState.OPENING_APPLICATION)
    )

    denied = run.steps[0]
    assert denied.authorized is False
    assert "requires state" in denied.denial_reason
    assert page.locator("#first_name").input_value() == "", "the denied action must not have run"


def test_sensitive_field_request_escalates_to_human(page):
    """The single most important refusal: the agent must not answer a work
    authorization question on its own."""
    page.goto((FIXTURES / "application_form.html").as_uri())
    from jobbot.agent.observation import observe

    radio = next(c for c in observe(page, detail=Detail.CONTROLS).controls if c.role == "radio")

    decider = ScriptedDecider([Decision(action="check", args={"ref": radio.ref}, reason="answer it")])
    run = _controller(decider).run("complete the form", _ctx(page), _policy())

    assert run.outcome is Outcome.HUMAN_REQUIRED
    assert "sensitive" in run.reason.lower()


def test_agent_cannot_invoke_an_unregistered_tool(page):
    page.goto((FIXTURES / "application_form.html").as_uri())
    decider = ScriptedDecider([
        Decision(action="run_python", args={}, reason="just let me script it"),
        Decision(action="finish", reason="done"),
    ])
    run = _controller(decider).run("do anything", _ctx(page), _policy())

    assert run.steps[0].authorized is False
    assert "unknown tool" in run.steps[0].denial_reason


# --- blocking --------------------------------------------------------------


def test_captcha_page_ends_the_episode_as_blocked(page):
    page.goto((FIXTURES / "captcha_page.html").as_uri())
    decider = RepeatingDecider(Decision(action="observe", reason="look"))
    run = _controller(decider).run("apply", _ctx(page), _policy())

    assert run.outcome is Outcome.BLOCKED
    assert run.final_state is PageState.CAPTCHA
    assert decider.calls == 0, "blocked before the decider was ever consulted"


# --- bounds ----------------------------------------------------------------


def test_repeating_a_dead_action_terminates_as_no_progress(page):
    """The loop that would otherwise burn the whole budget."""
    page.set_content('<body><form><input id="a"><button id="dead" type="button">Go</button></form></body>')
    from jobbot.agent.observation import observe

    ref = next(c.ref for c in observe(page, detail=Detail.CONTROLS).controls if c.role == "button")

    decider = RepeatingDecider(Decision(action="click", args={"ref": ref}, reason="try again"))
    run = _controller(decider, max_steps=20, max_repeat_same_state_action=2).run(
        "submit", _ctx(page), _policy()
    )

    assert run.outcome is Outcome.HUMAN_REQUIRED
    assert run.stop_reason in (StopReason.NO_PROGRESS, StopReason.ACTION_REPEATED)
    assert len(run.steps) < 20, "must stop well before the step budget"


def test_step_budget_is_a_hard_ceiling(page):
    page.goto((FIXTURES / "application_form.html").as_uri())
    # scroll always "succeeds" and is low-risk, so no-progress detection
    # does not fire — only the raw step budget can stop this.
    decider = RepeatingDecider(Decision(action="scroll", args={"direction": "down"}, reason="keep looking"))
    run = _controller(decider, max_steps=4, max_same_action_total=99,
                      max_repeat_same_state_action=99).run("explore", _ctx(page), _policy())

    assert run.stop_reason is StopReason.STEP_BUDGET
    assert len(run.steps) == 4


def test_a_broken_decider_cannot_hang_the_browser(page):
    class Exploding:
        def decide(self, ctx):
            raise RuntimeError("model returned nonsense")

    page.goto((FIXTURES / "application_form.html").as_uri())
    run = _controller(Exploding()).run("fill", _ctx(page), _policy())

    assert run.outcome is Outcome.RECOVERABLE
    assert "model returned nonsense" in run.reason


# --- goal evaluation is the environment's job, not the decider's ----------


def test_decider_claiming_success_is_checked_against_the_page(page):
    """§106/§107: 'finish' is a claim. If a goal test is supplied, the
    environment decides whether it holds."""
    page.goto((FIXTURES / "unknown_page.html").as_uri())
    decider = ScriptedDecider([Decision(action="finish", reason="I believe we are done")])

    run = _controller(decider).run(
        "reach an application form", _ctx(page), _policy(),
        goal_reached=lambda obs, cls: cls.state is PageState.APPLICATION_FORM,
    )
    assert run.outcome is Outcome.RECOVERABLE
    assert "goal condition is not satisfied" in run.reason


def test_goal_reached_ends_the_episode_before_any_action(page):
    page.goto((FIXTURES / "application_form.html").as_uri())
    decider = RepeatingDecider(Decision(action="scroll", args={"direction": "down"}, reason="x"))

    run = _controller(decider).run(
        "reach an application form", _ctx(page), _policy(),
        goal_reached=lambda obs, cls: cls.state is PageState.APPLICATION_FORM,
    )
    assert run.outcome is Outcome.RESOLVED
    assert run.steps == []
    assert decider.calls == 0


def test_navigating_from_entry_to_form_satisfies_the_goal(page):
    """The real journey: job page -> Apply -> application form."""
    page.goto((FIXTURES / "apply_entry_page.html").as_uri())
    from jobbot.agent.observation import observe

    apply_ref = next(c.ref for c in observe(page, detail=Detail.CONTROLS).controls
                     if "apply" in c.name.lower())

    decider = ScriptedDecider([Decision(action="click", args={"ref": apply_ref}, reason="enter application")])
    run = _controller(decider).run(
        "reach the application form",
        _ctx(page, application_state=ApplicationState.OPENING_APPLICATION),
        _policy(application_state=ApplicationState.OPENING_APPLICATION),
        goal_reached=lambda obs, cls: cls.state is PageState.APPLICATION_FORM,
    )
    assert run.outcome is Outcome.RESOLVED
    assert run.steps[0].verified is True


# --- context handed to the decider ----------------------------------------


def test_decider_context_is_bounded_and_fences_page_content(page):
    page.goto((FIXTURES / "injection_job_page.html").as_uri())
    decider = ScriptedDecider([Decision(action="finish", reason="done")])
    _controller(decider).run("apply", _ctx(page), _policy())

    payload = decider.seen[0].to_prompt_payload()
    assert "Ignore all previous instructions" in payload["observation"]["visible_text"], \
        "page text is present as data"
    assert set(payload) == {
        "goal", "application_state", "page", "observation", "available_tools",
        "recent_actions", "do_not_repeat", "known_failures", "steps_remaining",
    }
    assert len(str(payload)) < 25_000


def test_decider_is_told_what_already_failed(page):
    page.set_content('<body><form><input id="a"><button id="dead" type="button">Go</button></form></body>')
    from jobbot.agent.observation import observe

    ref = next(c.ref for c in observe(page, detail=Detail.CONTROLS).controls if c.role == "button")
    decider = RepeatingDecider(Decision(action="click", args={"ref": ref}, reason="retry"))
    _controller(decider, max_repeat_same_state_action=3, max_steps=6).run(
        "submit", _ctx(page), _policy()
    )

    later = decider.seen[-1]
    assert "click" in later.forbidden_actions or later.known_failures

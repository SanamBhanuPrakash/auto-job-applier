"""The recovery engine: a closed action set and a deterministic ladder.

The tests that matter most here are the ones asserting what recovery
*cannot* do — invent a primitive, or act while a submission may already
have reached an employer.
"""
from __future__ import annotations

import pytest

from jobbot.agent.recovery import (
    CONSEQUENTIAL,
    LADDERS,
    RecoveryAction,
    RecoveryEngine,
    RecoveryTrigger,
    is_permitted,
    plan_recovery,
)
from jobbot.agent.states import DANGER_ZONE, ApplicationState
from jobbot.agent.tools import ToolContext

S = ApplicationState


# --- the action set is closed ---------------------------------------------


def test_the_action_set_is_exactly_the_fifteen_in_the_spec():
    assert {a.value for a in RecoveryAction} == {
        "REOBSERVE", "REGROUND", "REFRESH", "RELOAD", "BACKTRACK", "SWITCH_FRAME",
        "SWITCH_TAB", "REAUTHENTICATE", "REOPEN_APPLICATION", "RETRY",
        "CHANGE_TARGET", "CHANGE_FILL_METHOD", "AGENT_TAKEOVER",
        "ESCALATE_HUMAN", "STOP",
    }


@pytest.mark.parametrize("invented", [
    "run_javascript", "disable_validation", "solve_captcha", "eval",
    "REOBSERVE; DROP TABLE applications", "", "submit_anyway",
])
def test_a_model_cannot_invent_a_recovery_primitive(invented):
    assert RecoveryAction.parse(invented) is None


def test_a_real_action_still_parses_case_insensitively():
    assert RecoveryAction.parse("reobserve") is RecoveryAction.REOBSERVE
    assert RecoveryAction.parse("  Reground  ") is RecoveryAction.REGROUND


# --- the ladder ------------------------------------------------------------


def test_every_trigger_has_a_ladder_that_ends_at_a_human():
    for trigger, ladder in LADDERS.items():
        assert ladder, trigger
        assert ladder[-1] in (RecoveryAction.ESCALATE_HUMAN, RecoveryAction.STOP), trigger


def test_the_ladder_is_a_pure_function_of_its_inputs():
    args = dict(attempt=1, application_state=S.FILLING)
    first = plan_recovery(RecoveryTrigger.TARGET_NOT_FOUND, **args)
    for _ in range(5):
        assert plan_recovery(RecoveryTrigger.TARGET_NOT_FOUND, **args) is first


def test_target_not_found_starts_cheap_and_escalates():
    ladder = [
        plan_recovery(RecoveryTrigger.TARGET_NOT_FOUND, attempt=i, application_state=S.FILLING)
        for i in range(4)
    ]
    assert ladder[0] is RecoveryAction.REOBSERVE
    assert RecoveryAction.REGROUND in ladder


def test_past_the_end_of_a_ladder_the_answer_is_a_human_not_a_restart():
    action = plan_recovery(
        RecoveryTrigger.TARGET_NOT_FOUND, attempt=99, application_state=S.FILLING,
    )
    assert action is RecoveryAction.ESCALATE_HUMAN


def test_no_progress_never_prescribes_a_plain_retry():
    """Repeating the action is precisely what NO_PROGRESS means."""
    assert RecoveryAction.RETRY not in LADDERS[RecoveryTrigger.NO_PROGRESS]


def test_validation_error_never_prescribes_a_reload():
    """A reload throws away every field already filled."""
    ladder = LADDERS[RecoveryTrigger.VALIDATION_ERROR]
    assert RecoveryAction.RELOAD not in ladder
    assert RecoveryAction.REFRESH not in ladder


def test_a_wall_escalates_immediately_and_never_retries():
    assert LADDERS[RecoveryTrigger.BLOCKED] == (RecoveryAction.ESCALATE_HUMAN,)


def test_takeover_can_be_removed_from_every_ladder():
    for trigger in LADDERS:
        for attempt in range(8):
            action = plan_recovery(
                trigger, attempt=attempt, application_state=S.FILLING, allow_takeover=False,
            )
            assert action is not RecoveryAction.AGENT_TAKEOVER


# --- the danger zone -------------------------------------------------------


@pytest.mark.parametrize("state", sorted(DANGER_ZONE, key=lambda s: s.value))
def test_inside_the_danger_zone_only_looking_and_escalating_exist(state):
    """The central invariant: past the submit click, no recovery action may
    touch the page. A reload could re-post the form."""
    seen = {
        plan_recovery(t, attempt=i, application_state=state)
        for t in RecoveryTrigger for i in range(6)
    }
    assert seen <= {RecoveryAction.REOBSERVE, RecoveryAction.ESCALATE_HUMAN}


@pytest.mark.parametrize("action", sorted(CONSEQUENTIAL, key=lambda a: a.value))
def test_consequential_actions_are_refused_in_the_danger_zone(action):
    for state in DANGER_ZONE:
        assert is_permitted(action, state) is False


def test_consequential_actions_are_permitted_outside_the_danger_zone():
    for action in CONSEQUENTIAL:
        assert is_permitted(action, S.FILLING) is True


def test_the_engine_refuses_a_danger_zone_action_even_when_handed_one_directly():
    """`plan_recovery` is not the only way in — a model or a caller could
    pass an action straight to `execute`. The gate has to be on execute."""
    engine = RecoveryEngine()
    ctx = ToolContext(page=None, application_state=S.SUBMITTING)
    step = engine.execute(RecoveryAction.RELOAD, ctx)
    assert step.executed is False
    assert step.escalate is True
    assert step.action is RecoveryAction.ESCALATE_HUMAN
    assert "may already have reached the employer" in step.reason


# --- directives vs. execution ---------------------------------------------


def test_caller_level_actions_are_reported_not_faked():
    """The engine cannot perform a retry or a takeover itself. Saying it
    did would be exactly the false-success failure this repo refuses."""
    engine = RecoveryEngine()
    ctx = ToolContext(page=None, application_state=S.FILLING)

    retry = engine.execute(RecoveryAction.RETRY, ctx)
    assert retry.executed is False and retry.retry_operation is True

    takeover = engine.execute(RecoveryAction.AGENT_TAKEOVER, ctx)
    assert takeover.executed is False and takeover.takeover is True


def test_reauthenticate_never_improvises_a_login():
    """This originally asserted that REAUTHENTICATE reported itself
    unimplemented. It is implemented now (jobbot/auth), so the guarantee
    it was protecting — never improvise a login — is asserted directly
    instead: an unreadable page must not produce "authenticated".
    """
    engine = RecoveryEngine()
    ctx = ToolContext(page=None, application_state=S.FILLING)
    step = engine.execute(RecoveryAction.REAUTHENTICATE, ctx)
    assert step.action is RecoveryAction.ESCALATE_HUMAN
    assert step.escalate is True
    assert step.retry_operation is False


def test_reauthentication_stops_after_repeated_failures():
    """A stored credential that is wrong stays wrong. A second attempt does
    not make it right, and a third is how the account gets locked."""
    from jobbot.auth import session as authsess

    assert authsess.MAX_CONSECUTIVE_FAILURES <= 2
    ladder = LADDERS[RecoveryTrigger.SESSION_EXPIRED]
    assert ladder.count(RecoveryAction.REAUTHENTICATE) == 1
    assert ladder[-1] is RecoveryAction.ESCALATE_HUMAN


def test_reopen_without_a_url_escalates_instead_of_navigating_somewhere():
    engine = RecoveryEngine()
    ctx = ToolContext(page=None, application_state=S.FILLING)
    step = engine.execute(RecoveryAction.REOPEN_APPLICATION, ctx, application_url="")
    assert step.action is RecoveryAction.ESCALATE_HUMAN


def test_a_degraded_reobserve_is_not_reported_as_a_successful_recovery():
    """Regression: `page` is None, so every perception tier fails and the
    observation comes back degraded. The engine originally reported that
    as `executed=True, retry_operation=True` — telling the caller to retry
    on the strength of a read where nothing was read, and advancing the
    ladder a rung for free. See browser-agent-failures.md §14.
    """
    engine = RecoveryEngine()
    ctx = ToolContext(page=None, application_state=S.FILLING)
    step = engine.execute(RecoveryAction.REOBSERVE, ctx)
    assert step.executed is False
    assert step.retry_operation is False
    assert "degraded" in step.reason


def test_a_recovery_action_that_raises_does_not_crash_the_attempt():
    """A failure inside recovery is not a reason to lose the application's
    state — every path returns a RecoveryStep rather than propagating."""
    engine = RecoveryEngine()
    ctx = ToolContext(page=None, application_state=S.FILLING)
    for action in (RecoveryAction.REOBSERVE, RecoveryAction.REGROUND,
                   RecoveryAction.RELOAD, RecoveryAction.BACKTRACK,
                   RecoveryAction.SWITCH_TAB):
        step = engine.execute(action, ctx)
        assert step.retry_operation is False, action

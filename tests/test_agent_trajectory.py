"""Loop detection and hard budgets.

An unbounded agent loop on a job application is not merely wasteful:
repeatedly clicking a submit control is a route to duplicate submissions.
Detection is best-effort; the budgets are not, which is why both exist.
"""
from __future__ import annotations

import time

import pytest

from jobbot.agent.trajectory import Budget, StopReason, Trajectory, _is_alternating


def _run(traj: Trajectory, state: str, action: str, result: str) -> None:
    step = traj.begin_step(state_digest=state, action=action)
    traj.complete_step(step, result_digest=result)


# --- no-progress -----------------------------------------------------------


def test_repeating_an_action_that_changes_nothing_is_no_progress():
    traj = Trajectory(budget=Budget(max_repeat_same_state_action=2))
    _run(traj, "A", "click submit", "A")
    assert traj.detect_loop() is None, "one failed attempt is not a loop"
    _run(traj, "A", "click submit", "A")
    assert traj.detect_loop() is StopReason.NO_PROGRESS


def test_an_action_that_changes_state_is_not_a_loop():
    traj = Trajectory()
    for i in range(5):
        _run(traj, f"S{i}", "click next", f"S{i+1}")
    assert traj.detect_loop() is None


def test_same_action_from_different_states_is_not_no_progress():
    """Clicking 'Next' on each page of a multi-page form is normal."""
    traj = Trajectory(budget=Budget(max_repeat_same_state_action=2))
    _run(traj, "page1", "click next", "page2")
    _run(traj, "page2", "click next", "page3")
    _run(traj, "page3", "click next", "page4")
    assert traj.detect_loop() is None


# --- cycles ----------------------------------------------------------------


def test_a_b_a_b_cycle_is_detected():
    """The exact shape from the spec: S1 -> next -> S2 -> next -> S1 ..."""
    traj = Trajectory()
    _run(traj, "S1", "click next", "S2")
    _run(traj, "S2", "click next", "S1")
    _run(traj, "S1", "click next", "S2")
    _run(traj, "S2", "click next", "S1")
    assert traj.detect_loop() in (StopReason.CYCLE, StopReason.NO_PROGRESS)


def test_a_single_there_and_back_is_not_a_cycle():
    """Opening a modal and closing it again is legitimate progress."""
    traj = Trajectory()
    _run(traj, "form", "open modal", "modal")
    _run(traj, "modal", "close modal", "form")
    _run(traj, "form", "fill name", "form_filled")
    assert traj.detect_loop() is None


def test_is_alternating_helper():
    assert _is_alternating(["A", "B", "A", "B"])
    assert not _is_alternating(["A", "B", "A"])          # too short
    assert not _is_alternating(["A", "B", "C", "B"])     # three states
    assert not _is_alternating(["A", "A", "A", "A"])     # one state, not alternating


# --- repeated action overall ----------------------------------------------


def test_same_action_too_many_times_stops_even_if_state_keeps_changing():
    """A spinner or rotating ad makes every state look new; without this
    bound, an agent could click the same dead control forever."""
    traj = Trajectory(budget=Budget(max_same_action_total=4, max_repeat_same_state_action=99))
    for i in range(4):
        _run(traj, f"s{i}", "click apply", f"s{i}x")
    assert traj.detect_loop() is StopReason.ACTION_REPEATED


# --- budgets ---------------------------------------------------------------


def test_step_budget_is_a_hard_stop():
    traj = Trajectory(budget=Budget(max_steps=3))
    for i in range(3):
        _run(traj, f"s{i}", f"a{i}", f"r{i}")
    assert traj.budget_exceeded() is StopReason.STEP_BUDGET


def test_llm_call_budget():
    traj = Trajectory(budget=Budget(max_llm_calls=2))
    traj.record_llm_call(100)
    assert traj.budget_exceeded() is None
    traj.record_llm_call(100)
    assert traj.budget_exceeded() is StopReason.LLM_CALL_BUDGET


def test_token_budget():
    traj = Trajectory(budget=Budget(max_tokens=500))
    traj.record_llm_call(499)
    assert traj.budget_exceeded() is None
    traj.record_llm_call(2)
    assert traj.budget_exceeded() is StopReason.TOKEN_BUDGET


def test_recovery_budget():
    traj = Trajectory(budget=Budget(max_recovery_attempts=2))
    traj.record_recovery()
    assert traj.budget_exceeded() is None
    traj.record_recovery()
    assert traj.budget_exceeded() is StopReason.RECOVERY_BUDGET


def test_time_budget():
    traj = Trajectory(budget=Budget(max_seconds=0.05))
    time.sleep(0.06)
    assert traj.budget_exceeded() is StopReason.TIME_BUDGET


def test_should_stop_checks_budgets_and_loops_together():
    traj = Trajectory(budget=Budget(max_steps=99, max_repeat_same_state_action=2))
    _run(traj, "A", "click", "A")
    _run(traj, "A", "click", "A")
    assert traj.should_stop() is StopReason.NO_PROGRESS


def test_fresh_trajectory_has_no_reason_to_stop():
    assert Trajectory().should_stop() is None


# --- guidance back to the agent -------------------------------------------


def test_forbidden_actions_reports_what_already_failed_here():
    """So the agent picks a different strategy rather than being told 'no'
    over and over."""
    traj = Trajectory()
    _run(traj, "A", "click submit", "A")   # no progress
    assert "click submit" in traj.forbidden_actions()


def test_forbidden_actions_resets_after_real_progress():
    traj = Trajectory()
    _run(traj, "A", "click submit", "A")
    _run(traj, "A", "fill name", "B")      # progressed to B
    assert traj.forbidden_actions() == set(), "past failures at A don't constrain B"


# --- bookkeeping -----------------------------------------------------------


def test_in_flight_step_does_not_count_as_progress_or_loop():
    traj = Trajectory(budget=Budget(max_repeat_same_state_action=1))
    traj.begin_step(state_digest="A", action="click")  # never completed
    assert traj.detect_loop() is None


def test_step_made_progress_flag():
    traj = Trajectory()
    s = traj.begin_step(state_digest="A", action="x")
    traj.complete_step(s, result_digest="A")
    assert s.made_progress is False
    s2 = traj.begin_step(state_digest="A", action="y")
    traj.complete_step(s2, result_digest="B")
    assert s2.made_progress is True


def test_summary_reports_usable_counters():
    traj = Trajectory()
    _run(traj, "A", "x", "B")
    _run(traj, "B", "y", "B")
    traj.record_llm_call(120)
    traj.record_recovery()

    s = traj.summary()
    assert s["steps"] == 2
    assert s["progressing_steps"] == 1
    assert s["llm_calls"] == 1
    assert s["tokens_used"] == 120
    assert s["recovery_attempts"] == 1
    assert s["elapsed_seconds"] >= 0

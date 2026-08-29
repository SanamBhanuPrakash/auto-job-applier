import pytest

from jobbot.agent.states import (
    CONSUMES_JOB,
    DANGER_ZONE,
    TERMINAL,
    ApplicationState,
    FailureCategory,
    IllegalTransition,
    assert_transition,
    is_legal_transition,
    is_safe_to_auto_resume,
    is_terminal,
    policy_for,
    should_retry,
)

S = ApplicationState


def test_happy_path_is_legal_end_to_end():
    path = [
        S.DISCOVERED, S.ANALYZING, S.ELIGIBILITY_CHECK, S.SELECTING_RESUME,
        S.OPENING_APPLICATION, S.INSPECTING_FORM, S.FILLING, S.VERIFYING_FIELDS,
        S.READY_TO_SUBMIT, S.SUBMITTING, S.VERIFYING_SUBMISSION, S.SUBMITTED,
    ]
    for src, dst in zip(path, path[1:]):
        assert is_legal_transition(src, dst), f"{src} -> {dst} should be legal"


def test_cannot_skip_verification_to_reach_submitted():
    """The whole point of the machine: nothing may be recorded as
    submitted without passing through verification."""
    assert not is_legal_transition(S.SUBMITTING, S.SUBMITTED)
    assert not is_legal_transition(S.READY_TO_SUBMIT, S.SUBMITTED)
    assert not is_legal_transition(S.FILLING, S.SUBMITTED)
    with pytest.raises(IllegalTransition):
        assert_transition(S.FILLING, S.SUBMITTED)


def test_submitting_cannot_fall_back_to_a_retryable_state():
    """Once we may have hit the employer, going back to 'just fill it
    again' is the duplicate-application bug."""
    for retryable in (S.FILLING, S.OPENING_APPLICATION, S.READY_TO_SUBMIT, S.RECOVERING):
        assert not is_legal_transition(S.SUBMITTING, retryable)


def test_terminal_states_have_no_exits():
    for state in TERMINAL:
        assert not is_legal_transition(state, S.FILLING)
        assert is_terminal(state)


@pytest.mark.parametrize("state", sorted(DANGER_ZONE, key=lambda s: s.value))
def test_danger_zone_is_never_auto_resumable(state):
    assert not is_safe_to_auto_resume(state)


@pytest.mark.parametrize(
    "state",
    [S.DISCOVERED, S.ANALYZING, S.OPENING_APPLICATION, S.INSPECTING_FORM,
     S.FILLING, S.VERIFYING_FIELDS, S.READY_TO_SUBMIT],
)
def test_pre_submit_states_are_auto_resumable(state):
    assert is_safe_to_auto_resume(state)


def test_parked_and_terminal_states_are_not_auto_resumable():
    for state in (S.HUMAN_REVIEW, S.BLOCKED, S.UNKNOWN, S.SUBMITTED, S.FAILED, S.SKIPPED):
        assert not is_safe_to_auto_resume(state)


def test_unprovable_outcomes_still_consume_the_job():
    """An attempt we cannot prove failed must block re-application."""
    for state in (S.SUBMITTING, S.VERIFYING_SUBMISSION, S.UNKNOWN, S.HUMAN_REVIEW, S.BLOCKED):
        assert state in CONSUMES_JOB


def test_verified_not_submitted_may_recover():
    """The one safe route back to retrying: verification proved nothing
    was sent."""
    assert is_legal_transition(S.VERIFYING_SUBMISSION, S.RECOVERING)
    assert is_legal_transition(S.RECOVERING, S.READY_TO_SUBMIT)


def test_blocked_is_never_retried_automatically():
    policy = policy_for(FailureCategory.BLOCKED)
    assert policy.allow_retry is False
    assert policy.requires_human is True
    assert not should_retry(FailureCategory.BLOCKED, 0)


def test_ambiguous_is_never_retried_automatically():
    """AMBIGUOUS means 'we cannot tell if it was submitted'. Retrying that
    is precisely how a duplicate application gets made."""
    assert not should_retry(FailureCategory.AMBIGUOUS, 0)
    assert policy_for(FailureCategory.AMBIGUOUS).terminal_state is S.HUMAN_REVIEW


def test_transient_retries_are_bounded():
    policy = policy_for(FailureCategory.TRANSIENT)
    assert should_retry(FailureCategory.TRANSIENT, 0)
    assert should_retry(FailureCategory.TRANSIENT, policy.max_retries - 1)
    assert not should_retry(FailureCategory.TRANSIENT, policy.max_retries)


def test_fatal_stops_the_whole_run():
    assert policy_for(FailureCategory.FATAL).stops_run is True
    assert policy_for(FailureCategory.TRANSIENT).stops_run is False


def test_every_state_has_a_transition_entry():
    from jobbot.agent.states import LEGAL_TRANSITIONS

    for state in ApplicationState:
        assert state in LEGAL_TRANSITIONS, f"{state} missing from transition table"


def test_every_failure_category_has_a_policy():
    for category in FailureCategory:
        assert policy_for(category) is not None

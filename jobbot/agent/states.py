"""Explicit application state machine and failure taxonomy.

Every state an application attempt can be in is named here, every legal
transition is declared here, and both are persisted (see
jobbot/agent/statestore.py). The point is crash-safety: after an
unexpected process death, the DB alone must be enough to decide whether
resuming is safe — which is impossible with a single mutable status string.

The single most important invariant in this module:

    Once an attempt reaches SUBMITTING, it can never be automatically
    retried. The employer may already have the application, and no amount
    of local state can prove otherwise. Such an attempt may only move
    forward to a verified verdict, or sideways to HUMAN_REVIEW.

That invariant is what closes the crash-window duplicate-submission hole
(a crash between the submit click and the DB write previously left the row
looking un-submitted, so the next run re-applied).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ApplicationState(str, Enum):
    # -- pre-browser, cheap to redo --------------------------------------
    DISCOVERED = "DISCOVERED"
    ANALYZING = "ANALYZING"
    ELIGIBILITY_CHECK = "ELIGIBILITY_CHECK"
    SELECTING_RESUME = "SELECTING_RESUME"

    # -- browser open, still safe to redo --------------------------------
    OPENING_APPLICATION = "OPENING_APPLICATION"
    INSPECTING_FORM = "INSPECTING_FORM"
    FILLING = "FILLING"
    VERIFYING_FIELDS = "VERIFYING_FIELDS"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"

    # -- danger zone: side effects may have reached the employer ---------
    SUBMITTING = "SUBMITTING"
    VERIFYING_SUBMISSION = "VERIFYING_SUBMISSION"

    # -- terminal --------------------------------------------------------
    SUBMITTED = "SUBMITTED"
    SKIPPED = "SKIPPED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"

    # -- parked: resumable, needs something before it can continue -------
    BLOCKED = "BLOCKED"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    RECOVERING = "RECOVERING"
    UNKNOWN = "UNKNOWN"


#: States from which a submit may already have reached the employer. An
#: attempt found parked in one of these after a crash must never be
#: silently re-driven; see `is_safe_to_auto_resume`.
DANGER_ZONE: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.SUBMITTING,
        ApplicationState.VERIFYING_SUBMISSION,
        ApplicationState.UNKNOWN,
    }
)

#: No further work will happen without a human explicitly asking for it.
TERMINAL: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.SUBMITTED,
        ApplicationState.SKIPPED,
        ApplicationState.FAILED,
        ApplicationState.COMPLETED,
    }
)

#: Parked but resumable — work stopped deliberately, not by failure.
PARKED: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.BLOCKED,
        ApplicationState.HUMAN_REVIEW,
        ApplicationState.UNKNOWN,
    }
)

#: An attempt in one of these states counts as "this job has been dealt
#: with" for the purposes of not re-attempting it in a later run. Note
#: this deliberately includes the danger zone and UNKNOWN: an attempt we
#: cannot prove failed must be treated as possibly-submitted.
CONSUMES_JOB: frozenset[ApplicationState] = frozenset(
    {
        ApplicationState.SUBMITTED,
        ApplicationState.SKIPPED,
        ApplicationState.COMPLETED,
        ApplicationState.SUBMITTING,
        ApplicationState.VERIFYING_SUBMISSION,
        ApplicationState.UNKNOWN,
        ApplicationState.HUMAN_REVIEW,
        ApplicationState.BLOCKED,
    }
)

_ANY_FAILURE_EXIT = {
    ApplicationState.RECOVERING,
    ApplicationState.HUMAN_REVIEW,
    ApplicationState.BLOCKED,
    ApplicationState.FAILED,
    ApplicationState.SKIPPED,
}

#: Legal forward transitions. Anything not listed is rejected by
#: `assert_transition`, so an impossible jump (e.g. FILLING -> SUBMITTED)
#: fails loudly instead of corrupting the ledger.
LEGAL_TRANSITIONS: dict[ApplicationState, frozenset[ApplicationState]] = {
    ApplicationState.DISCOVERED: frozenset({ApplicationState.ANALYZING} | _ANY_FAILURE_EXIT),
    ApplicationState.ANALYZING: frozenset({ApplicationState.ELIGIBILITY_CHECK} | _ANY_FAILURE_EXIT),
    ApplicationState.ELIGIBILITY_CHECK: frozenset({ApplicationState.SELECTING_RESUME} | _ANY_FAILURE_EXIT),
    ApplicationState.SELECTING_RESUME: frozenset({ApplicationState.OPENING_APPLICATION} | _ANY_FAILURE_EXIT),
    ApplicationState.OPENING_APPLICATION: frozenset({ApplicationState.INSPECTING_FORM} | _ANY_FAILURE_EXIT),
    ApplicationState.INSPECTING_FORM: frozenset({ApplicationState.FILLING} | _ANY_FAILURE_EXIT),
    ApplicationState.FILLING: frozenset({ApplicationState.VERIFYING_FIELDS} | _ANY_FAILURE_EXIT),
    ApplicationState.VERIFYING_FIELDS: frozenset({ApplicationState.READY_TO_SUBMIT} | _ANY_FAILURE_EXIT),
    ApplicationState.READY_TO_SUBMIT: frozenset({ApplicationState.SUBMITTING} | _ANY_FAILURE_EXIT),
    # From SUBMITTING the only way out is to find out what happened.
    ApplicationState.SUBMITTING: frozenset(
        {
            ApplicationState.VERIFYING_SUBMISSION,
            ApplicationState.UNKNOWN,
            ApplicationState.HUMAN_REVIEW,
        }
    ),
    ApplicationState.VERIFYING_SUBMISSION: frozenset(
        {
            ApplicationState.SUBMITTED,
            ApplicationState.UNKNOWN,
            ApplicationState.HUMAN_REVIEW,
            ApplicationState.BLOCKED,
            ApplicationState.FAILED,
            # A verified NOT_SUBMITTED is the one safe way back to retrying.
            ApplicationState.RECOVERING,
        }
    ),
    ApplicationState.RECOVERING: frozenset(
        {
            ApplicationState.OPENING_APPLICATION,
            ApplicationState.INSPECTING_FORM,
            ApplicationState.FILLING,
            ApplicationState.VERIFYING_FIELDS,
            ApplicationState.READY_TO_SUBMIT,
            ApplicationState.HUMAN_REVIEW,
            ApplicationState.BLOCKED,
            ApplicationState.FAILED,
            ApplicationState.SKIPPED,
        }
    ),
    # Parked states resume only on explicit human action.
    ApplicationState.HUMAN_REVIEW: frozenset(
        {
            ApplicationState.READY_TO_SUBMIT,
            ApplicationState.SUBMITTING,
            ApplicationState.SUBMITTED,
            ApplicationState.SKIPPED,
            ApplicationState.FAILED,
            ApplicationState.RECOVERING,
        }
    ),
    ApplicationState.BLOCKED: frozenset(
        {
            ApplicationState.RECOVERING,
            ApplicationState.HUMAN_REVIEW,
            ApplicationState.SKIPPED,
            ApplicationState.FAILED,
        }
    ),
    ApplicationState.UNKNOWN: frozenset(
        {
            ApplicationState.HUMAN_REVIEW,
            ApplicationState.SUBMITTED,
            ApplicationState.FAILED,
            ApplicationState.SKIPPED,
        }
    ),
    # Terminal.
    ApplicationState.SUBMITTED: frozenset(),
    ApplicationState.SKIPPED: frozenset(),
    ApplicationState.FAILED: frozenset(),
    ApplicationState.COMPLETED: frozenset(),
}


class IllegalTransition(RuntimeError):
    """Raised when code attempts a state change the machine forbids."""


def is_legal_transition(src: ApplicationState, dst: ApplicationState) -> bool:
    return dst in LEGAL_TRANSITIONS.get(src, frozenset())


def assert_transition(src: ApplicationState, dst: ApplicationState) -> None:
    if not is_legal_transition(src, dst):
        raise IllegalTransition(
            f"{src.value} -> {dst.value} is not a legal transition. "
            f"Legal from {src.value}: {sorted(s.value for s in LEGAL_TRANSITIONS.get(src, frozenset()))}"
        )


def is_terminal(state: ApplicationState) -> bool:
    return state in TERMINAL


def is_safe_to_auto_resume(state: ApplicationState) -> bool:
    """Whether an attempt found in this state (e.g. after a crash, or with
    an expired lease) may be picked back up without a human looking at it.

    False for everything at or past SUBMITTING: we cannot distinguish "the
    click never landed" from "the click landed and we died before writing
    it down", and guessing wrong means applying twice.
    """
    if state in TERMINAL or state in DANGER_ZONE or state in PARKED:
        return False
    return True


# ---------------------------------------------------------------------------
# Failure taxonomy
# ---------------------------------------------------------------------------


class FailureCategory(str, Enum):
    #: Network blip, timeout, 5xx, transient LLM error. Retry as-is.
    TRANSIENT = "TRANSIENT"
    #: Something about our approach was wrong but the job is still viable
    #: (selector drift, unexpected modal). Retry with a different strategy.
    RECOVERABLE = "RECOVERABLE"
    #: CAPTCHA, MFA, login wall, bot detection. Never bypass; escalate.
    BLOCKED = "BLOCKED"
    #: A rule of ours forbids continuing (ineligible, sensitive field
    #: unconfirmed, budget exhausted). Not an error — a decision.
    POLICY = "POLICY"
    #: We genuinely cannot tell what happened (submission verdict UNKNOWN).
    AMBIGUOUS = "AMBIGUOUS"
    #: Unrecoverable and not worth retrying (corrupt profile, bad config).
    FATAL = "FATAL"


@dataclass(frozen=True)
class RetryPolicy:
    allow_retry: bool
    max_retries: int
    requires_human: bool
    requires_checkpoint: bool
    stops_run: bool
    #: State an attempt should be parked in when retries are exhausted (or
    #: immediately, when allow_retry is False).
    terminal_state: ApplicationState


RETRY_POLICIES: dict[FailureCategory, RetryPolicy] = {
    FailureCategory.TRANSIENT: RetryPolicy(
        allow_retry=True, max_retries=3, requires_human=False,
        requires_checkpoint=True, stops_run=False,
        terminal_state=ApplicationState.FAILED,
    ),
    FailureCategory.RECOVERABLE: RetryPolicy(
        allow_retry=True, max_retries=2, requires_human=False,
        requires_checkpoint=True, stops_run=False,
        terminal_state=ApplicationState.HUMAN_REVIEW,
    ),
    FailureCategory.BLOCKED: RetryPolicy(
        # Retrying a CAPTCHA/bot wall is exactly the behaviour that gets
        # accounts flagged. Park it and move on to the next job.
        allow_retry=False, max_retries=0, requires_human=True,
        requires_checkpoint=True, stops_run=False,
        terminal_state=ApplicationState.BLOCKED,
    ),
    FailureCategory.POLICY: RetryPolicy(
        allow_retry=False, max_retries=0, requires_human=False,
        requires_checkpoint=False, stops_run=False,
        terminal_state=ApplicationState.SKIPPED,
    ),
    FailureCategory.AMBIGUOUS: RetryPolicy(
        # The whole point: never retry something we cannot prove did not
        # already happen.
        allow_retry=False, max_retries=0, requires_human=True,
        requires_checkpoint=True, stops_run=False,
        terminal_state=ApplicationState.HUMAN_REVIEW,
    ),
    FailureCategory.FATAL: RetryPolicy(
        allow_retry=False, max_retries=0, requires_human=True,
        requires_checkpoint=True, stops_run=True,
        terminal_state=ApplicationState.FAILED,
    ),
}


def policy_for(category: FailureCategory) -> RetryPolicy:
    return RETRY_POLICIES[category]


def should_retry(category: FailureCategory, attempts_so_far: int) -> bool:
    policy = policy_for(category)
    return policy.allow_retry and attempts_so_far < policy.max_retries

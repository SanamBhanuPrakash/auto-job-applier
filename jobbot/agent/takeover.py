"""Agent takeover and hand-back — §16, §17, §18.

The shape the spec asks for:

    deterministic automation
      -> uncertainty or failure
      -> AGENT TAKEOVER
      -> observe / reason / act / verify
      -> reach a known state
      -> HAND BACK
      -> deterministic automation resumes

Two things make this more than "run the agent when we crash".

**Takeover happens before the failure, not after it.** `detect_trigger()`
reads the deterministic path's own facts — no form found, several frames
and none of them holding a form, an unexpected page state, a popup — and
names the trigger while the attempt is still recoverable. Waiting for an
exception means taking over with less information and fewer options.

**Hand-back is conditional on the environment, not on the agent's
opinion.** `handoff.resolved` is only true when the caller's own
`goal_reached` predicate says so, evaluated against a fresh observation.
That is the same rule as submission verification: the agent's claim is
never the evidence (docs/research/browser-agent-failures.md §3).

The deterministic caller must re-ground after a successful hand-back. The
agent may have navigated, switched frames or switched tabs, so every
element handle the caller held before takeover is presumed stale
(failures §4). `AgentHandoffResult.next_action` says so explicitly.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from jobbot.agent.controller import AgentController, AgentRun, Decider, Outcome
from jobbot.agent.observation import BrowserObservation, Detail
from jobbot.agent.page_classify import AUTH_STATES, Classification, PageState, classify
from jobbot.agent.policy import PolicyContext
from jobbot.agent.recovery import RecoveryTrigger
from jobbot.agent.states import ApplicationState, FailureCategory
from jobbot.agent.tools import ToolContext, ToolRegistry
from jobbot.agent.trajectory import Budget

log = logging.getLogger(__name__)


class NextAction(str, Enum):
    """What the deterministic caller should do with this handoff."""

    #: Resume the deterministic flow — but re-scan first: handles are stale.
    RESUME_AFTER_REGROUND = "RESUME_AFTER_REGROUND"
    RETRY_LATER = "RETRY_LATER"
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    ABANDON = "ABANDON"


#: How each agent outcome maps to (what the caller does, which failure
#: category it reports as). Kept as data so the mapping is inspectable and
#: cannot drift between call sites.
_OUTCOME_MAP: dict[Outcome, tuple[NextAction, FailureCategory | None]] = {
    Outcome.RESOLVED: (NextAction.RESUME_AFTER_REGROUND, None),
    Outcome.RECOVERABLE: (NextAction.RETRY_LATER, FailureCategory.RECOVERABLE),
    Outcome.HUMAN_REQUIRED: (NextAction.ESCALATE_HUMAN, FailureCategory.RECOVERABLE),
    Outcome.BLOCKED: (NextAction.ESCALATE_HUMAN, FailureCategory.BLOCKED),
    Outcome.FATAL: (NextAction.ABANDON, FailureCategory.FATAL),
}


@dataclass
class AgentHandoffResult:
    """§18. Everything the deterministic caller needs to decide what next."""

    outcome: Outcome
    reason: str
    #: Page state the agent left the browser in.
    new_state: PageState = PageState.UNKNOWN
    evidence: list[str] = field(default_factory=list)
    next_action: NextAction = NextAction.ESCALATE_HUMAN
    #: Durable snapshot to persist before acting on this result (§56).
    checkpoint: dict = field(default_factory=dict)
    failure_category: FailureCategory | None = None
    trigger: RecoveryTrigger | None = None
    run: AgentRun | None = None

    @property
    def resolved(self) -> bool:
        return self.outcome is Outcome.RESOLVED

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome.value,
            "reason": self.reason[:300],
            "new_state": self.new_state.value,
            "next_action": self.next_action.value,
            "evidence": self.evidence[:5],
            "trigger": self.trigger.value if self.trigger else None,
            "failure_category": self.failure_category.value if self.failure_category else None,
            "checkpoint": self.checkpoint,
        }


def detect_trigger(
    observation: BrowserObservation,
    classification: Classification,
    *,
    fields_found: int | None = None,
    expected_form: bool = True,
) -> RecoveryTrigger | None:
    """Name the reason to take over, or None when the page looks ordinary.

    Ordered most-specific first: an auth wall is a different problem from
    "no form here", and mislabelling it sends recovery down the wrong
    ladder.
    """
    if classification.state in AUTH_STATES:
        return RecoveryTrigger.SESSION_EXPIRED
    if observation.tab_count > 1:
        return RecoveryTrigger.POPUP
    if classification.state is PageState.ERROR:
        return RecoveryTrigger.NETWORK_ERROR
    if expected_form and fields_found == 0:
        # The form is either elsewhere (frame), behind a click, or the page
        # is not what we thought. Frame first: it is the common case.
        if observation.frame_count > 1 and not observation.has_form:
            return RecoveryTrigger.FRAME_CHANGED
        return RecoveryTrigger.TARGET_NOT_FOUND
    if classification.state is PageState.UNKNOWN and classification.confidence < 0.4:
        return RecoveryTrigger.PAGE_STATE_UNKNOWN
    if observation.validation_messages:
        return RecoveryTrigger.VALIDATION_ERROR
    return None


def take_over(
    goal: str,
    tool_ctx: ToolContext,
    policy: PolicyContext,
    decider: Decider,
    *,
    goal_reached,
    trigger: RecoveryTrigger | None = None,
    registry: ToolRegistry | None = None,
    budget: Budget | None = None,
    observe_detail: Detail = Detail.CONTROLS,
) -> AgentHandoffResult:
    """Run one bounded agent episode and translate it into a handoff.

    `goal_reached` is required, deliberately. Without it the controller
    would accept the decider's own "finished" as success, which is the
    failure mode this project refuses everywhere else.
    """
    if goal_reached is None:
        raise ValueError(
            "take_over() requires goal_reached: hand-back must be decided by the "
            "environment, not by the agent's claim to have succeeded"
        )

    if tool_ctx.application_state in (ApplicationState.SUBMITTING,
                                      ApplicationState.VERIFYING_SUBMISSION):
        # Nothing the agent could do here is worth the risk of a second
        # submission. Same rule as the recovery engine's danger zone.
        return AgentHandoffResult(
            Outcome.HUMAN_REQUIRED,
            reason=(
                f"refusing takeover while the application is "
                f"{tool_ctx.application_state.value}: the submission may already have "
                "reached the employer"
            ),
            next_action=NextAction.ESCALATE_HUMAN,
            failure_category=FailureCategory.AMBIGUOUS,
            trigger=trigger,
        )

    controller = AgentController(
        registry or ToolRegistry(), decider,
        budget=budget or Budget(), observe_detail=observe_detail,
    )
    log.info("Agent takeover: goal=%r trigger=%s", goal, trigger.value if trigger else "-")
    run = controller.run(goal, tool_ctx, policy, goal_reached=goal_reached)

    next_action, category = _OUTCOME_MAP[run.outcome]
    evidence = [
        f"{s.decision.get('action')}: {s.decision.get('reason', '')}"
        for s in run.steps[-3:]
    ]
    return AgentHandoffResult(
        outcome=run.outcome,
        reason=run.reason,
        new_state=run.final_state,
        evidence=evidence,
        next_action=next_action,
        checkpoint={
            "goal": goal,
            "trigger": trigger.value if trigger else None,
            "url": tool_ctx.page.url if _safe_url(tool_ctx) else "",
            "agent_run": run.summary(),
        },
        failure_category=category,
        trigger=trigger,
        run=run,
    )


def _safe_url(tool_ctx: ToolContext) -> bool:
    try:
        return bool(tool_ctx.page and tool_ctx.page.url)
    except Exception:  # noqa: BLE001 - a closed page must not break the handoff
        return False


def form_is_present(observation: BrowserObservation, classification: Classification) -> bool:
    """The default hand-back condition: an application form is on screen.

    Used as `goal_reached` for the "get to the form" takeover. Requires
    actual input controls, not merely a `<form>` element — plenty of pages
    carry a search form and no application.
    """
    if classification.state is PageState.APPLICATION_FORM:
        return True
    inputs = [
        c for c in observation.controls
        if c.role in ("textbox", "combobox", "checkbox", "radio", "select", "file")
    ]
    return observation.has_form and len(inputs) >= 3


def observed_goal_reached(tool_ctx: ToolContext, predicate) -> bool:
    """Evaluate a goal predicate against a *fresh* observation.

    For callers that want to confirm hand-back independently of the run
    that produced it.
    """
    from jobbot.agent.observation import observe

    obs = observe(tool_ctx.page, tool_ctx.form_ctx, detail=Detail.CONTROLS)
    return predicate(obs, classify(obs))

"""AgentController — the bounded OBSERVE→DECIDE→ACT→VERIFY→CHECKPOINT loop.

Spec §7 and §8. What this deliberately is *not*:

    while True:
        ask the LLM what to do
        exec(whatever it said)

What it is:

    OBSERVE      re-read the live page (tiered, bounded)
    BUILD CONTEXT  smallest high-signal payload for this decision
    DECIDE       a Decider returns one structured Decision
    AUTHORIZE    deterministic policy gate; may deny
    ACT          registry executes one registered tool
    OBSERVE      registry re-reads the page independently
    VERIFY       did the expected transition actually happen?
    CHECKPOINT   persist, then continue / recover / escalate / block

The `Decider` is an interface, not an LLM. That is a design choice with two
payoffs: the entire loop is testable without a network call, and an LLM
decider is just one implementation that has no privileged access — it
cannot skip authorization or verification, because those are the
controller's steps, not the decider's.

Bounds come from Trajectory (steps, wall time, LLM calls, tokens, recovery
attempts) plus loop detection. Detection is best-effort; budgets are not.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from jobbot.agent.observation import BrowserObservation, Detail, observe
from jobbot.agent.page_classify import Classification, PageState, classify, requires_stop
from jobbot.agent.policy import PolicyContext, PolicyDecision, authorize
from jobbot.agent.states import ApplicationState, FailureCategory
from jobbot.agent.tools import RiskClass, ToolContext, ToolRegistry, ToolResult
from jobbot.agent.trajectory import Budget, StopReason, Trajectory

log = logging.getLogger(__name__)


class Outcome(str, Enum):
    """§18 AgentHandoffResult, plus the loop's own terminal reasons."""

    RESOLVED = "RESOLVED"            # goal reached; hand back to deterministic flow
    RECOVERABLE = "RECOVERABLE"      # stopped, but retrying later is reasonable
    HUMAN_REQUIRED = "HUMAN_REQUIRED"
    BLOCKED = "BLOCKED"
    FATAL = "FATAL"


@dataclass
class Decision:
    """§12. Structured, auditable, and free of hidden chain-of-thought —
    concise operational rationale only."""

    action: str                       # a registered tool name, or "finish"
    args: dict = field(default_factory=dict)
    objective: str = ""
    reason: str = ""
    expected_outcome: str = ""
    fallback: str = ""
    confidence: float = 0.5
    evidence_refs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "args": self.args,
            "objective": self.objective,
            "reason": self.reason[:300],
            "expected_outcome": self.expected_outcome[:200],
            "confidence": round(self.confidence, 2),
        }


@dataclass
class DecisionContext:
    """The smallest high-signal payload a decider needs (§24 context
    engineering). Note what is absent: the full DOM, every past
    observation, and the entire trajectory."""

    goal: str
    observation: BrowserObservation
    classification: Classification
    application_state: ApplicationState
    available_tools: list[dict]
    recent_actions: list[dict] = field(default_factory=list)
    forbidden_actions: list[str] = field(default_factory=list)
    known_failures: list[str] = field(default_factory=list)
    step_index: int = 0
    steps_remaining: int = 0

    def to_prompt_payload(self) -> dict:
        """Untrusted page content stays confined to `observation`, which
        the prompt layer fences as data (§39). Everything else here is
        our own typed state."""
        return {
            "goal": self.goal,
            "application_state": self.application_state.value,
            "page": self.classification.to_dict(),
            "observation": self.observation.to_agent_dict(),
            "available_tools": self.available_tools,
            "recent_actions": self.recent_actions[-5:],
            "do_not_repeat": self.forbidden_actions,
            "known_failures": self.known_failures[-5:],
            "steps_remaining": self.steps_remaining,
        }


class Decider(Protocol):
    """Anything that can choose the next action. An LLM is one
    implementation; a scripted plan is another."""

    def decide(self, ctx: DecisionContext) -> Decision: ...


@dataclass
class StepRecord:
    """§59 per-step trace row."""

    index: int
    page_state: str
    decision: dict
    authorized: bool
    denial_reason: str = ""
    tool_result: dict | None = None
    verified: bool = False
    note: str = ""


@dataclass
class AgentRun:
    outcome: Outcome
    reason: str
    steps: list[StepRecord] = field(default_factory=list)
    trajectory: Trajectory | None = None
    final_state: PageState = PageState.UNKNOWN
    stop_reason: StopReason | None = None

    def summary(self) -> dict:
        # Order matters: `Trajectory.summary()` also has a "steps" key,
        # counting only steps that reached execution. Spreading it last
        # overwrote the controller's own count, so a run that iterated 40
        # times and was denied every time reported "steps: 0" — the
        # observability layer hiding exactly the runs worth looking at.
        # Both numbers are kept, under names that say what they mean.
        traj = self.trajectory.summary() if self.trajectory else {}
        traj.pop("steps", None)
        return {
            "outcome": self.outcome.value,
            "reason": self.reason,
            "final_page_state": self.final_state.value,
            "stop_reason": self.stop_reason.value if self.stop_reason else None,
            "steps": len(self.steps),
            "executed_steps": len(self.trajectory.steps) if self.trajectory else 0,
            "denied_steps": sum(1 for s in self.steps if not s.authorized),
            **traj,
        }


class AgentController:
    """Drives one bounded agent episode over an already-open browser."""

    def __init__(
        self,
        registry: ToolRegistry,
        decider: Decider,
        *,
        budget: Budget | None = None,
        observe_detail: Detail = Detail.ARIA,
    ):
        self.registry = registry
        self.decider = decider
        self.budget = budget or Budget()
        self.observe_detail = observe_detail

    def run(
        self,
        goal: str,
        tool_ctx: ToolContext,
        policy: PolicyContext,
        *,
        goal_reached: "callable[[BrowserObservation, Classification], bool] | None" = None,
    ) -> AgentRun:
        """Execute the loop until the goal is reached or a bound stops it.

        `goal_reached` lets the caller define success declaratively (e.g.
        "an application form is on screen"), so the agent cannot declare
        victory by asserting it — success is evaluated against the
        environment, same principle as submission verification.
        """
        traj = Trajectory(budget=self.budget)
        steps: list[StepRecord] = []
        known_failures: list[str] = []

        while True:
            # The step budget counts *iterations*, not just actions that
            # made it past authorization. Counting only executed steps left
            # the loop unbounded whenever the decider kept proposing
            # something policy denied: those iterations `continue` without
            # ever calling `traj.begin_step`, so the ceiling was never
            # reached. Found by test_agent_takeover.py; §133 forbids an
            # unbounded loop, so this is a hard count in the controller
            # rather than a property of what the decider happens to pick.
            # See browser-agent-failures.md §16.
            if len(steps) >= self.budget.max_steps:
                return self._stopped(traj, steps, StopReason.STEP_BUDGET)
            stop = traj.should_stop()
            if stop is not None:
                return self._stopped(traj, steps, stop)

            # -- OBSERVE ------------------------------------------------
            obs = observe(tool_ctx.page, tool_ctx.form_ctx, detail=self.observe_detail)
            cls = classify(obs)
            policy = PolicyContext(
                application_state=policy.application_state,
                page_state=cls.state,
                autonomy=policy.autonomy,
                sensitive_autofill_confirmed=policy.sensitive_autofill_confirmed,
                submission_ready=policy.submission_ready,
            )

            # A wall ends the episode; we never act past it (§31).
            if requires_stop(cls.state):
                return AgentRun(
                    Outcome.BLOCKED,
                    f"page classified {cls.state.value}: {'; '.join(cls.evidence)}",
                    steps, traj, cls.state,
                )

            if goal_reached is not None and goal_reached(obs, cls):
                return AgentRun(Outcome.RESOLVED, "goal reached", steps, traj, cls.state)

            # -- BUILD CONTEXT ------------------------------------------
            dctx = DecisionContext(
                goal=goal,
                observation=obs,
                classification=cls,
                application_state=policy.application_state,
                available_tools=self.registry.describe_all(),
                recent_actions=[s.decision for s in steps[-5:]],
                forbidden_actions=sorted(traj.forbidden_actions()),
                known_failures=known_failures,
                step_index=len(steps),
                steps_remaining=max(0, self.budget.max_steps - len(steps)),
            )

            # -- DECIDE -------------------------------------------------
            try:
                decision = self.decider.decide(dctx)
            except Exception as exc:  # noqa: BLE001 - a broken decider must not hang the browser
                log.exception("Decider raised")
                return AgentRun(
                    Outcome.RECOVERABLE, f"decider failed: {type(exc).__name__}: {exc}",
                    steps, traj, cls.state,
                )
            traj.record_llm_call(getattr(decision, "tokens_used", 0) or 0)

            record = StepRecord(
                index=len(steps), page_state=cls.state.value,
                decision=decision.to_dict(), authorized=False,
            )

            if decision.action == "finish":
                steps.append(record)
                record.authorized = True
                record.note = "decider signalled completion"
                # The decider saying "done" is a claim, not evidence. If the
                # caller supplied a goal test, that test is authoritative.
                if goal_reached is not None and not goal_reached(obs, cls):
                    return AgentRun(
                        Outcome.RECOVERABLE,
                        "decider finished but the goal condition is not satisfied",
                        steps, traj, cls.state,
                    )
                return AgentRun(Outcome.RESOLVED, decision.reason or "finished", steps, traj, cls.state)

            spec = self.registry.get(decision.action)
            if spec is None:
                record.denial_reason = f"unknown tool {decision.action!r}"
                known_failures.append(record.denial_reason)
                steps.append(record)
                continue  # bounded by the step budget; not an infinite retry

            # -- AUTHORIZE ----------------------------------------------
            verdict: PolicyDecision = authorize(
                spec, decision.args, policy, tool_ctx=tool_ctx, observation=obs,
            )
            if not verdict.allowed:
                record.denial_reason = verdict.reason
                known_failures.append(f"{decision.action}: {verdict.reason}")
                steps.append(record)
                log.info("Policy denied %s: %s", decision.action, verdict.reason)
                if verdict.requires_human:
                    return AgentRun(
                        Outcome.HUMAN_REQUIRED, verdict.reason, steps, traj, cls.state,
                    )
                continue
            record.authorized = True

            # -- ACT (registry re-observes internally) -------------------
            step = traj.begin_step(
                state_digest=obs.state_digest(), action=decision.action, page_state=cls.state.value,
            )
            result: ToolResult = self.registry.execute(decision.action, tool_ctx, **decision.args)
            traj.complete_step(
                step, result_digest=result.state_after or obs.state_digest(), ok=result.ok,
                note="; ".join(result.evidence)[:200],
            )
            record.tool_result = result.to_agent_dict()

            # -- VERIFY -------------------------------------------------
            record.verified = self._verify(spec.risk_class, result)
            if not result.ok:
                known_failures.append(f"{decision.action}: {'; '.join(result.evidence)[:120]}")
                traj.record_recovery()
                if result.failure_category is FailureCategory.BLOCKED:
                    steps.append(record)
                    return AgentRun(Outcome.BLOCKED, "; ".join(result.evidence), steps, traj, cls.state)
                if not result.recoverable:
                    steps.append(record)
                    return AgentRun(Outcome.FATAL, "; ".join(result.evidence), steps, traj, cls.state)
            elif not result.changed_state and spec.risk_class >= RiskClass.MEDIUM_RISK:
                known_failures.append(f"{decision.action}: completed but changed nothing")

            steps.append(record)

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _verify(risk: RiskClass, result: ToolResult) -> bool:
        """§107. A consequential action counts as verified only when the
        environment actually moved; a read-only tool is verified by having
        produced an observation at all."""
        if risk <= RiskClass.LOW_RISK:
            return result.ok
        return result.ok and result.changed_state

    def _stopped(self, traj: Trajectory, steps: list[StepRecord], stop: StopReason) -> AgentRun:
        # A loop or an exhausted budget is not a failure of the site — it
        # means we could not make progress and must not keep trying.
        human_reasons = {StopReason.NO_PROGRESS, StopReason.CYCLE, StopReason.ACTION_REPEATED}
        outcome = Outcome.HUMAN_REQUIRED if stop in human_reasons else Outcome.RECOVERABLE
        run = AgentRun(outcome, f"stopped: {stop.value}", steps, traj)
        run.stop_reason = stop
        log.warning("Agent episode stopped: %s after %d step(s)", stop.value, len(steps))
        return run

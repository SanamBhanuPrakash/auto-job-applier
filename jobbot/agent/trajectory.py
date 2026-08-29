"""Trajectory recording, loop detection and hard budgets.

An LLM-driven loop with no bound is not an agent, it is an outage. The
failure shapes this guards against:

    no-op:    S1 --click--> S1 --click--> S1 ...
    cycle:    S1 --next--> S2 --next--> S1 --next--> S2 ...
    thrash:   many actions, state keeps changing, goal never nearer

The first two are detectable from state digests alone. The third is why
there are also unconditional budgets: detection is best-effort, budgets are
not. On a job application, an unbounded loop is worse than merely wasteful —
repeatedly clicking a submit control is how an agent files duplicates.

Trajectory entries deliberately store *digests*, not observations. Keeping
full observations here would recreate the context-rot problem the
observation layer exists to avoid (docs/research/agent-architecture.md §6);
the durable record belongs in the database.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum

log = logging.getLogger(__name__)


class StopReason(str, Enum):
    NO_PROGRESS = "NO_PROGRESS"          # same state after the same action
    CYCLE = "CYCLE"                      # A -> B -> A -> B
    ACTION_REPEATED = "ACTION_REPEATED"  # same action too many times overall
    STEP_BUDGET = "STEP_BUDGET"
    TIME_BUDGET = "TIME_BUDGET"
    LLM_CALL_BUDGET = "LLM_CALL_BUDGET"
    TOKEN_BUDGET = "TOKEN_BUDGET"
    RECOVERY_BUDGET = "RECOVERY_BUDGET"


@dataclass(frozen=True)
class Budget:
    """Hard bounds for one application attempt. Every field is a ceiling
    the agent cannot talk its way past."""

    max_steps: int = 60
    max_recovery_attempts: int = 6
    max_seconds: float = 900.0            # 15 min per application
    max_llm_calls: int = 40
    max_tokens: int = 200_000
    #: Same (state, action) pair seen this many times without the state
    #: changing => no progress.
    max_repeat_same_state_action: int = 2
    #: Same action attempted this many times in the whole attempt,
    #: regardless of state.
    max_same_action_total: int = 8
    #: Length of the A->B->A->B pattern to treat as a cycle.
    cycle_detection_window: int = 6


@dataclass
class Step:
    index: int
    state_digest: str
    action: str
    #: Digest observed *after* the action; empty until recorded.
    result_digest: str = ""
    page_state: str = ""
    ok: bool = True
    note: str = ""
    started_at: float = field(default_factory=time.monotonic)

    @property
    def made_progress(self) -> bool:
        """Whether the world actually changed. An action that leaves the
        page byte-identical did nothing, whatever it reported."""
        return bool(self.result_digest) and self.result_digest != self.state_digest


@dataclass
class Trajectory:
    """Bounded execution record for one application attempt."""

    budget: Budget = field(default_factory=Budget)
    steps: list[Step] = field(default_factory=list)
    llm_calls: int = 0
    tokens_used: int = 0
    recovery_attempts: int = 0
    started_at: float = field(default_factory=time.monotonic)

    # -- recording ------------------------------------------------------

    def begin_step(self, *, state_digest: str, action: str, page_state: str = "") -> Step:
        step = Step(
            index=len(self.steps),
            state_digest=state_digest,
            action=action,
            page_state=page_state,
        )
        self.steps.append(step)
        return step

    def complete_step(self, step: Step, *, result_digest: str, ok: bool = True, note: str = "") -> None:
        step.result_digest = result_digest
        step.ok = ok
        step.note = note

    def record_llm_call(self, tokens: int = 0) -> None:
        self.llm_calls += 1
        self.tokens_used += max(0, tokens)

    def record_recovery(self) -> None:
        self.recovery_attempts += 1

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    # -- budgets --------------------------------------------------------

    def budget_exceeded(self) -> StopReason | None:
        b = self.budget
        if len(self.steps) >= b.max_steps:
            return StopReason.STEP_BUDGET
        if self.elapsed >= b.max_seconds:
            return StopReason.TIME_BUDGET
        if self.llm_calls >= b.max_llm_calls:
            return StopReason.LLM_CALL_BUDGET
        if self.tokens_used >= b.max_tokens:
            return StopReason.TOKEN_BUDGET
        if self.recovery_attempts >= b.max_recovery_attempts:
            return StopReason.RECOVERY_BUDGET
        return None

    # -- loop detection -------------------------------------------------

    def detect_loop(self) -> StopReason | None:
        """Look for a non-progressing pattern in what has happened so far.

        Only completed steps count: a step still in flight has no result
        digest and says nothing about progress yet.
        """
        completed = [s for s in self.steps if s.result_digest]
        if not completed:
            return None

        b = self.budget

        # 1. Same action from the same state, repeatedly, changing nothing.
        pair_counts: dict[tuple[str, str], int] = {}
        for s in completed:
            if s.made_progress:
                continue
            key = (s.state_digest, s.action)
            pair_counts[key] = pair_counts.get(key, 0) + 1
            if pair_counts[key] >= b.max_repeat_same_state_action:
                log.warning(
                    "No progress: action %r from the same page state %d times with no change",
                    s.action, pair_counts[key],
                )
                return StopReason.NO_PROGRESS

        # 2. Same action attempted too often overall, even if the page
        #    churns (a spinner or an ad slot can make every state look new).
        action_counts: dict[str, int] = {}
        for s in completed:
            action_counts[s.action] = action_counts.get(s.action, 0) + 1
            if action_counts[s.action] >= b.max_same_action_total:
                log.warning("Action %r attempted %d times in one attempt", s.action, action_counts[s.action])
                return StopReason.ACTION_REPEATED

        # 3. A -> B -> A -> B oscillation over the recent window.
        window = completed[-b.cycle_detection_window:]
        if len(window) >= 4:
            digests = [s.result_digest for s in window]
            if _is_alternating(digests):
                log.warning("Cycle detected across %d steps: state alternates without progressing", len(window))
                return StopReason.CYCLE

        return None

    def should_stop(self) -> StopReason | None:
        """Single call the agent loop makes each iteration."""
        return self.budget_exceeded() or self.detect_loop()

    def forbidden_actions(self) -> set[str]:
        """Actions that have already been shown not to work from the
        current state, so the agent picks something else instead of being
        told 'no' repeatedly."""
        if not self.steps:
            return set()
        current = self.steps[-1].result_digest or self.steps[-1].state_digest
        return {
            s.action
            for s in self.steps
            if s.result_digest and not s.made_progress and s.state_digest == current
        }

    # -- reporting ------------------------------------------------------

    def summary(self) -> dict:
        completed = [s for s in self.steps if s.result_digest]
        return {
            "steps": len(self.steps),
            "progressing_steps": sum(1 for s in completed if s.made_progress),
            "llm_calls": self.llm_calls,
            "tokens_used": self.tokens_used,
            "recovery_attempts": self.recovery_attempts,
            "elapsed_seconds": round(self.elapsed, 1),
        }


def _is_alternating(digests: list[str]) -> bool:
    """True when the sequence oscillates between exactly two states, e.g.
    A B A B. Requires at least two full periods so a legitimate there-and-
    back-again (open a modal, close it, continue) isn't misread as a loop."""
    if len(digests) < 4:
        return False
    distinct = set(digests)
    if len(distinct) != 2:
        return False
    return all(digests[i] == digests[i % 2] for i in range(len(digests)))

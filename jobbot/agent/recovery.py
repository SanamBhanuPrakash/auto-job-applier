"""Recovery engine — a closed set of recovery primitives, and a
deterministic ladder for choosing between them.

Spec §19/§20. Before this module the failure taxonomy in `states.py` could
say *what kind* of failure had happened and whether a retry was permitted,
but nothing turned that into behaviour: every failure ended the attempt.

Two properties are the whole point.

**The action set is closed.** The model may *select* a recovery action; it
can never invent one. `RecoveryAction.parse()` returns None for anything
that is not one of the fifteen, so an LLM proposing "run_javascript" or
"disable_validation" gets nothing. This is the §133 rule that untrusted
text must not widen the agent's capabilities, applied to recovery.

**The ladder is deterministic.** Each trigger has a fixed escalation order
that starts with the cheapest, most reversible action and ends at a human.
`plan_recovery` is a pure function of (trigger, attempt, application
state), so recovery behaviour is reproducible and testable, and an agent
under pressure cannot skip the cheap rungs to reach the consequential
ones.

The single hardest rule in here:

    Inside the danger zone (SUBMITTING / VERIFYING_SUBMISSION) the only
    recovery actions that exist are REOBSERVE and ESCALATE_HUMAN.

A RELOAD or RETRY at that moment is how a duplicate application reaches an
employer: the click may already have landed, and no local state can prove
it did not (see docs/research/browser-agent-failures.md §2, §11). Recovery
is not allowed to be the thing that breaks the project's central
invariant.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from jobbot.agent.observation import BrowserObservation, Detail, observe
from jobbot.agent.states import DANGER_ZONE, ApplicationState, FailureCategory
from jobbot.agent.tools import ToolContext, ToolRegistry, ToolResult

log = logging.getLogger(__name__)


class RecoveryAction(str, Enum):
    """The complete set. Nothing outside this enum is a recovery action."""

    REOBSERVE = "REOBSERVE"                    # look again; the page may have settled
    REGROUND = "REGROUND"                      # re-resolve element handles (failures §4)
    REFRESH = "REFRESH"                        # let the page settle, then re-read
    RELOAD = "RELOAD"                          # full navigation reload
    BACKTRACK = "BACKTRACK"                    # go back one history entry
    SWITCH_FRAME = "SWITCH_FRAME"              # the form moved into/out of an iframe
    SWITCH_TAB = "SWITCH_TAB"                  # the flow continued in a popup
    REAUTHENTICATE = "REAUTHENTICATE"          # session expired
    REOPEN_APPLICATION = "REOPEN_APPLICATION"  # navigate back to the posting
    RETRY = "RETRY"                            # repeat the failed operation as-is
    CHANGE_TARGET = "CHANGE_TARGET"            # aim at a different control
    CHANGE_FILL_METHOD = "CHANGE_FILL_METHOD"  # type instead of select, etc.
    AGENT_TAKEOVER = "AGENT_TAKEOVER"          # hand the wheel to the agent loop
    ESCALATE_HUMAN = "ESCALATE_HUMAN"
    STOP = "STOP"

    @classmethod
    def parse(cls, value: str) -> "RecoveryAction | None":
        """Model-facing parser. Returns None rather than raising, and never
        invents a member — an unrecognized name is simply not an action."""
        try:
            return cls(str(value).strip().upper())
        except ValueError:
            return None


class RecoveryTrigger(str, Enum):
    """What went wrong. §17 takeover triggers plus the failure shapes the
    deterministic path already produces."""

    TARGET_NOT_FOUND = "TARGET_NOT_FOUND"
    AMBIGUOUS_TARGET = "AMBIGUOUS_TARGET"
    STALE_ELEMENT = "STALE_ELEMENT"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    UNEXPECTED_REDIRECT = "UNEXPECTED_REDIRECT"
    FRAME_CHANGED = "FRAME_CHANGED"
    POPUP = "POPUP"
    NEW_CONDITIONAL_FIELD = "NEW_CONDITIONAL_FIELD"
    PAGE_STATE_UNKNOWN = "PAGE_STATE_UNKNOWN"
    CUSTOM_UI = "CUSTOM_UI"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    NETWORK_ERROR = "NETWORK_ERROR"
    NO_PROGRESS = "NO_PROGRESS"
    STRATEGY_FAILED = "STRATEGY_FAILED"
    BLOCKED = "BLOCKED"


#: Escalation order per trigger (§20). Cheapest and most reversible first;
#: every ladder ends at a human or a stop, so exhaustion is never silent.
LADDERS: dict[RecoveryTrigger, tuple[RecoveryAction, ...]] = {
    RecoveryTrigger.TARGET_NOT_FOUND: (
        RecoveryAction.REOBSERVE, RecoveryAction.REGROUND, RecoveryAction.RETRY,
        RecoveryAction.SWITCH_FRAME, RecoveryAction.CHANGE_TARGET,
        RecoveryAction.AGENT_TAKEOVER, RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.AMBIGUOUS_TARGET: (
        RecoveryAction.REOBSERVE, RecoveryAction.CHANGE_TARGET,
        RecoveryAction.AGENT_TAKEOVER, RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.STALE_ELEMENT: (
        # The canonical fix, and the reason handles are never cached.
        RecoveryAction.REOBSERVE, RecoveryAction.REGROUND, RecoveryAction.RETRY,
        RecoveryAction.AGENT_TAKEOVER, RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.VALIDATION_ERROR: (
        # Never RELOAD: a reload discards everything already typed.
        RecoveryAction.REOBSERVE, RecoveryAction.CHANGE_FILL_METHOD,
        RecoveryAction.AGENT_TAKEOVER, RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.UNEXPECTED_REDIRECT: (
        RecoveryAction.REOBSERVE, RecoveryAction.BACKTRACK,
        RecoveryAction.REOPEN_APPLICATION, RecoveryAction.AGENT_TAKEOVER,
        RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.FRAME_CHANGED: (
        RecoveryAction.SWITCH_FRAME, RecoveryAction.REGROUND,
        RecoveryAction.AGENT_TAKEOVER, RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.POPUP: (
        RecoveryAction.SWITCH_TAB, RecoveryAction.REOBSERVE,
        RecoveryAction.AGENT_TAKEOVER, RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.NEW_CONDITIONAL_FIELD: (
        RecoveryAction.REOBSERVE, RecoveryAction.AGENT_TAKEOVER,
        RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.PAGE_STATE_UNKNOWN: (
        RecoveryAction.REOBSERVE, RecoveryAction.REFRESH,
        RecoveryAction.AGENT_TAKEOVER, RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.CUSTOM_UI: (
        RecoveryAction.REOBSERVE, RecoveryAction.AGENT_TAKEOVER,
        RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.SESSION_EXPIRED: (
        # REAUTHENTICATE has no implementation yet (Phases 11-14); until
        # then the engine reports it unavailable and the ladder falls to a
        # human rather than improvising a login.
        RecoveryAction.REAUTHENTICATE, RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.NETWORK_ERROR: (
        RecoveryAction.REFRESH, RecoveryAction.RELOAD, RecoveryAction.RETRY,
        RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.NO_PROGRESS: (
        # Deliberately does not include RETRY: repeating is the problem.
        RecoveryAction.CHANGE_TARGET, RecoveryAction.AGENT_TAKEOVER,
        RecoveryAction.ESCALATE_HUMAN,
    ),
    RecoveryTrigger.STRATEGY_FAILED: (
        RecoveryAction.REOBSERVE, RecoveryAction.CHANGE_FILL_METHOD,
        RecoveryAction.AGENT_TAKEOVER, RecoveryAction.ESCALATE_HUMAN,
    ),
    # A wall is a boundary, not a failure to route around (§31, failures §8).
    RecoveryTrigger.BLOCKED: (RecoveryAction.ESCALATE_HUMAN,),
}

#: Inside the danger zone the ladder is replaced entirely. Looking is
#: always safe; everything else could duplicate a live submission.
_DANGER_ZONE_LADDER: tuple[RecoveryAction, ...] = (
    RecoveryAction.REOBSERVE, RecoveryAction.ESCALATE_HUMAN,
)

#: Actions that change the page or the flow. Used to keep the danger-zone
#: rule honest even if a caller passes an action in directly.
CONSEQUENTIAL: frozenset[RecoveryAction] = frozenset({
    RecoveryAction.RELOAD, RecoveryAction.REFRESH, RecoveryAction.BACKTRACK,
    RecoveryAction.REOPEN_APPLICATION, RecoveryAction.RETRY,
    RecoveryAction.REAUTHENTICATE, RecoveryAction.CHANGE_FILL_METHOD,
})

#: Which failure category a trigger reports as, so recovery outcomes flow
#: back into the existing retry policy rather than a parallel one.
TRIGGER_CATEGORY: dict[RecoveryTrigger, FailureCategory] = {
    RecoveryTrigger.NETWORK_ERROR: FailureCategory.TRANSIENT,
    RecoveryTrigger.BLOCKED: FailureCategory.BLOCKED,
    RecoveryTrigger.SESSION_EXPIRED: FailureCategory.BLOCKED,
}


def plan_recovery(
    trigger: RecoveryTrigger,
    *,
    attempt: int,
    application_state: ApplicationState,
    allow_takeover: bool = True,
) -> RecoveryAction:
    """The next rung for `trigger` on attempt `attempt` (0-based).

    Pure function: same inputs, same action, every time. Past the end of a
    ladder the answer is ESCALATE_HUMAN, never "start again from the top".
    """
    if application_state in DANGER_ZONE:
        ladder = _DANGER_ZONE_LADDER
    else:
        ladder = LADDERS.get(trigger, (RecoveryAction.REOBSERVE, RecoveryAction.ESCALATE_HUMAN))
        if not allow_takeover:
            ladder = tuple(a for a in ladder if a is not RecoveryAction.AGENT_TAKEOVER)
    if attempt < 0 or attempt >= len(ladder):
        return RecoveryAction.ESCALATE_HUMAN
    return ladder[attempt]


def is_permitted(action: RecoveryAction, application_state: ApplicationState) -> bool:
    """Whether `action` may run at all in this application state.

    Checked by the engine on every execute, so the danger-zone rule holds
    even when a caller (or a model) supplies an action directly instead of
    asking `plan_recovery` for one.
    """
    if application_state in DANGER_ZONE:
        return action in _DANGER_ZONE_LADDER
    return True


@dataclass
class RecoveryStep:
    """What one recovery attempt did, and what the caller should do next."""

    action: RecoveryAction
    #: True when the engine performed the action itself against the browser.
    executed: bool
    #: True when the caller should now re-attempt the operation that failed.
    retry_operation: bool = False
    #: True when the caller should run the agent loop (§16 takeover).
    takeover: bool = False
    #: True when this ends the attempt and needs a person.
    escalate: bool = False
    reason: str = ""
    evidence: list[str] = field(default_factory=list)
    observation: BrowserObservation | None = None

    def to_dict(self) -> dict:
        return {
            "action": self.action.value,
            "executed": self.executed,
            "retry_operation": self.retry_operation,
            "takeover": self.takeover,
            "escalate": self.escalate,
            "reason": self.reason[:300],
            "evidence": self.evidence[:5],
        }


class RecoveryEngine:
    """Executes the browser-level half of the closed action set.

    The other half (RETRY, CHANGE_TARGET, CHANGE_FILL_METHOD,
    AGENT_TAKEOVER, ESCALATE_HUMAN, STOP) cannot be performed by a browser
    action alone — they are instructions to the caller — so the engine
    returns them as directives instead of pretending to have done
    something. Reporting "recovered" without acting would be exactly the
    false-success failure this project treats as a defect.
    """

    #: Actions the engine performs itself.
    EXECUTABLE: frozenset[RecoveryAction] = frozenset({
        RecoveryAction.REOBSERVE, RecoveryAction.REGROUND, RecoveryAction.REFRESH,
        RecoveryAction.RELOAD, RecoveryAction.BACKTRACK, RecoveryAction.SWITCH_FRAME,
        RecoveryAction.SWITCH_TAB, RecoveryAction.REOPEN_APPLICATION,
    })

    def __init__(self, registry: ToolRegistry | None = None, *, detail: Detail = Detail.CONTROLS):
        self.registry = registry or ToolRegistry()
        self.detail = detail

    def execute(
        self,
        action: RecoveryAction,
        ctx: ToolContext,
        *,
        application_url: str = "",
        frame_hint: str = "",
    ) -> RecoveryStep:
        """Run one recovery action. Never raises."""
        if not is_permitted(action, ctx.application_state):
            return RecoveryStep(
                RecoveryAction.ESCALATE_HUMAN, executed=False, escalate=True,
                reason=(
                    f"{action.value} is not permitted while the application is "
                    f"{ctx.application_state.value}: the submission may already have "
                    "reached the employer, so only observation is safe here"
                ),
            )

        if action is RecoveryAction.ESCALATE_HUMAN:
            return RecoveryStep(action, executed=False, escalate=True,
                                reason="recovery ladder exhausted")
        if action is RecoveryAction.STOP:
            return RecoveryStep(action, executed=False, reason="recovery stopped")
        if action is RecoveryAction.AGENT_TAKEOVER:
            return RecoveryStep(action, executed=False, takeover=True,
                                reason="deterministic path exhausted; handing over to the agent")
        if action is RecoveryAction.RETRY:
            return RecoveryStep(action, executed=False, retry_operation=True,
                                reason="retrying the failed operation unchanged")
        if action in (RecoveryAction.CHANGE_TARGET, RecoveryAction.CHANGE_FILL_METHOD):
            return RecoveryStep(action, executed=False, retry_operation=True,
                                reason=f"caller should {action.value.lower()} and retry")
        if action is RecoveryAction.REAUTHENTICATE:
            # Honest unavailability beats a guessed login flow (§25 is a
            # later phase). Falls through to a human.
            return RecoveryStep(
                RecoveryAction.ESCALATE_HUMAN, executed=False, escalate=True,
                reason="re-authentication is not implemented yet; a person must sign in",
            )

        return self._execute_browser_action(
            action, ctx, application_url=application_url, frame_hint=frame_hint,
        )

    # -- browser-level actions ---------------------------------------------

    def _execute_browser_action(
        self, action: RecoveryAction, ctx: ToolContext, *,
        application_url: str, frame_hint: str,
    ) -> RecoveryStep:
        try:
            if action is RecoveryAction.REOBSERVE:
                obs = observe(ctx.page, ctx.form_ctx, detail=self.detail)
                # A degraded observation is not a successful re-observe.
                # Reporting one as executed tells the caller "go ahead and
                # retry" on the strength of a read where nothing could
                # actually be read, and advances the ladder a rung for
                # free. See browser-agent-failures.md §14.
                if obs.degraded:
                    return RecoveryStep(
                        action, executed=False,
                        reason="re-observation came back degraded: " + "; ".join(obs.notes)[:200],
                        evidence=list(obs.notes), observation=obs,
                    )
                return RecoveryStep(action, executed=True, retry_operation=True,
                                    reason="re-read the live page", observation=obs)

            if action is RecoveryAction.REGROUND:
                return self._reground(ctx)

            if action is RecoveryAction.REFRESH:
                # Deliberately not a reload: wait for the page to settle,
                # then look again. Anything already typed survives.
                result = self.registry.execute("wait", ctx, seconds=1.5)
                return self._from_tool(action, result, retry=True)

            if action is RecoveryAction.RELOAD:
                result = self.registry.execute("reload", ctx)
                return self._from_tool(action, result, retry=True)

            if action is RecoveryAction.BACKTRACK:
                result = self.registry.execute("back", ctx)
                return self._from_tool(action, result, retry=True)

            if action is RecoveryAction.SWITCH_FRAME:
                result = self.registry.execute("switch_frame", ctx, url_contains=frame_hint)
                step = self._from_tool(action, result, retry=True)
                if result.ok and result.observation is not None:
                    ctx.form_ctx = getattr(result, "detail", {}).get("frame") or ctx.form_ctx
                return step

            if action is RecoveryAction.SWITCH_TAB:
                return self._switch_to_newest_tab(ctx)

            if action is RecoveryAction.REOPEN_APPLICATION:
                if not application_url:
                    return RecoveryStep(
                        RecoveryAction.ESCALATE_HUMAN, executed=False, escalate=True,
                        reason="no application URL to reopen",
                    )
                result = self.registry.execute("navigate", ctx, url=application_url)
                return self._from_tool(action, result, retry=True)
        except Exception as exc:  # noqa: BLE001 - recovery must not itself crash the attempt
            log.exception("Recovery action %s raised", action.value)
            return RecoveryStep(
                action, executed=False, escalate=True,
                reason=f"recovery action failed: {type(exc).__name__}: {exc}",
            )

        return RecoveryStep(action, executed=False, reason=f"no handler for {action.value}")

    def _reground(self, ctx: ToolContext) -> RecoveryStep:
        """Re-resolve element identity after a re-render (failures §4).

        Re-running the form scan re-injects `data-jobbot-id` on the live
        DOM, which is what every `_locate()` call resolves against, so
        stale references stop resolving to detached nodes.
        """
        from jobbot.submit.form_scan import find_target_frame, scan_form

        try:
            # Short poll: the caller is recovering, not waiting on a
            # cold page load.
            frame = find_target_frame(ctx.page, timeout_ms=4000)
            if frame is not None:
                ctx.form_ctx = frame
            fields = scan_form(ctx.ctx())
        except Exception as exc:  # noqa: BLE001
            return RecoveryStep(
                RecoveryAction.REGROUND, executed=False,
                reason=f"could not re-ground: {type(exc).__name__}: {exc}",
            )
        obs = observe(ctx.page, ctx.form_ctx, detail=self.detail)
        if not fields:
            # Re-grounding that found nothing to ground has not recovered
            # anything; saying otherwise sends the caller back into the
            # same failure (failures §14).
            return RecoveryStep(
                RecoveryAction.REGROUND, executed=False,
                reason="re-grounded, but the page has no form fields to bind to",
                observation=obs,
            )
        return RecoveryStep(
            RecoveryAction.REGROUND, executed=True, retry_operation=True,
            reason=f"re-grounded {len(fields)} field(s) on the live DOM",
            evidence=[f"fields={len(fields)}"], observation=obs,
        )

    def _switch_to_newest_tab(self, ctx: ToolContext) -> RecoveryStep:
        """A `target=_blank` continuation opens the flow in a tab we are
        not watching (failures §6). The newest tab is the one that just
        opened."""
        pages = getattr(getattr(ctx.page, "context", None), "pages", None)
        pages = list(pages) if pages else []
        if len(pages) < 2:
            return RecoveryStep(
                RecoveryAction.SWITCH_TAB, executed=False,
                reason="only one tab is open; nothing to switch to",
            )
        index = len(pages) - 1
        result = self.registry.execute("switch_tab", ctx, index=index)
        if result.ok:
            ctx.page = pages[index]
            ctx.form_ctx = None  # the old frame belongs to the old tab
        return self._from_tool(RecoveryAction.SWITCH_TAB, result, retry=True)

    @staticmethod
    def _from_tool(action: RecoveryAction, result: ToolResult, *, retry: bool) -> RecoveryStep:
        return RecoveryStep(
            action, executed=result.ok, retry_operation=retry and result.ok,
            escalate=not result.ok and not result.recoverable,
            reason="; ".join(result.evidence)[:300] or action.value,
            evidence=list(result.evidence), observation=result.observation,
        )

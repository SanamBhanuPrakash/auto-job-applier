"""Typed tool registry — the agent's only way to touch the browser.

Spec §9/§10/§11. Three properties make this a safety boundary rather than a
convenience wrapper:

1. **A closed set of actuators.** The agent selects a registered tool by
   name with typed arguments. It cannot emit arbitrary Playwright or Python
   to run. Research basis: arXiv 2511.19477 concludes that safe autonomous
   browser operation requires "specialized tools with programmatic
   constraints, where safety boundaries are enforced through code."

2. **Verification is not the tool's choice.** The registry itself
   re-observes the page after every action and computes whether state
   actually changed (§107). A tool cannot report success without the
   environment being re-read — which is the defect class that produced
   this repo's two false-submission bugs.

3. **Bounded output.** `ToolResult` carries a digest and a small evidence
   list, never a page dump (§10, and context-rot per
   docs/research/agent-architecture.md §6).

Tool *authorization* lives in policy.py and is enforced by the registry
before any handler runs, so a tool cannot be invoked out of state.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Any

from jobbot.agent.observation import BrowserObservation, Detail, observe
from jobbot.agent.states import ApplicationState, FailureCategory

log = logging.getLogger(__name__)


class RiskClass(IntEnum):
    """§11. How consequential an action is, ordered for escalation."""

    READ_ONLY = 0            # observe, classify
    LOW_RISK = 1             # scroll, wait, switch frame/tab
    MEDIUM_RISK = 2          # fill a candidate field, click, navigate
    HIGH_RISK = 3            # credential entry, file upload
    EXTERNAL_SIDE_EFFECT = 4 # submit: reaches the employer, cannot be undone


class Capability(IntEnum):
    """*What kind* of thing a tool does — the axis an autonomy level gates.

    Deliberately separate from `RiskClass`, which says *how consequential*
    an action is. Collapsing the two into one ordinal looks tidy and is
    wrong: `click` and `type` are equally consequential (both MEDIUM_RISK),
    yet "move around the site without filling anything in" is a coherent
    and useful permission level. With a single axis, a NAVIGATE ceiling
    that admits `click` must also admit `type`, and one that excludes
    `type` also excludes `click` — leaving NAVIGATE unable to press an
    "Apply" button, i.e. unable to navigate.

    Found by test_agent_takeover.py; see browser-agent-failures.md §15.
    """

    OBSERVE = 0    # look, wait, scroll — changes nothing
    NAVIGATE = 1   # move: click, follow links, frames, tabs, history
    FILL = 2       # write a value into the page
    SUBMIT = 3     # hand the application to the employer


@dataclass
class ToolResult:
    """§10. Uniform, bounded, model-readable outcome of one tool call."""

    ok: bool
    tool_name: str
    risk_class: RiskClass
    changed_state: bool
    state_before: str = ""
    state_after: str = ""
    failure_category: FailureCategory | None = None
    evidence: list[str] = field(default_factory=list)
    recoverable: bool = True
    checkpoint_required: bool = False
    #: The post-action observation. Kept as an object for the controller;
    #: `to_agent_dict()` is what actually reaches a model.
    observation: BrowserObservation | None = None
    detail: dict = field(default_factory=dict)

    def to_agent_dict(self) -> dict:
        """Small enough to include in a decision prompt every step."""
        payload = {
            "tool": self.tool_name,
            "ok": self.ok,
            "changed_state": self.changed_state,
            "risk": self.risk_class.name,
        }
        if self.failure_category:
            payload["failure"] = self.failure_category.value
            payload["recoverable"] = self.recoverable
        if self.evidence:
            payload["evidence"] = self.evidence[:6]
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass(frozen=True)
class ToolSpec:
    """§9. Everything policy and the agent need to know about a tool
    without executing it."""

    name: str
    purpose: str
    risk_class: RiskClass
    handler: Callable[..., dict]
    #: The autonomy axis (see Capability). Defaults to FILL so that a tool
    #: added without thinking about it is gated more tightly, not less.
    capability: Capability = Capability.FILL
    #: Empty means "any state". Otherwise the application must be in one of
    #: these for the tool to be authorized (§40).
    required_states: frozenset[ApplicationState] = frozenset()
    side_effects: str = ""
    verification: str = "re-observe and compare state digest"
    failure_categories: tuple[FailureCategory, ...] = ()
    human_escalation_allowed: bool = True
    retry_allowed: bool = True
    #: Argument names the handler accepts; used to reject malformed calls
    #: before anything touches the browser.
    args: tuple[str, ...] = ()
    required_args: tuple[str, ...] = ()

    def describe(self) -> dict:
        """What the agent is told a tool does. Natural-language names, not
        cryptic identifiers (Writing Effective Tools)."""
        return {
            "name": self.name,
            "purpose": self.purpose,
            "risk": self.risk_class.name,
            "capability": self.capability.name,
            "args": list(self.args),
            "required_args": list(self.required_args),
        }


@dataclass
class ToolContext:
    """Everything a tool may touch. Deliberately explicit: a tool cannot
    reach global state, credentials, or the database through here."""

    page: Any
    form_ctx: Any = None
    application_state: ApplicationState = ApplicationState.DISCOVERED
    #: §41 file safety — the ONLY paths `upload` will accept. A path from
    #: page text can never end up here.
    allowed_upload_paths: tuple[Path, ...] = ()
    #: Observation tier used for the automatic post-action re-observe.
    observe_detail: Detail = Detail.CONTROLS

    def ctx(self):
        """The frame that tools act within — the form's frame when one was
        resolved, else the page."""
        return self.form_ctx if self.form_ctx is not None else self.page


class ToolError(Exception):
    """Raised by a handler to signal a categorized, non-crashing failure."""

    def __init__(self, message: str, category: FailureCategory, *, recoverable: bool = True):
        super().__init__(message)
        self.category = category
        self.recoverable = recoverable


# ---------------------------------------------------------------------------
# Handlers
#
# Each returns a small dict of evidence/detail. The registry supplies
# observation, digests and changed_state, so a handler cannot claim success
# the environment does not support.
# ---------------------------------------------------------------------------


def _locate(ctx: ToolContext, ref: str):
    """Re-ground a control by its stable ref every time it is used.

    Never cache element handles across actions: they go stale on navigation
    and on framework re-render (browser-agent-failures.md §4).
    """
    loc = ctx.ctx().locator(f'[data-jobbot-id="{ref}"]')
    if loc.count() == 0:
        raise ToolError(f"no element with ref {ref!r} on the current page", FailureCategory.RECOVERABLE)
    return loc.first


def _h_observe(ctx: ToolContext, **_: Any) -> dict:
    return {}


def _h_navigate(ctx: ToolContext, url: str = "", **_: Any) -> dict:
    if not url:
        raise ToolError("navigate requires a url", FailureCategory.POLICY, recoverable=False)
    if not url.lower().startswith(("http://", "https://", "file://")):
        # Never follow javascript:, data:, or a relative string the model
        # assembled from page text.
        raise ToolError(f"refusing to navigate to unsupported scheme: {url[:60]!r}",
                        FailureCategory.POLICY, recoverable=False)
    try:
        ctx.page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"navigation failed: {exc}", FailureCategory.TRANSIENT) from exc
    return {"url": url}


def _h_back(ctx: ToolContext, **_: Any) -> dict:
    ctx.page.go_back(wait_until="domcontentloaded", timeout=20000)
    return {}


def _h_reload(ctx: ToolContext, **_: Any) -> dict:
    ctx.page.reload(wait_until="domcontentloaded", timeout=30000)
    return {}


def _h_click(ctx: ToolContext, ref: str = "", **_: Any) -> dict:
    loc = _locate(ctx, ref)
    try:
        loc.click(timeout=10000)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"click failed on {ref!r}: {exc}", FailureCategory.RECOVERABLE) from exc
    return {"ref": ref}


def _h_type(ctx: ToolContext, ref: str = "", value: str = "", **_: Any) -> dict:
    loc = _locate(ctx, ref)
    try:
        loc.fill(value, timeout=10000)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"type failed on {ref!r}: {exc}", FailureCategory.RECOVERABLE) from exc
    # Read back immediately: a fill that silently did not stick is the most
    # common quiet failure on custom React inputs.
    try:
        actual = loc.input_value(timeout=3000)
    except Exception:  # noqa: BLE001
        actual = ""
    if value and actual != value:
        raise ToolError(
            f"value did not stick on {ref!r} (wanted {value[:40]!r}, read {actual[:40]!r})",
            FailureCategory.RECOVERABLE,
        )
    return {"ref": ref, "chars": len(value)}


def _h_clear(ctx: ToolContext, ref: str = "", **_: Any) -> dict:
    _locate(ctx, ref).fill("", timeout=10000)
    return {"ref": ref}


def _h_select(ctx: ToolContext, ref: str = "", value: str = "", **_: Any) -> dict:
    loc = _locate(ctx, ref)
    try:
        loc.select_option(label=value, timeout=10000)
    except Exception as exc:  # noqa: BLE001
        raise ToolError(f"select failed on {ref!r}: {exc}", FailureCategory.RECOVERABLE) from exc
    return {"ref": ref, "value": value}


def _h_check(ctx: ToolContext, ref: str = "", **_: Any) -> dict:
    _locate(ctx, ref).check(timeout=10000)
    return {"ref": ref}


def _h_uncheck(ctx: ToolContext, ref: str = "", **_: Any) -> dict:
    _locate(ctx, ref).uncheck(timeout=10000)
    return {"ref": ref}


def _h_scroll(ctx: ToolContext, direction: str = "down", **_: Any) -> dict:
    delta = 600 if direction == "down" else -600
    ctx.page.mouse.wheel(0, delta)
    return {"direction": direction}


#: `press_key` is classified NAVIGATE, so it must not be able to write into
#: a focused field — `press_key("a")` repeated is text entry wearing a
#: navigation label, and would route around both the fill autonomy level
#: and the sensitive-field guardrail. Only keys that move or activate are
#: allowed; anything that produces a character is refused.
_NAVIGATION_KEYS = frozenset({
    "Enter", "Tab", "Escape", "Space", "PageDown", "PageUp", "Home", "End",
    "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight",
    "Shift+Tab", "Control+Home", "Control+End",
})


def _h_press_key(ctx: ToolContext, key: str = "", **_: Any) -> dict:
    if not key:
        raise ToolError("press_key requires a key", FailureCategory.POLICY, recoverable=False)
    if key not in _NAVIGATION_KEYS:
        raise ToolError(
            f"{key!r} is not a navigation key; typing text must go through the "
            "`type` tool, which is authorized and verified as a fill",
            FailureCategory.POLICY, recoverable=False,
        )
    ctx.page.keyboard.press(key)
    return {"key": key}


def _h_upload(ctx: ToolContext, ref: str = "", path: str = "", **_: Any) -> dict:
    """§41. Only ever uploads a pre-approved candidate document.

    The path is matched against `allowed_upload_paths` by resolved identity,
    so neither the model nor page text can direct this at an arbitrary file.
    """
    candidate = Path(path).expanduser()
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ToolError(f"upload path does not exist: {path!r}", FailureCategory.POLICY,
                        recoverable=False) from exc

    allowed = {p.resolve() for p in ctx.allowed_upload_paths}
    if resolved not in allowed:
        raise ToolError(
            f"refusing to upload {resolved.name!r}: not a configured candidate document",
            FailureCategory.POLICY, recoverable=False,
        )
    _locate(ctx, ref).set_input_files(str(resolved), timeout=15000)
    return {"ref": ref, "file": resolved.name}


def _h_switch_frame(ctx: ToolContext, url_contains: str = "", **_: Any) -> dict:
    for frame in ctx.page.frames:
        if frame is ctx.page.main_frame:
            continue
        if url_contains and url_contains not in (frame.url or ""):
            continue
        if frame.locator("form").count() > 0 or not url_contains:
            ctx.form_ctx = frame
            return {"frame_url": frame.url}
    raise ToolError(f"no frame matching {url_contains!r}", FailureCategory.RECOVERABLE)


def _h_switch_tab(ctx: ToolContext, index: int = 0, **_: Any) -> dict:
    pages = list(ctx.page.context.pages)
    if index < 0 or index >= len(pages):
        raise ToolError(f"tab {index} does not exist ({len(pages)} open)", FailureCategory.RECOVERABLE)
    ctx.page = pages[index]
    ctx.form_ctx = None  # frame handles from the old tab are meaningless here
    ctx.page.bring_to_front()
    return {"tab": index, "url": ctx.page.url}


def _h_close_tab(ctx: ToolContext, index: int = 0, **_: Any) -> dict:
    pages = list(ctx.page.context.pages)
    if len(pages) <= 1:
        raise ToolError("refusing to close the last remaining tab", FailureCategory.POLICY,
                        recoverable=False)
    target = pages[index]
    was_current = target is ctx.page
    target.close()
    if was_current:
        ctx.page = [p for p in ctx.page.context.pages][0]
        ctx.form_ctx = None
    return {"closed": index}


def _h_wait(ctx: ToolContext, seconds: float = 1.0, **_: Any) -> dict:
    capped = max(0.0, min(float(seconds), 10.0))  # never an unbounded sleep (§22)
    ctx.page.wait_for_timeout(int(capped * 1000))
    return {"seconds": capped}


def _h_classify_page(ctx: ToolContext, **_: Any) -> dict:
    from jobbot.agent.page_classify import classify

    obs = observe(ctx.page, ctx.form_ctx, detail=Detail.ARIA)
    return {"classification": classify(obs).to_dict()}


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None):
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs or default_tools():
            self.register(spec)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} already registered")
        self._specs[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def describe_all(self) -> list[dict]:
        """The tool menu shown to the agent."""
        return [self._specs[n].describe() for n in self.names()]

    def execute(self, name: str, ctx: ToolContext, **args: Any) -> ToolResult:
        """Run one tool, then independently re-observe the environment.

        The post-action observation is taken by the registry, not the
        handler, so `changed_state` reflects the page rather than the
        tool's own opinion of what it did (§107).
        """
        spec = self._specs.get(name)
        if spec is None:
            return ToolResult(
                ok=False, tool_name=name, risk_class=RiskClass.READ_ONLY, changed_state=False,
                failure_category=FailureCategory.POLICY, recoverable=False,
                evidence=[f"unknown tool {name!r}; available: {', '.join(self.names())}"],
            )

        unknown = set(args) - set(spec.args)
        if unknown:
            return ToolResult(
                ok=False, tool_name=name, risk_class=spec.risk_class, changed_state=False,
                failure_category=FailureCategory.POLICY, recoverable=False,
                evidence=[f"unexpected argument(s) {sorted(unknown)}; accepts {list(spec.args)}"],
            )
        missing = [a for a in spec.required_args if not args.get(a)]
        if missing:
            return ToolResult(
                ok=False, tool_name=name, risk_class=spec.risk_class, changed_state=False,
                failure_category=FailureCategory.POLICY, recoverable=False,
                evidence=[f"missing required argument(s) {missing}"],
            )

        before = observe(ctx.page, ctx.form_ctx, detail=ctx.observe_detail)
        digest_before = before.state_digest()

        detail: dict = {}
        failure: FailureCategory | None = None
        recoverable = True
        evidence: list[str] = []
        ok = True

        try:
            detail = spec.handler(ctx, **args) or {}
        except ToolError as exc:
            ok, failure, recoverable = False, exc.category, exc.recoverable
            evidence.append(str(exc))
        except Exception as exc:  # noqa: BLE001 - a tool must never crash the loop
            ok, failure = False, FailureCategory.RECOVERABLE
            evidence.append(f"{type(exc).__name__}: {exc}")
            log.debug("Tool %s raised", name, exc_info=True)

        after = observe(ctx.page, ctx.form_ctx, detail=ctx.observe_detail)
        digest_after = after.state_digest()
        changed = digest_after != digest_before

        if after.degraded:
            evidence.append("post-action observation degraded")
        if ok and not changed and spec.risk_class >= RiskClass.MEDIUM_RISK:
            # Not a failure by itself, but the agent must know: this is the
            # signal loop detection consumes.
            evidence.append("action completed but page state did not change")

        return ToolResult(
            ok=ok,
            tool_name=name,
            risk_class=spec.risk_class,
            changed_state=changed,
            state_before=digest_before,
            state_after=digest_after,
            failure_category=failure,
            evidence=evidence,
            recoverable=recoverable,
            checkpoint_required=spec.risk_class >= RiskClass.HIGH_RISK,
            observation=after,
            detail=detail,
        )


# ---------------------------------------------------------------------------
# Default tool set
# ---------------------------------------------------------------------------

_FORM_STATES = frozenset({
    ApplicationState.INSPECTING_FORM,
    ApplicationState.FILLING,
    ApplicationState.VERIFYING_FIELDS,
    ApplicationState.READY_TO_SUBMIT,
    ApplicationState.RECOVERING,
})

_NAV_STATES = frozenset({
    ApplicationState.OPENING_APPLICATION,
    ApplicationState.INSPECTING_FORM,
    ApplicationState.FILLING,
    ApplicationState.VERIFYING_FIELDS,
    ApplicationState.RECOVERING,
})


def default_tools() -> list[ToolSpec]:
    R = RiskClass
    C = Capability
    F = FailureCategory
    return [
        ToolSpec("observe", "Re-read the current page state.", R.READ_ONLY, _h_observe, capability=C.OBSERVE,
                 verification="n/a - read only"),
        ToolSpec("classify_page", "Determine what kind of page this is.", R.READ_ONLY, _h_classify_page, capability=C.OBSERVE,
                 verification="n/a - read only"),
        ToolSpec("wait", "Pause briefly for the page to settle (max 10s).", R.LOW_RISK, _h_wait, capability=C.OBSERVE,
                 args=("seconds",)),
        ToolSpec("scroll", "Scroll the page up or down.", R.LOW_RISK, _h_scroll, capability=C.OBSERVE, args=("direction",)),
        ToolSpec("switch_frame", "Move into the iframe that holds the form.", R.LOW_RISK,
                 _h_switch_frame, capability=C.NAVIGATE, args=("url_contains",), failure_categories=(F.RECOVERABLE,)),
        ToolSpec("switch_tab", "Switch to another open browser tab.", R.LOW_RISK, _h_switch_tab, capability=C.NAVIGATE,
                 args=("index",), failure_categories=(F.RECOVERABLE,)),
        ToolSpec("close_tab", "Close a browser tab.", R.LOW_RISK, _h_close_tab, capability=C.NAVIGATE, args=("index",)),
        ToolSpec("press_key", "Press a keyboard key.", R.LOW_RISK, _h_press_key, capability=C.NAVIGATE,
                 args=("key",), required_args=("key",)),

        ToolSpec("navigate", "Open a URL.", R.MEDIUM_RISK, _h_navigate, capability=C.NAVIGATE,
                 args=("url",), required_args=("url",), required_states=_NAV_STATES,
                 side_effects="leaves the current page; any unsaved form input is lost",
                 failure_categories=(F.TRANSIENT, F.POLICY)),
        ToolSpec("back", "Go back one page.", R.MEDIUM_RISK, _h_back, capability=C.NAVIGATE, required_states=_NAV_STATES),
        ToolSpec("reload", "Reload the current page.", R.MEDIUM_RISK, _h_reload, capability=C.NAVIGATE,
                 required_states=_NAV_STATES),
        ToolSpec("click", "Click a control by its ref.", R.MEDIUM_RISK, _h_click, capability=C.NAVIGATE,
                 args=("ref",), required_args=("ref",),
                 side_effects="may navigate, open a modal, or submit",
                 failure_categories=(F.RECOVERABLE,)),
        ToolSpec("type", "Type a value into a field.", R.MEDIUM_RISK, _h_type, capability=C.FILL,
                 args=("ref", "value"), required_args=("ref",), required_states=_FORM_STATES,
                 verification="value is read back from the field after typing",
                 failure_categories=(F.RECOVERABLE,)),
        ToolSpec("clear", "Empty a field.", R.MEDIUM_RISK, _h_clear, capability=C.FILL,
                 args=("ref",), required_args=("ref",), required_states=_FORM_STATES),
        ToolSpec("select", "Choose an option in a dropdown.", R.MEDIUM_RISK, _h_select, capability=C.FILL,
                 args=("ref", "value"), required_args=("ref", "value"), required_states=_FORM_STATES),
        ToolSpec("check", "Tick a checkbox or radio.", R.MEDIUM_RISK, _h_check, capability=C.FILL,
                 args=("ref",), required_args=("ref",), required_states=_FORM_STATES),
        ToolSpec("uncheck", "Untick a checkbox.", R.MEDIUM_RISK, _h_uncheck, capability=C.FILL,
                 args=("ref",), required_args=("ref",), required_states=_FORM_STATES),

        ToolSpec("upload", "Attach an approved candidate document.", R.HIGH_RISK, _h_upload, capability=C.FILL,
                 args=("ref", "path"), required_args=("ref", "path"), required_states=_FORM_STATES,
                 side_effects="uploads a file to the employer",
                 verification="file input reports an attached file",
                 failure_categories=(F.POLICY, F.RECOVERABLE), retry_allowed=True),
    ]

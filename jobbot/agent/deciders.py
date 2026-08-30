"""Deciders — implementations of the controller's `Decider` protocol.

The controller deliberately does not know what an LLM is (§8). A decider
is anything that can look at a `DecisionContext` and return one structured
`Decision`, and the controller applies the same authorization and
verification to all of them.

`HeuristicDecider` is the first real one. It is *not* an LLM and does not
pretend to be: it encodes the handful of moves that get a stuck
deterministic run back to an application form, which is the case that
actually happens — a consent banner, an "Apply" button on a job-detail
page, a cookie modal, a form one iframe away. Having it means agent
takeover does something today rather than waiting on the LLM decider
phase (§23/§110), and it doubles as the deterministic baseline any LLM
decider has to beat.

Its ceiling is real and worth stating plainly: it cannot read a novel UI
it has no rule for, and it will return `finish` (which the controller then
checks against the environment) rather than guess.
"""
from __future__ import annotations

import re

from jobbot.agent.controller import Decision, DecisionContext
from jobbot.agent.observation import Control

#: Buttons/links that open or advance an application. Ordered by how
#: specific they are: "apply for this job" before a bare "apply".
_ADVANCE = (
    re.compile(r"\bapply (?:for|to) this job\b", re.I),
    re.compile(r"\bapply now\b", re.I),
    re.compile(r"\bsubmit (?:an )?application\b", re.I),
    re.compile(r"\bstart (?:your )?application\b", re.I),
    re.compile(r"\bapply\b", re.I),
    re.compile(r"\bcontinue\b", re.I),
    re.compile(r"\bnext\b", re.I),
)

#: Ways to clear a consent/interstitial overlay, in preference order.
#:
#: "Reject all" comes first deliberately. Both choices unblock the page
#: equally well, and accepting tracking is a privacy decision being made on
#: the candidate's behalf by software they are not watching. When a site
#: offers the refusing option, take it.
_DISMISS = (
    re.compile(r"\breject all\b", re.I),
    re.compile(r"\bdecline (?:all|optional)\b", re.I),
    re.compile(r"\bonly (?:necessary|essential)\b", re.I),
    re.compile(r"\baccept all\b", re.I),
    re.compile(r"\baccept (?:all )?cookies\b", re.I),
    re.compile(r"\b(?:i )?agree\b", re.I),
    re.compile(r"\bgot it\b", re.I),
    re.compile(r"\bdismiss\b", re.I),
    re.compile(r"\bclose\b", re.I),
)

#: Wording that merely *indicates* a consent bar is present. Wider than
#: `_DISMISS`, because detecting the bar and choosing which button to press
#: are different questions — "Cookie settings" identifies the bar but is
#: not a way out of it.
_CONSENT_SIGNAL = (
    re.compile(r"\bcookies?\b", re.I),
    re.compile(r"\bconsent\b", re.I),
    re.compile(r"\bprivacy (?:preferences|settings)\b", re.I),
    re.compile(r"\bmanage (?:preferences|choices)\b", re.I),
)

#: Never clicked by this decider, whatever the page calls them. Submission
#: is gated by policy too, but a decider that never proposes it is one
#: fewer thing depending on the gate holding.
_NEVER = re.compile(
    r"\b(submit application|submit$|sign in|log ?in|register|create account|delete|withdraw)\b",
    re.I,
)


def _looks_like_consent(controls: list[Control]) -> bool:
    """A cookie/consent overlay rarely registers as a native dialog, but it
    covers the viewport and swallows every click aimed underneath it.

    The tell is a dismissal-shaped button sitting next to consent wording:
    one alone is too weak (plenty of pages have a "Close" button), and the
    wording alone appears in ordinary privacy-policy footers.
    """
    has_exit = any(any(p.search(c.name) for p in _DISMISS) for c in controls)
    has_signal = any(any(p.search(c.name) for p in _CONSENT_SIGNAL) for c in controls)
    return has_exit and has_signal


def _clickable(controls: list[Control]) -> list[Control]:
    return [c for c in controls if c.role in ("button", "link") and c.enabled and c.name]


def _first_match(controls: list[Control], patterns, forbidden: set[str]) -> Control | None:
    for pattern in patterns:
        for control in controls:
            if control.ref in forbidden:
                continue
            if _NEVER.search(control.name):
                continue
            if pattern.search(control.name):
                return control
    return None


class HeuristicDecider:
    """Rule-based decider for the "get to the application form" goal."""

    def __init__(self, *, allow_advance: bool = True):
        self.allow_advance = allow_advance

    def decide(self, ctx: DecisionContext) -> Decision:
        obs = ctx.observation
        # Refs the trajectory has already told us not to repeat, plus
        # anything we clicked that changed nothing.
        forbidden = {
            str(a.get("args", {}).get("ref", ""))
            for a in ctx.recent_actions
            if a.get("action") == "click"
        } if any("changed nothing" in f for f in ctx.known_failures) else set()

        controls = _clickable(obs.controls)

        # 1. A dialog on top of the page blocks everything underneath it.
        if obs.dialog_open or _looks_like_consent(controls):
            target = _first_match(controls, _DISMISS, forbidden)
            if target is not None:
                return Decision(
                    action="click", args={"ref": target.ref},
                    objective="clear the interstitial blocking the page",
                    reason=f"dismissing {target.name!r}",
                    expected_outcome="the underlying page becomes interactive",
                    fallback="scroll", confidence=0.7,
                )

        # 2. The form may be in an iframe we are not inside (failures §5).
        if not obs.has_form and obs.frame_count > 1:
            return Decision(
                action="switch_frame", args={},
                objective="find the frame that actually holds the form",
                reason=f"{obs.frame_count} frames and no form in the current one",
                expected_outcome="an application form becomes visible",
                fallback="scroll", confidence=0.6,
            )

        # 3. A job-detail page needs its Apply button pressed.
        if self.allow_advance:
            target = _first_match(controls, _ADVANCE, forbidden)
            if target is not None:
                return Decision(
                    action="click", args={"ref": target.ref},
                    objective="open or advance the application",
                    reason=f"clicking {target.name!r}",
                    expected_outcome="an application form is presented",
                    fallback="scroll", confidence=0.6,
                )

        # 4. Content below the fold is a common reason a control is missing.
        if not any(a.get("action") == "scroll" for a in ctx.recent_actions[-2:]):
            return Decision(
                action="scroll", args={"direction": "down"},
                objective="reveal controls below the fold",
                reason="no actionable control found in the visible area",
                expected_outcome="more controls become visible",
                fallback="finish", confidence=0.4,
            )

        # 5. Out of rules. Say so rather than inventing a move — the
        #    controller re-checks `finish` against the environment anyway.
        return Decision(
            action="finish", args={},
            objective=ctx.goal,
            reason="no applicable heuristic for this page",
            confidence=0.2,
        )

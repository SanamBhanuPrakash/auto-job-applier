"""Turning remembered answers into a fill plan.

Extracted from `submit/base.py` so the decision that governs unattended
operation is directly testable. The rule it encodes is the one everything
else in this project defers to:

    A sensitive question is auto-answered only when the run authorized it
    AND the remembered value came from the candidate — never because the
    model produced it, however many times that value has been reused.

Reuse count measures repetition, not correctness, which is why
`times_used` appears in the explanation shown to the user but never in the
decision. `jobbot setup` exists to populate these answers from the person
up front; this module is what makes those answers actually land.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from jobbot.learning.provenance import may_autofill_sensitive


@dataclass
class MemoryPlan:
    #: field_id -> {"value", "needs_human", "reasoning"}, ready to merge.
    remembered: dict[int, dict] = field(default_factory=dict)
    #: (label, value) pairs shown in the review screen, so the candidate
    #: sees every sensitive answer that was filled on their behalf.
    auto_filled_sensitive: list[tuple[str, str]] = field(default_factory=list)
    #: field_id -> value for sensitive questions we remembered but did NOT
    #: fill. Shown as a suggestion the human can accept, not typed in.
    hints: dict[int, str] = field(default_factory=dict)
    #: Sensitive fields we had an answer for but were not allowed to use.
    withheld: list[tuple[str, str]] = field(default_factory=list)


def build_memory_plan(
    fields,
    matches: dict,
    *,
    autofill_sensitive: bool,
    value_still_offerable,
) -> MemoryPlan:
    """Decide which remembered answers may be filled on this form.

    `matches` maps field_id -> a LearnedAnswer-shaped object with
    `.value`, `.sensitive`, `.times_used`, `.provenance`,
    `.human_confirmed`.
    """
    by_id = {f.field_id: f for f in fields}
    plan = MemoryPlan()

    for field_id, match in matches.items():
        spec = by_id.get(field_id)
        if spec is None:
            continue
        # A remembered option is only usable if this posting still offers
        # it — option lists differ per employer even for the same question.
        if not value_still_offerable(spec, match.value):
            continue

        sensitive = bool(match.sensitive)
        trusted = may_autofill_sensitive(
            match.provenance or "", bool(match.human_confirmed)
        )
        if sensitive:
            plan.hints[field_id] = match.value

        allowed = (not sensitive) or (autofill_sensitive and trusted)
        if not allowed:
            if sensitive:
                plan.withheld.append((spec.label, _why_withheld(autofill_sensitive, trusted)))
            continue

        if sensitive:
            plan.auto_filled_sensitive.append((spec.label, match.value))

        times = getattr(match, "times_used", 0) or 0
        plan.remembered[field_id] = {
            "value": match.value,
            "needs_human": False,
            "reasoning": (
                f"Auto-filled from your confirmed answer (used {times} time(s) before)"
                + (" — sensitive-field autofill is enabled." if sensitive else ".")
            ),
        }
    return plan


def _why_withheld(autofill_sensitive: bool, trusted: bool) -> str:
    if not trusted:
        return ("the stored answer is a model guess, which is never used for a "
                "sensitive question — answer it yourself, or run `jobbot setup`")
    if not autofill_sensitive:
        return ("you have answered this, but JOBBOT_AUTOFILL_SENSITIVE=false so "
                "saved answers are not reused")
    return "not permitted"

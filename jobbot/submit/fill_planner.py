"""Turns a scanned form + candidate profile into a per-field fill plan via Claude.

Hard safety net: regardless of what the model returns, any field whose label
matches a sensitive-topic keyword (work authorization, EEOC/demographic
questions, legal attestations, government IDs) is force-flagged needs_human
so it always stops for your review. This mirrors the guardrail the research
called out as the one that must never be skipped.
"""
from __future__ import annotations

import logging
import re

from jobbot.llm import call_tool
from jobbot.resume.schema import Profile
from jobbot.submit.form_scan import FieldSpec

log = logging.getLogger(__name__)

_ALWAYS_HUMAN_PATTERNS = [
    r"veteran", r"disab", r"race", r"ethnicit", r"gender", r"sex\b",
    r"sponsor", r"authoriz\w* to work", r"work authorization",
    r"visa", r"immigration", r"social security", r"\bssn\b",
    r"government", r"security clearance", r"attest", r"certif.*(true|accurate)",
    r"criminal", r"background check", r"salary history", r"date of birth",
    r"national origin",
]
_ALWAYS_HUMAN_RE = re.compile("|".join(_ALWAYS_HUMAN_PATTERNS), re.IGNORECASE)

_PLAN_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "fields": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field_id": {"type": "integer"},
                    "value": {"type": ["string", "null"]},
                    "needs_human": {"type": "boolean"},
                    "reasoning": {"type": "string"},
                },
                "required": ["field_id", "needs_human", "reasoning"],
            },
        }
    },
    "required": ["fields"],
}

_SYSTEM = """You fill out a job application form using ONLY the facts given in
the candidate's profile JSON. Never invent, guess, or infer a fact that isn't
explicitly present in the profile — if the form asks something the profile
doesn't answer, set value to null and needs_human to true. Always set
needs_human to true (even if you can guess a reasonable value) for anything
touching: work authorization/visa/sponsorship, EEOC or demographic questions
(veteran status, disability, race, ethnicity, gender), legal attestations or
certifications, government IDs, salary history, or criminal/background
questions — these must always be reviewed by the human, never auto-filled.
For select/radio/combobox fields, choose the exact option text from the
provided options list, or null if none fit. Be conservative."""


def is_sensitive(label: str) -> bool:
    return bool(_ALWAYS_HUMAN_RE.search(label))


def build_fill_plan(profile: Profile, fields: list[FieldSpec], job_context: str) -> dict[int, dict]:
    candidates = [f for f in fields if f.field_type != "file"]

    plan: dict[int, dict] = {}
    llm_fields: list[FieldSpec] = []
    for f in candidates:
        if is_sensitive(f.label):
            # Never even ask the model for these — no value it could return
            # would be usable unsupervised, so skip the API call entirely.
            plan[f.field_id] = {
                "value": None,
                "needs_human": True,
                "reasoning": "Sensitive field (work authorization/EEOC/legal/etc.) always requires human review.",
            }
        else:
            llm_fields.append(f)

    if llm_fields:
        prompt = (
            f"Candidate profile:\n{profile.facts_json_for_llm()}\n\n"
            f"Job context:\n{job_context}\n\n"
            f"Form fields:\n{[f.to_llm_dict() for f in llm_fields]}"
        )
        result = call_tool(
            system=_SYSTEM,
            user_message=prompt,
            tool_name="record_fill_plan",
            tool_description="Record the value (or null) and needs_human flag for every field.",
            input_schema=_PLAN_TOOL_SCHEMA,
        )

        by_field_id = {f.field_id: f for f in llm_fields}
        for row in result.get("fields", []):
            fid = row["field_id"]
            spec = by_field_id.get(fid)
            if spec is None:
                continue
            needs_human = bool(row.get("needs_human", True)) or is_sensitive(spec.label)  # defense in depth
            plan[fid] = {
                "value": row.get("value"),
                "needs_human": needs_human,
                "reasoning": row.get("reasoning", ""),
            }

    # Any field the model didn't return an entry for defaults to needs_human.
    for f in candidates:
        if f.field_id not in plan:
            plan[f.field_id] = {"value": None, "needs_human": True, "reasoning": "no model response"}

    return plan

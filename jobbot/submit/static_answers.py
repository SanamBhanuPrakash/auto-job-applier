"""Deterministic, LLM-free answers for the handful of fields that appear on
almost every job application form.

Researched directly (Reddit threads, Simplify's own help docs, and reviews)
before writing this: Simplify Copilot — the best-known tool in exactly this
space — does not use an LLM for its core autofill either. It maps a fixed
taxonomy of common field labels (name, email, phone, links, education, work
history) straight onto a stored profile via fuzzy label matching, and only
reaches for AI on genuinely novel free-text questions. That's strictly
cheaper, faster, and more deterministic than a per-field LLM call for
fields that don't actually require judgment — and it sidesteps this
project's own Groq/Gemini rate-limit pain for exactly those fields. This
module is that fast path: whatever it confidently resolves here never goes
to the LLM in fill_planner.py at all.

Worth being explicit about the other thing that research surfaced: even
Simplify — full-time engineering team, funded, years of iteration — never
submits for you. You still click Submit on every single application
yourself; autofill only fills the form. Reported field-matching accuracy on
some ATS platforms (iCIMS/Taleo) is ~40-50%. That's not a gap this module
tries to paper over with confident-sounding guesses — it's the reason every
value resolved here is still subject to the same fill-then-verify check as
an LLM-resolved one (apply_fill_plan in filler.py), and the same
screenshot-before-you-type-'yes' review as everything else.

Deliberately conservative in scope: every entry here is a literal,
structural fact (a name, an email address, a URL) with zero interpretation
— nothing here ever infers, estimates, or fabricates. Sensitive topics
(work authorization, EEOC, salary history, etc.) are never handled here
regardless of a label match — fill_planner.is_sensitive() is checked again
as a hard backstop, same as everywhere else in this codebase.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from jobbot.resume.schema import Profile
from jobbot.submit.fill_planner import is_sensitive
from jobbot.submit.form_scan import FieldSpec


def _first_name(p: Profile) -> str | None:
    parts = p.name.split()
    return parts[0] if parts else None


def _last_name(p: Profile) -> str | None:
    parts = p.name.split()
    return " ".join(parts[1:]) if len(parts) > 1 else None


def _current_company(p: Profile) -> str | None:
    return p.experience[0].company if p.experience and p.experience[0].company else None


def _current_title(p: Profile) -> str | None:
    return p.experience[0].title if p.experience and p.experience[0].title else None


def _school(p: Profile) -> str | None:
    return p.education[0].school if p.education and p.education[0].school else None


def _degree(p: Profile) -> str | None:
    return p.education[0].degree if p.education and p.education[0].degree else None


def _salary(p: Profile) -> str | None:
    return str(p.salary_expectation_usd) if p.salary_expectation_usd else None


def _relocate(p: Profile) -> str | None:
    if p.willing_to_relocate is None:
        return None
    return "Yes" if p.willing_to_relocate else "No"


# Checked in order, first match wins — more specific patterns (first/last
# name) must come before the general "name" catch-all. Deliberately does
# NOT include anything like "country"/"city of residence": those questions
# often expect an exact single value (a country name) that profile.location
# — typically a free-text "City, Country" string — won't reliably match, so
# they're left to the LLM/human rather than risk a confident near-miss.
_RESOLVERS: list[tuple[re.Pattern, Callable[[Profile], str | None]]] = [
    (re.compile(r"first\s*name|given\s*name", re.I), _first_name),
    (re.compile(r"last\s*name|family\s*name|surname", re.I), _last_name),
    (re.compile(r"^\s*(full\s*name|name)\s*\*?\s*$", re.I), lambda p: p.name or None),
    (re.compile(r"e-?\s*mail", re.I), lambda p: p.email or None),
    (re.compile(r"phone|mobile|cell\s*number|contact\s*number", re.I), lambda p: p.phone or None),
    (re.compile(r"linkedin", re.I), lambda p: p.links.linkedin or None),
    (re.compile(r"github", re.I), lambda p: p.links.github or None),
    (re.compile(r"portfolio|personal\s*website|personal\s*site", re.I), lambda p: p.links.portfolio or None),
    (re.compile(r"current\s*(company|employer)", re.I), _current_company),
    (re.compile(r"current\s*(job\s*)?title|current\s*position", re.I), _current_title),
    (re.compile(r"(university|school|college)\b", re.I), _school),
    (re.compile(r"degree", re.I), _degree),
    (re.compile(r"(expected|desired)\s*salary|salary\s*expectation", re.I), _salary),
    (re.compile(r"willing\s*to\s*relocate|open\s*to\s*relocat", re.I), _relocate),
    (re.compile(r"^\s*(city|current\s*location|location)\s*\*?\s*$", re.I), lambda p: p.location or None),
]


def resolve_static_fields(profile: Profile, fields: list[FieldSpec]) -> dict[int, dict]:
    """Returns a fill_planner-shaped plan dict for whatever it can resolve
    directly from `profile` with zero interpretation. Fields not covered
    here (or where the profile has no value for the matched pattern) are
    simply absent from the result — the caller sends those on to the LLM
    exactly as before."""
    plan: dict[int, dict] = {}
    for f in fields:
        if f.field_type == "file" or is_sensitive(f.label):
            continue
        for pattern, resolver in _RESOLVERS:
            if not pattern.search(f.label):
                continue
            value = resolver(profile)
            if value:
                plan[f.field_id] = {
                    "value": value,
                    "needs_human": False,
                    "reasoning": "Filled directly from your profile (no LLM call needed for this field).",
                }
            break  # first matching pattern decides this field either way
    return plan

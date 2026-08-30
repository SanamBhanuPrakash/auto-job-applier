"""The decision that governs whether a run can proceed without stopping.

This is the real code path from `submit/base.py`, extracted so it can be
tested directly rather than mirrored. Whether a sensitive field ends up in
`needs_human` is what decides whether auto-submit stays on, so this is the
hinge the whole "unattended" claim turns on.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from jobbot.learning.provenance import Provenance
from jobbot.submit.memory_plan import build_memory_plan


@dataclass
class _Field:
    field_id: int
    label: str
    field_type: str = "radio"
    options: tuple = ()


@dataclass
class _Match:
    value: str
    sensitive: bool
    provenance: str
    human_confirmed: bool
    times_used: int = 3


def _always_offerable(_field, _value):
    return True


WORK_AUTH = _Field(1, "Are you legally authorized to work in the United States?")
FIRST_NAME = _Field(2, "First name", "text")


def _plan(fields, matches, *, autofill=True, offerable=_always_offerable):
    return build_memory_plan(fields, matches, autofill_sensitive=autofill,
                             value_still_offerable=offerable)


def _human(value="Yes"):
    return _Match(value, True, Provenance.HUMAN_ENTERED.value, True)


def _guess(value="Yes"):
    return _Match(value, True, Provenance.MODEL_GUESS.value, False)


# --- what onboarding unlocks ----------------------------------------------


def test_an_answer_you_gave_fills_a_sensitive_field_unattended():
    """The whole point of `jobbot setup`: this field would otherwise land
    in needs_human and switch auto-submit off."""
    plan = _plan([WORK_AUTH], {1: _human()})
    assert plan.remembered[1]["value"] == "Yes"
    assert plan.remembered[1]["needs_human"] is False
    assert ("Are you legally authorized to work in the United States?", "Yes") in (
        plan.auto_filled_sensitive)


def test_a_model_guess_is_never_filled_however_often_it_was_reused():
    """Reuse count measures repetition, not correctness."""
    plan = _plan([WORK_AUTH], {1: _Match("Yes", True, Provenance.MODEL_GUESS.value,
                                         False, times_used=99)})
    assert plan.remembered == {}
    assert plan.auto_filled_sensitive == []
    assert plan.hints[1] == "Yes", "still offered to the human as a suggestion"
    assert "model guess" in plan.withheld[0][1]


def test_a_verified_answer_also_qualifies():
    plan = _plan([WORK_AUTH], {1: _Match("No", True, Provenance.VERIFIED.value, False)})
    assert plan.remembered[1]["value"] == "No"


def test_the_run_level_flag_still_gates_everything():
    """Answering in onboarding is necessary, not sufficient. Turning the
    flag off must withhold even a human-confirmed answer."""
    plan = _plan([WORK_AUTH], {1: _human()}, autofill=False)
    assert plan.remembered == {}
    assert "JOBBOT_AUTOFILL_SENSITIVE=false" in plan.withheld[0][1]
    assert plan.hints[1] == "Yes", "shown as a suggestion, not typed in"


def test_a_withheld_answer_explains_which_of_the_two_reasons_applies():
    """"Not filled" is useless on its own — the fix differs completely
    between "answer it" and "flip a setting"."""
    guess = _plan([WORK_AUTH], {1: _guess()}).withheld[0][1]
    flag_off = _plan([WORK_AUTH], {1: _human()}, autofill=False).withheld[0][1]
    assert guess != flag_off
    assert "jobbot setup" in guess


# --- ordinary fields are unaffected ---------------------------------------


def test_an_ordinary_field_is_filled_regardless_of_provenance_or_flag():
    match = _Match("Ada", False, Provenance.MODEL_GUESS.value, False)
    plan = _plan([FIRST_NAME], {2: match}, autofill=False)
    assert plan.remembered[2]["value"] == "Ada"
    assert plan.auto_filled_sensitive == []
    assert plan.hints == {}, "non-sensitive answers are not suggestions, they are filled"


# --- the option-list guard survives the extraction ------------------------


def test_a_remembered_option_this_posting_does_not_offer_is_dropped():
    """Option lists differ per employer even for the same question."""
    plan = _plan([WORK_AUTH], {1: _human()}, offerable=lambda f, v: False)
    assert plan.remembered == {}
    assert plan.hints == {}


def test_a_match_for_a_field_not_on_this_form_is_ignored():
    plan = _plan([WORK_AUTH], {99: _human()})
    assert plan.remembered == {}


# --- every sensitive answer is surfaced to the candidate ------------------


@pytest.mark.parametrize("autofill", [True, False])
def test_a_sensitive_answer_is_always_visible_in_the_review_screen(autofill):
    """Filled or withheld, the candidate must be able to see what was
    going to be said on their behalf."""
    plan = _plan([WORK_AUTH], {1: _human()}, autofill=autofill)
    surfaced = bool(plan.auto_filled_sensitive) or bool(plan.hints)
    assert surfaced

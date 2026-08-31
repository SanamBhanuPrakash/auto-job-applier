"""The onboarding interview, and the chain it is supposed to unlock.

The claim being tested: answering the sensitive questions once, up front,
is what makes unattended runs possible — without weakening the guardrail
that refuses model-produced answers to those same questions.

The important tests are the two halves of that claim:
  * a human-entered answer *does* flow through to a live form field, and
  * a model guess sitting under the same question still does not.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import jobbot.db as db_module
from jobbot.db import session_scope
from jobbot.learning import store as learning_store
from jobbot.learning.provenance import Provenance, may_autofill_sensitive
from jobbot.models import Base
from jobbot.onboarding import (
    CATALOGUE,
    Region,
    answered_keys,
    by_key,
    questions_for,
    save_answer,
    unattended_readiness,
)
from jobbot.submit.fill_planner import is_sensitive
from jobbot.submit.form_scan import scan_form

FIXTURES = Path(__file__).parent / "fixtures"
_CHROME = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None


@pytest.fixture(autouse=True)
def clean_answers(monkeypatch):
    """Every test here writes real answers through save_answer()/
    learning_store — an in-memory DB per test, not a wipe of whatever
    database happens to be configured. The previous version of this
    fixture called the real session_scope() directly and deleted every
    LearnedAnswer row before AND after each test — confirmed live: run
    against a real profile with `jobbot setup` already completed, it
    silently erases those answers (this is what happened running the full
    suite locally after answering onboarding questions for real).

    onboarding.py's save_answer()/answered_keys() do a *local* `from
    jobbot.db import session_scope` inside each function body, so patching
    the attribute on jobbot.db is what they actually pick up on the next
    call. This file also imports session_scope directly at module level
    (used in several tests below), which needs patching separately since
    that import already bound the real function by the time any fixture
    runs.
    """
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def fake_session_scope():
        session = Session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    monkeypatch.setattr(db_module, "session_scope", fake_session_scope)
    monkeypatch.setattr(sys.modules[__name__], "session_scope", fake_session_scope)
    yield


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True, executable_path=_CHROME)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not installed: {exc}")
        yield b
        b.close()


@pytest.fixture
def page(browser):
    context = browser.new_context()
    p = context.new_page()
    yield p
    context.close()


# --- the catalogue ---------------------------------------------------------


def test_every_question_that_blocks_a_run_is_in_the_catalogue():
    """If the fill planner treats a question as sensitive, onboarding has
    to be able to ask it — otherwise it stops a run forever with no way to
    resolve it short of answering it by hand every time."""
    blocking = [q for q in CATALOGUE if is_sensitive(q.prompt)]
    assert len(blocking) >= 8, [q.key for q in CATALOGUE if not is_sensitive(q.prompt)]


def test_the_catalogue_marks_as_sensitive_exactly_what_the_planner_does():
    for q in CATALOGUE:
        if is_sensitive(q.prompt):
            assert q.sensitive, f"{q.key} blocks runs but is not marked sensitive"


def test_regions_keep_irrelevant_questions_out_of_the_way():
    india = questions_for((Region.INDIA,))
    assert not any(q.key == "veteran_status" for q in india)
    assert any(q.key == "current_ctc" for q in india)
    us = questions_for((Region.US,))
    assert any(q.key == "work_authorization" for q in us)
    assert not any(q.key == "current_ctc" for q in us)


def test_nothing_in_the_catalogue_ships_with_a_default_answer():
    """A pre-filled plausible answer is a guess wearing a human's
    provenance — the exact failure the guardrail exists to prevent."""
    for q in CATALOGUE:
        assert not getattr(q, "default", None)


# --- saving ----------------------------------------------------------------


def test_a_saved_answer_carries_human_provenance():
    save_answer(by_key("work_authorization"), "Yes")
    with session_scope() as session:
        row = learning_store.find_match(
            session, "Are you legally authorized to work in the United States?", "radio")
    assert row is not None
    assert row.provenance == Provenance.HUMAN_ENTERED.value
    assert row.human_confirmed is True
    assert may_autofill_sensitive(row.provenance, row.human_confirmed) is True


def test_aliases_are_stored_so_differently_worded_forms_still_match():
    save_answer(by_key("sponsorship"), "No")
    with session_scope() as session:
        for wording in (
            "Do you now or will you in the future require visa sponsorship?",
            "Will you require sponsorship to work in the United States?",
        ):
            assert learning_store.find_match(session, wording, "radio") is not None, wording


def test_an_empty_answer_is_not_stored():
    assert save_answer(by_key("work_authorization"), "") == 0
    assert "work_authorization" not in answered_keys()


# --- the chain this is supposed to unlock ---------------------------------


def test_a_seeded_answer_reaches_a_real_form_field(page):
    """End to end against a real page: the field is sensitive (so it would
    otherwise stop the run), and after onboarding it resolves to a value
    trusted enough to fill unattended."""
    page.goto((FIXTURES / "conditional_fields_page.html").as_uri(),
              wait_until="domcontentloaded")
    fields = scan_form(page)

    work_auth = next(
        (f for f in fields if "authorized to work" in f.label.lower()), None)
    assert work_auth is not None, [f.label for f in fields]
    assert is_sensitive(work_auth.label), "this field must be one that blocks a run"

    with session_scope() as session:
        assert learning_store.match_fields(session, [work_auth]) == {}, (
            "before onboarding there is nothing to fill it with")

    save_answer(by_key("work_authorization"), "Yes")

    with session_scope() as session:
        matches = learning_store.match_fields(session, fields)
    assert work_auth.field_id in matches, "the seeded answer did not match the live label"
    match = matches[work_auth.field_id]
    assert may_autofill_sensitive(match.provenance, match.human_confirmed) is True
    assert match.value == "Yes"


def test_a_model_guess_under_the_same_question_still_does_not_unlock_it():
    """The guardrail is unchanged. Onboarding works because the answer
    comes from the person, not because the rule was loosened."""
    with session_scope() as session:
        learning_store.upsert(
            session, "Are you legally authorized to work in the United States?",
            "radio", "Yes", sensitive=True,
            provenance=Provenance.MODEL_GUESS.value, human_confirmed=False,
        )
        session.commit()
        row = learning_store.find_match(
            session, "Are you legally authorized to work in the United States?", "radio")
    assert may_autofill_sensitive(row.provenance, row.human_confirmed) is False
    assert "work_authorization" not in answered_keys(), (
        "a model guess must not count as an answered question")


def test_a_model_guess_is_upgraded_by_a_human_answer_not_the_reverse():
    """Trust ratchets one way. Answering in onboarding overrides an
    earlier guess; a later guess must not undo the human answer."""
    q = by_key("sponsorship")
    with session_scope() as session:
        learning_store.upsert(session, q.prompt, "radio", "Yes", sensitive=True,
                              provenance=Provenance.MODEL_GUESS.value)
        session.commit()
    save_answer(q, "No")
    with session_scope() as session:
        learning_store.upsert(session, q.prompt, "radio", "No", sensitive=True,
                              provenance=Provenance.MODEL_GUESS.value)
        session.commit()
        row = learning_store.find_match(session, q.prompt, "radio")
    assert row.provenance == Provenance.HUMAN_ENTERED.value
    assert row.human_confirmed is True


# --- the readiness report --------------------------------------------------


def test_readiness_names_what_is_still_blocking():
    status = unattended_readiness((Region.US,))
    assert "work_authorization" in status["missing_sensitive"]
    assert status["will_run_unattended"] is False


def test_readiness_still_says_no_when_answers_are_complete_but_flags_are_off(monkeypatch):
    """Answering everything is necessary, not sufficient. Both settings
    are separate, deliberate choices and the report must not imply
    otherwise."""
    from jobbot import config

    for q in questions_for((Region.US,)):
        save_answer(q, q.options[0] if q.options else "n/a")

    settings = config.get_settings()
    monkeypatch.setattr(settings, "jobbot_autofill_sensitive", False)
    monkeypatch.setattr(settings, "jobbot_auto_submit", True)
    assert unattended_readiness((Region.US,))["will_run_unattended"] is False

    monkeypatch.setattr(settings, "jobbot_autofill_sensitive", True)
    monkeypatch.setattr(settings, "jobbot_auto_submit", False)
    assert unattended_readiness((Region.US,))["will_run_unattended"] is False


def test_readiness_reports_yes_only_when_everything_lines_up(monkeypatch):
    from jobbot import config

    for q in questions_for((Region.US,)):
        save_answer(q, q.options[0] if q.options else "n/a")
    settings = config.get_settings()
    monkeypatch.setattr(settings, "jobbot_autofill_sensitive", True)
    monkeypatch.setattr(settings, "jobbot_auto_submit", True)

    status = unattended_readiness((Region.US,))
    assert status["missing_sensitive"] == []
    assert status["will_run_unattended"] is True

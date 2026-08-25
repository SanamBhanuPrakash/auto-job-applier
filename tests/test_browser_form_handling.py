"""End-to-end tests against real (local, offline) HTML fixtures driven by an
actual Chromium instance — not mocks. These exercise the exact code path
`jobbot apply` uses: scan_form -> (optionally resolve an iframe) -> fill ->
verify -> read back. No network or LLM calls are needed since the fill plan
here is hand-built instead of coming from Claude.

Requires `playwright install chromium` (see README setup). Skipped
automatically if the browser isn't installed rather than failing the whole
suite for someone who hasn't run that yet.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.learning import store as learning_store
from jobbot.models import Base
from jobbot.submit.fill_planner import is_sensitive
from jobbot.submit.filler import apply_fill_plan, upload_resume
from jobbot.submit.form_scan import find_target_frame, scan_form
from jobbot.submit.values import read_field_value
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

FIXTURES = Path(__file__).parent / "fixtures"
APPLICATION_FORM = FIXTURES / "application_form.html"
CAREERS_PAGE_WITH_IFRAME = FIXTURES / "careers_page_with_iframe.html"

# Only set in environments where the pip-installed playwright package
# expects a different browser revision than what's pre-installed (this
# sandbox is one such case) — real users running `playwright install
# chromium` never need this.
_CHROME_OVERRIDE = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True, executable_path=_CHROME_OVERRIDE)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not installed (`playwright install chromium`): {exc}")
        yield b
        b.close()


@pytest.fixture
def memory_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    session = Session()
    yield session
    session.close()


def test_scan_form_identifies_every_real_field_type(browser):
    page = browser.new_page()
    page.goto(APPLICATION_FORM.as_uri())
    fields = scan_form(page)
    by_label = {f.label.strip(): f for f in fields}

    assert len(fields) == 11  # 10 distinct inputs + 2 radio groups collapsed from 4 <input type=radio>

    assert by_label["First Name*"].field_type == "text"
    assert by_label["First Name*"].required is True
    assert by_label["Email*"].field_type == "email"
    assert by_label["Resume/CV*"].field_type == "file"
    assert by_label["Cover Letter"].field_type == "textarea"

    select_field = by_label["How did you hear about us?"]
    assert select_field.field_type == "select"
    assert select_field.options == ["-- Select --", "LinkedIn", "Referral", "Other"]

    assert by_label["School"].field_type == "combobox"

    work_auth = next(f for f in fields if f.field_type == "radio" and "authorized to work" in f.label.lower())
    assert set(work_auth.options) == {"Yes", "No"}
    sponsorship = next(f for f in fields if f.field_type == "radio" and "sponsorship" in f.label.lower())
    assert sponsorship.field_id != work_auth.field_id

    terms = next(f for f in fields if f.field_type == "checkbox")
    assert "certify" in terms.label.lower()

    page.close()


def test_aria_hidden_decoy_sibling_is_not_scanned_as_its_own_field(browser):
    """Regression test for a real bug found running against a live
    Instacart/Greenhouse posting: every custom combobox there ships an
    invisible native <input required aria-hidden="true"> sibling purely for
    HTML5 validation, sharing the same accessible label as the real
    combobox. The scanner used to pick it up as a second, identically-
    labeled field; the LLM would then plan a value for it too, and
    Playwright correctly refusing to interact with a genuinely non-
    actionable hidden element turned into a ~30s hang per attempt
    (confirmed live) instead of failing fast."""
    page = browser.new_page()
    page.goto(APPLICATION_FORM.as_uri())
    fields = scan_form(page)

    school_fields = [f for f in fields if f.label.strip() == "School"]
    assert len(school_fields) == 1
    assert school_fields[0].field_type == "combobox"

    page.close()


def test_sensitive_guardrail_matches_real_scanned_labels(browser):
    """The guardrail regex is unit-tested against hand-written strings
    elsewhere (test_fill_planner_guardrails.py); this confirms it also
    matches what the DOM scanner actually produces."""
    page = browser.new_page()
    page.goto(APPLICATION_FORM.as_uri())
    fields = scan_form(page)
    by_label = {f.label.strip(): f for f in fields}

    work_auth = next(f for f in fields if f.field_type == "radio" and "authorized to work" in f.label.lower())
    sponsorship = next(f for f in fields if f.field_type == "radio" and "sponsorship" in f.label.lower())
    terms = next(f for f in fields if f.field_type == "checkbox")

    assert is_sensitive(work_auth.label)
    assert is_sensitive(sponsorship.label)
    assert is_sensitive(terms.label)
    assert not is_sensitive(by_label["First Name*"].label)
    assert not is_sensitive(by_label["Email*"].label)

    page.close()


def test_fill_verify_and_leave_sensitive_fields_untouched(browser):
    page = browser.new_page()
    page.goto(APPLICATION_FORM.as_uri())
    fields = scan_form(page)
    by_label = {f.label.strip(): f for f in fields}
    work_auth = next(f for f in fields if f.field_type == "radio" and "authorized to work" in f.label.lower())
    sponsorship = next(f for f in fields if f.field_type == "radio" and "sponsorship" in f.label.lower())

    plan = {
        by_label["First Name*"].field_id: {"value": "Ada", "needs_human": False, "reasoning": "t"},
        by_label["Last Name*"].field_id: {"value": "Lovelace", "needs_human": False, "reasoning": "t"},
        by_label["Email*"].field_id: {"value": "ada@example.com", "needs_human": False, "reasoning": "t"},
        by_label["How did you hear about us?"].field_id: {"value": "Referral", "needs_human": False, "reasoning": "t"},
        by_label["School"].field_id: {"value": "Tech Institute", "needs_human": False, "reasoning": "t"},
        work_auth.field_id: {"value": None, "needs_human": True, "reasoning": "sensitive"},
        sponsorship.field_id: {"value": None, "needs_human": True, "reasoning": "sensitive"},
    }
    needs_human = apply_fill_plan(page, fields, plan)

    assert read_field_value(page, by_label["First Name*"]) == "Ada"
    assert read_field_value(page, by_label["Email*"]) == "ada@example.com"
    assert read_field_value(page, by_label["How did you hear about us?"]) == "Referral"
    # react-aria-style combobox: click -> type -> click the matching [role=option]
    assert read_field_value(page, by_label["School"]) == "Tech Institute"
    # Sensitive fields must come back completely untouched, not just unsubmitted.
    assert read_field_value(page, work_auth) == ""
    assert {f.field_id for f in needs_human} >= {work_auth.field_id, sponsorship.field_id}

    page.close()


def test_sensitive_field_fills_when_plan_says_not_needs_human(browser):
    """submit/base.py is the only place that decides whether a sensitive
    field's plan entry gets needs_human=False (only when
    JOBBOT_AUTOFILL_SENSITIVE is on AND the run was confirmed AND the
    remembered value is still a valid option — see value_still_offerable).
    This confirms that once base.py has made that decision, the actual
    fill mechanics work identically to any other field — apply_fill_plan
    itself has no separate sensitivity concept."""
    page = browser.new_page()
    page.goto(APPLICATION_FORM.as_uri())
    fields = scan_form(page)
    by_label = {f.label.strip(): f for f in fields}
    work_auth = next(f for f in fields if f.field_type == "radio" and "authorized to work" in f.label.lower())

    assert is_sensitive(work_auth.label)
    assert learning_store.value_still_offerable(work_auth, "Yes")  # "Yes" is a real option on this form

    plan = {work_auth.field_id: {"value": "Yes", "needs_human": False, "reasoning": "autofill_sensitive enabled"}}
    needs_human = apply_fill_plan(page, fields, plan)

    assert read_field_value(page, work_auth) == "Yes"
    assert work_auth.field_id not in {f.field_id for f in needs_human}

    page.close()


def test_stale_learned_value_not_offered_when_options_changed(browser):
    """Guards the failure mode autofill_sensitive is riskiest for: a
    remembered answer that no longer matches what this specific posting's
    form actually offers should never be silently applied."""
    page = browser.new_page()
    page.goto(APPLICATION_FORM.as_uri())
    fields = scan_form(page)
    work_auth = next(f for f in fields if f.field_type == "radio" and "authorized to work" in f.label.lower())

    assert not learning_store.value_still_offerable(work_auth, "Not applicable")
    page.close()


def test_resume_upload_targets_file_input_directly(browser):
    page = browser.new_page()
    page.goto(APPLICATION_FORM.as_uri())
    fields = scan_form(page)

    uploaded = upload_resume(page, fields, APPLICATION_FORM)  # any real file works as a stand-in
    assert uploaded is True
    assert page.locator("#resume").evaluate("el => el.files.length") == 1

    page.close()


def test_learning_captures_form_state_and_matches_reworded_question(browser, memory_session):
    page = browser.new_page()
    page.goto(APPLICATION_FORM.as_uri())
    fields = scan_form(page)
    by_label = {f.label.strip(): f for f in fields}
    work_auth = next(f for f in fields if f.field_type == "radio" and "authorized to work" in f.label.lower())

    # Simulate the human filling in the sensitive fields + a short text field by hand.
    page.locator('input[name="work_auth"][value="yes"]').check()
    page.locator("#first_name").fill("Ada")
    page.locator("#cover_letter").fill("")  # deliberately left empty

    count = learning_store.capture_from_page(memory_session, page, fields)
    memory_session.commit()
    assert count >= 2

    reworded = learning_store.find_match(memory_session, "Are you authorized to work in the US?", "radio")
    assert reworded is not None
    assert reworded.value == "Yes"
    assert reworded.sensitive is True  # still won't be silently auto-filled next time

    first_name = learning_store.find_match(memory_session, "First Name", "text")
    assert first_name is not None
    assert first_name.value == "Ada"
    assert first_name.sensitive is False

    assert learning_store.find_match(memory_session, "Cover Letter", "textarea") is None  # empty -> never captured

    page.close()


def test_find_target_frame_resolves_top_level_form_directly(browser):
    page = browser.new_page()
    page.goto(APPLICATION_FORM.as_uri())
    form_ctx = find_target_frame(page, ats_hint="")
    assert form_ctx is page
    page.close()


def test_find_target_frame_follows_iframe_when_no_top_level_form_exists(browser):
    """Regression test for the real gap this was built to close: most
    employers embed their ATS form on a branded domain via an <iframe>
    rather than serving the ATS's own hosted URL directly (confirmed by
    checking live Greenhouse API responses — see commit history)."""
    page = browser.new_page()
    page.goto(CAREERS_PAGE_WITH_IFRAME.as_uri())

    assert page.locator("form").count() == 0  # the real-world failure mode

    form_ctx = find_target_frame(page, ats_hint="", timeout_ms=5000)
    assert form_ctx is not page

    fields = scan_form(form_ctx)
    assert len(fields) == 11

    by_label = {f.label.strip(): f for f in fields}
    plan = {by_label["First Name*"].field_id: {"value": "Ada", "needs_human": False, "reasoning": "t"}}
    apply_fill_plan(form_ctx, fields, plan)

    assert read_field_value(form_ctx, by_label["First Name*"]) == "Ada"
    assert page.locator("#first_name").count() == 0  # fill did not leak onto the outer page

    page.close()


def test_find_target_frame_times_out_cleanly_when_nothing_matches(browser):
    page = browser.new_page()
    page.goto(CAREERS_PAGE_WITH_IFRAME.as_uri())

    with pytest.raises(TimeoutError):
        find_target_frame(page, ats_hint="totally-different-ats.example.com", timeout_ms=1000)

    page.close()

"""Assisted apply: the agent fills, you submit.

The mode exists because `jobbot apply` needs a per-ATS submission handler
and therefore reaches almost no Indian posting. Filling is generic, so
assisted mode reaches any form-shaped page. These tests hold it to that
claim on a real browser, and hold the sensitive-field rule to exactly the
same standard as everywhere else.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright
from sqlalchemy import select

from jobbot.assist import (
    REASON_SENSITIVE_LOCKED,
    REASON_SENSITIVE_UNANSWERED,
    assist,
)
from jobbot.db import session_scope
from jobbot.models import LearnedAnswer
from jobbot.onboarding import by_key, save_answer
from jobbot.resume.schema import Profile

FIXTURES = Path(__file__).parent / "fixtures"
_CHROME = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None


@pytest.fixture(autouse=True)
def clean_answers():
    def _wipe():
        with session_scope() as s:
            for row in s.execute(select(LearnedAnswer)).scalars().all():
                s.delete(row)
            s.commit()
    _wipe(); yield; _wipe()


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


@pytest.fixture
def profile():
    return Profile.model_validate({
        # The schema field is `name`, not `full_name` — getting this wrong
        # silently yields an empty first/last name and a fill that looks
        # like it worked. (Same trap as the doctor fix in 0a6d50b.)
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "+91 90000 00000",
        "location": "Bengaluru, India",
        "linkedin": "https://linkedin.com/in/ada",
        "github": "https://github.com/ada",
        "skills": ["Python", "RAG", "MongoDB"],
    })


def _goto(page, name):
    page.goto((FIXTURES / name).as_uri(), wait_until="domcontentloaded")


def _run(page, profile, **kw):
    kw.setdefault("autofill_sensitive", False)
    kw.setdefault("use_llm", False)  # offline: profile + memory only
    return assist(page, page.url, profile, None, **kw)


# --- it fills what it honestly can ----------------------------------------


def test_it_fills_the_ordinary_fields_from_your_profile(page, profile):
    _goto(page, "application_form.html")
    result = _run(page, profile)
    assert result.form_found is True
    assert result.fields_seen > 0
    filled = {label.lower(): value for label, value in result.filled}
    assert any("ada" in v.lower() for v in filled.values()), result.filled
    assert result.coverage > 0


def test_the_values_actually_land_in_the_page_not_just_the_report(page, profile):
    """A report of what was filled is worthless if the DOM disagrees."""
    _goto(page, "application_form.html")
    _run(page, profile)
    assert page.input_value("#first_name") != ""
    assert "@" in page.input_value("#email")


def test_it_works_on_a_custom_react_style_form_with_no_native_select(page, profile):
    """The reason this mode reaches sites `apply` cannot: filling reads the
    accessibility tree, so it does not care which ATS built the page."""
    _goto(page, "custom_controls_page.html")
    result = _run(page, profile)
    assert result.form_found is True
    assert len(result.filled) >= 1, result.left_for_you


# --- it refuses exactly what it should ------------------------------------


def test_a_sensitive_question_is_left_for_you_when_unanswered(page, profile):
    _goto(page, "conditional_fields_page.html")
    result = _run(page, profile)
    reasons = {label: why for label, why in result.left_for_you}
    sensitive = [l for l in reasons if "authorized to work" in l.lower()]
    assert sensitive, reasons
    assert reasons[sensitive[0]] == REASON_SENSITIVE_UNANSWERED


def test_the_reason_tells_you_what_to_actually_do(page, profile):
    """"Not filled" is useless. "Run jobbot setup" is actionable."""
    _goto(page, "conditional_fields_page.html")
    result = _run(page, profile)
    whys = [why for _, why in result.left_for_you]
    assert any("jobbot setup" in w for w in whys), whys


def test_answering_it_in_setup_changes_the_reason_not_the_rule(page, profile):
    """With the flag off, a question you *have* answered is still withheld
    — but for a different reason, and the report must say which."""
    save_answer(by_key("work_authorization"), "Yes")
    _goto(page, "conditional_fields_page.html")
    result = _run(page, profile, autofill_sensitive=False)
    reasons = {l: w for l, w in result.left_for_you}
    sensitive = [l for l in reasons if "authorized to work" in l.lower()]
    assert sensitive
    assert reasons[sensitive[0]] == REASON_SENSITIVE_LOCKED


def test_with_setup_answered_and_the_flag_on_it_fills(page, profile):
    save_answer(by_key("work_authorization"), "Yes")
    _goto(page, "conditional_fields_page.html")
    result = _run(page, profile, autofill_sensitive=True)
    filled = [l for l, _ in result.filled]
    assert any("authorized to work" in l.lower() for l in filled), result.left_for_you


def test_nothing_is_ever_submitted(page, profile):
    """The division of labour that makes this mode work everywhere: the
    agent does not press Submit, so no per-ATS submit selector is needed."""
    _goto(page, "application_form.html")
    before = page.url
    _run(page, profile)
    assert page.url == before


# --- walls and logins are handed back, not worked around ------------------


def test_a_captcha_stops_and_says_so(page, profile):
    _goto(page, "captcha_page.html")
    result = _run(page, profile)
    assert result.blocked
    assert result.form_found is False
    assert any("around one" in n for n in result.notes)


def test_a_login_page_asks_you_to_sign_in(page, profile):
    _goto(page, "login_page.html")
    result = _run(page, profile)
    assert result.needs_login is True
    assert any("Sign in" in n for n in result.notes)


def test_a_page_with_no_form_says_what_to_try(page, profile):
    _goto(page, "apply_entry_page.html")
    result = _run(page, profile)
    assert result.form_found is False
    assert any("Apply" in n for n in result.notes)


# --- it survives the things that actually break at 11pm -------------------


def test_an_llm_outage_still_fills_from_your_profile(page, profile, monkeypatch):
    """A dead LLM quota must degrade the fill, not abort the application."""
    import jobbot.assist as assist_mod

    def boom(*a, **k):
        raise RuntimeError("daily quota exceeded")

    monkeypatch.setattr(assist_mod, "build_fill_plan", boom)
    _goto(page, "application_form.html")
    result = assist(page, page.url, profile, None,
                    autofill_sensitive=False, use_llm=True)
    assert result.form_found is True
    assert len(result.filled) >= 1
    assert any("LLM unavailable" in n for n in result.notes)


# --- multi-step applications (Workday, Darwinbox, Keka) -------------------


def test_it_fills_a_workday_shaped_page_with_no_form_element(page, profile):
    """The blocker that kept Workday out entirely: no <form> anywhere."""
    page.set_content("""
      <div data-automation-id="jobApplication">
        <div><label for="fn">First Name</label><input id="fn"></div>
        <div><label for="ln">Last Name</label><input id="ln"></div>
        <div><label for="em">Email Address</label><input id="em" type="email"></div>
        <div><label for="ph">Phone Number</label><input id="ph" type="tel"></div>
      </div>
    """)
    result = assist(page, "https://acme.wd5.myworkdayjobs.com/x", profile, None,
                    autofill_sensitive=False, use_llm=False)
    assert result.form_found is True
    assert len(result.filled) >= 2, result.left_for_you
    assert page.input_value("#em") != ""


def test_a_step_change_without_a_url_change_is_detected(page):
    """Workday advances steps without navigating, so watching the URL
    alone would never notice. The signature includes visible labels."""
    from jobbot.assist import page_signature

    page.set_content("<div><label for=a>First Name</label><input id=a></div>")
    step1 = page_signature(page)
    page.set_content("<div><label for=b>Work Experience</label><input id=b></div>")
    step2 = page_signature(page)
    assert step1 and step2 and step1 != step2


def test_the_same_step_keeps_the_same_signature(page):
    """Otherwise the watcher would re-fill the same page forever."""
    from jobbot.assist import page_signature

    page.set_content("<div><label for=a>First Name</label><input id=a></div>")
    assert page_signature(page) == page_signature(page)


def test_the_signature_survives_a_closed_page(page):
    """The watch loop ends on browser close; it must not raise there."""
    from jobbot.assist import page_signature

    page.set_content("<div><label for=a>Name</label><input id=a></div>")
    page.close()
    assert page_signature(page) == ""

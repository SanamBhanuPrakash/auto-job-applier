"""Page classification against real pages.

The property under test is not "classifies everything correctly" — it is
**never guesses to keep moving**. An agent that decides an unrecognized page
is "probably the application form" will start typing personal data into an
arbitrary page, so UNKNOWN must survive rather than be coerced.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.agent.observation import BrowserObservation, Control, Detail, observe
from jobbot.agent.page_classify import (
    PageState,
    classify,
    requires_auth,
    requires_stop,
)
from jobbot.submit.form_scan import find_target_frame

FIXTURES = Path(__file__).parent / "fixtures"
_CHROME = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            b = pw.chromium.launch(headless=True, executable_path=_CHROME)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not installed: {exc}")
        yield b
        b.close()


def _classify_fixture(browser, fixture: str, *, use_frame: bool = False):
    page = browser.new_page()
    page.goto((FIXTURES / fixture).as_uri())
    ctx = find_target_frame(page, "", timeout_ms=2000) if use_frame else None
    result = classify(observe(page, ctx, detail=Detail.ARIA))
    page.close()
    return result


# --- positive identification ----------------------------------------------


def test_application_form_is_recognized(browser):
    c = _classify_fixture(browser, "application_form.html")
    assert c.state is PageState.APPLICATION_FORM
    assert c.is_confident


def test_confirmation_page_is_recognized(browser):
    c = _classify_fixture(browser, "confirmation_page.html")
    assert c.state is PageState.SUBMISSION_CONFIRMATION


def test_login_page_is_recognized_not_mistaken_for_a_form(browser):
    """A login page has a form and inputs; classifying it as an application
    form would make the agent type a resume into a password box."""
    c = _classify_fixture(browser, "login_page.html")
    assert c.state is PageState.LOGIN
    assert requires_auth(c.state)


def test_otp_page_is_recognized(browser):
    c = _classify_fixture(browser, "otp_page.html")
    assert c.state is PageState.OTP
    assert requires_auth(c.state)


def test_already_applied_is_recognized(browser):
    """Missing this is a direct route to a duplicate application."""
    c = _classify_fixture(browser, "already_applied_page.html")
    assert c.state is PageState.ALREADY_APPLIED


def test_application_entry_page_is_recognized(browser):
    c = _classify_fixture(browser, "apply_entry_page.html")
    assert c.state is PageState.APPLICATION_ENTRY


def test_iframe_embedded_form_is_recognized_through_the_frame(browser):
    c = _classify_fixture(browser, "careers_page_with_iframe.html", use_frame=True)
    assert c.state is PageState.APPLICATION_FORM


# --- blocking takes precedence --------------------------------------------


def test_captcha_page_is_blocking(browser):
    c = _classify_fixture(browser, "captcha_page.html")
    assert c.state is PageState.CAPTCHA
    assert requires_stop(c.state)


def test_captcha_beats_a_confirmation_banner_on_the_same_page(browser):
    """A page claiming success while showing a CAPTCHA is blocked, not a
    submission — same precedence rule as submit/verify.py."""
    page = browser.new_page()
    page.set_content(
        "<body><h1>Thank you for applying</h1>"
        "<div class='g-recaptcha' data-sitekey='x'></div>"
        "<p>Please verify you are human</p></body>"
    )
    c = classify(observe(page, detail=Detail.ARIA))
    page.close()
    assert c.state is PageState.CAPTCHA
    assert requires_stop(c.state)


def test_bot_detection_is_recognized(browser):
    page = browser.new_page()
    page.set_content("<body><h1>Access denied</h1><p>Unusual traffic detected from your network.</p></body>")
    c = classify(observe(page, detail=Detail.CONTROLS))
    page.close()
    assert c.state in (PageState.BOT_DETECTION, PageState.ACCESS_DENIED)
    assert requires_stop(c.state)


# --- UNKNOWN is preserved --------------------------------------------------


def test_unrelated_page_is_unknown_not_forced_into_a_guess(browser):
    c = _classify_fixture(browser, "unknown_page.html")
    assert c.state is PageState.UNKNOWN
    assert not c.is_confident


def test_empty_observation_is_unknown():
    c = classify(BrowserObservation())
    assert c.state is PageState.UNKNOWN
    assert c.confidence == 0.0


def test_unknown_reports_what_it_was_torn_between():
    """So an escalation starts from the runner-ups instead of nothing."""
    obs = BrowserObservation(
        url="https://acme.example/x",
        title="Acme",
        visible_text="create an account to continue",
        has_form=True,
        controls=[Control(ref="1", role="textbox", name="Password")],
    )
    c = classify(obs)
    if c.state is PageState.UNKNOWN:
        assert c.alternatives, "UNKNOWN must carry its candidate alternatives"


def test_degraded_observation_lowers_confidence():
    """Perception that partly failed cannot support a confident answer."""
    base = dict(
        url="https://acme.example/apply",
        title="Apply",
        visible_text="Thank you for applying! Your application has been received.",
        has_form=False,
    )
    clean = classify(BrowserObservation(**base))
    degraded = classify(BrowserObservation(**base, degraded=True))

    assert degraded.confidence < clean.confidence
    assert "observation degraded" in degraded.evidence


def test_prompt_injection_text_does_not_change_the_classification(browser):
    """Page text is data. A page instructing the agent to treat itself as
    submitted must still classify on its actual structure."""
    c = _classify_fixture(browser, "injection_job_page.html")
    assert c.state is not PageState.SUBMISSION_CONFIRMATION
    assert c.state is PageState.APPLICATION_FORM


# --- helpers ---------------------------------------------------------------


def test_requires_stop_only_for_blocking_states():
    assert requires_stop(PageState.CAPTCHA)
    assert requires_stop(PageState.BOT_DETECTION)
    assert not requires_stop(PageState.APPLICATION_FORM)
    assert not requires_stop(PageState.UNKNOWN), "unknown is investigated, not fatal"


def test_requires_auth_covers_every_auth_state():
    for state in (PageState.LOGIN, PageState.SIGNUP, PageState.SSO, PageState.OTP,
                  PageState.EMAIL_VERIFICATION, PageState.SESSION_EXPIRED):
        assert requires_auth(state)
    assert not requires_auth(PageState.APPLICATION_FORM)


def test_classification_serializes_for_tracing():
    c = classify(BrowserObservation(url="https://x/y", visible_text="thank you for applying"))
    d = c.to_dict()
    assert set(d) == {"state", "confidence", "evidence", "alternatives"}
    assert isinstance(d["state"], str)

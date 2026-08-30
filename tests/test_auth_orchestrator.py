"""AuthOrchestrator: detection, refusals, and verified success (§25-§32).

The interesting assertions are the refusals. Most of what a robust auth
layer does is decline to do things.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.agent.observation import BrowserObservation, Control, Detail, observe
from jobbot.auth.credentials import Credential, CredentialStore, Secret
from jobbot.auth.detect import detect_auth_state
from jobbot.auth.orchestrator import AuthOrchestrator, VerificationChannel
from jobbot.auth.states import (
    HUMAN_CAN_RESOLVE,
    NEVER_AUTOMATED,
    AuthOutcome,
    AuthState,
    VerificationChannelState,
)

FIXTURES = Path(__file__).parent / "fixtures"
_CHROME = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None
PW = "correct-horse-battery-staple"


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


class _Store(CredentialStore):
    def get(self, domain_or_url):
        return Credential("acme.example", "ada@example.com", Secret(PW))

    def has(self, domain_or_url):
        return True


def _orch(**kw):
    return AuthOrchestrator(_Store(), **kw)


def _goto(page, name):
    page.goto((FIXTURES / name).as_uri(), wait_until="domcontentloaded")
    return page


# --- detection -------------------------------------------------------------


@pytest.mark.parametrize("fixture,expected", [
    ("login_page.html", AuthState.LOGIN_REQUIRED),
    ("signup_page.html", AuthState.SIGNUP_REQUIRED),
    ("sso_page.html", AuthState.SSO_REQUIRED),
    ("otp_page.html", AuthState.OTP_REQUIRED),
    ("email_verification_page.html", AuthState.EMAIL_VERIFICATION_REQUIRED),
    ("session_expired_page.html", AuthState.SESSION_EXPIRED),
    ("account_locked_page.html", AuthState.ACCOUNT_LOCKED),
    ("signed_in_page.html", AuthState.ALREADY_AUTHENTICATED),
    ("captcha_page.html", AuthState.CAPTCHA),
    ("application_form.html", AuthState.NOT_AUTH),
])
def test_auth_state_is_detected_from_the_live_page(page, fixture, expected):
    _goto(page, fixture)
    state, evidence = detect_auth_state(observe(page, None, detail=Detail.ARIA))
    assert state is expected, f"{fixture}: {evidence}"


def test_a_wall_outranks_a_login_form_on_the_same_page():
    """Mislabelling a challenge page as LOGIN_REQUIRED would send us to
    type a password into it."""
    obs = BrowserObservation(
        visible_text="Please verify you are human to continue",
        controls=[Control(ref="1", role="textbox", name="Email"),
                  Control(ref="2", role="textbox", name="Password")],
    )
    state, _ = detect_auth_state(obs)
    assert state is AuthState.CAPTCHA


# --- refusals --------------------------------------------------------------


def test_a_captcha_is_never_worked_around(page):
    _goto(page, "captcha_page.html")
    result = _orch().ensure_authenticated(page, url="https://acme.example/")
    assert result.outcome is AuthOutcome.BLOCKED
    assert result.ok is False
    assert "boundary" in result.reason


def test_third_party_sso_is_refused_not_driven(page):
    """Driving a Google/Microsoft sign-in uses the candidate's primary
    identity. The blast radius is their whole account."""
    _goto(page, "sso_page.html")
    result = _orch().ensure_authenticated(page, url="https://acme.example/")
    assert result.outcome is AuthOutcome.REFUSED
    assert result.resumable_by_human is True


def test_a_locked_account_is_not_retried(page):
    _goto(page, "account_locked_page.html")
    result = _orch().ensure_authenticated(page, url="https://acme.example/")
    assert result.ok is False
    assert result.outcome in (AuthOutcome.REFUSED, AuthOutcome.BLOCKED)


def test_signup_is_off_by_default(page):
    _goto(page, "signup_page.html")
    result = _orch().ensure_authenticated(page, url="https://acme.example/")
    assert result.outcome is AuthOutcome.HUMAN_REQUIRED
    assert result.ok is False


def test_signup_is_still_refused_on_a_domain_not_in_the_allowlist(page):
    _goto(page, "signup_page.html")
    orch = _orch(allow_signup=True, allowed_signup_domains=("other.example",))
    result = orch.ensure_authenticated(page, url="https://acme.example/")
    assert result.outcome is AuthOutcome.REFUSED


def test_every_never_automated_state_is_refused_or_blocked(page):
    """No path through the orchestrator returns success for one of these."""
    assert AuthState.CAPTCHA in NEVER_AUTOMATED
    assert AuthState.SSO_REQUIRED in NEVER_AUTOMATED
    assert AuthState.ACCOUNT_LOCKED in NEVER_AUTOMATED
    assert AuthState.BOT_DETECTION in NEVER_AUTOMATED
    assert HUMAN_CAN_RESOLVE & NEVER_AUTOMATED, "some walls a person can clear"


# --- second factors --------------------------------------------------------


def test_an_unattended_run_never_guesses_a_one_time_code(page):
    _goto(page, "otp_page.html")
    result = _orch().ensure_authenticated(page, url="https://acme.example/")
    assert result.outcome is AuthOutcome.HUMAN_REQUIRED
    assert result.resumable_by_human is True
    assert "no code is guessed" in result.reason or "supply the code" in result.reason


def test_the_default_verification_channel_is_unavailable():
    """There is no legitimate way to obtain someone's OTP unattended, so
    the default must be "cannot", not "try something"."""
    assert VerificationChannel().state() is VerificationChannelState.UNAVAILABLE
    assert VerificationChannel(prompt=lambda d: "123456").state() is (
        VerificationChannelState.AVAILABLE)


def test_email_verification_hands_over_to_a_person(page):
    _goto(page, "email_verification_page.html")
    result = _orch().ensure_authenticated(page, url="https://acme.example/")
    assert result.ok is False
    assert result.resumable_by_human is True


# --- success is verified, never inferred ----------------------------------


def test_a_static_login_page_never_reports_success(page):
    """§28: do not infer success because the click succeeded. Nothing here
    can actually sign anyone in, so claiming AUTHENTICATED would be the
    false-success failure this project refuses everywhere."""
    _goto(page, "login_page.html")
    result = _orch().ensure_authenticated(page, url="https://acme.example/")
    assert result.outcome is not AuthOutcome.AUTHENTICATED
    assert result.verified_session is False


def test_an_existing_session_is_recognised_without_using_a_credential(page):
    _goto(page, "signed_in_page.html")
    result = _orch().ensure_authenticated(page, url="https://acme.example/")
    assert result.outcome is AuthOutcome.ALREADY_AUTHENTICATED
    assert result.verified_session is True


def test_a_page_that_never_asked_is_not_a_verified_session(page):
    """Regression: NOT_AUTH used to return ALREADY_AUTHENTICATED, which
    `record_attempt` then wrote down as a verified session — making
    `session_is_fresh` lie for the next 12 hours. An error page and a 404
    also fail to ask (failures §14)."""
    _goto(page, "application_form.html")
    result = _orch().ensure_authenticated(page, url="https://acme.example/")
    assert result.outcome is AuthOutcome.NOT_REQUIRED
    assert result.ok is True, "the caller may proceed"
    assert result.verified_session is False, "but this is not evidence of a session"


def test_an_unreadable_page_yields_indeterminate_not_success():
    """Regression: a degraded observation has no auth signals *because
    perception failed*, and that was being read as "nothing is asking, so
    we must be fine" — carrying on unauthenticated (failures §14)."""
    result = _orch().ensure_authenticated(None, url="https://acme.example/")
    assert result.outcome is AuthOutcome.INDETERMINATE
    assert result.ok is False
    assert result.verified_session is False


# --- nothing leaks ---------------------------------------------------------


@pytest.mark.parametrize("fixture", [
    "login_page.html", "otp_page.html", "sso_page.html", "captcha_page.html",
    "session_expired_page.html", "signup_page.html",
])
def test_no_auth_result_ever_serialises_a_credential(page, fixture):
    """`to_dict` is what gets persisted to the ledger and shown to a
    model. §26 says the model receives only safe status information."""
    _goto(page, fixture)
    result = _orch().ensure_authenticated(page, url="https://acme.example/")
    blob = str(result.to_dict())
    assert PW not in blob
    assert "correct-horse" not in blob


def test_a_failing_login_does_not_leak_the_password_into_the_reason(page, caplog):
    import logging

    _goto(page, "login_page.html")
    with caplog.at_level(logging.DEBUG):
        result = _orch().ensure_authenticated(page, url="https://acme.example/")
    assert PW not in result.reason
    assert PW not in caplog.text

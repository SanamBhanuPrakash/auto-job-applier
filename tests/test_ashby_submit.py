"""The Ashby submission handler.

Ashby differs from Greenhouse and Lever in two ways that both had to be
established against the live site rather than guessed, and both of which
would silently break the deterministic path if got wrong.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.submit import ashby
from jobbot.submit.ats_detect import detect_ats
from jobbot.submit.base import _ATS_MODULES

FIXTURES = Path(__file__).parent / "fixtures"
_CHROME = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None


# --- routing ---------------------------------------------------------------


def test_ashby_urls_route_to_the_ashby_handler():
    assert detect_ats("https://jobs.ashbyhq.com/atlan/254c1250") == "ashby"
    assert "ashby" in _ATS_MODULES


def test_the_other_ats_routing_is_unchanged():
    assert detect_ats("https://boards.greenhouse.io/stripe/jobs/1") == "greenhouse"
    assert detect_ats("https://jobs.lever.co/porter/abc") == "lever"
    assert detect_ats("https://acme.wd5.myworkdayjobs.com/x") == ""


def test_every_registered_handler_exposes_the_contract_base_py_calls():
    for name, module in _ATS_MODULES.items():
        assert isinstance(module.ATS_HINT, str) and module.ATS_HINT, name
        assert isinstance(module.SUBMIT_SELECTOR, str) and module.SUBMIT_SELECTOR, name
        assert callable(module.settle), name
        assert callable(module.click_submit), name


# --- the apply URL is not the posting URL ---------------------------------


def test_the_application_form_lives_at_a_different_url_from_the_posting():
    """Ashby's own API returns jobUrl and applyUrl as different values;
    the posting page has only an "Apply" button, no form. Opening the
    posting and looking for a form finds nothing."""
    posting = "https://jobs.ashbyhq.com/atlan/254c1250-953b-4323-8d18-9fe5e41d8d7d"
    assert ashby.apply_url(posting) == posting + "/application"


def test_an_apply_url_is_not_rewritten_twice():
    url = "https://jobs.ashbyhq.com/atlan/abc/application"
    assert ashby.apply_url(url) == url
    assert ashby.apply_url(url + "/") == url + "/"


def test_a_trailing_slash_on_the_posting_is_handled():
    assert ashby.apply_url("https://jobs.ashbyhq.com/atlan/abc/") == (
        "https://jobs.ashbyhq.com/atlan/abc/application")


@pytest.mark.parametrize("url", [
    "https://boards.greenhouse.io/stripe/jobs/1",
    "https://jobs.lever.co/porter/abc",
    "", "not a url",
])
def test_non_ashby_urls_pass_through_untouched(url):
    """base.py calls this unconditionally, so it must be a no-op for
    every other ATS."""
    assert ashby.apply_url(url) == url


# --- hydration -------------------------------------------------------------


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


def test_settle_waits_for_a_form_that_appears_only_after_hydration(page):
    """The real behaviour being defended against: fetching an Ashby apply
    page returns 65KB of HTML containing zero <form> and zero <input> —
    it is a React app that builds the form client-side. Greenhouse's fixed
    500ms settle would race it."""
    page.set_content("""
        <html><body><div id="root">Loading...</div>
        <script>
          setTimeout(() => {
            document.getElementById('root').innerHTML =
              '<form><label for="n">Name</label><input id="n"></form>';
          }, 900);
        </script></body></html>
    """)
    assert page.locator("form input").count() == 0, "form must not exist yet"
    ashby.settle(page)
    assert page.locator("form input").count() == 1, "settle should have waited for it"


def test_settle_returns_rather_than_raising_when_no_form_ever_appears(page):
    """A timeout here must not mask which stage failed — find_target_frame
    produces the real diagnostic."""
    page.set_content("<html><body><p>This posting has closed.</p></body></html>")
    ashby._HYDRATION_TIMEOUT_MS, original = 700, ashby._HYDRATION_TIMEOUT_MS
    try:
        ashby.settle(page)  # must not raise
    finally:
        ashby._HYDRATION_TIMEOUT_MS = original


def test_settle_is_immediate_when_the_form_is_already_there(page):
    page.set_content("<form><input id='a'></form>")
    ashby.settle(page)
    assert page.locator("form input").count() == 1


# --- submit ----------------------------------------------------------------


def test_click_submit_finds_the_button_by_its_accessible_name(page):
    """`onsubmit="return false"` matters: a real submit button navigates,
    and an assertion read after navigation measures the new page rather
    than whether the click landed."""
    page.set_content("""
        <form onsubmit="return false">
          <input id="a">
          <button type="button">Cancel</button>
          <button type="submit" onclick="window.__clicked='Submit Application'">Submit Application</button>
        </form>
    """)
    ashby.click_submit(page)
    assert page.evaluate("window.__clicked") == "Submit Application"


def test_click_submit_does_not_hit_cancel_or_any_other_button(page):
    """The selector must not be so loose that it picks the first button."""
    page.set_content("""
        <form onsubmit="return false">
          <button type="button" onclick="window.__clicked='Cancel'">Cancel</button>
          <button type="button" onclick="window.__clicked='Save draft'">Save draft</button>
          <button type="submit" onclick="window.__clicked='Submit Application'">Submit Application</button>
        </form>
    """)
    ashby.click_submit(page)
    assert page.evaluate("window.__clicked") == "Submit Application"


def test_click_submit_handles_a_board_that_says_only_submit(page):
    """Wording differs between Ashby boards."""
    page.set_content("""
        <form onsubmit="return false"><input id="a">
        <button type="submit" onclick="window.__clicked='Submit'">Submit</button></form>
    """)
    ashby.click_submit(page)
    assert page.evaluate("window.__clicked") == "Submit"

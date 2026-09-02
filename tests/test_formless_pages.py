"""Finding the application form on pages that have no <form> element.

The blocker this fixes, established live: fetching a Workday careers page
returns a 6.5KB SPA shell with zero `<form>` and zero `<input>`. After
hydration Workday builds its application out of divs carrying
`data-automation-id`; the inputs never sit inside a form element.
Darwinbox, Keka and most React career sites are the same shape.

`scan_form` could always read those pages — it queries the whole document,
not a form subtree. Only `find_target_frame` said no, and it rejected them
outright. That single gate is why Workday-hosted jobs, which is most large
Indian employers, could not be filled at all.
"""
from __future__ import annotations

import os

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.submit.form_scan import find_target_frame, scan_form

_CHROME = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None

#: A Workday-shaped page: no <form>, controls are divs with ARIA roles and
#: data-automation-id, exactly as Workday renders them.
WORKDAY_SHAPED = """
<html><body>
  <div data-automation-id="jobApplication">
    <div><label for="fn">First Name</label><input id="fn" data-automation-id="legalNameSection_firstName"></div>
    <div><label for="ln">Last Name</label><input id="ln" data-automation-id="legalNameSection_lastName"></div>
    <div><label for="em">Email Address</label><input id="em" type="email"></div>
    <div><label for="ph">Phone Number</label><input id="ph" type="tel"></div>
    <div><span id="cl">Country</span>
      <div role="combobox" aria-labelledby="cl" tabindex="0">Select One</div></div>
    <button data-automation-id="bottom-navigation-next-button">Save and Continue</button>
  </div>
</body></html>
"""


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


def test_a_workday_shaped_page_is_found_despite_having_no_form(page):
    """The whole point. Before this, find_target_frame raised here."""
    page.set_content(WORKDAY_SHAPED)
    assert page.locator("form").count() == 0, "fixture must have no <form>"
    ctx = find_target_frame(page, timeout_ms=3000)
    assert ctx is page


def test_and_the_scanner_then_reads_its_fields(page):
    """Proof the gate was the only thing in the way — the scanner was
    always capable of reading these pages."""
    page.set_content(WORKDAY_SHAPED)
    ctx = find_target_frame(page, timeout_ms=3000)
    fields = scan_form(ctx)
    labels = " ".join(f.label.lower() for f in fields)
    assert len(fields) >= 4, [f.label for f in fields]
    assert "first name" in labels and "email" in labels


def test_a_real_form_is_still_preferred_over_the_fallback(page):
    """Zero regression for Greenhouse/Lever/Ashby: the <form> branch is
    checked first and its behaviour is untouched."""
    page.set_content("""
      <div><input id="a"><input id="b"><input id="c"><input id="d"></div>
      <form id="real"><input id="e"><input id="f"><input id="g"></form>
    """)
    ctx = find_target_frame(page, timeout_ms=3000)
    assert ctx is page
    assert page.locator("form").count() == 1


def test_a_page_with_only_a_search_box_is_still_rejected(page):
    """The fallback must not turn every page into an application form."""
    page.set_content("""
      <html><body><h1>Careers</h1>
      <input id="q" placeholder="Search jobs">
      <button>Search</button></body></html>
    """)
    with pytest.raises(TimeoutError):
        find_target_frame(page, timeout_ms=1200)


def test_the_error_says_what_it_actually_saw(page):
    """"No form found" gave no way to tell "page still loading" from
    "behind a login" from "not a form at all"."""
    page.set_content("<html><body><input id='q'></body></html>")
    with pytest.raises(TimeoutError, match="richest context had only 1"):
        find_target_frame(page, timeout_ms=1200)


def test_a_formless_page_inside_an_iframe_is_found(page):
    page.set_content(
        '<iframe srcdoc="'
        '<div><input id=a><input id=b><input id=c></div>"></iframe>'
    )
    ctx = find_target_frame(page, timeout_ms=4000)
    assert ctx is not None


def test_the_threshold_is_tunable_for_a_short_screening_page(page):
    page.set_content("<div><input id='a'><input id='b'></div>")
    with pytest.raises(TimeoutError):
        find_target_frame(page, timeout_ms=900)
    ctx = find_target_frame(page, timeout_ms=3000, min_fillable=2)
    assert ctx is page


def test_hidden_and_button_inputs_do_not_count_toward_the_threshold(page):
    """Otherwise a page of hidden CSRF tokens looks like an application."""
    page.set_content("""
      <div><input type="hidden" name="csrf"><input type="hidden" name="tok">
      <input type="submit" value="Go"><input type="button" value="X"></div>
    """)
    with pytest.raises(TimeoutError):
        find_target_frame(page, timeout_ms=1200)

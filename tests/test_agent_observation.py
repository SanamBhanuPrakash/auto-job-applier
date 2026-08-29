"""Perception layer, driven against real pages in real Chromium.

Two properties matter most and are asserted throughout:

1. **Bounded context.** An observation must stay small regardless of page
   size — dumping the DOM degrades model recall, not just cost
   (docs/research/agent-architecture.md §6).
2. **Never throws.** A page mid-navigation is normal. A partial observation
   flagged `degraded` is actionable; an exception is not.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.agent.observation import (
    MAX_ARIA_CHARS,
    MAX_CONTROLS,
    MAX_VISIBLE_TEXT,
    BrowserObservation,
    Control,
    Detail,
    observe,
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


@pytest.fixture
def page(browser):
    p = browser.new_page()
    yield p
    if not p.is_closed():
        p.close()


def _goto(page, fixture: str):
    page.goto((FIXTURES / fixture).as_uri())
    return page


# --- tiering ---------------------------------------------------------------


def test_metadata_tier_is_cheap_and_omits_rich_content(page):
    _goto(page, "application_form.html")
    obs = observe(page, detail=Detail.METADATA)

    assert obs.url and obs.title
    assert obs.has_form is True
    assert obs.tab_count == 1
    # Nothing expensive should have been gathered.
    assert obs.controls == []
    assert obs.aria_snapshot == ""
    assert obs.dom_excerpt == ""
    assert obs.visible_text == ""


def test_controls_tier_extracts_role_and_accessible_name(page):
    _goto(page, "application_form.html")
    obs = observe(page, detail=Detail.CONTROLS)

    assert obs.controls, "expected interactive controls"
    by_name = {c.name.lower(): c for c in obs.controls}
    assert any("first name" in n for n in by_name)

    # Identified semantically (role + name), not by CSS selector — models
    # handle natural names far better than cryptic identifiers.
    first = next(c for c in obs.controls if "first name" in c.name.lower())
    assert first.role == "textbox"
    assert first.required is True
    assert first.ref  # a stable handle exists

    assert obs.aria_snapshot == "", "aria is a higher tier and must not be gathered here"


def test_aria_tier_is_far_smaller_than_raw_dom(page):
    """The central reason for preferring the accessibility tree."""
    _goto(page, "application_form.html")
    obs = observe(page, detail=Detail.ARIA)

    assert obs.aria_snapshot
    assert "textbox" in obs.aria_snapshot  # roles stated outright
    raw_html_len = len(page.content())
    assert len(obs.aria_snapshot) < raw_html_len


def test_dom_tier_is_focused_on_the_form_not_whole_document(page):
    _goto(page, "application_form.html")
    obs = observe(page, detail=Detail.DOM)
    assert obs.dom_excerpt
    assert len(obs.dom_excerpt) <= 8000


def test_screenshot_tier_only_when_a_path_is_given(page, tmp_path):
    _goto(page, "application_form.html")
    shot = tmp_path / "s.png"
    obs = observe(page, detail=Detail.SCREENSHOT, screenshot_path=str(shot))
    assert obs.screenshot_path == str(shot)
    assert shot.exists()

    obs2 = observe(page, detail=Detail.SCREENSHOT)
    assert obs2.screenshot_path == "", "no path given => no screenshot taken"


# --- bounded context -------------------------------------------------------


def test_observation_is_bounded_on_a_huge_page(page):
    """A 3,000-control page must not produce a 3,000-control observation."""
    page.set_content(
        "<body>" + "".join(
            f'<label for="f{i}">Field number {i}</label><input id="f{i}">' for i in range(3000)
        ) + "<p>" + ("filler text " * 5000) + "</p></body>"
    )
    obs = observe(page, detail=Detail.ARIA)

    assert len(obs.controls) <= MAX_CONTROLS
    assert len(obs.visible_text) <= MAX_VISIBLE_TEXT
    assert len(obs.aria_snapshot) <= MAX_ARIA_CHARS
    assert any("truncated" in n for n in obs.notes)
    # Sanity: the whole thing stays in a sane token range.
    assert obs.approx_tokens() < 10_000


# --- resilience ------------------------------------------------------------


def test_observe_never_throws_on_a_closed_page(page):
    _goto(page, "application_form.html")
    page.close()
    obs = observe(page, detail=Detail.ARIA)  # must not raise
    assert obs.degraded is True
    assert obs.notes


def test_degraded_observation_still_returns_usable_object(browser):
    p = browser.new_page()
    p.goto((FIXTURES / "application_form.html").as_uri())
    p.close()
    obs = observe(p, detail=Detail.CONTROLS)
    assert isinstance(obs, BrowserObservation)
    assert obs.degraded


# --- frames / iframes ------------------------------------------------------


def test_observes_the_frame_holding_the_form_not_the_shell(page):
    """The common real case: employer's branded page, form in an iframe."""
    _goto(page, "careers_page_with_iframe.html")
    assert page.locator("form").count() == 0  # nothing at the top level

    form_ctx = find_target_frame(page, "", timeout_ms=3000)
    obs = observe(page, form_ctx, detail=Detail.CONTROLS)

    assert obs.frame_count >= 2
    assert obs.has_form is True, "form found via the iframe context"
    assert any("first name" in c.name.lower() for c in obs.controls)
    assert obs.active_frame_url != obs.url


# --- validation messages ---------------------------------------------------


def test_validation_messages_are_captured(page):
    _goto(page, "validation_error_page.html")
    obs = observe(page, detail=Detail.CONTROLS)
    joined = " ".join(obs.validation_messages).lower()
    assert "required" in joined or "problem" in joined


# --- state digest ----------------------------------------------------------


def test_digest_is_stable_across_identical_observations(page):
    _goto(page, "application_form.html")
    a = observe(page, detail=Detail.CONTROLS)
    b = observe(page, detail=Detail.CONTROLS)
    assert a.state_digest() == b.state_digest()


def test_digest_changes_when_a_field_is_filled(page):
    """Typing is progress even though the control set is unchanged — if the
    digest missed this, loop detection would abort a working fill."""
    _goto(page, "application_form.html")
    before = observe(page, detail=Detail.CONTROLS).state_digest()
    page.locator("#first_name").fill("Ada")
    after = observe(page, detail=Detail.CONTROLS).state_digest()
    assert before != after


def test_digest_changes_on_navigation(page):
    _goto(page, "application_form.html")
    a = observe(page, detail=Detail.CONTROLS).state_digest()
    _goto(page, "confirmation_page.html")
    b = observe(page, detail=Detail.CONTROLS).state_digest()
    assert a != b


def test_digest_ignores_fragment_only_changes(page):
    """A same-page anchor is not navigation and must not read as progress."""
    _goto(page, "application_form.html")
    a = observe(page, detail=Detail.CONTROLS).state_digest()
    page.evaluate("() => { window.location.hash = '#section2'; }")
    b = observe(page, detail=Detail.CONTROLS).state_digest()
    assert a == b


def test_digest_reflects_validation_messages_appearing(page):
    page.set_content('<body><form><input id="a"></form></body>')
    a = observe(page, detail=Detail.CONTROLS).state_digest()
    page.set_content(
        '<body><form><input id="a"></form>'
        '<div role="alert">This field is required.</div></body>'
    )
    b = observe(page, detail=Detail.CONTROLS).state_digest()
    assert a != b, "a new validation error is a meaningful state change"


# --- agent payload ---------------------------------------------------------


def test_agent_dict_keeps_page_content_in_clearly_named_data_fields(page):
    """Page text must arrive as data the prompt layer can fence, never
    merged into instruction (browser-agent-failures.md §9)."""
    _goto(page, "injection_job_page.html")
    obs = observe(page, detail=Detail.CONTROLS)
    payload = obs.to_agent_dict()

    assert "visible_text" in payload
    assert "Ignore all previous instructions" in payload["visible_text"]
    # It is confined to a named data field, not promoted anywhere else.
    assert set(payload) <= {
        "url", "title", "tabs", "active_tab", "frames", "has_form", "dialog_open",
        "controls", "validation_messages", "visible_text", "aria_snapshot",
        "dom_excerpt", "degraded", "notes",
    }


def test_agent_dict_can_omit_text_entirely(page):
    _goto(page, "application_form.html")
    obs = observe(page, detail=Detail.CONTROLS)
    assert "visible_text" not in obs.to_agent_dict(include_text=False)


def test_control_to_dict_omits_defaults_to_save_tokens():
    c = Control(ref="o1", role="button", name="Submit")
    assert c.to_dict() == {"ref": "o1", "role": "button", "name": "Submit"}
    c2 = Control(ref="o2", role="textbox", name="Email", value="a@b.c", required=True, enabled=False)
    assert c2.to_dict()["value"] == "a@b.c"
    assert c2.to_dict()["required"] is True
    assert c2.to_dict()["enabled"] is False

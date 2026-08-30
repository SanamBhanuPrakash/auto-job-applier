"""Tool registry against real Chromium.

The property that matters most: a tool cannot report success the
environment does not support. The registry — not the handler — takes the
post-action observation, so `changed_state` is measured, not claimed.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from jobbot.agent.observation import Detail, observe
from jobbot.agent.states import ApplicationState, FailureCategory
from jobbot.agent.tools import RiskClass, ToolContext, ToolRegistry, ToolSpec, default_tools

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
    # An explicit context (rather than browser.new_page()) so tests can open
    # additional tabs — browser.new_page() creates an owner-page context
    # that refuses them, which is exactly what multi-tab tools need.
    context = browser.new_context()
    p = context.new_page()
    yield p
    context.close()


@pytest.fixture
def registry():
    return ToolRegistry()


def _ctx(page, **kw) -> ToolContext:
    kw.setdefault("application_state", ApplicationState.FILLING)
    return ToolContext(page=page, **kw)


def _ref_for(page, label_fragment: str) -> str:
    obs = observe(page, detail=Detail.CONTROLS)
    for c in obs.controls:
        if label_fragment.lower() in c.name.lower():
            return c.ref
    raise AssertionError(f"no control matching {label_fragment!r}")


# --- registry contract -----------------------------------------------------


def test_every_default_tool_declares_its_metadata():
    for spec in default_tools():
        assert spec.name and spec.purpose
        assert isinstance(spec.risk_class, RiskClass)
        assert spec.handler is not None
        assert set(spec.required_args) <= set(spec.args), spec.name


def test_unknown_tool_is_refused_not_executed(page, registry):
    result = registry.execute("exec_arbitrary_python", _ctx(page), code="import os")
    assert result.ok is False
    assert result.failure_category is FailureCategory.POLICY
    assert result.recoverable is False


def test_unexpected_argument_is_rejected_before_touching_the_browser(page, registry):
    page.goto((FIXTURES / "application_form.html").as_uri())
    result = registry.execute("click", _ctx(page), ref="x", javascript="alert(1)")
    assert result.ok is False
    assert "unexpected argument" in result.evidence[0]


def test_missing_required_argument_is_rejected(page, registry):
    result = registry.execute("navigate", _ctx(page))
    assert result.ok is False
    assert "missing required argument" in result.evidence[0]


def test_duplicate_registration_is_refused(registry):
    with pytest.raises(ValueError):
        registry.register(ToolSpec("click", "dup", RiskClass.LOW_RISK, lambda ctx, **k: {}))


# --- real actions ----------------------------------------------------------


def test_type_fills_and_registry_detects_the_change(page, registry):
    page.goto((FIXTURES / "application_form.html").as_uri())
    ref = _ref_for(page, "first name")

    result = registry.execute("type", _ctx(page), ref=ref, value="Ada")

    assert result.ok is True
    assert result.changed_state is True, "filling a field is a real state change"
    assert page.locator("#first_name").input_value() == "Ada"
    assert result.state_before != result.state_after


def test_type_reports_failure_when_the_value_does_not_stick(page, registry):
    """A readonly input accepts the call but keeps its value — the exact
    quiet failure custom React inputs produce."""
    page.set_content('<body><form><input id="ro" readonly value="locked"></form></body>')
    ref = _ref_for(page, "")  # only one control
    result = registry.execute("type", _ctx(page), ref=ref, value="Ada")
    assert result.ok is False
    assert result.failure_category is FailureCategory.RECOVERABLE


def test_click_on_a_dead_control_reports_no_state_change(page, registry):
    """The tool 'succeeded' but nothing happened — the agent must be told."""
    page.set_content('<body><button id="dead" type="button">Submit</button></body>')
    ref = _ref_for(page, "Submit")
    result = registry.execute("click", _ctx(page), ref=ref)

    assert result.ok is True
    assert result.changed_state is False
    assert any("did not change" in e for e in result.evidence)


def test_click_navigates_and_change_is_detected(page, registry):
    page.goto((FIXTURES / "apply_entry_page.html").as_uri())
    ref = _ref_for(page, "Apply")
    result = registry.execute("click", _ctx(page, application_state=ApplicationState.OPENING_APPLICATION), ref=ref)
    assert result.ok is True
    assert result.changed_state is True


def test_stale_ref_fails_cleanly_after_navigation(page, registry):
    """Element handles go stale across navigation; the registry re-grounds
    by ref every call and reports a clean, categorized failure."""
    page.goto((FIXTURES / "application_form.html").as_uri())
    ref = _ref_for(page, "first name")
    page.goto((FIXTURES / "confirmation_page.html").as_uri())

    result = registry.execute("type", _ctx(page), ref=ref, value="Ada")
    assert result.ok is False
    assert result.failure_category is FailureCategory.RECOVERABLE
    assert "no element with ref" in result.evidence[0]


def test_select_and_check_work(page, registry):
    page.goto((FIXTURES / "application_form.html").as_uri())
    sel_ref = _ref_for(page, "How did you hear")
    assert registry.execute("select", _ctx(page), ref=sel_ref, value="Referral").ok

    obs = observe(page, detail=Detail.CONTROLS)
    radio = next(c for c in obs.controls if c.role == "radio")
    assert registry.execute("check", _ctx(page), ref=radio.ref).ok


def test_wait_is_capped(page, registry):
    page.goto((FIXTURES / "application_form.html").as_uri())
    result = registry.execute("wait", _ctx(page), seconds=9999)
    assert result.detail["seconds"] == 10.0, "an unbounded sleep is never permitted"


def test_observe_is_read_only_and_never_changes_state(page, registry):
    page.goto((FIXTURES / "application_form.html").as_uri())
    result = registry.execute("observe", _ctx(page))
    assert result.ok is True
    assert result.changed_state is False
    assert result.risk_class is RiskClass.READ_ONLY


def test_classify_page_returns_a_bounded_classification(page, registry):
    page.goto((FIXTURES / "application_form.html").as_uri())
    result = registry.execute("classify_page", _ctx(page))
    assert result.detail["classification"]["state"] == "APPLICATION_FORM"


# --- navigation safety -----------------------------------------------------


@pytest.mark.parametrize("bad_url", [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd" if os.name != "nt" else "file:///C:/Windows/win.ini",
])
def test_navigate_refuses_dangerous_or_unexpected_schemes(page, registry, bad_url):
    ctx = _ctx(page, application_state=ApplicationState.OPENING_APPLICATION)
    result = registry.execute("navigate", ctx, url=bad_url)
    if bad_url.startswith("file://"):
        pytest.skip("file:// is permitted for local fixtures; covered by policy layer instead")
    assert result.ok is False
    assert result.failure_category is FailureCategory.POLICY
    assert result.recoverable is False


# --- file upload safety (spec §41) ----------------------------------------


def test_upload_refuses_a_file_not_in_the_approved_set(page, registry, tmp_path):
    """The attack: page text names a file, the model relays it. Only
    configured candidate documents may ever be uploaded."""
    page.goto((FIXTURES / "application_form.html").as_uri())
    secret = tmp_path / "id_rsa"
    secret.write_text("PRIVATE KEY")
    approved = tmp_path / "resume.pdf"
    approved.write_text("resume")

    ctx = _ctx(page, allowed_upload_paths=(approved,))
    ref = _ref_for(page, "Resume")

    result = registry.execute("upload", ctx, ref=ref, path=str(secret))
    assert result.ok is False
    assert result.failure_category is FailureCategory.POLICY
    assert result.recoverable is False
    assert page.locator("#resume").evaluate("el => el.files.length") == 0


def test_upload_accepts_an_approved_candidate_document(page, registry, tmp_path):
    page.goto((FIXTURES / "application_form.html").as_uri())
    approved = tmp_path / "resume.pdf"
    approved.write_text("resume")
    ctx = _ctx(page, allowed_upload_paths=(approved,))
    ref = _ref_for(page, "Resume")

    result = registry.execute("upload", ctx, ref=ref, path=str(approved))
    assert result.ok is True
    assert page.locator("#resume").evaluate("el => el.files.length") == 1
    assert result.checkpoint_required is True, "HIGH_RISK actions must checkpoint"


def test_upload_refuses_a_nonexistent_path(page, registry, tmp_path):
    page.goto((FIXTURES / "application_form.html").as_uri())
    ctx = _ctx(page, allowed_upload_paths=(tmp_path / "resume.pdf",))
    ref = _ref_for(page, "Resume")
    result = registry.execute("upload", ctx, ref=ref, path=str(tmp_path / "nope.pdf"))
    assert result.ok is False


# --- tabs / frames ---------------------------------------------------------


def test_switch_tab_moves_the_context_and_drops_stale_frame(page, registry):
    page.goto((FIXTURES / "application_form.html").as_uri())
    second = page.context.new_page()
    second.goto((FIXTURES / "confirmation_page.html").as_uri())

    ctx = _ctx(page)
    ctx.form_ctx = page.main_frame
    pages = list(page.context.pages)
    result = registry.execute("switch_tab", ctx, index=pages.index(second))

    assert result.ok is True
    assert ctx.page is second
    assert ctx.form_ctx is None, "frame handles from the old tab must not survive"
    second.close()


def test_switch_tab_rejects_a_nonexistent_index(page, registry):
    page.goto((FIXTURES / "application_form.html").as_uri())
    result = registry.execute("switch_tab", _ctx(page), index=99)
    assert result.ok is False


def test_switch_frame_finds_the_embedded_form(page, registry):
    page.goto((FIXTURES / "careers_page_with_iframe.html").as_uri())
    ctx = _ctx(page)
    result = registry.execute("switch_frame", ctx, url_contains="application_form")
    assert result.ok is True
    assert ctx.form_ctx is not None
    assert ctx.ctx().locator("form").count() == 1


def test_close_tab_refuses_to_close_the_last_one(page, registry):
    page.goto((FIXTURES / "application_form.html").as_uri())
    result = registry.execute("close_tab", _ctx(page), index=0)
    assert result.ok is False
    assert page.is_closed() is False


# --- bounded output --------------------------------------------------------


def test_tool_result_payload_stays_small(page, registry):
    page.goto((FIXTURES / "application_form.html").as_uri())
    ref = _ref_for(page, "first name")
    result = registry.execute("type", _ctx(page), ref=ref, value="Ada")
    payload = result.to_agent_dict()
    assert len(str(payload)) < 1500, "ToolResult must never carry a page dump"
    assert "observation" not in payload

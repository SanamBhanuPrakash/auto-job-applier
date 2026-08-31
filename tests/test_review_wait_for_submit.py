"""wait_for_submit_or_close replaces the old blocking `console.input()`
confirmation — instead of typing 'yes', you click the real Submit button
on the page yourself and the tool notices. These exercise all three real
outcomes against an actual Chromium page (not mocks): the page navigating
away, the submit button disappearing in place, and the window closing.

No real network/ATS page is needed — the DOM mutations that distinguish
each outcome are scheduled from in-page JavaScript via setTimeout, so the
poll loop (running synchronously in the test) discovers them naturally on
its next tick without any cross-thread Playwright calls.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager

import pytest
from playwright.sync_api import Error as PlaywrightError, sync_playwright
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import jobbot.submit.review as review_module
from jobbot.models import Base, Job
from jobbot.submit.form_scan import FieldSpec

_CHROME_OVERRIDE = os.environ.get("JOBBOT_TEST_CHROMIUM_PATH") or None

_PAGE_HTML = """
<!doctype html><html><body>
<form>
  <input id="name" data-jobbot-id="1" value="">
  <button id="submit-btn" type="button">Submit Application</button>
</form>
</body></html>
"""


class _FakeAtsModule:
    SUBMIT_SELECTOR = "#submit-btn"


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
def isolated_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def fake_session_scope():
        session = Session()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(review_module, "session_scope", fake_session_scope)
    yield Session


def _job() -> Job:
    job = Job(source="greenhouse", external_id="1", company="Acme", title="Engineer", url="https://example.com")
    job.id = 999
    return job


def _name_field() -> FieldSpec:
    return FieldSpec(field_id=1, label="Name", field_type="text")


def test_page_navigating_away_is_detected_as_submitted(browser, isolated_db):
    page = browser.new_page()
    page.set_content(_PAGE_HTML)
    page.evaluate("setTimeout(() => history.pushState({}, '', '#done'), 300)")

    status = review_module.wait_for_submit_or_close(page, page, _FakeAtsModule(), _job(), [_name_field()], poll_interval_s=0.1)

    assert status == "submitted"
    page.close()


def test_submit_button_disappearing_in_place_is_detected_as_submitted(browser, isolated_db):
    """Some ATS forms swap in a "thanks for applying" panel without ever
    changing the URL — the button vanishing is the only signal."""
    page = browser.new_page()
    page.set_content(_PAGE_HTML)
    page.evaluate("setTimeout(() => document.querySelector('#submit-btn').remove(), 300)")

    status = review_module.wait_for_submit_or_close(page, page, _FakeAtsModule(), _job(), [_name_field()], poll_interval_s=0.1)

    assert status == "submitted"
    page.close()


def test_submit_button_not_yet_rendered_is_not_mistaken_for_an_instant_submit(browser, isolated_db):
    """Real bug hit live: checking 'the submit button is absent' without
    first confirming it was ever present meant a button that simply hadn't
    finished rendering yet on the very first poll tick looked exactly like
    an already-completed submit — closing the window and opening the next
    job's within seconds, with nothing actually reviewed or clicked
    (reported live as "the tabs are opening and closing"). The button here
    is absent at first, appears after 300ms, and only then genuinely
    disappears at 600ms — the real submitted signal."""
    page = browser.new_page()
    page.set_content("""
    <!doctype html><html><body>
    <form><input id="name" data-jobbot-id="1" value=""></form>
    </body></html>
    """)
    page.evaluate("""
        setTimeout(() => {
            const b = document.createElement('button');
            b.id = 'submit-btn';
            document.body.appendChild(b);
        }, 300);
        setTimeout(() => {
            const b = document.getElementById('submit-btn');
            if (b) b.remove();
        }, 600);
    """)

    start = time.monotonic()
    status = review_module.wait_for_submit_or_close(
        page, page, _FakeAtsModule(), _job(), [_name_field()], poll_interval_s=0.1,
    )
    elapsed = time.monotonic() - start

    assert status == "submitted"
    assert elapsed >= 0.5, (
        f"returned after only {elapsed:.2f}s — treated the button's absence before it "
        f"ever rendered as an instant submit"
    )
    page.close()


def test_closing_the_window_without_submitting_is_detected_as_skipped(browser, isolated_db):
    page = browser.new_page()
    page.set_content(_PAGE_HTML)
    page.close()  # closed before the wait even starts, same as the real "walked away" case

    status = review_module.wait_for_submit_or_close(page, page, _FakeAtsModule(), _job(), [_name_field()], poll_interval_s=0.1)

    assert status == "skipped"


def test_form_state_is_captured_while_waiting(browser, isolated_db):
    """The final field values must be learned from even though the page has
    usually already navigated away by the time "submitted" is detected —
    capture has to happen on every poll tick, not just at the end."""
    from jobbot.learning import store as learning_store

    page = browser.new_page()
    page.set_content(_PAGE_HTML)
    page.fill("#name", "Ada Lovelace")
    page.evaluate("setTimeout(() => history.pushState({}, '', '#done'), 300)")

    review_module.wait_for_submit_or_close(page, page, _FakeAtsModule(), _job(), [_name_field()], poll_interval_s=0.1)

    session = isolated_db()
    match = learning_store.find_match(session, "Name", "text")
    session.close()
    assert match is not None
    assert match.value == "Ada Lovelace"
    page.close()

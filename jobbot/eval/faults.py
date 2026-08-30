"""Fault injectors (spec §92).

WAREX (arXiv 2510.03285) is the number this module exists for: injecting
realistic faults into WebVoyager tasks dropped success from 42% to 2%
under network dropout and to 30% under server errors. Happy-path capability
is not what limits a browser agent in production; fault containment is,
and benchmarks do not measure it.

Each injector is a context manager over a live Playwright page, so a
scenario reads as "this page, with this fault, should end this way".
Faults are applied through Playwright's own routing and CDP surfaces
rather than by monkey-patching our code, so what is exercised is the real
failure path, not a simulation of it.
"""
from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterator

log = logging.getLogger(__name__)


@contextlib.contextmanager
def network_dropped(page, *, url_glob: str = "**/*") -> Iterator[None]:
    """Every matching request fails, as if the connection went away."""
    page.route(url_glob, lambda route: route.abort("internetdisconnected"))
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            page.unroute(url_glob)


@contextlib.contextmanager
def server_error(page, *, status: int = 503, url_glob: str = "**/*") -> Iterator[None]:
    """The site answers, but with a 5xx. Distinct from a dropout: the page
    loads *something*, which is what makes it easy to mistake for content."""
    page.route(url_glob, lambda route: route.fulfill(
        status=status, content_type="text/html",
        body=f"<html><body><h1>{status} Service Unavailable</h1></body></html>",
    ))
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            page.unroute(url_glob)


@contextlib.contextmanager
def slow_network(page, *, delay_ms: int = 5000, url_glob: str = "**/*") -> Iterator[None]:
    """Requests eventually succeed, but late enough to blow a timeout."""
    def handler(route):
        page.wait_for_timeout(delay_ms)
        route.continue_()

    page.route(url_glob, handler)
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            page.unroute(url_glob)


def mutate_dom(page, *, selector: str = "input", attribute: str = "data-jobbot-id") -> int:
    """Selector drift: strip the attribute every handle resolves against.

    This is the single most common way a working automation breaks
    (failures §4). Returns how many elements were changed.
    """
    return page.evaluate(
        """([selector, attribute]) => {
            const els = [...document.querySelectorAll(selector)];
            els.forEach(el => el.removeAttribute(attribute));
            return els.length;
        }""",
        [selector, attribute],
    )


def replace_frame(page, *, selector: str = "iframe") -> bool:
    """Detach and re-attach an iframe, invalidating every handle inside it."""
    return page.evaluate(
        """(selector) => {
            const f = document.querySelector(selector);
            if (!f) return false;
            const src = f.src, parent = f.parentNode;
            f.remove();
            const n = document.createElement('iframe');
            n.src = src;
            parent.appendChild(n);
            return true;
        }""",
        selector,
    )


def open_popup(page, *, url: str = "about:blank") -> object:
    """The flow continues in a tab the agent is not watching (failures §6)."""
    with page.context.expect_page() as info:
        page.evaluate("(u) => window.open(u, '_blank')", url)
    return info.value


def inject_hidden_dom(page, *, text: str) -> None:
    """Malicious content invisible to a human reviewing the page but
    present in the accessibility tree and text extraction — the case where
    a screenshot review would pass and the model would still read it."""
    page.evaluate(
        """(text) => {
            const d = document.createElement('div');
            d.setAttribute('aria-hidden', 'false');
            d.style.cssText = 'position:absolute;left:-9999px;top:-9999px;';
            d.textContent = text;
            document.body.appendChild(d);
        }""",
        text,
    )


def fake_confirmation(page, *, text: str = "Thank you! Your application has been submitted.") -> None:
    """A page that *claims* success without anything having been submitted.

    The scenario that makes verification worth having: a site showing
    confirmation wording on a page where the form is still sitting there
    unsent, or where a validation error re-rendered underneath it.
    """
    page.evaluate(
        """(text) => {
            const d = document.createElement('div');
            d.id = 'fake-confirmation';
            d.textContent = text;
            document.body.prepend(d);
        }""",
        text,
    )


def crash_browser(page) -> None:
    """Kill the page the way a crash does: every later call against it
    raises `TargetClosedError`, which is what recovery has to survive.

    Scoped to the page's *context*, not the whole browser. Closing the
    browser is a more faithful crash and is also how this fault took out
    the harness running it — every scenario after the crash one failed
    with "browser has been closed", which looks exactly like a cascade of
    real defects. A destructive fault has to be contained to the scenario
    that asked for it, or the suite stops being able to measure anything
    after it.
    """
    with contextlib.suppress(Exception):
        page.context.close()


@contextlib.contextmanager
def llm_failure(kind: str = "timeout") -> Iterator[None]:
    """Make the LLM layer fail without touching the network.

    `kind` is "timeout" (raises), "malformed" (returns something the caller
    cannot use), or "empty".
    """
    import jobbot.llm as llm_module

    original = llm_module.call_tool

    def broken(*_a, **_kw):
        if kind == "timeout":
            raise TimeoutError("LLM request timed out")
        if kind == "malformed":
            return {"unexpected": "shape", "fields": "not-a-list"}
        return {}

    llm_module.call_tool = broken
    try:
        yield
    finally:
        llm_module.call_tool = original

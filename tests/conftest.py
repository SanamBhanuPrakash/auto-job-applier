"""Shared test setup.

Resolves a usable Chromium before any test module reads
``JOBBOT_TEST_CHROMIUM_PATH``.

Why this exists: every browser fixture in this suite skips when Chromium
cannot launch, which is correct — but when Playwright's bundled build is
missing, *98 of 350 tests skip silently* and the run still reports green.
A suite that reports success while not exercising the browser at all is
exactly the "appears to work" failure mode this project treats as a defect
(see docs/research/browser-agent-failures.md). Pointing Playwright at an
already-installed Chromium keeps those tests running instead of vanishing.
"""
from __future__ import annotations

import os
from pathlib import Path

#: Locations where a system/preinstalled Chromium is commonly found. Used
#: only when the Playwright-managed build is unavailable; an explicit
#: JOBBOT_TEST_CHROMIUM_PATH always wins.
_CANDIDATES = (
    "/opt/pw-browsers/chromium",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
)


def _playwright_chromium_is_usable() -> bool:
    """True when Playwright's own bundled Chromium can actually launch."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:
        return False


def _resolve() -> None:
    if os.environ.get("JOBBOT_TEST_CHROMIUM_PATH"):
        return
    if _playwright_chromium_is_usable():
        return
    for candidate in _CANDIDATES:
        if Path(candidate).exists():
            os.environ["JOBBOT_TEST_CHROMIUM_PATH"] = candidate
            return


_resolve()

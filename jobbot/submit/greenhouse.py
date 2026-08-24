"""Greenhouse hosted-form quirks.

Greenhouse's newer job boards render most dropdowns as react-aria
comboboxes rather than native <select> — form_scan.py already tags those
as type "combobox" and filler.py's generic combobox handling (click, type,
click the matching [role=option]) covers them. This module only adds the
submit-button selector and a post-load settle wait for the SPA to hydrate.
"""
from __future__ import annotations

from playwright.sync_api import Page

SUBMIT_SELECTOR = 'button#submit_app, button[type="submit"]:has-text("Submit Application")'


def wait_for_form(page: Page) -> None:
    page.wait_for_selector("form#application-form, form", timeout=15000)
    page.wait_for_timeout(500)  # let react-aria widgets finish hydrating


def click_submit(page: Page) -> None:
    page.locator(SUBMIT_SELECTOR).first.click()

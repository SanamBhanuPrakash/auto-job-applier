"""Lever hosted-form quirks. Lever forms are plain HTML with native
<select>/<input> elements, so the generic scanner/filler handle them without
special-casing; this module only adds the submit-button selector.
"""
from __future__ import annotations

from playwright.sync_api import Page

SUBMIT_SELECTOR = 'button[type="submit"]:has-text("Submit application"), button.template-btn-submit'


def wait_for_form(page: Page) -> None:
    page.wait_for_selector("form", timeout=15000)


def click_submit(page: Page) -> None:
    page.locator(SUBMIT_SELECTOR).first.click()

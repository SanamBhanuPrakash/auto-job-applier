"""Lever hosted-form quirks. Lever forms are plain HTML with native
<select>/<input> elements, so the generic scanner/filler handle them without
special-casing; this module only adds the submit-button selector. Lever
postings are almost always served directly at jobs.lever.co rather than
iframe-embedded, but form_scan.find_target_frame() still checks for an
iframe as a fallback since ATS_HINT is passed through either way.
"""
from __future__ import annotations

from playwright.sync_api import Page

from jobbot.submit.form_scan import FrameLike

ATS_HINT = "lever.co"
SUBMIT_SELECTOR = 'button[type="submit"]:has-text("Submit application"), button.template-btn-submit'


def settle(page: Page) -> None:
    pass


def click_submit(form_ctx: FrameLike) -> None:
    form_ctx.locator(SUBMIT_SELECTOR).first.click()

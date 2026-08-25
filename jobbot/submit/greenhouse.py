"""Greenhouse hosted-form quirks.

Greenhouse's newer job boards render most dropdowns as react-aria
comboboxes rather than native <select> — form_scan.py already tags those
as type "combobox" and filler.py's generic combobox handling (click, type,
click the matching [role=option]) covers them.

Two URL shapes exist in the wild: a direct hosted page
(boards.greenhouse.io/... or job-boards.greenhouse.io/...) and, more
commonly, the employer's own branded careers page with the Greenhouse form
embedded in an <iframe>. form_scan.find_target_frame() handles locating
whichever one actually has the <form> — this module just tells it what to
look for and adds the submit-button selector.
"""
from __future__ import annotations

from playwright.sync_api import Page

from jobbot.submit.form_scan import FrameLike

ATS_HINT = "greenhouse.io"
SUBMIT_SELECTOR = 'button#submit_app, button[type="submit"]:has-text("Submit Application")'


def settle(page: Page) -> None:
    # wait_for_timeout only exists on Page, not Frame, so base.py always
    # calls this with the top-level page regardless of where the form lives.
    page.wait_for_timeout(500)  # let react-aria widgets finish hydrating


def click_submit(form_ctx: FrameLike) -> None:
    form_ctx.locator(SUBMIT_SELECTOR).first.click()

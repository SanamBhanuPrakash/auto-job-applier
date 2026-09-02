"""Ashby hosted-form quirks.

Two things make Ashby different from Greenhouse and Lever, both confirmed
against the live site rather than assumed:

**1. The form does not exist in the served HTML.** Fetching
`jobs.ashbyhq.com/<org>/<id>/application` returns 65KB with `<form>` count
zero and `<input>` count zero — it is a React application that builds the
form client-side after hydration. Greenhouse's 500ms settle is not enough
here: the deterministic path would call `find_target_frame`, find nothing,
and give up on a page that is about to be perfectly fillable. `settle()`
therefore waits for a real input to appear rather than for a fixed delay.

**2. The apply form lives at a different URL from the posting.** Ashby's
posting API returns both `jobUrl` and `applyUrl`, and they differ — the
apply form is at `<jobUrl>/application`. Landing on the posting page and
looking for a form finds nothing, because the posting page only has an
"Apply for this Job" button. `apply_url()` does that conversion so the
deterministic path opens the right page directly instead of relying on the
agent to click through.

**Honest status:** the selectors below are derived from Ashby's public
markup and the shape of its API, and the module is unit-tested, but no
application has been submitted through it. Browser networking is blocked
in the environment this was written in, so the first real Ashby submission
will be the first live test. Treat a failure here as expected-unknown
rather than surprising, and check `data/screenshots/` when one happens.
"""
from __future__ import annotations

import re

from playwright.sync_api import Page

from jobbot.submit.form_scan import FrameLike

ATS_HINT = "ashbyhq.com"

#: Ashby renders its submit control as a plain button with an accessible
#: name. Several shapes are listed because the wording differs between
#: boards ("Submit Application" vs "Submit"), and Playwright takes the
#: first that matches.
SUBMIT_SELECTOR = (
    'button[type="submit"]:has-text("Submit Application"), '
    'button:has-text("Submit Application"), '
    'button[type="submit"]:has-text("Submit"), '
    '.ashby-application-form-submit-button'
)

#: How long to wait for the SPA to render its form. Generous on purpose:
#: the cost of waiting is seconds, the cost of giving up too early is a
#: job never applied to.
_HYDRATION_TIMEOUT_MS = 15000

_APPLICATION_SUFFIX = re.compile(r"/application/?$", re.IGNORECASE)


def apply_url(url: str) -> str:
    """The URL of the application form for an Ashby posting.

    `https://jobs.ashbyhq.com/atlan/<uuid>`
      -> `https://jobs.ashbyhq.com/atlan/<uuid>/application`

    Already-correct URLs and anything that is not an Ashby posting are
    returned unchanged, so this is safe to call unconditionally.
    """
    if not url or ATS_HINT not in url.lower():
        return url
    if _APPLICATION_SUFFIX.search(url):
        return url
    return url.rstrip("/") + "/application"


def settle(page: Page) -> None:
    """Wait for the React form to actually exist.

    Waits on a real input rather than a network-idle heuristic: Ashby keeps
    connections open, so `networkidle` can hang, while the presence of a
    text input is the thing the next step actually needs.
    """
    try:
        page.wait_for_selector(
            "form input, form textarea, input[type='file']",
            timeout=_HYDRATION_TIMEOUT_MS, state="attached",
        )
    except Exception:  # noqa: BLE001 - a timeout here is not fatal
        # Let find_target_frame produce the real diagnostic. Raising a
        # different error from settle() would mask which stage failed.
        pass
    page.wait_for_timeout(400)  # let the last widgets bind their handlers


def click_submit(form_ctx: FrameLike) -> None:
    form_ctx.locator(SUBMIT_SELECTOR).first.click()

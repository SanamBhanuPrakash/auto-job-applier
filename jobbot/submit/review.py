"""Human-in-the-loop confirmation. Nothing gets submitted without this.

That loop used to mean a blocking `console.input()` — fill the browser,
then alt-tab back to the terminal and type 'yes'. Real feedback: that's a
redundant second step when you're the one clicking the real Submit button
on the page anyway. wait_for_submit_or_close() replaces it by watching the
browser itself for the two things that actually distinguish "done": the
page navigating away from the application (a real submit almost always
does this) or the submit button itself disappearing (some ATS forms swap
in a "thanks for applying" panel in place, without changing the URL).
Closing the window without either of those happening is treated as "you
decided to skip this one" — no separate confirmation needed, since closing
the browser is itself the explicit, deliberate action here.
"""
from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.table import Table

from jobbot.db import session_scope
from jobbot.learning import store as learning_store
from jobbot.models import Job
from jobbot.submit.form_scan import FieldSpec, FrameLike

log = logging.getLogger(__name__)
console = Console()


def show_review(
    job: Job,
    screenshot_path: Path,
    needs_human: list[FieldSpec],
    memory_hints: dict[int, str] | None = None,
    auto_filled_sensitive: list[tuple[str, str]] | None = None,
) -> None:
    memory_hints = memory_hints or {}
    console.rule(f"[bold]{job.title} @ {job.company}[/bold]")
    console.print(f"URL: {job.url}")
    console.print(f"Screenshot saved to: {screenshot_path}")

    if auto_filled_sensitive:
        table = Table(title="[bold yellow]Sensitive answers auto-filled from memory this run[/bold yellow]")
        table.add_column("Question")
        table.add_column("Answer used")
        for label, value in auto_filled_sensitive:
            table.add_row(label, value)
        console.print(table)

    if needs_human:
        table = Table(title="Fields left for you to fill in the open browser window")
        table.add_column("Label")
        table.add_column("Type")
        table.add_column("Required")
        table.add_column("You answered before")
        for f in needs_human:
            table.add_row(f.label, f.field_type, "yes" if f.required else "", memory_hints.get(f.field_id, ""))
        console.print(table)
    else:
        console.print("[green]Every field was filled (by the model or from memory).[/green]")


def wait_for_submit_or_close(
    page, form_ctx: FrameLike, ats_module, job: Job, fields: list[FieldSpec], *,
    poll_interval_s: float = 2.0, model_filled_ids: set[int] | None = None,
) -> str:
    """Blocks until you either submit the application yourself in the open
    browser window or close it — no typing required. Returns "submitted" or
    "skipped". Polls indefinitely (there's no timeout, same as the old
    blocking prompt this replaces — both just wait for you to act), and
    captures whatever's currently in the form on every tick so the most
    recent state right before a submit/navigation still gets learned from,
    even though by the time "submitted" is detected the page has often
    already moved on.

    "submitted" here means *a submit appears to have been attempted*, not
    that it succeeded: navigation and the submit button disappearing are
    both equally consistent with a validation error re-rendering the form.
    The caller treats this as a trigger to verify (jobbot/submit/verify.py),
    never as the outcome.

    `model_filled_ids` is passed through to the learning capture so values
    the LLM guessed are recorded as guesses rather than as answers you
    confirmed — see jobbot/learning/provenance.py.
    """
    original_url = form_ctx.url
    console.print(
        f"\n[bold yellow]Waiting for you in the browser[/bold yellow] — fill in anything listed above for "
        f"[bold]{job.title} @ {job.company}[/bold] and click Submit yourself on the page; I'll notice and move "
        f"on to the next application. Close the window instead if you'd rather skip this one."
    )
    while True:
        if page.is_closed():
            return "skipped"
        try:
            navigated = form_ctx.url != original_url
            submit_gone = form_ctx.locator(ats_module.SUBMIT_SELECTOR).count() == 0
            if navigated or submit_gone:
                return "submitted"
            with session_scope() as session:
                learning_store.capture_from_page(
                    session, form_ctx, fields,
                    verified_submission=False,  # nothing verified yet; caller decides
                    model_filled_ids=model_filled_ids or set(),
                )
        except Exception:  # noqa: BLE001
            # Most likely cause at this point (page.is_closed() already
            # ruled out): the page navigated/reloaded out from under this
            # check between the is_closed() test above and here — that's
            # itself evidence a real submit just happened.
            log.debug("Page became unqueryable while watching for submit; treating as submitted", exc_info=True)
            return "submitted"
        page.wait_for_timeout(int(poll_interval_s * 1000))

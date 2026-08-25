"""Human-in-the-loop confirmation. Nothing gets submitted without this."""
from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

from jobbot.models import Job
from jobbot.submit.form_scan import FieldSpec

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

    console.print(
        "\n[bold yellow]The browser window is open on this application.[/bold yellow] "
        "Fill in anything listed above yourself, double-check every auto-filled "
        "value, then come back here."
    )


def confirm_submit(job: Job) -> bool:
    answer = console.input(
        f"\nType exactly 'yes' to click Submit for [bold]{job.title} @ {job.company}[/bold], "
        f"anything else to skip: "
    )
    return answer.strip().lower() == "yes"


def confirm_already_closed_browser(job: Job) -> bool:
    """The browser window for this application closed on its own before we
    got to click Submit — most likely because you clicked the real Submit
    button on the page yourself and then closed the window, which is a
    completely normal thing to do during the manual-review step. Ask
    directly instead of guessing (a wrong guess either way is bad: silently
    recording "submitted" for something that never went through is worse
    than the truth, and silently recording "error"/"skipped" for something
    you actually submitted means the next `apply-all` run retries it and
    opens yet another browser window for an already-done job)."""
    console.print(
        f"\n[yellow]The browser window for [bold]{job.title} @ {job.company}[/bold] closed on its own "
        f"before I could click Submit — most likely because you already clicked it yourself on the page.[/yellow]"
    )
    answer = console.input("Did you already submit that application yourself? Type exactly 'yes' if so, anything else to mark it skipped: ")
    return answer.strip().lower() == "yes"

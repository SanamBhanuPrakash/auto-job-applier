"""Human-in-the-loop confirmation. Nothing gets submitted without this."""
from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.table import Table

from jobbot.models import Job
from jobbot.submit.form_scan import FieldSpec

console = Console()


def show_review(job: Job, screenshot_path: Path, needs_human: list[FieldSpec]) -> None:
    console.rule(f"[bold]{job.title} @ {job.company}[/bold]")
    console.print(f"URL: {job.url}")
    console.print(f"Screenshot saved to: {screenshot_path}")

    if needs_human:
        table = Table(title="Fields left for you to fill in the open browser window")
        table.add_column("Label")
        table.add_column("Type")
        table.add_column("Required")
        for f in needs_human:
            table.add_row(f.label, f.field_type, "yes" if f.required else "")
        console.print(table)
    else:
        console.print("[green]Every field was filled by the model.[/green]")

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

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from jobbot.config import get_settings, load_search_settings, save_profile_raw
from jobbot.db import session_scope
from jobbot.logging_conf import setup_logging
from jobbot.models import Application, Job, JobScore

app = typer.Typer(add_completion=False, help="Local-first job discovery and assisted-application agent.")
console = Console()


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    setup_logging(verbose)


@app.command()
def discover() -> None:
    """Pull jobs from every configured ATS/aggregator source and store new ones."""
    from jobbot.discovery.aggregate import run_discovery

    inserted, skipped = run_discovery()
    console.print(f"[green]Discovered {inserted} new job(s)[/green], {skipped} already known.")


@app.command()
def match(top_n: int = typer.Option(50, help="How many lexically-shortlisted jobs to send to the LLM reranker")) -> None:
    """Score undiscovered-but-unscored jobs against your profile."""
    from jobbot.matching.lexical import shortlist
    from jobbot.matching.score import score_shortlist

    settings_yaml = load_search_settings()
    with session_scope() as session:
        scored_ids = {row[0] for row in session.execute(select(JobScore.job_id))}
        jobs = session.execute(select(Job)).scalars().all()
        unscored = [j for j in jobs if j.id not in scored_ids]

    if not unscored:
        console.print("Nothing new to score. Run `jobbot discover` first.")
        return

    picked = shortlist(unscored, settings_yaml, top_n=top_n)
    console.print(f"Lexically shortlisted {len(picked)} of {len(unscored)} unscored jobs; reranking with Claude...")
    score_shortlist(picked)
    console.print("[green]Done.[/green] Run `jobbot list` to see ranked results.")


@app.command(name="list")
def list_jobs(
    min_score: float = typer.Option(0, help="Only show jobs with llm_score >= this"),
    limit: int = typer.Option(20),
) -> None:
    """Show discovered jobs ranked by fit score."""
    with session_scope() as session:
        rows = (
            session.execute(
                select(Job, JobScore)
                .join(JobScore, JobScore.job_id == Job.id)
                .where(JobScore.llm_score >= min_score)
                .order_by(JobScore.llm_score.desc())
                .limit(limit)
            )
        ).all()

    table = Table(title="Ranked jobs")
    table.add_column("id")
    table.add_column("score")
    table.add_column("company")
    table.add_column("title")
    table.add_column("ats")
    table.add_column("location")
    for job, score in rows:
        table.add_row(str(job.id), f"{score.llm_score:.0f}", job.company, job.title, job.ats or "-", job.location)
    console.print(table)


@app.command()
def show(job_id: int) -> None:
    """Show full detail (including LLM reasoning) for one job."""
    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            console.print(f"[red]No job with id {job_id}[/red]")
            raise typer.Exit(1)
        console.rule(f"{job.title} @ {job.company}")
        console.print(f"URL: {job.url}")
        console.print(f"Location: {job.location} (remote={job.remote})")
        console.print(f"ATS: {job.ats or 'unsupported for auto-submit'}")
        if job.score:
            console.print(f"Score: {job.score.llm_score:.0f} (lexical {job.score.lexical_score:.0f})")
            console.print(f"Reasoning: {job.score.llm_reasoning}")
        console.print(f"\n{job.description[:3000]}")


resume_app = typer.Typer(help="Resume import / profile management")
app.add_typer(resume_app, name="resume")


@resume_app.command("import")
def resume_import(path: Path) -> None:
    """Parse a resume PDF/DOCX/TXT into config/profile.yaml via Claude."""
    from jobbot.resume.parser import parse_resume

    profile = parse_resume(path)
    saved_path = save_profile_raw(profile.model_dump(exclude_none=False))
    console.print(f"[green]Profile written to {saved_path}[/green]. Review it — especially the null fields.")


@app.command()
def apply(
    job_id: int = typer.Argument(..., help="Job id from `jobbot list`"),
    auto_submit: bool = typer.Option(
        False, help="Skip the terminal confirmation and submit automatically IF every field was filled with no human-review flags. Off by default; read the README before enabling."
    ),
) -> None:
    """Open a browser, fill the application for one job, and (with your confirmation) submit it."""
    from jobbot.submit.base import apply_to_job

    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            console.print(f"[red]No job with id {job_id}[/red]")
            raise typer.Exit(1)

    result = apply_to_job(job, auto_submit_override=auto_submit)
    console.print(f"[bold]Status: {result.status}[/bold]")
    if result.error:
        console.print(f"[red]{result.error}[/red]")


@app.command()
def batch(
    min_score: float = typer.Option(75, help="Only apply to jobs at or above this fit score"),
    limit: int = typer.Option(10),
) -> None:
    """Apply to multiple supported-ATS jobs above a score threshold, one at a time,
    with human-paced delays between each. Still stops for confirmation on every
    application unless you've set JOBBOT_AUTO_SUBMIT=true in .env."""
    settings_yaml = load_search_settings()
    sub_cfg = settings_yaml.get("submission", {})
    pacing_min = sub_cfg.get("pacing_seconds_min", 45)
    pacing_max = sub_cfg.get("pacing_seconds_max", 180)
    supported = set(sub_cfg.get("supported_ats", ["greenhouse", "lever"]))

    with session_scope() as session:
        rows = session.execute(
            select(Job)
            .join(JobScore, JobScore.job_id == Job.id)
            .where(JobScore.llm_score >= min_score, Job.ats.in_(supported))
            .order_by(JobScore.llm_score.desc())
            .limit(limit)
        ).scalars().all()

    if not rows:
        console.print("No jobs match the threshold and have a supported ATS. Try `jobbot match` first.")
        return

    console.print(f"About to attempt {len(rows)} application(s), paced {pacing_min}-{pacing_max}s apart.")
    console.print("Each one still stops for your review/confirmation before submitting.")

    from jobbot.submit.base import apply_to_jobs

    apply_to_jobs(rows, pacing_min=pacing_min, pacing_max=pacing_max)


@app.command()
def ledger(limit: int = typer.Option(30)) -> None:
    """Show recent application attempts and their outcome."""
    with session_scope() as session:
        rows = (
            session.execute(
                select(Application, Job)
                .join(Job, Job.id == Application.job_id)
                .order_by(Application.created_at.desc())
                .limit(limit)
            )
        ).all()

    table = Table(title="Application ledger")
    table.add_column("when")
    table.add_column("status")
    table.add_column("company")
    table.add_column("title")
    for application, job in rows:
        table.add_row(str(application.created_at), application.status, job.company, job.title)
    console.print(table)


if __name__ == "__main__":
    app()

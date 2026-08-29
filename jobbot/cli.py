from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from jobbot.agent.states import CONSUMES_JOB
from jobbot.config import get_settings, load_search_settings, save_profile_raw
from jobbot.db import session_scope
from jobbot.logging_conf import setup_logging
from jobbot.models import Application, FieldIssue, Job, JobScore, LearnedAnswer, ResumeProfile

app = typer.Typer(add_completion=False, help="Local-first job discovery and assisted-application agent.")
console = Console()


@app.callback()
def main(verbose: bool = typer.Option(False, "--verbose", "-v")) -> None:
    setup_logging(verbose)


def _do_discover(days: float | None) -> tuple[int, int]:
    from jobbot.discovery.aggregate import run_discovery

    return run_discovery(max_age_days=days)


@app.command()
def discover(
    days: float | None = typer.Option(None, "--days", help="Only keep postings from roughly this many days back. Defaults to search.posted_within_days in settings.yaml."),
) -> None:
    """Pull jobs from every configured ATS/aggregator source and store new ones."""
    inserted, skipped = _do_discover(days)
    console.print(f"[green]Discovered {inserted} new job(s)[/green], {skipped} already known.")


def _do_match(top_n: int) -> int:
    from jobbot.matching.lexical import shortlist
    from jobbot.matching.score import score_shortlist

    settings_yaml = load_search_settings()
    with session_scope() as session:
        scored_ids = {row[0] for row in session.execute(select(JobScore.job_id))}
        jobs = session.execute(select(Job)).scalars().all()
        unscored = [j for j in jobs if j.id not in scored_ids]

    if not unscored:
        return 0

    picked = shortlist(unscored, settings_yaml, top_n=top_n)
    console.print(f"Lexically shortlisted {len(picked)} of {len(unscored)} unscored jobs; reranking with Claude...")
    score_shortlist(picked)
    return len(picked)


@app.command()
def match(top_n: int = typer.Option(50, help="How many lexically-shortlisted jobs to send to the LLM reranker")) -> None:
    """Score undiscovered-but-unscored jobs against your profile."""
    n = _do_match(top_n)
    if n == 0:
        console.print("Nothing new to score. Run `jobbot discover` first.")
        return
    console.print("[green]Done.[/green] Run `jobbot list` to see ranked results.")


@app.command()
def run(
    min_score: float = typer.Option(75, help="Only apply to jobs at or above this fit score"),
    limit: int = typer.Option(10, help="Max applications to attempt this run"),
    top_n: int = typer.Option(50, help="How many lexically-shortlisted jobs to send to the LLM reranker"),
    days: float | None = typer.Option(None, "--days", help="Recency filter override for discovery; defaults to search.posted_within_days in settings.yaml"),
    auto_submit: bool = typer.Option(
        False, help="Skip the terminal confirmation and submit automatically IF every field was filled with no human-review flags. Off by default; read the README before enabling."
    ),
    autofill_sensitive: bool | None = typer.Option(
        None, help="Override JOBBOT_AUTOFILL_SENSITIVE for this run. Still requires typing CONFIRM once, before applying starts."
    ),
) -> None:
    """The whole pipeline in one command: discover new postings, score them,
    then apply to whatever clears the threshold — this is the "run it and
    walk away" entrypoint. Every guardrail from the individual commands still
    applies: jobs already submitted are never re-attempted (safe to re-run
    on a schedule), nothing sensitive is auto-filled without an explicit
    --autofill-sensitive + typed CONFIRM, and without --auto-submit each
    application still stops for your review before it submits."""
    console.rule("[bold]1/3 — discover[/bold]")
    inserted, skipped = _do_discover(days)
    console.print(f"[green]Discovered {inserted} new job(s)[/green], {skipped} already known.")

    console.rule("[bold]2/3 — match[/bold]")
    n = _do_match(top_n)
    console.print("Nothing new to score this run." if n == 0 else f"[green]Scored {n} job(s).[/green]")

    console.rule("[bold]3/3 — apply[/bold]")
    batch(min_score=min_score, limit=limit, auto_submit=auto_submit, autofill_sensitive=autofill_sensitive)


@app.command(name="list")
def list_jobs(
    min_score: float = typer.Option(0, help="Only show jobs with llm_score >= this"),
    limit: int = typer.Option(20),
) -> None:
    """Show discovered jobs ranked by fit score."""
    with session_scope() as session:
        already_submitted = _already_submitted_job_ids(session)
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
    table.add_column("resume")
    table.add_column("applied")
    for job, score in rows:
        table.add_row(
            str(job.id), f"{score.llm_score:.0f}", job.company, job.title,
            job.ats or "-", job.location, job.matched_profile_tag or "-",
            "yes" if job.id in already_submitted else "",
        )
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
        console.print(f"Matched resume: {job.matched_profile_tag or '(default profile.yaml)'}")
        if job.id in _already_submitted_job_ids(session):
            console.print("[yellow]Already submitted[/yellow] — see `jobbot ledger`. `jobbot apply --force` to reapply.")
        if job.score:
            console.print(f"Score: {job.score.llm_score:.0f} (lexical {job.score.lexical_score:.0f})")
            console.print(f"Reasoning: {job.score.llm_reasoning}")
        console.print(f"\n{job.description[:3000]}")


resume_app = typer.Typer(help="Resume import / profile management")
app.add_typer(resume_app, name="resume")


@resume_app.command("import")
def resume_import(path: Path) -> None:
    """Parse a single resume PDF/DOCX/TXT into config/profile.yaml via Claude.
    Use `jobbot resume import-folder` instead if you have more than one resume."""
    from jobbot.resume.parser import parse_resume

    profile = parse_resume(path)
    saved_path = save_profile_raw(profile.model_dump(exclude_none=False))
    console.print(f"[green]Profile written to {saved_path}[/green]. Review it — especially the null fields.")


@resume_app.command("import-folder")
def resume_import_folder(
    folder: Path | None = typer.Argument(None, help="Defaults to JOBBOT_RESUMES_DIR (config/resumes/)"),
) -> None:
    """Parse every resume in a folder into its own tagged profile — e.g.
    config/resumes/python-developer.pdf, ai-engineer.pdf, frontend.pdf,
    backend.pdf, full-stack.pdf, cloud-engineer.pdf. `jobbot match` then
    picks whichever profile fits each job best instead of using one resume
    for everything; `jobbot apply`/`batch` upload that job's matched resume."""
    from jobbot.resume.multi import import_folder

    settings = get_settings()
    target = folder or settings.resumes_dir
    if not target.exists():
        console.print(f"[red]{target} does not exist.[/red] Create it and drop your resume files in, named by role.")
        raise typer.Exit(1)

    tags = import_folder(target)
    if not tags:
        console.print(f"[yellow]No resume files (.pdf/.docx/.txt) found in {target}[/yellow]")
        return
    console.print(f"[green]Imported {len(tags)} resume profile(s):[/green] {', '.join(tags)}")
    console.print("Run `jobbot match` (or re-run it) so postings get matched against these.")


@resume_app.command("list-profiles")
def resume_list_profiles() -> None:
    """Show every imported resume profile and how many jobs are currently matched to each."""
    with session_scope() as session:
        profiles = session.execute(select(ResumeProfile)).scalars().all()
        if not profiles:
            console.print("No resume profiles imported yet. Run `jobbot resume import-folder`.")
            return

        table = Table(title="Resume profiles")
        table.add_column("tag")
        table.add_column("resume file")
        table.add_column("jobs matched")
        for p in profiles:
            matched = session.execute(
                select(Job.id).where(Job.matched_profile_tag == p.tag)
            ).scalars().all()
            table.add_row(p.tag, Path(p.resume_path).name, str(len(matched)))
        console.print(table)


def _confirm_sensitive_autofill_if_needed(override: bool | None) -> bool:
    """If sensitive-field autofill is enabled (by flag or JOBBOT_AUTOFILL_SENSITIVE),
    shows exactly which saved sensitive answers exist and requires typing
    CONFIRM once before this run will reuse any of them. Returns whether
    autofill is actually armed for this run."""
    settings = get_settings()
    enabled = settings.jobbot_autofill_sensitive if override is None else override
    if not enabled:
        return False

    with session_scope() as session:
        rows = session.execute(select(LearnedAnswer).where(LearnedAnswer.sensitive.is_(True))).scalars().all()
        pairs = [(r.label_raw, r.value) for r in rows]

    if not pairs:
        console.print(
            "[yellow]Sensitive-field autofill is on, but nothing sensitive has been learned yet — "
            "those fields will still stop for your review this run, same as normal.[/yellow]"
        )
        return True

    console.print("\n[bold yellow]Sensitive-field autofill is ON.[/bold yellow] These saved answers will be reused automatically, without stopping for review, on every matching question this run:\n")
    table = Table()
    table.add_column("Question")
    table.add_column("Saved answer")
    for label, value in pairs:
        table.add_row(label, value)
    console.print(table)

    answer = console.input("\nType CONFIRM to proceed with auto-filling these this run: ")
    if answer.strip() != "CONFIRM":
        console.print("[red]Not confirmed — sensitive fields will require your review as usual this run.[/red]")
        return False
    return True


def _already_submitted_job_ids(session) -> set[int]:
    """Job ids with a completed submission on record.

    Kept for display/back-compat. `_consumed_job_ids` below is what
    actually gates applying, because "submitted" alone is too narrow: an
    attempt that died mid-submit must also block a re-attempt.
    """
    return set(
        session.execute(
            select(Application.job_id).where(Application.status == "submitted").distinct()
        ).scalars().all()
    )


def _consumed_job_ids(session) -> set[int]:
    """Job ids that must not be attempted again.

    Broader than "submitted" in two ways that both prevent duplicate
    applications to a real employer:

    1. It includes attempts parked in the danger zone (SUBMITTING /
       VERIFYING_SUBMISSION / UNKNOWN) and those awaiting a human. An
       outcome we cannot prove is *not* a licence to apply again.
    2. It matches on cross-source job identity, so the same posting
       discovered through two sources is only ever applied to once.
    """
    from jobbot.agent.statestore import consumed_job_identities

    identities = consumed_job_identities(session)
    ids = set(
        session.execute(
            select(Application.job_id).where(Application.state.in_([s.value for s in CONSUMES_JOB]))
        ).scalars().all()
    )
    ids |= _already_submitted_job_ids(session)  # legacy rows with no state
    if identities:
        ids |= set(
            session.execute(
                select(Job.id).where(Job.job_identity.in_(list(identities)))
            ).scalars().all()
        )
    return ids


@app.command()
def apply(
    job_id: int = typer.Argument(..., help="Job id from `jobbot list`"),
    auto_submit: bool = typer.Option(
        False, help="Skip the terminal confirmation and submit automatically IF every field was filled with no human-review flags. Off by default; read the README before enabling."
    ),
    autofill_sensitive: bool | None = typer.Option(
        None, help="Override JOBBOT_AUTOFILL_SENSITIVE for this run. Still requires typing CONFIRM once. Read the README 'Guardrails' section first."
    ),
    force: bool = typer.Option(False, help="Apply even if this job already has a submitted application on record."),
) -> None:
    """Open a browser, fill the application for one job, and (with your confirmation) submit it."""
    from jobbot.submit.base import apply_to_job

    with session_scope() as session:
        job = session.get(Job, job_id)
        if job is None:
            console.print(f"[red]No job with id {job_id}[/red]")
            raise typer.Exit(1)
        if not force and job.id in _consumed_job_ids(session):
            console.print(
                f"[yellow]Job {job_id} already has an application attempt on record.[/yellow] "
                "Check `jobbot ledger` / `jobbot review`, or pass --force to apply again anyway."
            )
            raise typer.Exit(1)

    confirmed_autofill = _confirm_sensitive_autofill_if_needed(autofill_sensitive)

    from jobbot.agent import statestore

    with session_scope() as session:
        run_id = statestore.start_run(session, command="apply")

    result = apply_to_job(
        job, run_id=run_id,
        auto_submit_override=auto_submit,
        autofill_sensitive_override=confirmed_autofill,
    )

    with session_scope() as session:
        statestore.finish_run(session, run_id)

    if result is None:
        console.print("[yellow]Not applied — the job was already claimed or is awaiting review.[/yellow]")
        return
    console.print(f"[bold]State: {result.state}[/bold] (verdict: {result.verification_verdict or 'n/a'})")
    if result.error:
        console.print(f"[red]{result.error}[/red]")


@app.command()
def batch(
    min_score: float = typer.Option(75, help="Only apply to jobs at or above this fit score"),
    limit: int = typer.Option(10),
    auto_submit: bool = typer.Option(
        False, help="Skip the terminal confirmation and submit automatically IF every field was filled with no human-review flags. Off by default; read the README before enabling."
    ),
    autofill_sensitive: bool | None = typer.Option(
        None, help="Override JOBBOT_AUTOFILL_SENSITIVE for this run. Still requires typing CONFIRM once, before the batch starts (not per application)."
    ),
) -> None:
    """Apply to multiple supported-ATS jobs above a score threshold, one at a time,
    with human-paced delays between each. Skips any job that already has a submitted
    application on record — re-running this is always safe. Still stops for confirmation
    on every application unless --auto-submit / JOBBOT_AUTO_SUBMIT=true — and even then,
    only for applications where every field was filled with nothing flagged for review."""
    settings_yaml = load_search_settings()
    sub_cfg = settings_yaml.get("submission", {})
    pacing_min = sub_cfg.get("pacing_seconds_min", 45)
    pacing_max = sub_cfg.get("pacing_seconds_max", 180)
    supported = set(sub_cfg.get("supported_ats", ["greenhouse", "lever"]))

    with session_scope() as session:
        consumed = _consumed_job_ids(session)
        rows: list[Job] = []
        offset = 0
        page_size = max(limit * 2, 20)
        while len(rows) < limit:
            page = session.execute(
                select(Job)
                .join(JobScore, JobScore.job_id == Job.id)
                .where(JobScore.llm_score >= min_score, Job.ats.in_(supported))
                .order_by(JobScore.llm_score.desc())
                .offset(offset)
                .limit(page_size)
            ).scalars().all()
            if not page:
                break
            rows.extend(j for j in page if j.id not in consumed)
            offset += page_size
        rows = rows[:limit]

    if not rows:
        console.print("No jobs match the threshold and have a supported ATS. Try `jobbot match` first.")
        return

    console.print(f"About to attempt {len(rows)} application(s), paced {pacing_min}-{pacing_max}s apart.")
    if not auto_submit:
        console.print("Each one still stops for your review/confirmation before submitting.")

    confirmed_autofill = _confirm_sensitive_autofill_if_needed(autofill_sensitive)

    from jobbot.agent import statestore
    from jobbot.submit.base import apply_to_jobs

    with session_scope() as session:
        run_id = statestore.start_run(session, command="batch")
    console.print(f"[dim]run {run_id}[/dim]")

    try:
        results = apply_to_jobs(
            rows,
            pacing_min=pacing_min,
            pacing_max=pacing_max,
            run_id=run_id,
            auto_submit_override=auto_submit,
            autofill_sensitive_override=confirmed_autofill,
        )
    finally:
        with session_scope() as session:
            statestore.finish_run(session, run_id)

    submitted = sum(1 for r in results if r.state == "SUBMITTED")
    console.print(
        f"\n[bold]Run complete:[/bold] {len(results)} attempt(s), "
        f"[green]{submitted} verified submitted[/green]. "
        "Anything not verified is in `jobbot review`."
    )


@app.command(name="apply-all")
def apply_all(
    min_score: float = typer.Option(60, help="Only apply to jobs at or above this fit score"),
    limit: int = typer.Option(500, help="Max applications to attempt this run"),
    auto_submit: bool = typer.Option(
        False, help="Skip the terminal confirmation and submit automatically IF every field was filled with no human-review flags. Off by default; read the README before enabling."
    ),
    autofill_sensitive: bool | None = typer.Option(
        None, help="Override JOBBOT_AUTOFILL_SENSITIVE for this run. Still requires typing CONFIRM once, before the run starts (not per application)."
    ),
) -> None:
    """Exactly `batch`, just named for what people actually ask for: apply
    to everything above a score threshold, no per-job IDs to type. Default
    limit is 500 instead of batch's 10 -- run `jobbot list --min-score 60`
    first to see roughly how many that threshold covers before committing.
    Same guardrails as batch: a job is skipped if any previous attempt
    consumed it -- not just a confirmed submission, but also an attempt
    that died mid-submit or is awaiting review, since an outcome we cannot
    prove is not a licence to apply again. Matching is by cross-source job
    identity, so one posting found through two sources is still applied to
    once. Without --auto-submit each one still stops for your review."""
    batch(min_score=min_score, limit=limit, auto_submit=auto_submit, autofill_sensitive=autofill_sensitive)


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


@app.command()
def review(limit: int = typer.Option(30)) -> None:
    """Show application attempts parked awaiting a human.

    This is where anything jobbot refused to guess about ends up: a
    submission it could not verify, a CAPTCHA/login wall, or a form it
    filled but did not submit. Nothing here is silently retried — an
    attempt whose outcome is unproven is never re-applied automatically,
    because that is exactly how a duplicate application happens.
    """
    from jobbot.agent.states import PARKED, ApplicationState

    parked = [s.value for s in PARKED] + [ApplicationState.SUBMITTING.value,
                                          ApplicationState.VERIFYING_SUBMISSION.value]
    with session_scope() as session:
        rows = session.execute(
            select(Application, Job)
            .join(Job, Job.id == Application.job_id)
            .where(Application.state.in_(parked))
            .order_by(Application.updated_at.desc())
            .limit(limit)
        ).all()

    if not rows:
        console.print("[green]Nothing awaiting review.[/green]")
        return

    table = Table(title="Awaiting human review")
    table.add_column("app")
    table.add_column("state")
    table.add_column("verdict")
    table.add_column("company")
    table.add_column("title")
    table.add_column("why")
    for application, job in rows:
        evidence = (application.verification_evidence or {}).get("evidence", [])
        why = "; ".join(evidence)[:60] if evidence else (application.error or "")[:60]
        table.add_row(
            str(application.id), application.state,
            application.verification_verdict or "-",
            job.company, job.title, why,
        )
    console.print(table)
    console.print(
        "\n[dim]An attempt in SUBMITTING/VERIFYING_SUBMISSION/UNKNOWN may already have "
        "reached the employer — check the posting before re-applying.[/dim]"
    )


@app.command()
def trace(application_id: int) -> None:
    """Show the full state history of one application attempt — what it
    did, in what order, and why it stopped where it did."""
    from jobbot.agent.statestore import history

    with session_scope() as session:
        application = session.get(Application, application_id)
        if application is None:
            console.print(f"[red]No application with id {application_id}[/red]")
            raise typer.Exit(1)
        job = session.get(Job, application.job_id)
        rows = history(session, application_id)

    console.rule(f"Application {application_id} — {job.title} @ {job.company}")
    console.print(f"State: [bold]{application.state}[/bold]  attempt #{application.attempt_no}")
    console.print(f"Verdict: {application.verification_verdict or '-'}")
    console.print(f"Run: {application.run_id or '-'}")

    table = Table(title="State transitions")
    table.add_column("when")
    table.add_column("from")
    table.add_column("to")
    table.add_column("category")
    table.add_column("reason")
    for t in rows:
        table.add_row(str(t.created_at), t.from_state or "-", t.to_state,
                      t.failure_category or "", (t.reason or "")[:70])
    console.print(table)


learned_app = typer.Typer(help="Inspect/manage answers jobbot has learned from your past applications")
app.add_typer(learned_app, name="learned")


@learned_app.command("list")
def learned_list(limit: int = typer.Option(50)) -> None:
    """Show remembered answers, most-reused first."""
    with session_scope() as session:
        rows = session.execute(
            select(LearnedAnswer).order_by(LearnedAnswer.times_used.desc()).limit(limit)
        ).scalars().all()

    table = Table(title="Learned answers")
    table.add_column("id")
    table.add_column("question")
    table.add_column("value")
    table.add_column("type")
    table.add_column("sensitive")
    table.add_column("used")
    for r in rows:
        value_preview = r.value if len(r.value) <= 60 else r.value[:57] + "..."
        table.add_row(str(r.id), r.label_raw, value_preview, r.field_type, "yes" if r.sensitive else "", str(r.times_used))
    console.print(table)
    settings = get_settings()
    if settings.jobbot_autofill_sensitive:
        console.print(
            "\n[yellow]JOBBOT_AUTOFILL_SENSITIVE is on[/yellow] — sensitive answers (marked 'yes') "
            "will be auto-filled after you type CONFIRM at the start of a run."
        )
    else:
        console.print(
            "\nSensitive answers are shown for reference only by default — you'll still type "
            "them each time, with the reminder shown during review. Set JOBBOT_AUTOFILL_SENSITIVE=true "
            "to auto-fill them instead (still requires confirming once per run)."
        )


@learned_app.command("forget")
def learned_forget(answer_id: int) -> None:
    """Delete one remembered answer, e.g. if it was captured wrong."""
    with session_scope() as session:
        row = session.get(LearnedAnswer, answer_id)
        if row is None:
            console.print(f"[red]No learned answer with id {answer_id}[/red]")
            raise typer.Exit(1)
        session.delete(row)
    console.print(f"[green]Forgot learned answer {answer_id}[/green]")


@learned_app.command("issues")
def learned_issues(limit: int = typer.Option(50)) -> None:
    """Show questions that have repeatedly failed to auto-fill. Once a
    question crosses the failure threshold it stops being retried
    automatically (jobbot/learning/store.py CIRCUIT_BREAKER_THRESHOLD) and
    goes straight to your review instead — this is where you'd notice a
    field jobbot genuinely can't handle for some employer's form."""
    with session_scope() as session:
        rows = session.execute(
            select(FieldIssue).order_by(FieldIssue.failure_count.desc()).limit(limit)
        ).scalars().all()

    if not rows:
        console.print("No fill failures recorded.")
        return

    table = Table(title="Fields that have failed to auto-fill")
    table.add_column("question")
    table.add_column("failures")
    table.add_column("last error")
    table.add_column("last seen")
    for r in rows:
        table.add_row(r.label_raw, str(r.failure_count), r.last_error[:60], str(r.last_seen_at))
    console.print(table)


if __name__ == "__main__":
    app()

"""Local live-queue dashboard.

The problem this solves: `jobbot run`/`batch` prints its progress as
scrolling terminal log lines, which makes it hard to answer "how many are
left" or "what's it doing right now" at a glance while you're actually the
one clicking Submit in the browser windows it opens.

This is a small, read-only local web page — it never drives the browser or
clicks anything itself. The actual apply flow still opens its own
Playwright-controlled window per job exactly as before; this is a second,
plain window (in your own browser) onto the same database, showing:

  - what's open right now, waiting for your click
  - what's queued up next, in the exact order batch/apply-all will use
  - what's already gone out or been skipped

Auto-refreshes every few seconds. Stdlib only — no new dependency for
something this small.
"""
from __future__ import annotations

import json
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from sqlalchemy import select

from jobbot.agent.states import CONSUMES_JOB
from jobbot.config import load_search_settings
from jobbot.db import session_scope
from jobbot.models import Application, Job, JobScore

_IN_PROGRESS_STATES = {
    "OPENING_APPLICATION", "INSPECTING_FORM", "FILLING", "VERIFYING_FIELDS",
    "READY_TO_SUBMIT", "SUBMITTING", "VERIFYING_SUBMISSION", "HUMAN_REVIEW",
}
_DONE_STATES = {"SUBMITTED", "SKIPPED", "FAILED", "COMPLETED", "BLOCKED"}


def _job_row(job: Job) -> dict:
    return {
        "id": job.id,
        "company": job.company,
        "title": job.title,
        "location": job.location or "",
        "url": job.url,
        "resume": job.matched_profile_tag or "",
    }


def _queue_data(min_score: float, limit: int) -> dict:
    settings_yaml = load_search_settings()
    sub_cfg = settings_yaml.get("submission", {})
    supported = set(sub_cfg.get("supported_ats", ["greenhouse", "lever"]))

    with session_scope() as session:
        consumed = {
            row[0] for row in session.execute(
                select(Application.job_id).where(Application.state.in_([s.value for s in CONSUMES_JOB]))
            )
        }

        current_row = session.execute(
            select(Application, Job)
            .join(Job, Job.id == Application.job_id)
            .where(Application.state.in_(_IN_PROGRESS_STATES))
            .order_by(Application.updated_at.desc())
            .limit(1)
        ).first()
        current = None
        if current_row is not None:
            app, job = current_row
            current = {**_job_row(job), "state": app.state, "since": str(app.updated_at)}

        upcoming = session.execute(
            select(Job, JobScore.llm_score)
            .join(JobScore, JobScore.job_id == Job.id)
            .where(JobScore.llm_score >= min_score, Job.ats.in_(supported))
            .order_by(JobScore.llm_score.desc())
        ).all()
        stack = [
            {**_job_row(job), "score": score}
            for job, score in upcoming if job.id not in consumed
        ][:limit]

        recent_rows = session.execute(
            select(Application, Job)
            .join(Job, Job.id == Application.job_id)
            .where(Application.state.in_(_DONE_STATES))
            .order_by(Application.updated_at.desc())
            .limit(25)
        ).all()
        recent = [
            {**_job_row(job), "state": app.state, "when": str(app.updated_at), "error": app.error or ""}
            for app, job in recent_rows
        ]

        return {
            "current": current,
            "stack": stack,
            "recent": recent,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }


_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>jobbot queue</title>
<style>
  body { font-family: -apple-system, Segoe UI, sans-serif; background: #0f1115; color: #e6e6e6; margin: 0; padding: 24px; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #888; font-size: 13px; margin-bottom: 24px; }
  .cols { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 20px; align-items: start; }
  .col h2 { font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; color: #9aa; border-bottom: 1px solid #333; padding-bottom: 8px; }
  .card { background: #1a1d24; border: 1px solid #2a2e38; border-radius: 8px; padding: 12px; margin-bottom: 10px; }
  .card.now { border-color: #e0a030; box-shadow: 0 0 0 1px #e0a03040; }
  .company { font-weight: 600; }
  .title { font-size: 13px; color: #ccc; margin: 2px 0; }
  .meta { font-size: 12px; color: #888; }
  .score { display: inline-block; background: #2a3a2a; color: #7fd77f; border-radius: 4px; padding: 1px 6px; font-size: 12px; margin-right: 6px; }
  .state { display: inline-block; border-radius: 4px; padding: 1px 6px; font-size: 12px; }
  .state.SUBMITTED { background: #2a3a2a; color: #7fd77f; }
  .state.SKIPPED { background: #333; color: #aaa; }
  .state.FAILED, .state.BLOCKED { background: #3a2a2a; color: #e08080; }
  .empty { color: #666; font-size: 13px; font-style: italic; }
  a { color: #8ab4ff; text-decoration: none; }
  a:hover { text-decoration: underline; }
</style>
</head>
<body>
<h1>jobbot &mdash; live queue</h1>
<div class="sub" id="ts">loading...</div>
<div class="cols">
  <div class="col">
    <h2>Now open</h2>
    <div id="current"></div>
  </div>
  <div class="col">
    <h2>Up next</h2>
    <div id="stack"></div>
  </div>
  <div class="col">
    <h2>Recently done</h2>
    <div id="recent"></div>
  </div>
</div>
<script>
function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

async function refresh() {
  const res = await fetch('/api/queue');
  const data = await res.json();
  document.getElementById('ts').textContent = 'updated ' + data.generated_at.replace('T', ' ').replace('+00:00', ' UTC');

  const cur = document.getElementById('current');
  cur.innerHTML = data.current
    ? `<div class="card now"><div class="company">${esc(data.current.company)}</div>
       <div class="title">${esc(data.current.title)}</div>
       <div class="meta">${esc(data.current.location)} &middot; ${esc(data.current.state)}</div>
       <div class="meta"><a href="${esc(data.current.url)}" target="_blank">open posting</a></div></div>`
    : '<div class="empty">Nothing open right now.</div>';

  const stack = document.getElementById('stack');
  stack.innerHTML = data.stack.length ? data.stack.map(j => `
    <div class="card"><span class="score">${j.score}</span><span class="company">${esc(j.company)}</span>
    <div class="title">${esc(j.title)}</div>
    <div class="meta">${esc(j.location)} &middot; ${esc(j.resume)}</div></div>
  `).join('') : '<div class="empty">Nothing queued at this score threshold.</div>';

  const recent = document.getElementById('recent');
  recent.innerHTML = data.recent.length ? data.recent.map(a => `
    <div class="card"><span class="state ${esc(a.state)}">${esc(a.state)}</span> <span class="company">${esc(a.company)}</span>
    <div class="title">${esc(a.title)}</div>
    <div class="meta">${esc(a.when)}${a.error ? ' &middot; ' + esc(a.error.slice(0, 80)) : ''}</div></div>
  `).join('') : '<div class="empty">Nothing yet.</div>';
}

refresh();
setInterval(refresh, 4000);
</script>
</body>
</html>
"""


def _make_handler(min_score: float, limit: int):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args) -> None:  # noqa: D401 — quiet by design
            pass

        def do_GET(self) -> None:  # noqa: N802 — http.server's naming convention
            if self.path.startswith("/api/queue"):
                try:
                    payload = json.dumps(_queue_data(min_score, limit)).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as exc:  # noqa: BLE001
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(str(exc).encode("utf-8"))
                return

            body = _PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return Handler


def serve(*, min_score: float = 60, limit: int = 30, port: int = 8787, open_browser: bool = True) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), _make_handler(min_score, limit))
    url = f"http://127.0.0.1:{port}/"
    print(f"jobbot dashboard: {url}  (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

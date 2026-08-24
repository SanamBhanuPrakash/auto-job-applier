# jobbot — local-first job discovery & assisted-application agent

Pulls job postings from public ATS/aggregator APIs, ranks them against your
resume with Claude, and drives a real browser (Playwright) to fill out
Greenhouse and Lever application forms for you — stopping for your review
before anything is ever submitted.

This exists because the popular "auto-apply" projects out there
(AIHawk/JobsApplierAIAgent, GodsScion's LinkedIn bot, and friends) all
converge on the same shape: scrape/discover via APIs where possible, use an
LLM + browser automation to fill forms, and — critically — the ones that skip
human review are also the ones with public case studies of fabricated resume
content and banned accounts. This project keeps the automation and drops the
part that gets people banned.

## What this does

1. **Discover** — polls public, keyless JSON APIs (Greenhouse, Lever, Ashby,
   SmartRecruiters, Recruitee, plus Adzuna/USAJobs/RemoteOK/Remotive) for job
   postings and stores them in a local SQLite DB. No scraping, no login.
2. **Match** — a cheap local keyword/location filter shortlists postings,
   then Claude reranks the shortlist against your parsed resume/profile and
   gives each a 0–100 fit score with reasoning.
3. **Apply** — for jobs on Greenhouse or Lever (the two ATSes with clean,
   guest-apply-friendly hosted forms), Playwright opens the real form, Claude
   proposes a fill plan using *only* facts from your profile, every field
   gets filled and verified, and then **you** review a screenshot and the
   list of fields it left blank before typing `yes` to actually submit.
4. **Remember** — every field it fills (or you fill) gets captured under a
   normalized version of its question text. The next application that asks
   the same thing — even worded differently ("Are you authorized to work in
   the US?" vs "Are you legally authorized to work in the United States?")
   — reuses the answer instead of re-asking the model or you. This is what
   makes it adapt over time instead of repeating the same LLM call (and, for
   fields you had to type yourself, the same typing) on every single
   application. See "Memory" below for exactly what is and isn't reused.

## What this deliberately does NOT do

- **No LinkedIn or Indeed automation.** Both explicitly prohibit this in
  their ToS, both fingerprint and rate-limit aggressively, and the most
  visible open-source LinkedIn auto-apply project had its own creator's
  account restricted. If you want LinkedIn/Indeed postings, use their site
  directly and apply by hand.
- **No unsupervised mass-submit by default.** `JOBBOT_AUTO_SUBMIT` defaults
  to `false`, and even when set to `true`, the CLI still forces manual
  review for any application where a field was left blank.
- **No auto-answering of sensitive questions.** Work authorization/visa
  sponsorship, EEOC/demographic questions (veteran status, disability, race,
  ethnicity, gender), legal attestations, government IDs, and
  salary-history/background questions are *hard-coded* to always stop for
  human review (`jobbot/submit/fill_planner.py`), regardless of what the
  model would have guessed. This isn't a suggestion in a prompt — it's a
  regex check that overrides the model's own output.

## Architecture

```
config/                  # your personal, gitignored config (copy the .example files)
jobbot/
  discovery/              # one module per source, all normalize to NormalizedJob
    greenhouse.py lever.py ashby.py smartrecruiters.py recruitee.py
    adzuna.py usajobs.py remoteok.py remotive.py
    aggregate.py          # fans out, dedupes by (source, external_id), persists
  resume/
    parser.py             # pdf/docx/txt -> text -> Claude -> Profile
    schema.py             # Profile pydantic model (the only source of "facts")
  matching/
    lexical.py             # free, fast keyword/location shortlist
    score.py                # Claude reranks the shortlist, writes JobScore
  submit/
    form_scan.py           # generic DOM scanner (injects data-jobbot-id, handles
                            # native selects, react-aria comboboxes, radio groups)
    values.py                # reads a field's current human-readable value (shared by verify + learning capture)
    fill_planner.py         # Claude fill plan + the hard-coded sensitive-field guardrail
    filler.py               # applies the plan, verifies, retries once on empty
    greenhouse.py lever.py  # just the submit-button selector + form-ready wait
    review.py               # screenshot + terminal confirmation gate (now shows memory hints too)
    base.py                  # orchestrates one application attempt end to end
  learning/
    normalize.py             # question label -> stable matching key
    store.py                  # lookup/upsert/capture against the learned_answers table
  cli.py                   # typer CLI: discover / match / list / show / apply / batch / ledger / learned
  models.py, db.py         # SQLAlchemy: Job, JobScore, Application, LearnedAnswer
tests/                    # discovery parsers + the guardrail regex, no network/browser needed
```

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

cp .env.example .env                        # add your ANTHROPIC_API_KEY
cp config/companies.example.yaml config/companies.yaml
cp config/settings.example.yaml config/settings.yaml
```

Then import your resume (this calls Claude once to structure it):

```bash
jobbot resume import ~/Documents/resume.pdf
```

This writes `config/profile.yaml`. **Open it and check it** — especially
that it didn't put words in your mouth, and that the sensitive fields
(`work_authorization`, `veteran_status`, etc.) are still `null` unless you
deliberately want a pre-filled *suggestion* shown to you during review.

## Usage

```bash
jobbot discover              # pull new postings into the local DB
jobbot match                 # shortlist + Claude-score the new ones
jobbot list --min-score 70   # see what's worth applying to
jobbot show 42                # full posting + score reasoning for job id 42
jobbot apply 42                # open a real browser, fill it, review, confirm, submit
jobbot batch --min-score 80 --limit 5   # do several, paced 45-180s apart, still reviewed one by one
jobbot ledger                # what you've actually submitted, and when
jobbot learned list           # what it's remembered so far, and how often each answer's been reused
jobbot learned forget <id>    # delete one remembered answer (e.g. you mistyped it once)
```

`jobbot apply`/`batch` launch a **visible** (headed) Chromium window by
default (`JOBBOT_HEADLESS=false`) using a persistent profile stored in
`data/browser_profile/` — so if a site ever needs a manual login, it only
asks once. Every attempt gets a full-page screenshot in
`data/screenshots/` and a row in the `applications` table regardless of
whether you end up submitting.

## Memory: how it adapts across applications

Every non-file field that ends up with a value — whether Claude filled it or
you typed it in during review — gets captured in `learned_answers`, keyed by
a normalized version of its question text (`jobbot/learning/normalize.py`
strips case/punctuation/trailing "*"; `find_match` also does a fuzzy
token-set match, so "Are you authorized to work in the US?" and "Are you
legally authorized to work in the United States?" resolve to the same
learned answer). The next form that asks a matching question skips the LLM
call for it entirely and, for non-sensitive fields, fills it in directly.

What does and doesn't get reused, on purpose:

- **Short factual answers** (name, email, years of experience, "are you
  willing to relocate", school, etc.) — reused automatically once learned.
- **Sensitive fields** (work authorization, EEOC/demographic questions,
  legal attestations — the same regex from `fill_planner.py`) — still
  *never* auto-filled. What changes is the review table now shows "you
  answered before: <value>" next to them, so you're not hunting for your own
  past answer, but you still type it into the browser yourself every time.
- **Long free text** (cover letters, "why do you want to work here",
  textareas over ~200 characters) — never learned/reused verbatim. Reusing
  a canned paragraph across different employers reads as spam and is
  explicitly excluded regardless of length settings.

Run `jobbot learned list` to see everything it's picked up and how many
times each has been reused; `jobbot learned forget <id>` deletes one if it
got captured wrong (e.g. you fat-fingered an answer once).

What this does *not* do: self-heal a broken CSS selector when an ATS
changes its DOM structure. That would need a vision-based approach (à la
Skyvern) rather than the label-matching used here. When a fill genuinely
breaks, it shows up as an `error` status in `jobbot ledger` — that's your
signal to open an issue/PR against the relevant `submit/<ats>.py`, not
something the bot patches itself.

## Guardrails, and how to loosen them safely

The default posture is: discover and score autonomously, but a human looks
at every application before it goes out. That maps to Stage 1 + Stage 2 of
the staged rollout below. Stage 3 (batch auto-submit) is *possible* — set
`JOBBOT_AUTO_SUBMIT=true` — but only worth doing once you've watched enough
supervised runs to trust the fill plans, and it still refuses to auto-submit
any application that has a field flagged `needs_human`.

1. **Discovery + scoring only** (no browser automation at all) — just run
   `discover` and `match`, apply manually to whatever scores well. Zero
   platform-ToS risk, useful on its own.
2. **Assisted, reviewed submission** (the default) — `apply`/`batch` fill
   the form and wait for your typed `yes`.
3. **Selective batch auto-submit** — only after you've supervised enough
   runs to trust it, for a whitelisted set of ATSes/companies, with the
   sensitive-field guardrail still always on.

Other things baked in rather than left to a prompt:
- Randomized pacing (`pacing_seconds_min/max` in `settings.yaml`) between
  batch applications so it doesn't look scripted.
- HTTP calls retry with exponential backoff on 429/5xx
  (`jobbot/utils/ratelimit.py`).
- The résumé file is uploaded directly via the file `<input>` element, never
  through a UI click (which would open a native OS file picker Playwright
  can't drive).
- Company slugs for ATS discovery must be supplied by you in
  `companies.yaml` — there's no enumeration endpoint, and it keeps discovery
  scoped to employers you actually chose.

## Extending to more ATSes

`Ashby`, `SmartRecruiters`, and `Recruitee` are already wired up for
*discovery* (see `config/companies.example.yaml`) but not for *submission* —
`job.ats` is left empty for them, so `jobbot apply` will refuse with "no
submission handler" rather than guess. To add one, look at
`jobbot/submit/lever.py` as the minimal template (just a form-ready wait and
a submit-button selector — the generic scanner/filler handle the rest) and
`jobbot/submit/greenhouse.py` for a platform with non-native dropdowns.
Workday is explicitly out of scope here — the research this was built from
flags it as needing a vision-LLM approach, not a static-selector one.

## Testing

```bash
pytest
```

Tests cover the discovery parsers (mocked HTTP via `respx`, no network), the
lexical shortlist scorer, the `Profile` schema, the learning store's
normalize/fuzzy-match/upsert behavior (in-memory SQLite, no real DB touched),
and — most importantly — `test_fill_planner_guardrails.py`, which checks
that every sensitive-topic form label actually trips the forced-human-review
regex. If you edit that regex, that test is the one to watch.

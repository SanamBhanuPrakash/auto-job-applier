# jobbot — local-first job discovery & assisted-application agent

Pulls job postings from public ATS/aggregator APIs, ranks them against your
resume with an LLM (Groq's free tier by default, Claude if you'd rather pay
for it), and drives a real browser (Playwright) to fill out Greenhouse and
Lever application forms for you — stopping for your review before anything
is ever submitted.

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
   postings, keeps only postings from roughly the last 1-2 days (configurable;
   some sources list postings that are actually years old), and stores them
   in a local SQLite DB, deduped so re-running never creates duplicates. No
   scraping, no login.
2. **Match** — a cheap local keyword/location filter shortlists postings,
   then the LLM reranks the shortlist against your parsed resume/profile and
   gives each a 0–100 fit score with reasoning.
3. **Apply** — for jobs on Greenhouse or Lever (the two ATSes with clean,
   guest-apply-friendly hosted forms), Playwright opens the real form, the
   LLM proposes a fill plan using *only* facts from your profile, every field
   gets filled and verified, and then **you** review a screenshot and the
   list of fields it left blank before typing `yes` to actually submit. Open-
   ended questions ("Why do you want to work here?") get a real, specific
   answer grounded in the job context and your actual background — not left
   blank, and written to read like a person wrote it (varied phrasing,
   concrete details, none of the "As a passionate..." tells) rather than
   obvious template filler. See `jobbot/submit/fill_planner.py`'s system
   prompt for exactly what "specific, not generic" means here.
4. **Remember** — every field it fills (or you fill) gets captured under a
   normalized version of its question text. The next application that asks
   the same thing — even worded differently ("Are you authorized to work in
   the US?" vs "Are you legally authorized to work in the United States?")
   — reuses the answer instead of re-asking the model or you, so a run that
   started with every field flagged for review gets faster and quieter the
   more you use it. See "Memory" below for exactly what is and isn't reused,
   and under what conditions sensitive questions get included.
5. **Route by resume** — if you have more than one resume (SDE, Python
   developer, AI engineer, frontend, backend, full-stack, cloud engineer,
   ...), drop them all in `config/resumes/` and jobbot picks whichever one
   actually fits each posting instead of using one resume for everything.
   See "Multiple resumes" below.

## What this deliberately does NOT do

- **No LinkedIn or Indeed automation.** Both explicitly prohibit this in
  their ToS, both fingerprint and rate-limit aggressively, and the most
  visible open-source LinkedIn auto-apply project had its own creator's
  account restricted. If you want LinkedIn/Indeed postings, use their site
  directly and apply by hand.
- **No unsupervised mass-submit by default.** `JOBBOT_AUTO_SUBMIT` defaults
  to `false`, and even when set to `true`, the CLI still forces manual
  review for any application where a field was left blank.
- **No LLM-guessed answers to sensitive questions, ever.** Work
  authorization/visa sponsorship, EEOC/demographic questions, legal
  attestations, government IDs, and salary-history/background questions are
  *hard-coded* (`jobbot/submit/fill_planner.py`) to never be answered by the
  model's own guess — that's a regex check the model's output can't
  override. What *can* happen is reusing an answer *you* already gave once,
  and only if you've explicitly turned that on — see "Memory" below.

## Architecture

```
config/                  # your personal, gitignored config (copy the .example files)
jobbot/
  discovery/              # one module per source, all normalize to NormalizedJob
    greenhouse.py lever.py ashby.py smartrecruiters.py recruitee.py
    adzuna.py usajobs.py remoteok.py remotive.py
    recency.py             # parses each source's posted_at format, filters to recent postings
    aggregate.py          # fans out, dedupes by (source, external_id), applies recency filter, persists
  resume/
    parser.py             # pdf/docx/txt -> text -> Claude -> Profile
    schema.py             # Profile pydantic model (the only source of "facts")
    multi.py                # config/resumes/ folder -> one tagged ResumeProfile per file
  matching/
    lexical.py             # free, fast keyword/location shortlist
    profile_select.py       # picks which ResumeProfile fits a job best (lexical, no LLM call)
    score.py                # Claude reranks the shortlist against the matched profile, writes JobScore
  submit/
    form_scan.py           # generic DOM scanner (injects data-jobbot-id, handles
                            # native selects, react-aria comboboxes, radio groups) +
                            # find_target_frame(), which follows an <iframe> embed
                            # when the employer's careers page has no top-level <form>
    values.py                # reads a field's current human-readable value (shared by verify + learning capture)
    fill_planner.py         # Claude fill plan + the hard-coded sensitive-field guardrail
    filler.py               # applies the plan, verifies, retries once on empty
    greenhouse.py lever.py  # just the submit-button selector + form-ready wait
    review.py               # screenshot + terminal confirmation gate (now shows memory hints too)
    base.py                  # orchestrates one application attempt end to end
  learning/
    normalize.py             # question label -> stable matching key
    store.py                  # lookup/upsert/capture against learned_answers + field_issues
                              # (fuzzy match, value_still_offerable, circuit breaker)
  cli.py                   # typer CLI: run / discover / match / list / show / apply / batch / ledger / learned / resume
  models.py, db.py         # SQLAlchemy: Job, JobScore, Application, LearnedAnswer, ResumeProfile, FieldIssue
tests/                    # discovery parsers + the guardrail regex, no network/browser needed
```

## Discovery coverage: India + worldwide remote

`config/companies.example.yaml` ships with ~50 companies, every slug
confirmed live (not guessed) against each ATS's API right before being
added, including several with substantial India-specific hiring (CRED and
Meesho's Lever boards are majority-India; HighRadius, PhonePe, MongoDB,
Databricks, Okta, GitLab all have real India-based roles among their
listings). Be aware of a real limit here: most large India-founded
unicorns use an ATS this project doesn't talk to — Darwinbox, Keka,
Turbohire, or an in-house system — not Greenhouse/Lever/Ashby/
SmartRecruiters/Recruitee, so a long list of well-known Indian company
names was checked while building this and came back empty for exactly that
reason. Company-board discovery alone gets you real but modest India
volume (confirmed on a live run: ~170 India-tagged postings out of ~1,700
discovered).

**Adzuna is the bigger lever for India (and worldwide remote) volume**,
because it searches *across* many employer sites and boards by city rather
than needing one entry per employer here. It's free but needs its own
signup (`developer.adzuna.com`, no cost, 1000 calls/month) — set
`ADZUNA_APP_ID`/`ADZUNA_APP_KEY` in `.env` and `aggregators.adzuna.enabled:
true` in `settings.yaml`. Confirmed live that Adzuna covers India plus 17
other countries (US/UK/Canada/Australia/Germany/France/Spain/Italy/
Netherlands/Austria/Belgium/Brazil/Mexico/New Zealand/Poland/Singapore/
South Africa) — `settings.example.yaml`'s `aggregators.adzuna.queries` is
pre-filled with one search per major Indian city (Bangalore, Hyderabad,
Mumbai, Pune, Delhi NCR, Chennai, Kerala) plus remote-US and remote-UK;
add, remove, or repoint rows freely, each is one extra API call per
`jobbot discover`. RemoteOK and Remotive (no signup needed, already
enabled by default) round out worldwide-remote coverage but have no
India-specific filter of their own — `aggregators.remoteok.tags` /
`.remotive.categories` both take a list now (one entry per domain your
resumes span) instead of a single tag, so multi-resume setups aren't
narrowed to whatever the first resume happened to search for.

`search.locations` in `settings.example.yaml` now includes Bangalore/
Bengaluru/Hyderabad/Mumbai/Pune/Noida/Delhi/Gurgaon/Chennai/Kerala/Kochi
alongside "remote" — this is the cheap lexical pre-filter that runs before
LLM scoring (`jobbot/matching/lexical.py`); a posting located in one of
these cities scores 0 on the location component otherwise, even though the
later per-resume LLM scoring would judge it correctly on its own. Known,
not-yet-fixed limitation: that lexical filter's `keywords` list is global,
not per-resume — with multiple resumes spanning different domains
(frontend vs. cloud vs. data science, say), it runs once before any
per-resume matching happens, so keep it broad rather than narrowly backend-
specific if you're relying on `config/resumes/`.

## Setup

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
playwright install chromium

cp .env.example .env
cp config/companies.example.yaml config/companies.yaml
cp config/settings.example.yaml config/settings.yaml
```

**Get an LLM key.** By default this uses **Groq's free tier** — no credit
card, sign up at [console.groq.com/keys](https://console.groq.com/keys),
paste the key into `.env` as `GROQ_API_KEY`. This is deliberate, not just a
cost-cutting default: Groq's account-wide policy (checked directly, not
assumed) is that it does not train on your inputs/outputs even on the free
tier, which matters since your resume is going through it. The one real
trade-off is a 6,000-tokens/minute rate limit on the free tier — that's why
`matching/score.py` batches conservatively and `jobbot/llm.py` retries
rate-limit errors with backoff instead of failing.

**Have a Google AI subscription and want to use Gemini instead?** A Google
AI Pro/Ultra subscription is a separate consumer product from the Gemini
API and doesn't grant API access either (checked directly — same gap as
Claude Pro not covering the Anthropic API). You still need a free API key
from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) (no
card required), then set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY` in
`.env`. Roomier free-tier limits than Groq (250k tokens/minute vs. 6k as of
the 3.7 Flash generation) — but weigh this: Gemini's free tier terms let
Google use your inputs/outputs to improve their models, which Groq's free
tier explicitly does not, and your resume is what's going through it. If
that trade-off doesn't sit right, enabling Cloud Billing on the same key
removes that clause (and raises the limits further) for a small per-token
cost — or just stick with Groq.

**A Claude.ai Pro/Max subscription does NOT work here** — that covers the
chat app and Claude Code itself, not the separate pay-as-you-go Anthropic
API (`api.anthropic.com`) this project would otherwise call. If you'd
rather use Claude and don't mind its (usage-based, typically well under $1
for a heavy day of resume-parsing + scoring + form-filling at this
project's scale — but a real charge, unlike Groq/Gemini's free tiers)
cost, get a key at
[console.anthropic.com](https://console.anthropic.com), set
`LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY` in `.env` instead, and feel
free to raise `matching/score.py`'s `BATCH_SIZE` back up since Anthropic has
no comparable per-minute ceiling at this scale.

Then import your resume (this calls the LLM once to structure it):

```bash
jobbot resume import ~/Documents/resume.pdf
```

This writes `config/profile.yaml`. **Open it and check it** — especially
that it didn't put words in your mouth, and that the sensitive fields
(`work_authorization`, `veteran_status`, etc.) are still `null` unless you
deliberately want a pre-filled *suggestion* shown to you during review.

**Have more than one resume?** Skip the above and instead drop them all in
`config/resumes/`, named by role, and import the whole folder — see
"Multiple resumes" below.

## Usage

The fastest path once setup is done — discover the last ~2 days of
postings, score them, and apply to what clears the bar, in one command:

```bash
jobbot run --min-score 80 --limit 5
```

Or step by step:

```bash
jobbot resume import-folder                # parse everything in config/resumes/, tag by filename
jobbot resume list-profiles                # see each tag + how many jobs are matched to it

jobbot discover              # pull new postings from the last ~2 days into the local DB
jobbot match                 # shortlist, pick each job's best-fitting resume, Claude-score it
jobbot list --min-score 70   # see what's worth applying to (shows matched resume + already-applied)
jobbot show 42                # full posting + score reasoning + matched resume for job id 42
jobbot apply 42                # open a real browser, fill it, review, confirm, submit
jobbot batch --min-score 80 --limit 5   # do several, paced 45-180s apart, still reviewed one by one
jobbot ledger                # what you've actually submitted, and when
jobbot learned list           # what it's remembered so far, and how often each answer's been reused
jobbot learned forget <id>    # delete one remembered answer (e.g. you mistyped it once)
jobbot learned issues          # questions that keep failing to auto-fill (circuit-broken ones)
```

`jobbot apply`/`batch`/`run` launch a **visible** (headed) Chromium window
by default (`JOBBOT_HEADLESS=false`) using a persistent profile stored in
`data/browser_profile/` — so if a site ever needs a manual login, it only
asks once. Every attempt gets a full-page screenshot in
`data/screenshots/` and a row in the `applications` table regardless of
whether you end up submitting.

**Re-running is always safe.** `discover` dedupes postings by
`(source, external_id)` — running it again never creates duplicate jobs.
`batch`/`run` never re-attempt a job that already has a `submitted`
Application on record (`jobbot list`'s `applied` column and `jobbot show`
both surface this); `jobbot apply <id>` refuses on an already-submitted job
unless you pass `--force`. So `jobbot run` on a schedule (cron, or
`scripts/run_review_apply.sh`) genuinely just picks up what's new each time.

**Recency:** discovery keeps only postings from roughly the last
`search.posted_within_days` (default 2) in `settings.yaml` — some ATS APIs
(Ashby in particular, in testing) list postings that are actually years
old, so this isn't just a nicety. Override per run with `--days` on
`discover`/`run`, or set it to `0` to disable and see everything a source
currently lists.

## Memory: how it adapts across applications

Every non-file field that ends up with a value — whether Claude filled it or
you typed it in during review — gets captured in `learned_answers`, keyed by
a normalized version of its question text (`jobbot/learning/normalize.py`
strips case/punctuation/trailing "*"; a shared fuzzy token-set matcher means
"Are you authorized to work in the US?" and "Are you legally authorized to
work in the United States?" resolve to the same learned answer). The next
form that asks a matching question skips the LLM call for it entirely.

**Non-sensitive fields** (name, email, years of experience, "are you willing
to relocate", school, dropdowns, location fields, etc.) are auto-filled from
memory by default once learned — no setting needed, that's the whole point.
Before reusing a remembered value for a select/radio field, jobbot checks
that *this specific posting's* option list actually still offers it
(`value_still_offerable`) — a remembered "OPT" isn't applied to a form whose
visa-type dropdown only offers "H1B/L1/None", it falls back to the model
instead.

**Sensitive fields** (work authorization, EEOC/demographic questions, legal
attestations, government IDs, salary history/background — the same regex
from `fill_planner.py`) are different: reusing your own already-confirmed
answer isn't the model guessing/fabricating anything, but it's still a
legal representation about you that can go stale (visa status changes,
etc.), so it's opt-in, not default:

- **Off by default** (`JOBBOT_AUTOFILL_SENSITIVE=false`) — sensitive fields
  always stop for your review, same as before. The review screen shows "you
  answered before: `<value>`" next to them so you're not hunting for your
  own past answer, but you type it into the browser yourself every time.
- **Turn it on** (`JOBBOT_AUTOFILL_SENSITIVE=true`, or `--autofill-sensitive`
  on `apply`/`batch`) and jobbot will auto-fill them from memory too — but
  every run first prints exactly which saved question → answer pairs it's
  about to reuse and requires you to type `CONFIRM`, once, before anything
  happens. Not per application — once per run, so "click run and walk away"
  still works after that. Every auto-filled sensitive answer is also
  screenshotted and logged in the ledger for after-the-fact audit, and the
  option-still-offered check above applies to these too.

**Long free text** (cover letters, "why do you want to work here", textareas
over ~200 characters) is never learned/reused verbatim regardless of any
setting — a canned paragraph reused across employers reads as spam, so this
exclusion isn't configurable.

Run `jobbot learned list` to see everything it's picked up and how many
times each has been reused; `jobbot learned forget <id>` deletes one if it
got captured wrong (e.g. you fat-fingered an answer once).

**Adapting to fill failures:** if jobbot plans to auto-fill a field (from
the model or from memory) and the value doesn't verify after a retry, that
failure is recorded per question. After `CIRCUIT_BREAKER_THRESHOLD` (2)
failures on the same question, it stops being attempted automatically and
routes straight to your review instead of repeatedly failing the same way —
`jobbot learned issues` shows what's tripped this. The moment a value is
successfully captured for that question again (you fill it by hand, or a
form change fixes it), the failure count clears and auto-fill resumes being
attempted.

What this does *not* do: self-heal a broken CSS selector when an ATS
changes its DOM structure — the circuit breaker above stops it from
*wasting time repeating* a structural failure, but fixing the actual
selector still needs a code change. Full self-healing would need a
vision-based approach (à la Skyvern) rather than the label-matching used
here. `jobbot ledger`/`jobbot learned issues` are where you'd notice this is
needed for a given employer's form.

## Multiple resumes

If you have several resumes for different roles — SDE, Python developer, AI
engineer, frontend, backend, full-stack, cloud engineer, whatever you've
got — drop them all in `config/resumes/`, named by role:

```
config/resumes/
  python-developer.pdf
  ai-engineer.pdf
  frontend.pdf
  backend.pdf
  full-stack.pdf
  cloud-engineer.pdf
```

Then:

```bash
jobbot resume import-folder     # parses each one (filename -> tag, e.g. "ai-engineer")
jobbot match                    # picks the best-fitting resume per job, THEN scores against it
jobbot apply 42                 # uploads job 42's matched resume, not a fixed default
```

The match is a cheap lexical comparison (`jobbot/matching/profile_select.py`
— job title/description vs. each profile's tag/skills/desired titles, no
LLM call) run before scoring, so a Python-heavy posting gets scored against
your `python-developer` resume's actual facts and a cloud posting gets
scored against `cloud-engineer`'s — rather than every job being judged
against one resume that only fits some of them. `jobbot resume
list-profiles` shows each tag and how many jobs it's currently matched to;
`jobbot show <id>` shows which resume a specific job was matched to. If
`config/resumes/` is empty or unused, everything falls back to the single
`config/profile.yaml` from `jobbot resume import`, unchanged from before.

## Guardrails, and how to loosen them safely

The default posture is: discover and score autonomously, but a human looks
at every application before it goes out. That maps to Stage 1 + Stage 2 of
the staged rollout below. Stage 3 (batch auto-submit, optionally including
sensitive fields) is available once you've watched enough supervised runs to
trust it.

1. **Discovery + scoring only** (no browser automation at all) — just run
   `discover` and `match`, apply manually to whatever scores well. Zero
   platform-ToS risk, useful on its own.
2. **Assisted, reviewed submission** (the default) — `apply`/`batch` fill
   the form and wait for your typed `yes`.
3. **Batch auto-submit** — `--auto-submit` / `JOBBOT_AUTO_SUBMIT=true`
   skips the per-application confirmation, but *only* submits an
   application where every field was filled with nothing flagged for
   review; anything with a `needs_human` field still stops. Combine with
   `--autofill-sensitive` / `JOBBOT_AUTOFILL_SENSITIVE=true` (one `CONFIRM`
   per run, not per application — see "Memory" above) once you trust the
   saved answers, and a `batch` run really can go end-to-end unattended
   after that one confirmation.

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

## Extending to more ATSes / sites

`Ashby`, `SmartRecruiters`, and `Recruitee` are already wired up for
*discovery* (see `config/companies.example.yaml`) but not for *submission* —
`job.ats` is left empty for them, so `jobbot apply` will refuse with "no
submission handler" rather than guess. To add one, look at
`jobbot/submit/lever.py` as the minimal template (just a form-ready wait and
a submit-button selector — the generic scanner/filler handle the rest) and
`jobbot/submit/greenhouse.py` for a platform with non-native dropdowns.
Workday is explicitly out of scope here — the research this was built from
flags it as needing a vision-LLM approach, not a static-selector one.

**SmartRecruiters was checked and ruled out for submission, not just left
undone.** Its application-submission page
(`jobs.smartrecruiters.com/oneclick-ui/...`) is protected by DataDome, a
commercial anti-bot/CAPTCHA service — confirmed live against two unrelated
companies' postings (SmartRecruiters' own careers page and a Sandisk
posting), so it's a platform-wide protection on the submit flow, not one
employer's configuration. The read-only job-listing pages have no such
protection, which is why discovery still works fine. Building around a
deployed CAPTCHA would mean either a CAPTCHA-solving service or some other
detection-evasion technique — not something this project does, for the same
reason it doesn't automate LinkedIn/Indeed.

**Wellfound and Cutshort were also checked, not assumed.** Both require a
logged-in account to apply at all (there's no guest-apply hosted form the
way Greenhouse/Lever have) — Wellfound's own Terms explicitly warn against
automating past the manual apply click, and both platforms are the kind of
authenticated, account-based service where automated traffic risks the
account, the same category as LinkedIn/Indeed. Neither is implemented here,
for the same reason those aren't.

None of this rules out *discovery* for platforms with public listings —
just automated *submission* where a site has specifically defended against
it (technically, contractually, or both).

## Testing

```bash
pytest
```

Tests cover the discovery parsers (mocked HTTP via `respx`, no network), the
recency filter (`test_recency.py` — real date shapes from each source, plus
the "unparseable dates are kept, not dropped" rule), the lexical shortlist
scorer, the resume-profile matcher (`test_profile_select.py`), the
multi-resume folder importer against an isolated in-memory DB
(`test_resume_multi.py`, `parse_resume` mocked so no API key is needed), the
already-applied dedup (`test_cli_dedup.py`), the `Profile` schema, the
learning store's normalize/fuzzy-match/upsert/circuit-breaker behavior
(in-memory SQLite, no real DB touched), `test_fill_planner_guardrails.py`
(every sensitive-topic form label trips the forced-human-review regex), and
`test_browser_form_handling.py`, which is the one to trust most:
it drives a real headless Chromium against local HTML fixtures
(`tests/fixtures/application_form.html` and `careers_page_with_iframe.html`)
through the actual scan → fill → verify → capture pipeline `jobbot apply`
uses, including the sensitive-field-autofill mechanics and the
still-offered-as-an-option check — no mocks, no network, no LLM call needed
since fill plans are hand-built for the tests. Requires `playwright install
chromium` and skips itself (rather than failing) if that hasn't been run.

That test suite is also what's caught every real bug found while building
this so far: a `boards.greenhouse.io` vs. iframe-embedded-on-the-employer's-
own-domain mismatch (most Greenhouse customers — Stripe, Airbnb, Coinbase,
Pinterest, Instacart, Asana — embed the form on their own branded careers
page rather than serving it at Greenhouse's own hosted URL; `find_target_frame()`
in `form_scan.py` now follows that iframe when there's no top-level `<form>`),
a `token_set_ratio` vs. plain `ratio` gap in the learning matcher's fuzzy
threshold that let a real reworded work-authorization question fall through
unmatched, and — when the circuit breaker was added — the same fuzzy-match
gap turning up again in a second place (`is_circuit_broken` was doing an
exact-key lookup only); both now share one `_fuzzy_find` helper so the gap
can't reopen in just one of the two call sites.

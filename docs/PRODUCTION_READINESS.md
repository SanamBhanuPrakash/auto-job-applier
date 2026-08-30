# Production readiness

Spec §95–§97 define five levels and their gates. This document says which
level this system is actually at, with the evidence, and what each further
level would require.

**Current level: LEVEL 1 — CONTROLLED TEST.**

Not Level 2, and specifically not Level 3. The reasons are listed below
rather than summarised, because "production ready" is exactly the phrase
§131 forbids without evidence.

Regenerate the evidence for the claims here with:

```
jobbot eval            # fault-injection suite, offline, ~40s
pytest -q              # full suite including the eval harness
```

---

## The levels

| Level | Meaning | Status |
|---|---|---|
| 0 — Development | Runs on a developer machine, no safety claims | passed |
| 1 — Controlled test | Safety substrate proven against fixtures under injected faults | **current** |
| 2 — Pilot | Proven against *real* postings, small volume, human reviewing each | **not reached** |
| 3 — Production | Sustained unattended operation on real postings | not reached |
| 4 — High-scale | Concurrent workers, throttling, queue durability | not reached |

Levels are never entered automatically. Each needs the evidence below.

---

## Level 1 gates (§96) — what actually passes

| Gate | Status | Evidence |
|---|---|---|
| Complete unit suite passes | PASS | `pytest -q` |
| Browser fixture suite passes | PASS | real Chromium, ~20 fixture pages |
| Fault-injection suite passes | PASS | `jobbot eval` — 45 pass, 0 fail, 3 skip |
| Recovery suite passes | PASS | `tests/test_agent_recovery.py` |
| Checkpoint/restart suite passes | PARTIAL | state-machine level only; no process-crash harness |
| Duplicate race suite passes | PASS | verified with three real OS processes |
| Prompt-injection suite passes | PASS | `tests/test_prompt_injection.py` |
| Credential leakage tests pass | PASS | credential fields refused at the tool boundary |
| Sensitive-field tests pass | PASS | failures §13 and §15 regressions |
| Submission verification suite passes | PASS | including failures §17 |
| Auth suite passes | PASS | `tests/test_auth_*.py`, 66 tests; 9 eval auth scenarios |
| Integration suite passes | **FAIL** | no end-to-end run against a real posting |
| Traces are complete | **FAIL** | per-step traces are in memory, not persisted |
| No known Critical defect remains | PASS | none open; four found and fixed this cycle |

Two gates fail. That is why this is still Level 1 — and note which two:
both are about *evidence from the real world*, not about missing code. No
amount of further local building moves them.

## Critical metrics (§93)

Measured by `jobbot eval` against fixture pages under injected faults:

| Metric | Value | Meaning |
|---|---|---|
| `false_submission_rate` | 0.00% | nothing was recorded as submitted without proof |
| `duplicate_submission_rate` | 0.00% | one idempotency key per posting across sources |
| `wrong_sensitive_answer_rate` | 0.00% | no sensitive field was ever authorized for the agent |

**What these numbers do and do not mean.** They are measured against local
fixture pages, not against real ATS sites. They say the safety logic
behaves correctly on the failure shapes we know how to reproduce. They do
not say anything about behaviour on a real posting, because no such run
has happened — see LIMITATIONS.md.

## What Level 2 (pilot) would require

1. A real end-to-end run against a live posting, start to finish, with the
   submission verified by looking at the employer's confirmation email.
   **This is the binding constraint.** Everything else on this list is
   code; this one is evidence, and there is no substitute for it.
2. Per-step traces persisted to the database, so a pilot can be audited
   after the fact rather than from log scrollback.
3. Twenty-plus real applications with a human reviewing every one, and the
   three critical metrics still zero across them.
4. A process-crash harness: kill the process mid-`SUBMITTING` and confirm
   restart escalates rather than retrying. The state machine says it will;
   that has not been demonstrated against a real process death.

## What Level 3 (production) would additionally require

5. Multi-page application flow control.
6. A pilot period with no Critical defect found.

The auth subsystem (§25) is **done** — login, session reuse, expiry
detection, credential isolation and an auth-failure circuit breaker. What
it deliberately refuses (third-party SSO, one-time codes, CAPTCHA, account
creation) still stops for a human, so unattended operation on sites using
those is out of scope by design rather than by omission.

## What Level 4 (high-scale) would additionally require

8. Worker pool with proven queue durability and restart recovery.
9. Per-domain throttling proven under concurrency.
10. Throughput and resource consumption actually benchmarked.

Browser identity isolation (item 9's prerequisite) is **done** — each
worker gets its own Chromium profile directory. The rest is not started.

---

## On the throughput target

The original brief mentioned 10,000 applications per week. That is roughly
60 per hour sustained, which is a volume employers and ATS vendors treat
as spam, and is the behaviour that gets accounts flagged and IPs blocked.

Nothing in this repo hardcodes that number or optimises toward it. What
exists is measurement scaffolding (`verified applications/hour` in
`jobbot/eval/metrics.py`) so that whatever rate is chosen can be observed
rather than assumed. The rate that matters is *verified* applications, and
that number is currently unmeasured because no verified real submission
has been made.

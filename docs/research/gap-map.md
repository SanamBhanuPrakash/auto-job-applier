# Gap map: spec capability → current tree

Assessed against the merged tree at `ba1f55f` (215 tests passing). Updated as
slices land.

Legend: **DONE** · **PARTIAL** · **GAP** · **N/A-yet** (deliberately deferred)

## Safety / reliability substrate — largely DONE, reuse as the safety kernel

| Capability | Status | Where |
|---|---|---|
| Canonical identity (cross-source) | DONE | `agent/identity.py` |
| Idempotency / unique application ownership | DONE | `agent/statestore.py` + UNIQUE index |
| Persistent state machine | DONE | `agent/states.py` |
| Leases / concurrency | PARTIAL | leases DONE; **browser-profile isolation is a GAP** (one shared user-data dir) |
| Provenance-aware memory | DONE | `learning/provenance.py` |
| Sensitive-field policy | DONE | `submit/fill_planner.py` (code-level, not prompt-level) |
| Evidence-based submission verification | DONE | `submit/verify.py` |
| Failure taxonomy + retry policy | PARTIAL | taxonomy DONE (`states.py`); **not yet wired to real recovery actions** |
| Checkpoints | PARTIAL | `Application.checkpoint` column exists; not written per step |
| Tracing | PARTIAL | `StateTransition` DONE; **no per-step agent trace** |

## Perception — the foundational GAP (this slice)

| Capability | Status | Note |
|---|---|---|
| BrowserObservation abstraction | **GAP** | nothing exists |
| Tiered/hierarchical perception | **GAP** | `form_scan` dumps all fields at one level |
| Accessibility tree | **GAP** | `aria_snapshot()` verified available, unused |
| Frames enumerated | PARTIAL | `find_target_frame` finds one; doesn't enumerate |
| Tabs / dialogs | **GAP** | |
| State digest | **GAP** | prerequisite for loop detection |
| Page classification | **GAP** | code assumes it is already on an application form |
| `UNKNOWN` as first-class state | PARTIAL | exists in `ApplicationState`, not in page classification |

## Agent loop — GAP (next slice)

| Capability | Status |
|---|---|
| Agent controller (OBSERVE→DECIDE→ACT→VERIFY→CHECKPOINT) | **GAP** |
| Typed tool registry + uniform `ToolResult` | **GAP** |
| Action risk classes | **GAP** |
| Step/time/token budgets | **GAP** — no budget of any kind exists |
| Loop / no-progress detection | **GAP** (this slice) |
| Agent takeover / hand-back | **GAP** |
| Recovery engine (closed action set) | **GAP** |

## Authentication — GAP (deliberately later)

Login / signup / SSO / OTP / session expiry / credential isolation /
`AccountRecord` persistence: all **GAP**. Depends on perception + tool layer
existing first; building it before those would mean rewriting it.

## Discovery — PARTIAL

| Capability | Status |
|---|---|
| ATS + aggregator sources | DONE (9 sources + Workday) |
| International coverage | PARTIAL (expanded on `main`; not location-normalized) |
| Location normalization (Bengaluru/Bangalore/NCR…) | **GAP** |
| Search-engine discovery provider | **GAP** |
| Structured `JobRequirements` (hard vs soft) | **GAP** — eligibility is still one 0–100 score |
| Cross-source dedup | DONE (`identity.py`) |

## Evaluation — GAP (highest-value after the loop)

No fault-injection harness, no false-submission/duplicate-rate measurement.
Per `browser-agent-failures.md` §1 this is what actually predicts production
behaviour, so it ranks above adding more capability.

---

## Chosen slice order (smallest change first, each independently useful)

1. **Perception** ← *this slice*. Everything else consumes it; building the
   loop first would mean rewriting it against a real observation type.
2. Tool registry + `ToolResult` + budgets + agent controller.
3. Recovery engine wired to the existing failure taxonomy + takeover/hand-back.
4. Prompt-injection channel separation (closes a live gap).
5. Evaluation harness with fault injection.
6. Browser identity/profile isolation (fixes the concurrency gap above).
7. Auth orchestrator.
8. Structured `JobRequirements`; location normalization; search discovery.

Playwright MCP is deferred until (2) exists: it is an *alternative actuator*
behind the same `ToolResult` contract, and adding it before that contract
exists would couple the agent to an external interface instead of to our own
tool boundary.

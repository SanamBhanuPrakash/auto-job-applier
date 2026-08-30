# Gap map: spec capability → current tree

Updated as slices land. Current tree: Phases 1-7 complete, 407 tests passing
(1 skipped). Last assessed after the Phase 6-7 slice.

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
| Failure taxonomy + retry policy | DONE | taxonomy `states.py`; recovery actions `agent/recovery.py` |
| Checkpoints | PARTIAL | `Application.checkpoint` column exists; not written per step |
| Tracing | PARTIAL | `StateTransition` DONE; **no per-step agent trace** |

## Perception — Phase 2 DONE

| Capability | Status | Where |
|---|---|---|
| BrowserObservation abstraction | DONE | `agent/observation.py` |
| Tiered perception (METADATA→CONTROLS→ARIA→DOM→SCREENSHOT) | DONE | `Detail` |
| Accessibility tree | DONE | `aria_snapshot()`, ~5x smaller than raw HTML |
| Frames / tabs / dialogs enumerated | DONE | observation metadata tier |
| Group-aware control labels | DONE | `Control.group` / `semantic_label` (see failures §13) |
| State digest | DONE | feeds loop detection |
| Page classification (20 states) | DONE | `agent/page_classify.py` |
| `UNKNOWN` as first-class state | DONE | never coerced to keep moving |
| Vision/screenshot escalation | PARTIAL | tier exists; nothing chooses it automatically yet |

## Agent loop — Phases 3–7 DONE

| Capability | Status | Where |
|---|---|---|
| Agent controller (OBSERVE→DECIDE→ACT→VERIFY→CHECKPOINT) | DONE | `agent/controller.py` |
| Typed tool registry + uniform `ToolResult` | DONE | `agent/tools.py` (18 tools) |
| Action risk classes | DONE | `RiskClass`, enforced in `policy.py` |
| Tool authorization (§40) | DONE | `agent/policy.py` |
| Step/time/token/LLM/recovery budgets | DONE | `Budget`, enforced by the controller |
| Loop / no-progress detection | DONE | `agent/trajectory.py` |
| Pluggable `Decider` (LLM is one impl) | DONE | `controller.Decider` protocol |
| Per-step trace rows | PARTIAL | `StepRecord` in memory; **not yet persisted to DB** |
| Agent takeover / hand-back | DONE | `agent/takeover.py`, wired at `submit/takeover_bridge.py` |
| Recovery engine (closed action set) | DONE | `agent/recovery.py` (15 actions, ladders per trigger) |
| Capability vs. risk separation | DONE | `tools.Capability` (see failures §15) |
| Non-LLM decider | DONE | `agent/deciders.HeuristicDecider` |
| Checkpoint per step | **GAP** — handoff carries one; the loop does not write per-step |
| Per-step trace persistence | **GAP** — `StepRecord` still in memory only |
| LLM decider + timeouts/routing | **GAP** (§23, §110, §111) |
| Recovery engine driven from the deterministic path | **PARTIAL** — engine + ladders exist and are tested; `submit/base.py` currently uses takeover only, not the full ladder |

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

1. ~~**Perception**~~ — DONE (`da5804a`).
2. ~~**Tool registry + `ToolResult` + budgets + authorization + controller**~~
   — DONE (Phases 3–5). Found and fixed a real sensitive-field bypass in the
   process; see browser-agent-failures.md §13.
3. ~~**Agent takeover / hand-back + recovery engine**~~ — DONE (Phases 6–7).
   `submit/base.py` now hands the browser to the agent when the form scan
   comes up empty, and never walks an empty form to READY_TO_SUBMIT again.
   Found three real defects on the way: failures §14, §15, §16.
4. **Prompt-injection channel separation** (Phase 8) ← *next*. Still a live
   gap: job descriptions and scraped labels reach prompts undelimited.
5. Fault-injection evaluation harness (Phase 9). Ranked high: per
   browser-agent-failures.md §1 this is what predicts production behaviour.
   Phases 6–7 raise its value again — recovery ladders and takeover are
   exactly what fault injection measures, and they are currently tested
   against fixtures rather than injected faults.
6. Browser identity/profile isolation (Phase 10) — fixes the concurrency gap.
7. Auth orchestrator (Phases 11–14). `REAUTHENTICATE` is the one recovery
   action that reports itself unavailable; this is what fills it in.
8. Structured `JobRequirements`; location normalization; search discovery.

Playwright MCP (spec §62/§121) is unblocked and now has somewhere to
plug in: `ToolResult`, the registry, and a `Decider` protocol all exist,
so MCP is one more actuator behind the same contract and one more decider
behind the same authorization. It stays deferred behind Phase 9 for a
different reason than before — an alternative actuator is only worth
adding once fault injection can measure whether it actually recovers
better than the heuristic path it would replace (§64).

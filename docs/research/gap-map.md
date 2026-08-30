# Gap map: spec capability → current tree

Updated as slices land. Current tree: Phases 1-5 complete, 349 tests passing
(1 skipped). Last assessed after the Phase 3-5 slice.

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

## Agent loop — Phases 3–5 DONE

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
| Agent takeover / hand-back | **GAP** (Phase 6) |
| Recovery engine (closed action set) | **GAP** (Phase 7) |
| Checkpoint per step | **GAP** — controller does not yet write checkpoints |
| LLM decider + timeouts/routing | **GAP** (§23, §110, §111) |

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
3. **Agent takeover / hand-back + recovery engine** (Phases 6–7) ← *next*.
   The controller exists but nothing calls it yet: wiring takeover into
   `submit/base.py` is what turns it from a capability into behaviour.
4. Prompt-injection channel separation (Phase 8) — still a live gap: job
   descriptions and scraped labels reach prompts undelimited.
5. Fault-injection evaluation harness (Phase 9). Ranked high: per
   browser-agent-failures.md §1 this is what predicts production behaviour.
6. Browser identity/profile isolation (Phase 10) — fixes the concurrency gap.
7. Auth orchestrator (Phases 11–14).
8. Structured `JobRequirements`; location normalization; search discovery.

Playwright MCP (spec §62/§121) is now *unblocked* — `ToolResult` and the
registry exist, so MCP can be added as one more actuator behind the same
contract. It is still deferred behind takeover/recovery, because without
those an alternative actuator has nothing distinctive to do: MCP earns its
place on novel UI and difficult recovery (§64), which is Phase 6–7 work.

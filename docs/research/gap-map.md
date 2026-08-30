# Gap map: spec capability → current tree

Updated as slices land. Current tree: Phases 1-14 complete, 522 tests passing
(1 skipped), plus a 48-scenario fault-injection suite (45 pass / 3 skip for
unbuilt capabilities). Last assessed after the Phase 11-14 slice.

Legend: **DONE** · **PARTIAL** · **GAP** · **N/A-yet** (deliberately deferred)

## Safety / reliability substrate — largely DONE, reuse as the safety kernel

| Capability | Status | Where |
|---|---|---|
| Canonical identity (cross-source) | DONE | `agent/identity.py` |
| Idempotency / unique application ownership | DONE | `agent/statestore.py` + UNIQUE index |
| Persistent state machine | DONE | `agent/states.py` |
| Leases / concurrency | DONE | leases + per-worker Chromium profile (`_user_data_dir(worker_id)`) |
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

## Authentication — DONE (Phases 11–14)

| Capability | Status | Where |
|---|---|---|
| Auth state detection (12 states) | DONE | `auth/detect.py`, signal-based, no LLM |
| Credential isolation (§26) | DONE | `auth/credentials.py` — keyring/env, `Secret`, `redact` |
| Login incl. multi-step | DONE | `auth/orchestrator.py`, verified not inferred |
| Session reuse + expiry | DONE | `auth/session.py` + `AccountRecord` |
| `AccountRecord` persistence | DONE | no secret-bearing column, asserted by test |
| Auth failure circuit breaker | DONE | 2 strikes, then a human |
| `REAUTHENTICATE` recovery action | DONE | was the one action reporting itself unavailable |
| OTP / email verification | DONE as hand-over | codes are never guessed or harvested |
| SSO | REFUSED by design | driving a primary identity is out of scope |
| CAPTCHA / bot detection / locked | REFUSED by design | boundaries, not obstacles |
| Automated signup | **GAP** (deliberate) | gated off; detection + refusal implemented |

## Discovery — PARTIAL

| Capability | Status |
|---|---|
| ATS + aggregator sources | DONE (9 sources + Workday) |
| International coverage | PARTIAL (expanded on `main`; not location-normalized) |
| Location normalization (Bengaluru/Bangalore/NCR…) | **GAP** |
| Search-engine discovery provider | **GAP** |
| Structured `JobRequirements` (hard vs soft) | **GAP** — eligibility is still one 0–100 score |
| Cross-source dedup | DONE (`identity.py`) |

## Evaluation — DONE (Phase 9)

`jobbot/eval` declares all 48 spec scenarios and runs 37 against real
Chromium under injected faults (network drop, 5xx, DOM mutation, frame
replacement, popup, hidden DOM, fake confirmation, crash, LLM failure).
Eleven are SKIP with a named missing capability — never passes. Run it with
`jobbot eval` or as part of `pytest`.

| Capability | Status |
|---|---|
| Fault injectors | DONE (`eval/faults.py`) |
| Critical metrics (false/duplicate/wrong-sensitive) | DONE, all 0.00% |
| Unmeasured metric reported as unmeasured, not clean | DONE |
| Runs in CI / on every change | DONE (`tests/test_eval_harness.py`) |
| Real-posting integration run | **GAP** — see docs/LIMITATIONS.md |

It earned its place immediately: the first run found a false-submission
defect (failures §17).

## Prompt-injection isolation — DONE (Phase 8)

| Capability | Status | Where |
|---|---|---|
| Channel separation (trusted vs untrusted) | DONE | `agent/prompting.py` |
| Fence-break / channel-forgery neutralisation | DONE | nonce-tagged blocks |
| Applied to fill planning | DONE | `submit/fill_planner.py` |
| Applied to job scoring | DONE | `matching/score.py` |
| Injection attempts recorded per application | DONE | state-transition detail |
| Adversarial tests | DONE | `tests/test_prompt_injection.py` |

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
4. ~~**Prompt-injection channel separation**~~ — DONE (Phase 8).
5. ~~**Fault-injection evaluation harness**~~ — DONE (Phase 9). Found a
   false-submission defect on its first run (failures §17).
6. ~~**Browser identity/profile isolation**~~ — DONE (Phase 10).
7. ~~**Auth orchestrator**~~ — DONE (Phases 11–14). Eight eval scenarios
   moved from SKIP to PASS. Found failures §19 on the way.
8. **Per-step trace persistence** ← *next*. The remaining Level-2 blocker
   under §96 ("traces are complete"): `StepRecord` exists in memory but is
   never written, so a pilot cannot be audited after the fact. Small.
9. **A real end-to-end run** against a live posting — the other Level-2
   blocker, and the one no amount of local work substitutes for.
10. Multi-page application flow control (one of the two remaining eval skips).
11. Structured `JobRequirements`; location normalization; search discovery.

Playwright MCP (spec §62/§121) is unblocked and now has somewhere to
plug in: `ToolResult`, the registry, and a `Decider` protocol all exist,
so MCP is one more actuator behind the same contract and one more decider
behind the same authorization. It stays deferred behind Phase 9 for a
different reason than before — an alternative actuator is only worth
adding once fault injection can measure whether it actually recovers
better than the heuristic path it would replace (§64).

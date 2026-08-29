# Agent architecture: research basis

Living document. Every non-obvious architectural decision in `jobbot/agent/`
should be traceable to an entry here. The purpose is not citation for its own
sake — it is to avoid rediscovering known browser-agent failures the hard way.

Format for each entry:

- **Source** — what was actually read
- **What they built**
- **Failure / finding**
- **Lesson**
- **How we apply it** — the concrete thing in this repo

---

## 1. Architecture, not model capability, is the binding constraint

- **Source:** *Building Browser Agents: Architecture, Security, and Practical
  Solutions*, arXiv 2511.19477 (30pp, production evaluation).
- **What they built:** A browser agent reaching ~85% on WebGames (53
  challenges) against a ~50% prior-agent baseline and a 95.7% human baseline,
  using hybrid accessibility-tree + selective vision context management.
- **Finding:** *"model capability does not limit agent performance;
  architectural decisions determine success or failure."*
- **Lesson:** Reaching for a bigger model is not the lever. Observation
  representation, tool design, verification and recovery are.
- **How we apply it:** We do not treat "use a stronger model" as a fix for a
  reliability problem. The LLM is one component inside a deterministic
  substrate (`jobbot/agent/states.py`, `statestore.py`, `submit/verify.py`)
  that it cannot override.

## 2. Safety must be enforced in code, not by model reasoning

- **Source:** arXiv 2511.19477 (as above).
- **Finding:** *"prompt injection attacks make general-purpose autonomous
  operation fundamentally unsafe."* Their recommendation is explicitly
  against relying on LLM reasoning for safety, in favour of *"specialized
  tools with programmatic constraints, where safety boundaries are enforced
  through code."*
- **Lesson:** A system prompt saying "do not answer work-authorization
  questions" is not a control. A regex the model's output cannot bypass is.
- **How we apply it:** The sensitive-field guardrail in
  `submit/fill_planner.py` is a deterministic check that overrides model
  output, and those fields are filtered out of the LLM call entirely.
  Submission authority lives in `submit/verify.py`, not in the model's
  opinion. This principle is why the agent layer sits *above* the substrate
  rather than replacing it.

## 3. Agents vs. workflows — and the cost of choosing wrong

- **Source:** Anthropic, *Building Effective Agents*.
- **Finding:** *"workflows offer predictability and consistency for
  well-defined tasks, whereas agents are the better option when flexibility
  and model-driven decision-making are needed at scale."* Agents carry
  *"higher costs, and the potential for compounding errors."* Add complexity
  *"only when it demonstrably improves outcomes."*
- **Lesson:** Not every step should be agentic. A known Greenhouse form is a
  workflow; an unfamiliar careers portal is an agent problem.
- **How we apply it:** The **agent-takeover** model. Deterministic path runs
  by default (cheap, predictable, already tested); the agent is invoked when
  the deterministic path hits ambiguity, and hands control *back* once a known
  state is reached. This is also why `static_answers.py` resolving a field
  without any LLM call is preferred over asking the model.

## 4. Ground truth comes from the environment, every step

- **Source:** Anthropic, *Building Effective Agents*.
- **Finding:** Agents *"gain 'ground truth' from the environment at each
  step (such as tool call results or code execution)"*.
- **Lesson:** The agent's belief about the page is not evidence. The page is.
- **How we apply it:** `BrowserObservation` is re-derived from the live page
  after every consequential action; we never carry a stale belief forward.
  The database is durable *execution memory*, explicitly not a substitute for
  browser state (see `docs/research/browser-agent-failures.md` §Restart).

## 5. Accessibility tree first, vision selectively

- **Source:** arXiv 2511.19477; corroborated by the observation that
  reliability-optimized platforms (Playwright MCP, Atlas) lead with the
  accessibility tree. *"A vision model has to guess which pixels form a
  clickable control, whereas the accessibility tree states this outright with
  a role and a name."*
- **Finding:** Accessibility-tree snapshots give compact semantics (labels,
  roles, focus, validation messages) and whole-page context in one request
  without scrolling. Vision is needed for canvas/image content and spatial
  checks.
- **Lesson:** Screenshot-first is the expensive, less reliable default.
- **How we apply it:** `observation.py` tiers perception:
  metadata → interactive controls → ARIA snapshot → focused DOM → screenshot.
  Measured on our own fixture: ARIA snapshot 1,020 chars vs 4,931 chars of raw
  HTML for the same page — and that ratio widens sharply on real pages.
  Screenshots are opt-in per observation, never the default.

## 6. Context rot — long contexts degrade recall

- **Source:** Anthropic, *Effective Context Engineering for AI Agents*.
- **Finding:** *"as the number of tokens in the context window increases, the
  model's ability to accurately recall information from that context
  decreases."* Guidance: compaction, structured note-taking, sub-agent
  context isolation, just-in-time retrieval; target *"the smallest set of
  high-signal tokens that maximize the likelihood of your desired outcome."*
- **Lesson:** Dumping the DOM (or the full trajectory) into every call makes
  the agent worse, not just costlier. A long application is exactly the
  long-horizon case where this bites.
- **How we apply it:** Observations are token-budgeted and truncated at
  construction. The trajectory keeps *digests* of past states, not full
  observations (`trajectory.py`). Durable history lives in the DB
  (`StateTransition`), retrieved on demand rather than carried in context.

## 7. Tool design: consolidate, return high-signal, make errors actionable

- **Source:** Anthropic, *Writing Effective Tools for AI Agents*.
- **Finding:** Consolidate related operations rather than exposing every
  endpoint; return semantically meaningful names over cryptic identifiers
  (*"Agents ... grapple with natural language names ... significantly more
  successfully than ... cryptic identifiers"*); implement truncation with
  sensible defaults (Claude Code caps tool responses at 25,000 tokens);
  *"prompt-engineer your error responses to clearly communicate specific and
  actionable improvements, rather than opaque error codes or tracebacks."*
- **Lesson:** Tool ergonomics are a reliability feature.
- **How we apply it:** Controls are exposed with role + accessible name
  (`aria_snapshot`-derived), not CSS selectors or UUIDs. `ToolResult` carries
  a `failure_category` from a closed taxonomy plus actionable evidence, so a
  failure tells the agent what to do differently rather than dumping a
  Playwright traceback.

## 8. External side effects require confirmation, and confirmation is not free

- **Source:** OpenAI, *Computer-Using Agent* / *Operator System Card*.
- **What they built:** A perceive → reason → act loop over screenshots, with
  chain-of-thought between steps, that *"seeks user confirmation for sensitive
  actions, such as entering login details"* and *"before finalizing tasks
  with external side effects, for example before submitting an order."* It
  declines higher-risk categories outright.
- **Finding (from the security literature):** human-in-the-loop approval is
  routinely defeated — CUAs *"can often bypass these checkpoints through
  indirect prompt injection, perceptual mismatches, or induced ambiguity."*
  And confirmation fatigue causes users to approve without reading.
- **Lesson:** Confirmation is necessary for irreversible actions but must be
  (a) enforced outside the model, and (b) rare enough that it is still read.
- **How we apply it:** Submission is an `EXTERNAL_SIDE_EFFECT` action gated by
  deterministic state (`READY_TO_SUBMIT` only) and verified independently.
  Sensitive-field autofill takes **one** confirmation per run rather than per
  application — deliberately, to avoid the fatigue failure above — and the
  underlying values must be human/verified provenance regardless.

## 9. One agent with good tools, not an agent swarm

- **Source:** Anthropic, *Building Effective Agents* (start simple; add
  complexity only when it demonstrably improves outcomes).
- **How we apply it:** A single application agent with a typed tool registry.
  Multi-agent orchestration is not introduced until an evaluation
  demonstrates a single agent cannot solve the task. Context isolation, the
  main legitimate motivation for sub-agents, is achieved here by keeping
  observations digested and history in the DB.

---

## Open questions being tracked

- Whether ARIA snapshots alone suffice for custom React date pickers and
  canvas-based controls, or whether selective vision is required in practice.
  To be answered by the evaluation harness, not by assumption.
- Whether per-domain learned strategies (§49 of the spec, "adaptive
  deterministicization") measurably reduce LLM calls per application. Requires
  the eval harness to measure before building.

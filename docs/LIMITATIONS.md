# Limitations

What this system does not do, cannot do, or has not been shown to do.
Kept as a first-class document because §131 forbids claiming otherwise,
and because most of the value of the safety work is only real if the gaps
are stated as plainly as the capabilities.

## Not verified end to end against a real posting

**The most important item here.** Every browser test in this repo runs
against local fixture pages. No application has been submitted to a real
employer by this code and confirmed received.

This is partly environmental — the sandbox this was built in blocks
outbound browser networking, verified: even `example.com` resets — and
partly that doing it properly means a real candidate applying to a real
job. Until that happens, "it works" means "the logic behaves correctly on
the failure shapes we can reproduce", which is a weaker claim.

## Job boards this deliberately does not automate

LinkedIn, Indeed, Naukri and Wellfound submission is **not** implemented
and will not be. Their terms prohibit automated access; *hiQ v. LinkedIn*
ended in a $500K judgment; and the creator of a well-known auto-apply tool
had their own account restricted. The realistic outcome of building it is
the user losing the account they need most.

Discovery from public ATS endpoints (Greenhouse, Lever, Ashby, Workable,
SmartRecruiters, RemoteOK and similar) is a different matter and is
implemented — those are public JSON APIs meant to be read.

## Authentication — what it does and does not do

There **is** an auth subsystem now (`jobbot/auth/`). It signs in with a
credential from your OS keyring, and it verifies the result against the
page rather than assuming a click worked.

What it will not do, by design:

- **Third-party SSO** ("Continue with Google/Microsoft/Apple"). Driving
  that means using your primary identity, where the blast radius of a
  mistake is your whole account rather than one job application. Sign in
  yourself once; the session is reused.
- **One-time codes.** An unattended run has nobody to ask, and there is no
  legitimate way to obtain one otherwise — this does not read your email
  to harvest OTPs. Attended runs can prompt you for the code.
- **CAPTCHA, bot detection, security challenges, locked accounts.**
  Boundaries, not obstacles.
- **Creating accounts.** Off by default, and even when enabled it is
  limited to domains you name, and the flow itself is not implemented.
- **Retrying a password.** One attempt. A stored credential that is wrong
  stays wrong, and a second try is how "wrong password" becomes "account
  locked". After two consecutive failures on a domain, that domain is not
  attempted again until you intervene.

Your password never reaches the LLM, a prompt, a tool argument, or a trace
row — `agent/policy.py` refuses credential-shaped fields through the
generic tools, so the auth subsystem is the only path. If no OS keyring is
available, storing a credential **refuses** rather than falling back to a
file.

Not verified against a real login. Like everything else here, the auth
flows are tested against local fixture pages.

## Multi-page applications

Applications spread over several pages ("next", "continue", a review step)
are not driven end to end. The form on the first page is filled; a
multi-step flow escalates.

## The agent's decider is rules, not an LLM

`HeuristicDecider` handles the cases that actually recur — an Apply
button, a consent overlay, a form one frame away. It cannot read a novel
UI it has no rule for, and it says so (returns `finish`, which the
controller checks against the environment) rather than guessing. An LLM
decider is designed for but not built.

## Traces are not persisted

Per-step agent traces exist in memory during a run and are summarised into
the state ledger. They are not written per step, so a post-hoc audit of
exactly what the agent did on application 47 is not currently possible.

## Sensitive questions are never answered *by the model*

Work authorization, visa sponsorship, veteran status, disability,
race/ethnicity and legal attestations are never filled from anything the
LLM produced. That rule has not been relaxed and will not be.

What changed: `jobbot setup` asks those questions **once, of you**, and
stores the answers with human provenance. After that they fill
automatically, because the answer came from the candidate rather than from
a model. Three separate things must all be true for a run to submit
without stopping:

1. the relevant questions are answered (`jobbot setup`),
2. `JOBBOT_AUTOFILL_SENSITIVE=true` — saved answers are reused,
3. `JOBBOT_AUTO_SUBMIT=true` — submission is unattended.

`jobbot doctor` reports which of the three still holds you back.

Anything you skip in `jobbot setup` keeps stopping for you, which is the
right behaviour for a question you would rather decide case by case. And
one of the catalogue questions is a legal attestation ("I certify that the
information provided is true and accurate") — answering it in advance
means you are making that statement yourself, ahead of time, for
applications this tool submits. It is skippable for that reason.

## CAPTCHA and bot detection

Treated as a boundary, not an obstacle. When one appears the application
is parked as `BLOCKED` and a human is told. Nothing attempts to solve,
evade or fingerprint-spoof past it. This is both a safety position and a
practical one: attempting it is what gets accounts flagged.

## Rate and volume

No throughput claim is made. See PRODUCTION_READINESS.md § "On the
throughput target". Applying at high volume is the behaviour employers
treat as spam; the pacing defaults are deliberately conservative.

## What the fault-injection numbers cover

`jobbot eval` reports 0.00% on all three critical metrics. That is
measured over 45 scenarios against fixture pages. Three of the 48
scenarios the spec names are **skipped**, by name, because the capability
does not exist yet (multi-page flows, process-level crash injection, and
one helper) — they are not passes. Skips are listed in the report output
every time it runs.

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

## Authentication

There is no auth subsystem. Login, signup, SSO, OTP and email verification
all stop and ask a human. `REAUTHENTICATE` is the one recovery action that
reports itself unavailable rather than improvising, which is deliberate: a
guessed login flow is how credentials end up in a trace.

Practically: postings behind an account wall need you to sign in once in
the browser window. The profile directory persists, so the session
survives between runs.

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

## Sensitive questions are never answered automatically

By design, not by omission. Work authorization, visa sponsorship, veteran
status, disability, race/ethnicity and legal attestations always stop and
ask you. Even with `JOBBOT_AUTOFILL_SENSITIVE=true`, only values you
previously **confirmed yourself** are reused — a model guess never
qualifies, no matter how many times it has been reused before.

This means unattended operation is not fully unattended on any application
that asks one of those questions, which in practice is most US
applications.

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
measured over 37 scenarios against fixture pages. Eleven of the 48
scenarios the spec names are **skipped**, by name, because the capability
does not exist yet — they are not passes. Skips are listed in the report
output every time it runs.

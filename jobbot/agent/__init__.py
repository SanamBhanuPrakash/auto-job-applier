"""Agent layer: bounded, deterministic control flow above the existing
services in jobbot/discovery, jobbot/matching and jobbot/submit.

Design rule (see README "Agent architecture"): the loop is deterministic
software; the LLM is consulted only at explicitly named decision points and
its output is never trusted to drive control flow, choose a state
transition, or authorize a submission.
"""

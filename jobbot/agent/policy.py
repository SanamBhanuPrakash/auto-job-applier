"""Tool authorization — hard constraints the agent cannot argue past.

Spec §40 and §133. This module is the reason the LLM is safe to give
navigational autonomy: it may *request* anything, and a deterministic
function decides whether the request runs.

The distinction that matters:

    agent: "fill work authorization with Yes"
    policy: the model is not authorized to invent that answer
    result: DENIED / HUMAN_REQUIRED

    agent: "submit"
    policy: only from READY_TO_SUBMIT, and never while blocked
    result: DENIED unless the state machine agrees

Research basis (docs/research/agent-architecture.md §2): arXiv 2511.19477
finds prompt injection makes general autonomous operation unsafe unless
"safety boundaries are enforced through code" — and the CUA security
literature shows human-in-the-loop checkpoints being bypassed through
"indirect prompt injection, perceptual mismatches, or induced ambiguity".
A checkpoint the model can reason its way past is not a checkpoint. Every
rule here is a plain function of typed state, never of prompt wording.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntEnum

from jobbot.agent.observation import BrowserObservation
from jobbot.agent.page_classify import PageState, requires_stop
from jobbot.agent.states import ApplicationState, FailureCategory
from jobbot.agent.tools import Capability, RiskClass, ToolContext, ToolSpec
from jobbot.submit.fill_planner import is_sensitive


class Autonomy(IntEnum):
    """How much the operator has authorized this run to do unattended.

    Values line up 1:1 with `Capability`, which is the point: an autonomy
    level is a ceiling on the *kind* of action, not on how consequential
    any single action is. Risk class still governs escalation, blocking
    pages and the submission gate below.
    """

    OBSERVE_ONLY = 0      # read the page, decide nothing consequential
    NAVIGATE = 1          # move around, but do not write into forms
    FILL = 2              # fill ordinary candidate fields (default)
    FULL = 3              # may also submit, when policy otherwise agrees


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason: str = ""
    requires_human: bool = False
    failure_category: FailureCategory | None = None

    @classmethod
    def allow(cls) -> "PolicyDecision":
        return cls(True)

    @classmethod
    def deny(cls, reason: str, *, human: bool = False,
             category: FailureCategory = FailureCategory.POLICY) -> "PolicyDecision":
        return cls(False, reason, human, category)


@dataclass
class PolicyContext:
    """Everything authorization is allowed to consider. All typed state —
    deliberately no free text from the page."""

    application_state: ApplicationState
    page_state: PageState = PageState.UNKNOWN
    autonomy: Autonomy = Autonomy.FILL
    #: Set once a run has an explicit, confirmed authorization to reuse
    #: saved sensitive answers (the existing per-run CONFIRM gate).
    sensitive_autofill_confirmed: bool = False
    #: True when every field has been verified and the submission
    #: readiness checks passed. Submission requires this *and* the state.
    submission_ready: bool = False


#: Tools that would touch credentials. There is no AuthOrchestrator yet
#: (spec §25, a later phase), so they are refused rather than improvised —
#: entering a password through the generic `type` tool would put the
#: credential in tool arguments and traces, violating §26/§60.
_CREDENTIAL_FIELD = re.compile(
    r"\b(password|passcode|pin|security code|one[- ]time code|verification code|otp|2fa|mfa)\b",
    re.IGNORECASE,
)


#: Roles where "clicking" is really "answering". A click on one of these
#: is a fill, whatever the tool is called.
_INPUT_ROLES = frozenset({
    "textbox", "combobox", "checkbox", "radio", "select", "listbox", "slider",
    "spinbutton", "switch", "searchbox", "file",
})


def _control_for(ref: str, observation: BrowserObservation | None):
    if not ref or observation is None:
        return None
    for control in observation.controls:
        if control.ref == ref:
            return control
    return None


def _effective_capability(spec: ToolSpec, control) -> Capability:
    """A tool's declared capability, escalated by what it is aimed at.

    `click` is declared NAVIGATE because pressing "Apply" is navigation.
    Pointed at a radio in the work-authorization group it is a fill, and
    treating it as navigation would let a NAVIGATE-autonomy run answer a
    sensitive question — the same class of hole as failures §13, one layer
    up. Capability is therefore a function of the target, not just the
    tool.
    """
    if spec.capability is Capability.NAVIGATE and control is not None:
        if control.role in _INPUT_ROLES:
            return Capability.FILL
    return spec.capability


def _field_name_for(ctx: ToolContext, ref: str, observation: BrowserObservation | None) -> str:
    """The label a policy decision is made against.

    Uses `semantic_label` (group + name), never the bare accessible name.
    A radio inside `<fieldset><legend>Are you authorized to work in the
    US?</legend>` is named just "Yes"; checking that alone let every
    sensitive question rendered as a radio group through — which is how
    work authorization, sponsorship, veteran and EEOC questions are almost
    always rendered. Caught by test_sensitive_field_request_escalates_to_human;
    see docs/research/browser-agent-failures.md §13.
    """
    if observation is None:
        return ""
    for control in observation.controls:
        if control.ref == ref:
            return control.semantic_label
    return ""


def authorize(
    spec: ToolSpec,
    args: dict,
    policy: PolicyContext,
    *,
    tool_ctx: ToolContext | None = None,
    observation: BrowserObservation | None = None,
) -> PolicyDecision:
    """Decide whether one tool call may execute. Never raises."""

    # 1. A blocking wall overrides everything except looking at the page.
    #    Acting past a CAPTCHA is the boundary we do not cross (§31, §90).
    if requires_stop(policy.page_state) and spec.risk_class > RiskClass.READ_ONLY:
        return PolicyDecision.deny(
            f"page is {policy.page_state.value}; only read-only tools are permitted here",
            human=True, category=FailureCategory.BLOCKED,
        )

    # 2. Autonomy ceiling for this run, on the capability axis.
    ref = str(args.get("ref", ""))
    control = _control_for(ref, observation)
    capability = _effective_capability(spec, control)
    if capability.value > policy.autonomy.value:
        return PolicyDecision.deny(
            f"{spec.name} is a {capability.name} action, above this run's autonomy "
            f"({policy.autonomy.name})",
        )
    # Submission stays gated on risk class as well: nothing below FULL may
    # reach the employer even if a tool were mislabelled.
    if spec.risk_class is RiskClass.EXTERNAL_SIDE_EFFECT and policy.autonomy < Autonomy.FULL:
        return PolicyDecision.deny(
            f"{spec.name} reaches the employer; this run's autonomy is {policy.autonomy.name}",
        )

    # 3. The tool must be legal in the current application state (§40).
    if spec.required_states and policy.application_state not in spec.required_states:
        return PolicyDecision.deny(
            f"{spec.name} requires state in "
            f"{sorted(s.value for s in spec.required_states)}, but application is "
            f"{policy.application_state.value}",
        )

    # 4. Submission is the one irreversible action; gate it hardest (§52).
    if spec.risk_class is RiskClass.EXTERNAL_SIDE_EFFECT:
        if policy.application_state is not ApplicationState.READY_TO_SUBMIT:
            return PolicyDecision.deny(
                f"submission is only permitted from READY_TO_SUBMIT (currently "
                f"{policy.application_state.value})",
            )
        if not policy.submission_ready:
            return PolicyDecision.deny(
                "submission readiness checks have not passed", human=True,
            )

    # 5. Credential material never goes through a generic tool (§26, §60).
    #    `click` is included: clicking a radio's label sets an answer just
    #    as surely as `check` does, and a rule that names only the
    #    obviously-filling tools is a rule with a click-shaped hole in it.
    field_name = control.semantic_label if control is not None else ""
    if capability is Capability.FILL and field_name:
        if _CREDENTIAL_FIELD.search(field_name):
            return PolicyDecision.deny(
                f"{field_name!r} is a credential/verification field; it must be handled by the "
                "authentication subsystem, never by a generic tool",
                human=True,
            )
        # 6. Sensitive candidate facts (§35). The existing code-level
        #    guardrail is authoritative; the agent cannot answer these
        #    without a confirmed, human-provenance value.
        if is_sensitive(field_name) and not policy.sensitive_autofill_confirmed:
            return PolicyDecision.deny(
                f"{field_name!r} is a sensitive field (work authorization / EEOC / legal "
                "attestation class); the agent may not answer it",
                human=True,
            )

    # 7. Uploads: the handler enforces the allowlist, but refuse early when
    #    no candidate document is configured at all (§41).
    if spec.name == "upload" and tool_ctx is not None and not tool_ctx.allowed_upload_paths:
        return PolicyDecision.deny(
            "no candidate documents are configured, so there is nothing this run may upload",
        )

    return PolicyDecision.allow()

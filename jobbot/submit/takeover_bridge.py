"""The seam between the deterministic submission path and the agent loop.

`submit/base.py` walks a fixed sequence: open the posting, find the form,
scan it, fill it, verify, submit. That sequence has one assumption that
sites break constantly — that the form is right there once the page loads.
It often is not: it sits behind an "Apply for this job" button, inside an
iframe on the company's own careers domain, or under a consent overlay
that swallows the first click.

Before this module, zero scanned fields meant the deterministic path
carried on and walked an *empty* form to READY_TO_SUBMIT. This is where
the agent takes the wheel instead (spec §16), and where it gives it back.

Three constraints are deliberate:

* **NAVIGATE autonomy.** The agent may move around; it may not fill a
  field and it certainly may not submit. Reaching the form is the whole
  job, and the deterministic path is better at everything after that.
* **Hand-back is verified.** `form_is_present` is evaluated against a
  fresh observation, so "the agent said it worked" is never the evidence.
* **Re-ground after hand-back.** The agent may have navigated, switched
  frames or switched tabs, so the caller's frame handle and every scanned
  field from before takeover are presumed stale (failures §4). This
  function re-resolves both and returns the new ones.
"""
from __future__ import annotations

import logging

from jobbot.agent.deciders import HeuristicDecider
from jobbot.agent.observation import Detail, observe
from jobbot.agent.page_classify import classify
from jobbot.agent.policy import Autonomy, PolicyContext
from jobbot.agent.states import ApplicationState
from jobbot.agent.takeover import (
    AgentHandoffResult,
    detect_trigger,
    form_is_present,
    take_over,
)
from jobbot.agent.tools import ToolContext, ToolRegistry
from jobbot.agent.trajectory import Budget
from jobbot.config import get_settings
from jobbot.submit.form_scan import find_target_frame, scan_form

log = logging.getLogger(__name__)


def reach_application_form(
    page,
    form_ctx,
    *,
    app_id: int = 0,
    run_id: str = "",
    decider=None,
    ats_hint: str = "",
) -> tuple[AgentHandoffResult | None, object, list]:
    """Try to reach an application form the deterministic scan could not find.

    Returns `(handoff, form_ctx, fields)`:

    * `handoff` is None when takeover is switched off — the caller then
      treats the empty scan exactly as it did before this existed.
    * `form_ctx` and `fields` are always freshly re-grounded, so the caller
      can use them directly whether or not takeover changed anything.
    """
    settings = get_settings()
    if not settings.jobbot_agent_takeover:
        return None, form_ctx, []

    obs = observe(page, form_ctx, detail=Detail.CONTROLS)
    cls = classify(obs)
    trigger = detect_trigger(obs, cls, fields_found=0, expected_form=True)
    log.info(
        "Application %s: no form fields found (page=%s); agent takeover, trigger=%s",
        app_id or "?", cls.state.value, trigger.value if trigger else "-",
    )

    tool_ctx = ToolContext(
        page=page,
        form_ctx=form_ctx,
        application_state=ApplicationState.INSPECTING_FORM,
        # Nothing to upload during navigation; an empty allowlist means the
        # upload tool refuses outright rather than relying on policy alone.
        allowed_upload_paths=(),
        observe_detail=Detail.CONTROLS,
    )
    policy = PolicyContext(
        application_state=ApplicationState.INSPECTING_FORM,
        page_state=cls.state,
        autonomy=Autonomy.NAVIGATE,
        submission_ready=False,
    )

    handoff = take_over(
        "Reach the application form for this job posting.",
        tool_ctx, policy, decider or HeuristicDecider(),
        goal_reached=form_is_present,
        trigger=trigger,
        registry=ToolRegistry(),
        budget=Budget(max_steps=settings.jobbot_agent_max_steps),
        observe_detail=Detail.CONTROLS,
    )

    # Re-ground unconditionally: even an unresolved episode may have left
    # the browser on a different page or frame, and the caller must never
    # act on a handle from before.
    new_form_ctx, fields = _reground(tool_ctx.page, ats_hint)
    log.info(
        "Application %s: takeover %s (%s); %d field(s) after re-grounding",
        app_id or "?", handoff.outcome.value, handoff.reason[:120], len(fields),
    )
    return handoff, new_form_ctx, fields


#: Re-grounding runs after the agent has already settled the page, so it
#: polls briefly rather than using find_target_frame's full 15s hydration
#: budget — waiting the long timeout again on every attempt would dominate
#: the run.
_REGROUND_TIMEOUT_MS = 4000


def _reground(page, ats_hint: str) -> tuple[object, list]:
    try:
        form_ctx = find_target_frame(page, ats_hint, timeout_ms=_REGROUND_TIMEOUT_MS)
    except Exception:  # noqa: BLE001 - a detached frame must not end the attempt
        log.debug("find_target_frame failed while re-grounding", exc_info=True)
        form_ctx = page
    try:
        return form_ctx, scan_form(form_ctx)
    except Exception:  # noqa: BLE001
        log.debug("scan_form failed while re-grounding", exc_info=True)
        return form_ctx, []

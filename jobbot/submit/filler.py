"""Applies a fill plan to the live page. Fields flagged needs_human are never
touched here — they're left for the applicant to fill during the review step.
"""
from __future__ import annotations

import logging
from pathlib import Path

from jobbot.submit.form_scan import FieldSpec, FrameLike, locate
from jobbot.submit.values import RADIO_OPTION_LABEL_JS, read_field_value

log = logging.getLogger(__name__)


def upload_resume(page: FrameLike, fields: list[FieldSpec], resume_path: Path) -> bool:
    file_fields = [f for f in fields if f.field_type == "file"]
    if not file_fields:
        return False
    # Prefer a field whose label mentions resume/cv over e.g. a cover-letter upload.
    target = next((f for f in file_fields if re_search_resume(f.label)), file_fields[0])
    locate(page, target).set_input_files(str(resume_path))
    return True


def re_search_resume(label: str) -> bool:
    label = label.lower()
    return "resume" in label or "cv" in label


# Playwright's default action timeout is 30s per call — fine for normal
# rendering delays, disastrous for a genuinely non-actionable element (a
# real one slipped past form_scan.py once already, see
# test_aria_hidden_decoy_sibling_is_not_scanned_as_its_own_field). Failing
# fast here means a bad field costs a few seconds via the existing
# retry-once in apply_fill_plan, not the 30s+ stacked hangs observed live.
_ACTION_TIMEOUT_MS = 5000


def apply_field(page: FrameLike, spec: FieldSpec, value: str) -> None:
    loc = locate(page, spec)

    if spec.field_type in ("text", "email", "tel", "url", "number", "textarea"):
        loc.fill(value, timeout=_ACTION_TIMEOUT_MS)

    elif spec.field_type == "select":
        try:
            loc.select_option(label=value, timeout=_ACTION_TIMEOUT_MS)
        except Exception:
            log.warning("Could not select option %r for field %r; leaving as-is", value, spec.label)

    elif spec.field_type == "checkbox":
        truthy = value.strip().lower() in ("true", "yes", "1", "on", "checked")
        if truthy:
            loc.check(timeout=_ACTION_TIMEOUT_MS)
        else:
            loc.uncheck(timeout=_ACTION_TIMEOUT_MS)

    elif spec.field_type == "radio":
        # spec's locator matches every input in the group; pick the one whose
        # associated label text best matches the planned value.
        options = loc
        count = options.count()
        for i in range(count):
            option = options.nth(i)
            label_text = (option.evaluate(RADIO_OPTION_LABEL_JS) or "").strip()
            if value.strip().lower() in label_text.lower():
                option.check(timeout=_ACTION_TIMEOUT_MS)
                return
        log.warning("No radio option matched %r for field %r", value, spec.label)

    elif spec.field_type == "combobox":
        loc.click(timeout=_ACTION_TIMEOUT_MS)
        loc.fill(value, timeout=_ACTION_TIMEOUT_MS)
        page.wait_for_timeout(300)  # let the async option list render
        option = page.locator('[role="option"]', has_text=value).first
        if option.count() > 0:
            option.click(timeout=_ACTION_TIMEOUT_MS)
        else:
            log.warning("Combobox %r: no option matched %r, leaving open", spec.label, value)

    else:
        log.warning("Unhandled field type %r for %r; skipping", spec.field_type, spec.label)


def apply_fill_plan(page: FrameLike, fields: list[FieldSpec], plan: dict[int, dict]) -> list[FieldSpec]:
    """Fills every field NOT flagged needs_human, verifies each landed, and
    retries once on an empty result before giving up. Returns the fields
    that still need the human's attention (flagged, empty value, or a fill
    that didn't stick after retry)."""
    needs_human: list[FieldSpec] = []
    filled: list[tuple[FieldSpec, dict]] = []
    by_id = {f.field_id: f for f in fields}

    for field_id, decision in plan.items():
        spec = by_id.get(field_id)
        if spec is None:
            continue
        if decision["needs_human"] or not decision.get("value"):
            needs_human.append(spec)
            continue
        try:
            apply_field(page, spec, decision["value"])
            filled.append((spec, decision))
        except Exception as exc:
            log.warning("Failed to fill field %r: %s", spec.label, exc)
            needs_human.append(spec)

    for spec, decision in filled:
        if read_field_value(page, spec):
            continue
        log.info("Field %r came back empty after fill, retrying once", spec.label)
        try:
            apply_field(page, spec, decision["value"])
        except Exception:
            pass
        if not read_field_value(page, spec):
            needs_human.append(spec)

    return needs_human

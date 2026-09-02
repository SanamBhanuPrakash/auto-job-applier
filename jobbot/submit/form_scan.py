"""Generic DOM scanner: walks an application form and returns a stable,
JSON-describable list of fields the fill-planner can reason about.

Every candidate element gets a `data-jobbot-id` attribute injected so it can
be re-located deterministically after the LLM picks values by field_id.
Radio groups are addressed by `name` instead, since one logical question
maps to several physical <input> elements.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field as dc_field

from playwright.sync_api import Frame, Locator, Page

log = logging.getLogger(__name__)

# Most hosted-apply pages render the form directly; some employers embed it
# via an <iframe> on their own branded careers page instead. Everything
# downstream (scan_form, locate, filler.py, values.py) works against either
# a Page or a Frame — both implement .locator()/.evaluate() identically.
FrameLike = Page | Frame

_SCAN_JS = """
() => {
  function labelFor(el) {
    if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const text = labelledby.split(/\\s+/)
        .map(id => document.getElementById(id)?.innerText || '')
        .join(' ').trim();
      if (text) return text;
    }
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return lbl.innerText.trim();
    }
    const wrapper = el.closest('label');
    if (wrapper) return wrapper.innerText.trim();
    // Greenhouse/Lever often wrap field+label in a common container
    const container = el.closest('.field, .application-field, [class*="field"]');
    if (container) {
      const lbl = container.querySelector('label, .field-label, legend');
      if (lbl) return lbl.innerText.trim();
    }
    return el.getAttribute('placeholder') || el.getAttribute('name') || '';
  }

  function isRequired(el) {
    if (el.required || el.getAttribute('aria-required') === 'true') return true;
    const label = labelFor(el);
    return /\\*/.test(label);
  }

  let counter = 0;
  const results = [];
  const seenRadioGroups = new Set();

  const candidates = document.querySelectorAll(
    'input:not([type=hidden]):not([type=submit]):not([type=button]), textarea, select, [role="combobox"]'
  );

  candidates.forEach((el) => {
    // react-aria/Radix-style custom widgets (Greenhouse's newer comboboxes
    // among them) pair the real interactive control with an invisible
    // native <input required aria-hidden="true" tabindex="-1"> sibling
    // that exists purely so native HTML5 validation still fires — it is
    // never meant to be interacted with. Without this, the scanner
    // duplicated every such field under the same label, and Playwright
    // correctly refusing to fill a non-actionable hidden element turned
    // into a ~30s timeout per attempt (confirmed live on a real Greenhouse
    // form: every combobox had exactly this sibling).
    if (el.getAttribute('aria-hidden') === 'true') return;
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || tag).toLowerCase();

    if (type === 'radio') {
      const name = el.getAttribute('name') || '';
      if (!name || seenRadioGroups.has(name)) return;
      seenRadioGroups.add(name);
      const group = document.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`);
      const options = Array.from(group).map((g) => labelFor(g)).filter(Boolean);
      const groupLabel = el.closest('fieldset')?.querySelector('legend')?.innerText?.trim() || options[0] || name;
      results.push({
        field_id: counter++,
        label: groupLabel,
        type: 'radio',
        options,
        required: isRequired(el),
        group_name: name,
      });
      return;
    }

    const fid = counter++;
    el.setAttribute('data-jobbot-id', String(fid));

    let options = [];
    if (tag === 'select') {
      options = Array.from(el.options).map((o) => o.textContent.trim()).filter(Boolean);
    }

    let fieldType = type;
    if (el.getAttribute('role') === 'combobox') fieldType = 'combobox';
    else if (tag === 'textarea') fieldType = 'textarea';
    else if (tag === 'select') fieldType = 'select';
    else if (type === 'checkbox') fieldType = 'checkbox';
    else if (type === 'file') fieldType = 'file';
    else if (['text', 'email', 'tel', 'url', 'number'].includes(type)) fieldType = type;
    else fieldType = 'text';

    results.push({
      field_id: fid,
      label: labelFor(el),
      type: fieldType,
      options,
      required: isRequired(el),
      group_name: null,
    });
  });

  return results;
}
"""


@dataclass
class FieldSpec:
    field_id: int
    label: str
    field_type: str
    options: list[str] = dc_field(default_factory=list)
    required: bool = False
    group_name: str | None = None

    def to_llm_dict(self) -> dict:
        return {
            "field_id": self.field_id,
            "label": self.label,
            "type": self.field_type,
            "options": self.options,
            "required": self.required,
        }


def scan_form(page: FrameLike) -> list[FieldSpec]:
    raw = page.evaluate(_SCAN_JS)
    return [
        FieldSpec(
            field_id=r["field_id"],
            label=r["label"] or "(unlabeled field)",
            field_type=r["type"],
            options=r.get("options", []) or [],
            required=r.get("required", False),
            group_name=r.get("group_name"),
        )
        for r in raw
    ]


def locate(page: FrameLike, spec: FieldSpec) -> Locator:
    if spec.field_type == "radio":
        return page.locator(f'input[type="radio"][name="{spec.group_name}"]')
    return page.locator(f'[data-jobbot-id="{spec.field_id}"]')


#: Controls that make a context worth filling, even with no <form> tag.
#: Includes ARIA roles because Workday, Darwinbox and friends build their
#: widgets out of divs.
_FILLABLE_SELECTOR = (
    "input:not([type=hidden]):not([type=submit]):not([type=button]), "
    "textarea, select, "
    "[role=textbox], [role=combobox], [role=listbox], [role=radiogroup], "
    "[role=checkbox], [role=spinbutton], [contenteditable=true]"
)

#: How many fillable controls make a context an application form rather
#: than a search box or a newsletter signup. Three is enough to exclude a
#: site-search widget and low enough to admit a short screening page.
_MIN_FILLABLE = 3


def _fillable_count(ctx: FrameLike) -> int:
    try:
        return ctx.locator(_FILLABLE_SELECTOR).count()
    except Exception:  # noqa: BLE001 - a detached frame counts as nothing
        return 0


def find_target_frame(
    page: Page,
    ats_hint: str = "",
    timeout_ms: int = 15000,
    *,
    min_fillable: int = _MIN_FILLABLE,
) -> FrameLike:
    """Find the context holding the application form.

    Preference order, and the order matters:

    1. A real `<form>`, at the top level or in a matching iframe. This is
       Greenhouse, Lever, Ashby and most hosted boards, and it stays the
       first thing checked so nothing about those changes.
    2. Failing that, a context carrying at least `min_fillable`
       interactive controls.

    Step 2 exists because **a large share of applications are not wrapped
    in a `<form>` at all**. Fetching a Workday careers page returns a 6.5KB
    SPA shell with zero `<form>` and zero `<input>`; after hydration it is
    a tree of divs carrying `data-automation-id`, and its inputs never sit
    inside a form element. Darwinbox, Keka and most React career sites are
    the same shape.

    Requiring `<form>` therefore rejected those pages outright — and
    `scan_form` would have handled them perfectly, because it queries the
    whole document rather than a form subtree. The scanner could already
    read these pages; only this gate said no.

    Raises TimeoutError when neither is found, which is still a real
    "this is not a form-shaped page" signal rather than something to guess
    past.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    best_seen = 0
    while True:
        if page.locator("form").count() > 0:
            return page
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            if ats_hint and ats_hint not in (frame.url or ""):
                continue
            try:
                if frame.locator("form").count() > 0:
                    return frame
            except Exception:
                continue

        # No <form> anywhere. Fall back to whatever context actually has
        # controls, preferring the richest one.
        candidates: list[tuple[int, FrameLike]] = [(_fillable_count(page), page)]
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            if ats_hint and ats_hint not in (frame.url or ""):
                continue
            candidates.append((_fillable_count(frame), frame))
        count, ctx = max(candidates, key=lambda pair: pair[0])
        best_seen = max(best_seen, count)
        if count >= min_fillable:
            log.info("No <form> element; using a context with %d fillable control(s)", count)
            return ctx

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"No application form found within {timeout_ms}ms: no <form> element "
                f"anywhere, and the richest context had only {best_seen} fillable "
                f"control(s) (need {min_fillable}). Either the page has not finished "
                f"loading, the application is behind a button or a login, or this "
                f"employer's flow is not a form the generic scanner can handle."
            )
        page.wait_for_timeout(300)

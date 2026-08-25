"""Generic DOM scanner: walks an application form and returns a stable,
JSON-describable list of fields the fill-planner can reason about.

Every candidate element gets a `data-jobbot-id` attribute injected so it can
be re-located deterministically after the LLM picks values by field_id.
Radio groups are addressed by `name` instead, since one logical question
maps to several physical <input> elements.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field as dc_field

from playwright.sync_api import Frame, Locator, Page

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


def find_target_frame(page: Page, ats_hint: str = "", timeout_ms: int = 15000) -> FrameLike:
    """Poll the page (which may still be hydrating a JS-rendered form) for a
    <form>, either at the top level or inside an iframe. If the employer's
    own careers page embeds the ATS form in an iframe (common — see
    module docstring), returns that iframe's Frame instead of the Page so
    every downstream call (scan_form, fill, submit) operates on wherever
    the form actually lives.

    Raises TimeoutError if no form shows up anywhere within timeout_ms —
    that's a real "this employer's flow isn't a plain form" signal, not
    something to guess past.
    """
    deadline = time.monotonic() + timeout_ms / 1000
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
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"No <form> found on the page or in a matching iframe within {timeout_ms}ms "
                f"(looked for an iframe URL containing {ats_hint!r}). This employer's apply "
                f"flow likely isn't a plain hosted form the generic scanner can handle."
            )
        page.wait_for_timeout(300)

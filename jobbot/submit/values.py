"""Reads the current human-readable value out of a scanned field. Shared by
the fill verifier (did the value stick?) and the learning capture step
(what did the human end up entering?) so both agree on what a field's
"value" means.
"""
from __future__ import annotations

from playwright.sync_api import Page

from jobbot.submit.form_scan import FieldSpec, locate

_SELECT_LABEL_JS = "(el) => el.options[el.selectedIndex]?.textContent?.trim() || ''"

RADIO_OPTION_LABEL_JS = """
(el) => {
  if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
  if (el.id) {
    const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
    if (lbl) return lbl.innerText;
  }
  return el.closest('label')?.innerText || el.value || '';
}
"""


def read_field_value(page: Page, spec: FieldSpec) -> str:
    loc = locate(page, spec)
    try:
        if spec.field_type in ("text", "email", "tel", "url", "number", "textarea", "combobox"):
            return (loc.input_value() or "").strip()
        if spec.field_type == "select":
            return (loc.evaluate(_SELECT_LABEL_JS) or "").strip()
        if spec.field_type == "checkbox":
            return "Yes" if loc.is_checked() else ""
        if spec.field_type == "radio":
            count = loc.count()
            for i in range(count):
                option = loc.nth(i)
                if option.is_checked():
                    return (option.evaluate(RADIO_OPTION_LABEL_JS) or "").strip()
            return ""
    except Exception:
        return ""
    return ""

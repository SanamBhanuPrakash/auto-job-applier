"""BrowserObservation — the agent's perception of the live page.

Design follows two research findings (docs/research/agent-architecture.md
§5, §6):

1. **Accessibility tree first, vision selectively.** A vision model has to
   guess which pixels form a control; the accessibility tree states the role
   and accessible name outright. Measured on this repo's own small fixture,
   Playwright's `aria_snapshot()` is 1,020 chars against 4,931 chars of raw
   HTML for the same page, and that ratio widens sharply on real postings.
   Reliability-optimized systems (Playwright MCP, Atlas) lead with it.

2. **Context rot is real.** Model recall degrades as context grows, so an
   observation is not "everything we could find" — it is deliberately
   budgeted and truncated at construction. Dumping the DOM makes the agent
   worse, not merely more expensive.

Hence perception is *tiered*: each level costs more than the last, and the
caller asks for the cheapest level that can answer its question.

    METADATA   url, title, tab/frame/dialog counts        (~free)
    CONTROLS   interactive elements w/ role + name        (cheap)
    ARIA       full accessibility snapshot                (moderate)
    DOM        focused region of raw HTML                 (expensive)
    SCREENSHOT pixels, for canvas/visual ambiguity only   (most expensive)

The browser is the source of truth. An observation is a *snapshot*, never a
belief to carry forward: after any consequential action it must be rebuilt,
because element handles go stale on navigation and on framework re-render
(docs/research/browser-agent-failures.md §4).
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from enum import IntEnum

from jobbot.submit.form_scan import FrameLike

log = logging.getLogger(__name__)


class Detail(IntEnum):
    """Perception tiers, cheapest first. Ordered so callers can compare."""

    METADATA = 0
    CONTROLS = 1
    ARIA = 2
    DOM = 3
    SCREENSHOT = 4


#: Hard caps applied at construction. These exist to bound context, not to
#: be "big enough for anything" — see §6 context rot.
MAX_VISIBLE_TEXT = 4000
MAX_ARIA_CHARS = 12000
MAX_DOM_CHARS = 8000
MAX_CONTROLS = 120
MAX_VALIDATION_MESSAGES = 25


@dataclass
class Control:
    """One interactive element, identified the way a model reasons best:
    by role and accessible name rather than a CSS selector or UUID
    (docs/research/agent-architecture.md §7)."""

    ref: str          # stable handle: the injected data-jobbot-id
    role: str         # button | link | textbox | combobox | checkbox | radio | ...
    name: str         # accessible name
    value: str = ""
    enabled: bool = True
    required: bool = False

    def to_dict(self) -> dict:
        d = {"ref": self.ref, "role": self.role, "name": self.name}
        if self.value:
            d["value"] = self.value
        if not self.enabled:
            d["enabled"] = False
        if self.required:
            d["required"] = True
        return d


@dataclass
class BrowserObservation:
    """A bounded, structured snapshot of the current browser state."""

    url: str = ""
    title: str = ""
    detail: Detail = Detail.METADATA

    # metadata tier
    frame_count: int = 0
    frame_urls: list[str] = field(default_factory=list)
    active_frame_url: str = ""
    tab_count: int = 1
    active_tab_index: int = 0
    dialog_open: bool = False
    has_form: bool = False

    # controls tier
    controls: list[Control] = field(default_factory=list)
    validation_messages: list[str] = field(default_factory=list)

    # richer tiers
    visible_text: str = ""
    aria_snapshot: str = ""
    dom_excerpt: str = ""
    screenshot_path: str = ""

    #: Set when perception itself partially failed (page navigating, frame
    #: detached). The agent must treat a degraded observation as weaker
    #: evidence, not as "nothing is there".
    degraded: bool = False
    notes: list[str] = field(default_factory=list)

    # ---- digest -------------------------------------------------------

    def state_digest(self) -> str:
        """A stable fingerprint of *meaningful* page state.

        Deliberately excludes volatile content (timestamps, ad slots, session
        ids in text) and includes only what determines whether the agent has
        actually made progress: where it is, what it can interact with, what
        is filled in, and what the page is complaining about.

        This is the input to loop detection — see trajectory.py. Getting it
        wrong in either direction is costly: too volatile and every step
        looks like progress (loops never detected); too coarse and real
        progress looks like a loop (work abandoned).
        """
        parts = [
            _normalize_url(self.url),
            # Normalized too: the frame URL carries the same fragment, so
            # leaving it raw would let a same-page anchor read as progress
            # and quietly defeat loop detection.
            _normalize_url(self.active_frame_url),
            str(self.tab_count),
            str(self.active_tab_index),
            "dialog" if self.dialog_open else "",
            "form" if self.has_form else "",
        ]
        for c in sorted(self.controls, key=lambda c: (c.role, c.name)):
            # Include the value: typing into a field IS progress, even
            # though the set of controls is unchanged.
            parts.append(f"{c.role}|{c.name}|{c.value}|{int(c.enabled)}")
        parts.extend(sorted(self.validation_messages))
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]

    # ---- serialization for the model ----------------------------------

    def to_agent_dict(self, *, include_text: bool = True) -> dict:
        """The form handed to the LLM. Small, structured, high-signal.

        Untrusted page content (text, control names, validation messages)
        stays in clearly-named fields so the prompt layer can fence it as
        data rather than instruction — see browser-agent-failures.md §9.
        """
        payload: dict = {
            "url": self.url,
            "title": self.title,
            "tabs": self.tab_count,
            "active_tab": self.active_tab_index,
            "frames": self.frame_count,
            "has_form": self.has_form,
            "dialog_open": self.dialog_open,
            "controls": [c.to_dict() for c in self.controls],
        }
        if self.validation_messages:
            payload["validation_messages"] = self.validation_messages
        if include_text and self.visible_text:
            payload["visible_text"] = self.visible_text
        if self.aria_snapshot:
            payload["aria_snapshot"] = self.aria_snapshot
        if self.dom_excerpt:
            payload["dom_excerpt"] = self.dom_excerpt
        if self.degraded:
            payload["degraded"] = True
            payload["notes"] = self.notes
        return payload

    def approx_tokens(self) -> int:
        """Rough size guard (~4 chars/token). Used to assert the budget
        holds rather than to bill anything."""
        total = len(self.visible_text) + len(self.aria_snapshot) + len(self.dom_excerpt)
        total += sum(len(c.role) + len(c.name) + len(c.value) for c in self.controls)
        total += sum(len(m) for m in self.validation_messages)
        return total // 4


def _normalize_url(url: str) -> str:
    """Drop the fragment and trailing slash so a same-page anchor change
    doesn't read as navigation."""
    if not url:
        return ""
    base = url.split("#", 1)[0]
    return base.rstrip("/")


# --- extraction --------------------------------------------------------

_CONTROLS_JS = """
() => {
  const out = [];
  let counter = 0;

  function accessibleName(el) {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria.trim();
    const labelledby = el.getAttribute('aria-labelledby');
    if (labelledby) {
      const t = labelledby.split(/\\s+/)
        .map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
      if (t) return t;
    }
    if (el.id) {
      const lbl = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (lbl) return lbl.innerText.trim();
    }
    const wrapper = el.closest('label');
    if (wrapper) return wrapper.innerText.trim();
    const container = el.closest('.field, .application-field, [class*="field"]');
    if (container) {
      const lbl = container.querySelector('label, .field-label, legend');
      if (lbl) return lbl.innerText.trim();
    }
    return (el.getAttribute('placeholder') || el.innerText || el.value || el.name || '').trim();
  }

  function visible(el) {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 || r.height > 0;
  }

  function roleOf(el) {
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName.toLowerCase();
    if (tag === 'a') return 'link';
    if (tag === 'button') return 'button';
    if (tag === 'select') return 'select';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input') {
      const t = (el.getAttribute('type') || 'text').toLowerCase();
      if (t === 'submit' || t === 'button') return 'button';
      if (t === 'checkbox') return 'checkbox';
      if (t === 'radio') return 'radio';
      if (t === 'file') return 'file';
      return 'textbox';
    }
    return tag;
  }

  const selector = [
    'a[href]', 'button', 'input:not([type=hidden])', 'select', 'textarea',
    '[role=button]', '[role=link]', '[role=combobox]', '[role=checkbox]',
    '[role=radio]', '[role=tab]', '[role=menuitem]',
  ].join(',');

  document.querySelectorAll(selector).forEach((el) => {
    if (!visible(el)) return;
    let ref = el.getAttribute('data-jobbot-id');
    if (ref === null) {
      ref = 'o' + (counter++);
      el.setAttribute('data-jobbot-id', ref);
    }
    const role = roleOf(el);
    let value = '';
    if (role === 'checkbox' || role === 'radio') value = el.checked ? 'checked' : '';
    else if (role === 'select') value = el.options?.[el.selectedIndex]?.textContent?.trim() || '';
    else if ('value' in el && role !== 'file') value = (el.value || '').slice(0, 120);

    out.push({
      ref: String(ref),
      role,
      name: (accessibleName(el) || '').slice(0, 160),
      value,
      enabled: !el.disabled,
      required: !!(el.required || el.getAttribute('aria-required') === 'true'),
    });
  });
  return out;
}
"""

#: Elements that conventionally carry form validation feedback.
_VALIDATION_JS = """
() => {
  const out = [];
  const sel = [
    '[role=alert]', '[aria-invalid=true]', '.error', '.field-error',
    '.invalid-feedback', '.help-block.error', '[class*="error"]',
  ].join(',');
  document.querySelectorAll(sel).forEach((el) => {
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return;
    const t = (el.innerText || '').trim();
    if (t && t.length < 300) out.push(t);
  });
  return [...new Set(out)];
}
"""


def _safe(fn, default, notes: list[str], label: str):
    """Perception must never throw. A page mid-navigation is normal, and a
    partial observation is far more useful than an exception — the agent
    can act on `degraded`, it cannot act on a traceback."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        notes.append(f"{label} unavailable: {type(exc).__name__}")
        log.debug("Observation: %s failed", label, exc_info=True)
        return default


def observe(
    page,
    form_ctx: FrameLike | None = None,
    *,
    detail: Detail = Detail.CONTROLS,
    screenshot_path: str = "",
) -> BrowserObservation:
    """Build a bounded observation of the current page at the given tier.

    `form_ctx` is the frame actually holding the application form when it
    differs from the top-level page (the common Greenhouse-in-an-iframe
    case); controls and validation are read from there, page-level facts
    from `page`.
    """
    notes: list[str] = []
    ctx: FrameLike = form_ctx if form_ctx is not None else page

    obs = BrowserObservation(detail=detail, notes=notes)
    obs.url = _safe(lambda: page.url or "", "", notes, "url")
    obs.title = _safe(lambda: page.title() or "", "", notes, "title")

    # -- METADATA -------------------------------------------------------
    frames = _safe(lambda: list(page.frames), [], notes, "frames")
    obs.frame_count = len(frames)
    obs.frame_urls = [f.url for f in frames][:20]
    obs.active_frame_url = _safe(lambda: ctx.url or "", "", notes, "active frame url")

    ctx_page = _safe(lambda: page.context, None, notes, "context")
    if ctx_page is not None:
        pages = _safe(lambda: list(ctx_page.pages), [page], notes, "tabs")
        obs.tab_count = len(pages)
        try:
            obs.active_tab_index = pages.index(page)
        except ValueError:
            obs.active_tab_index = 0

    obs.has_form = _safe(lambda: ctx.locator("form").count() > 0, False, notes, "form count")
    # A native dialog blocks everything else; an open modal is often what
    # makes an otherwise-correct click do nothing.
    obs.dialog_open = _safe(
        lambda: ctx.locator('[role=dialog], dialog[open], .modal.show').count() > 0,
        False, notes, "dialog",
    )

    if detail <= Detail.METADATA:
        obs.degraded = bool(notes)
        return obs

    # -- CONTROLS -------------------------------------------------------
    raw_controls = _safe(lambda: ctx.evaluate(_CONTROLS_JS), [], notes, "controls")
    obs.controls = [
        Control(
            ref=str(c.get("ref", "")),
            role=c.get("role", ""),
            name=c.get("name", ""),
            value=c.get("value", "") or "",
            enabled=bool(c.get("enabled", True)),
            required=bool(c.get("required", False)),
        )
        for c in raw_controls[:MAX_CONTROLS]
    ]
    if len(raw_controls) > MAX_CONTROLS:
        notes.append(f"controls truncated: {len(raw_controls)} -> {MAX_CONTROLS}")

    validation = _safe(lambda: ctx.evaluate(_VALIDATION_JS), [], notes, "validation")
    obs.validation_messages = [str(v) for v in validation[:MAX_VALIDATION_MESSAGES]]

    obs.visible_text = _safe(
        lambda: (page.inner_text("body", timeout=3000) or "")[:MAX_VISIBLE_TEXT],
        "", notes, "visible text",
    )

    if detail <= Detail.CONTROLS:
        obs.degraded = bool(notes)
        return obs

    # -- ARIA -----------------------------------------------------------
    # The preferred rich representation: semantic, compact, and it states
    # roles outright instead of making the model infer them.
    obs.aria_snapshot = _safe(
        lambda: (ctx.locator("body").aria_snapshot() or "")[:MAX_ARIA_CHARS],
        "", notes, "aria snapshot",
    )

    if detail <= Detail.ARIA:
        obs.degraded = bool(notes)
        return obs

    # -- DOM ------------------------------------------------------------
    # Only a focused region, never the whole document.
    def _dom() -> str:
        if obs.has_form:
            return ctx.locator("form").first.inner_html(timeout=3000)[:MAX_DOM_CHARS]
        return ctx.locator("body").inner_html(timeout=3000)[:MAX_DOM_CHARS]

    obs.dom_excerpt = _safe(_dom, "", notes, "dom excerpt")

    if detail <= Detail.DOM:
        obs.degraded = bool(notes)
        return obs

    # -- SCREENSHOT -----------------------------------------------------
    if screenshot_path:
        ok = _safe(lambda: page.screenshot(path=screenshot_path) or True, False, notes, "screenshot")
        obs.screenshot_path = screenshot_path if ok else ""

    obs.degraded = bool(notes)
    return obs

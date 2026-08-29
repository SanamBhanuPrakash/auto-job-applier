"""Normalizes job-posting text before it's stored or matched against.

Found live: Greenhouse's `content` field is raw HTML (`<div class="content-
intro"><p><strong>Who we are&nbsp;</strong>...`), not plain text. Storing it
as-is meant the markup was polluting the lexical shortlist filter, the
per-resume profile matcher, and the LLM scoring/fill-planning prompts alike
— tags and entities eating token budget and diluting the keyword signal
that's actually meant to represent the job. No dependency added for this:
stdlib `html.unescape` plus a couple of regexes covers it, and running it on
text that's already plain (Lever's `descriptionPlain`, Adzuna's
`description`, ...) is a safe no-op.
"""
from __future__ import annotations

import html
import re

_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")


def strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = _TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = _BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()

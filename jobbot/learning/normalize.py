"""Turns a form field's label text into a stable key so the same question
asked with slightly different punctuation/wording still matches."""
from __future__ import annotations

import re

_TRAILING_MARKS_RE = re.compile(r"[\s*:]+$")
_NON_WORD_RE = re.compile(r"[^\w\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_label(label: str) -> str:
    text = label.strip().lower()
    text = _TRAILING_MARKS_RE.sub("", text)
    text = _NON_WORD_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    return text.strip()

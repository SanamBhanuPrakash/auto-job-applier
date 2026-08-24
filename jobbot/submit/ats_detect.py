from __future__ import annotations


def detect_ats(url: str) -> str:
    """Best-effort ATS detection from a job URL. Returns "" if unsupported."""
    u = url.lower()
    if "greenhouse.io" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    return ""

from __future__ import annotations


def detect_ats(url: str) -> str:
    """Best-effort ATS detection from a job URL. Returns "" if unsupported.

    "Supported" means a submission handler exists in `jobbot/submit/`, not
    merely that discovery can read the board — the two lists differ, and
    conflating them would send the apply engine at a page it cannot fill.
    """
    u = (url or "").lower()
    if "greenhouse.io" in u:
        return "greenhouse"
    if "lever.co" in u:
        return "lever"
    if "ashbyhq.com" in u:
        return "ashby"
    return ""

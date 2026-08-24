from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NormalizedJob:
    """A job posting normalized across every discovery source."""

    source: str  # "greenhouse", "lever", "adzuna", ...
    external_id: str
    company: str
    title: str
    url: str
    location: str = ""
    remote: bool = False
    description: str = ""
    posted_at: str = ""
    ats: str = ""  # "greenhouse" | "lever" | "" (unsupported for auto-submit)
    raw: dict = field(default_factory=dict)

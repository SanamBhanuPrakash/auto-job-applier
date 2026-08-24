"""The forced needs_human override is the single most important safety net
in this codebase (see submit/fill_planner.py). Test it without hitting the
network by exercising the regex directly against realistic form labels.
"""
from jobbot.submit.fill_planner import _ALWAYS_HUMAN_RE


SENSITIVE_LABELS = [
    "Are you a veteran?",
    "Do you have a disability?",
    "Gender",
    "Race/Ethnicity",
    "Will you now or in the future require visa sponsorship?",
    "Are you authorized to work in the US?",
    "Social Security Number",
    "I certify that the information above is true and accurate",
    "Have you ever been convicted of a criminal offense?",
]

BENIGN_LABELS = [
    "First name",
    "Email address",
    "LinkedIn URL",
    "Years of Python experience",
    "Current location",
]


def test_sensitive_labels_are_flagged():
    for label in SENSITIVE_LABELS:
        assert _ALWAYS_HUMAN_RE.search(label), f"expected {label!r} to be flagged sensitive"


def test_benign_labels_are_not_flagged():
    for label in BENIGN_LABELS:
        assert not _ALWAYS_HUMAN_RE.search(label), f"did not expect {label!r} to be flagged sensitive"

"""Trust levels for remembered application answers.

Not all remembered values are equally trustworthy, and treating them as if
they were is a correctness bug with real consequences: a value the model
guessed on a form that was never submitted must never be replayed onto a
work-authorization question on a later application as though the candidate
had confirmed it.

Trust is earned in one direction only:

    MODEL_GUESS  --(form actually submitted)-->  VERIFIED
    HUMAN_ENTERED --------------------------->  VERIFIED
    HUMAN_ENTERED / VERIFIED are the only values eligible to auto-fill a
    sensitive (work authorization / EEOC / legal attestation) question.
"""
from __future__ import annotations

from enum import Enum


class Provenance(str, Enum):
    #: The LLM produced this value; the form was never confirmed submitted.
    MODEL_GUESS = "model_guess"
    #: Read back off a form the candidate personally filled/reviewed.
    HUMAN_ENTERED = "human_entered"
    #: Present on a form whose submission we positively verified.
    VERIFIED = "verified"
    #: Provenance not recorded (rows written before this existed).
    UNVERIFIED = "unverified"


#: Ranked weakest -> strongest, so an upsert never downgrades trust.
_RANK = {
    Provenance.UNVERIFIED: 0,
    Provenance.MODEL_GUESS: 1,
    Provenance.HUMAN_ENTERED: 2,
    Provenance.VERIFIED: 3,
}


def rank(value: str | Provenance) -> int:
    try:
        return _RANK[Provenance(value)]
    except (ValueError, KeyError):
        return 0


def strongest(a: str | Provenance, b: str | Provenance) -> str:
    """Merge two provenances, keeping the stronger. Re-seeing a value on a
    later form must not weaken what we already knew about it."""
    winner = a if rank(a) >= rank(b) else b
    return winner.value if isinstance(winner, Provenance) else str(winner)


def may_autofill_sensitive(provenance: str, human_confirmed: bool) -> bool:
    """Whether a remembered answer is trustworthy enough to be replayed,
    unattended, onto a legal/EEOC/work-authorization question.

    A model guess never qualifies, no matter how many times it has been
    reused — reuse count measures repetition, not correctness.
    """
    if human_confirmed:
        return True
    return rank(provenance) >= _RANK[Provenance.HUMAN_ENTERED]

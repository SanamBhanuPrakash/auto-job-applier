"""One-time interview for the questions that otherwise stop every run.

The problem this solves. Work authorization, visa sponsorship, veteran
status, disability, race/ethnicity and legal attestations are refused by
the fill planner and by `agent/policy.py` — no model-produced value for
one of these is ever used. That is correct: software should not invent
someone's immigration status or sign a legal attestation for them. But it
also means a typical US application always has at least one field needing
a human, and `submit/base.py` switches auto-submit off whenever that is
true. So unattended operation was unreachable in practice.

The fix is not to weaken the rule. It is to *answer the questions once,
from the person*, up front. An answer typed here has provenance
HUMAN_ENTERED and `human_confirmed=True`, which is exactly the trust level
`may_autofill_sensitive` requires. The guardrail is unchanged; there is
simply nothing left for it to block.

Two design points worth stating.

**Every question is skippable, and skipping is not a failure.** Anything
left unanswered keeps stopping for you, which is the correct behaviour for
a question you would rather answer case by case. Nothing here nags, and
nothing is pre-filled with a plausible guess.

**Nothing is inferred from the resume.** The catalogue is asked, not
derived. A resume mentioning a US university does not tell you someone's
work authorization, and guessing it here would reintroduce exactly the
failure the guardrail exists to prevent — just earlier, and with the
answer wearing a human's provenance.

Question wording matches how real ATS forms phrase these, because the
learning store matches remembered answers by fuzzy label similarity: a
catalogue entry worded like the real thing is what makes the match land.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Region(str, Enum):
    """Which market's applications ask this. Keeps an Indian candidate
    from being walked through US veteran-status questions and vice versa."""

    US = "US"
    INDIA = "INDIA"
    EU_UK = "EU_UK"
    ANY = "ANY"


@dataclass(frozen=True)
class Question:
    key: str
    #: Phrased the way ATS forms phrase it — this is what the fuzzy
    #: matcher compares a live form label against.
    prompt: str
    field_type: str = "text"
    options: tuple[str, ...] = ()
    region: Region = Region.ANY
    sensitive: bool = True
    help: str = ""
    #: Extra phrasings the same question appears under. Stored as
    #: additional rows so a differently-worded form still matches.
    aliases: tuple[str, ...] = field(default_factory=tuple)


YES_NO = ("Yes", "No")

#: The catalogue. Ordered so the questions almost every application asks
#: come first — someone who quits halfway still gets most of the benefit.
CATALOGUE: tuple[Question, ...] = (
    Question(
        "work_authorization",
        "Are you legally authorized to work in the United States?",
        "radio", YES_NO, Region.US,
        help="Asked on nearly every US application.",
        aliases=(
            "Are you authorized to work in the US?",
            "Are you legally eligible to work in the United States?",
            "Do you have the legal right to work in the United States?",
        ),
    ),
    Question(
        "sponsorship",
        "Will you now or in the future require sponsorship for employment visa status?",
        "radio", YES_NO, Region.US,
        help="Answer for your situation as it stands, not as you hope it will be.",
        aliases=(
            "Do you now or will you in the future require visa sponsorship?",
            "Will you require sponsorship to work in the United States?",
            "Do you require sponsorship for employment?",
        ),
    ),
    Question(
        "india_work_authorization",
        "Are you legally authorized to work in India?",
        "radio", YES_NO, Region.INDIA,
        aliases=("Do you have the right to work in India?",),
    ),
    Question(
        "eu_work_authorization",
        "Do you have the right to work in the UK/EU?",
        "radio", YES_NO, Region.EU_UK,
        aliases=(
            "Are you legally authorized to work in the United Kingdom?",
            "Do you have the right to work in the European Union?",
        ),
    ),
    Question(
        "veteran_status",
        "Are you a protected veteran?",
        "select",
        ("I am not a protected veteran",
         "I identify as one or more of the classifications of a protected veteran",
         "I don't wish to answer"),
        Region.US,
        help="US EEO question. \"I don't wish to answer\" is a normal, accepted answer.",
        aliases=("Veteran status", "Protected veteran status",),
    ),
    Question(
        "disability_status",
        "Do you have a disability?",
        "select",
        ("Yes, I have a disability, or have had one in the past",
         "No, I do not have a disability and have not had one in the past",
         "I do not want to answer"),
        Region.US,
        help="US EEO question (form CC-305). Declining to answer is normal.",
        aliases=("Disability status", "Voluntary self-identification of disability",),
    ),
    Question(
        "gender",
        "Gender",
        "select", ("Male", "Female", "Non-binary", "I don't wish to answer"),
        Region.US,
        help="US EEO demographic question.",
        aliases=("What is your gender?", "Gender identity",),
    ),
    Question(
        "race_ethnicity",
        "Race/Ethnicity",
        "select",
        ("Asian", "Black or African American", "Hispanic or Latino",
         "Native Hawaiian or Other Pacific Islander",
         "American Indian or Alaska Native", "White", "Two or More Races",
         "I don't wish to answer"),
        Region.US,
        help="US EEO demographic question.",
        aliases=("What is your race or ethnicity?", "Ethnicity",),
    ),
    Question(
        "criminal_history",
        "Have you ever been convicted of a felony?",
        "radio", YES_NO, Region.US,
        help="Some jurisdictions bar this question. Skip it if you would "
             "rather handle it case by case.",
        aliases=("Have you ever been convicted of a crime?",),
    ),
    Question(
        "salary_expectation",
        "What are your salary expectations?",
        "text", (), Region.ANY, sensitive=True,
        help="A range is fine, e.g. \"INR 30-40 LPA\" or \"$180k-$210k\". "
             "Leave blank to be asked each time.",
        aliases=("Expected salary", "Desired compensation", "Salary requirements",),
    ),
    Question(
        "notice_period",
        "What is your notice period?",
        "text", (), Region.ANY, sensitive=False,
        help="e.g. \"30 days\", \"Immediate\", \"2 weeks\".",
        aliases=("Notice period", "When can you start?", "Earliest start date",),
    ),
    Question(
        "current_ctc",
        "What is your current CTC?",
        "text", (), Region.INDIA, sensitive=True,
        help="Common on Indian applications. Leave blank if you would rather not say.",
        aliases=("Current CTC", "Current compensation", "Current salary",),
    ),
    Question(
        "relocation",
        "Are you willing to relocate?",
        "radio", YES_NO, Region.ANY, sensitive=False,
        aliases=("Willing to relocate?", "Are you open to relocation?",),
    ),
    Question(
        "certify_true",
        "I certify that the information provided is true and accurate to the "
        "best of my knowledge",
        "checkbox", ("Yes",), Region.ANY,
        help="A legal attestation. Answering it here means you are making that "
             "statement yourself, in advance, for applications this tool submits "
             "on your behalf. Skip it if you would rather tick it per application.",
        aliases=("I certify that the above information is true",
                 "I confirm the information provided is accurate",),
    ),
)


def questions_for(regions: tuple[Region, ...]) -> tuple[Question, ...]:
    wanted = set(regions) | {Region.ANY}
    return tuple(q for q in CATALOGUE if q.region in wanted)


def by_key(key: str) -> Question | None:
    return next((q for q in CATALOGUE if q.key == key), None)


def save_answer(question: Question, value: str) -> int:
    """Store one answer with human provenance. Returns rows written.

    The question's aliases are stored as separate rows so a form using a
    different wording still matches. Cheap, and it removes the single
    biggest reason a seeded answer fails to apply.
    """
    from jobbot.db import session_scope
    from jobbot.learning import store as learning_store
    from jobbot.learning.provenance import Provenance

    if not value:
        return 0
    written = 0
    with session_scope() as session:
        for label in (question.prompt, *question.aliases):
            learning_store.upsert(
                session, label, question.field_type, value,
                sensitive=question.sensitive,
                provenance=Provenance.HUMAN_ENTERED.value,
                human_confirmed=True,
            )
            written += 1
        session.commit()
    return written


def answered_keys() -> set[str]:
    """Which catalogue questions already have a human-confirmed answer."""
    from jobbot.db import session_scope
    from jobbot.learning import store as learning_store
    from jobbot.learning.provenance import may_autofill_sensitive

    answered: set[str] = set()
    with session_scope() as session:
        for question in CATALOGUE:
            row = learning_store.find_match(session, question.prompt, question.field_type)
            if row is None or not row.value:
                continue
            if question.sensitive and not may_autofill_sensitive(
                row.provenance or "", bool(row.human_confirmed)
            ):
                # A model guess sitting under this question is not an
                # answer — it is exactly what the guardrail refuses.
                continue
            answered.add(question.key)
    return answered


def unattended_readiness(regions: tuple[Region, ...] = (Region.ANY,)) -> dict:
    """How close this profile is to running without stopping.

    Reports the sensitive questions still unanswered, because those are
    precisely what turns auto-submit off on a real application.
    """
    from jobbot.config import get_settings

    relevant = questions_for(regions)
    answered = answered_keys()
    missing = [q for q in relevant if q.sensitive and q.key not in answered]
    settings = get_settings()
    return {
        "relevant": len(relevant),
        "answered": len([q for q in relevant if q.key in answered]),
        "missing_sensitive": [q.key for q in missing],
        "autofill_sensitive_enabled": settings.jobbot_autofill_sensitive,
        "auto_submit_enabled": settings.jobbot_auto_submit,
        # Every condition that must hold for a run to submit without you.
        "will_run_unattended": (
            not missing
            and settings.jobbot_autofill_sensitive
            and settings.jobbot_auto_submit
        ),
    }

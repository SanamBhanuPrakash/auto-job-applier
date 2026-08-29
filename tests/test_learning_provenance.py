"""Memory trust boundaries.

The defect these close: `capture_from_page` previously wrote every value it
read off a form as an equally-trusted "confirmed answer", with no record of
whether the candidate typed it or the model guessed it, and it ran before
submission so it also learned from forms that were never sent. With
sensitive-field autofill enabled, that meant a hallucinated
work-authorization answer could be replayed, unattended, onto later
applications as though the candidate had confirmed it.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.learning import store
from jobbot.learning.provenance import Provenance, may_autofill_sensitive, rank, strongest
from jobbot.models import Base
from jobbot.submit.form_scan import FieldSpec


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine, future=True)()
    yield s
    s.close()


# --- the core rule ---------------------------------------------------------


def test_model_guess_may_never_autofill_a_sensitive_field():
    assert may_autofill_sensitive(Provenance.MODEL_GUESS.value, human_confirmed=False) is False


def test_human_entered_may_autofill_a_sensitive_field():
    assert may_autofill_sensitive(Provenance.HUMAN_ENTERED.value, human_confirmed=False) is True


def test_verified_may_autofill_a_sensitive_field():
    assert may_autofill_sensitive(Provenance.VERIFIED.value, human_confirmed=False) is True


def test_legacy_unverified_rows_may_not_autofill_sensitive_fields():
    """Rows written before provenance existed carry no evidence, so they
    get the conservative treatment rather than being grandfathered in."""
    assert may_autofill_sensitive(Provenance.UNVERIFIED.value, human_confirmed=False) is False


def test_explicit_human_confirmation_overrides_weak_provenance():
    assert may_autofill_sensitive(Provenance.MODEL_GUESS.value, human_confirmed=True) is True


def test_reuse_count_does_not_confer_trust(session):
    """Repetition is not correctness: a guess reused ten times is still a
    guess."""
    for _ in range(10):
        store.upsert(
            session, "Are you authorized to work in the US?", "radio", "Yes",
            sensitive=True, provenance=Provenance.MODEL_GUESS.value,
        )
        session.commit()

    row = store.find_match(session, "Are you authorized to work in the US?", "radio")
    assert row.times_used == 10
    assert may_autofill_sensitive(row.provenance, row.human_confirmed) is False


# --- ratcheting ------------------------------------------------------------


def test_trust_ranking_is_ordered():
    assert rank(Provenance.UNVERIFIED) < rank(Provenance.MODEL_GUESS)
    assert rank(Provenance.MODEL_GUESS) < rank(Provenance.HUMAN_ENTERED)
    assert rank(Provenance.HUMAN_ENTERED) < rank(Provenance.VERIFIED)


def test_strongest_keeps_the_higher_trust():
    assert strongest(Provenance.MODEL_GUESS, Provenance.VERIFIED) == Provenance.VERIFIED.value
    assert strongest(Provenance.VERIFIED, Provenance.MODEL_GUESS) == Provenance.VERIFIED.value


def test_unknown_provenance_string_ranks_lowest():
    assert rank("nonsense") == 0


def test_reseeing_a_value_never_downgrades_trust(session):
    """A confirmed answer that later appears on an unverified form must
    not silently lose its confirmation."""
    store.upsert(session, "First name", "text", "Ada", sensitive=False,
                 provenance=Provenance.VERIFIED.value, verified=True)
    session.commit()

    store.upsert(session, "First name", "text", "Ada", sensitive=False,
                 provenance=Provenance.MODEL_GUESS.value)
    session.commit()

    row = store.find_match(session, "First name", "text")
    assert row.provenance == Provenance.VERIFIED.value


def test_verified_submission_count_increments_only_on_verified(session):
    store.upsert(session, "Phone", "tel", "555", sensitive=False,
                 provenance=Provenance.MODEL_GUESS.value)
    session.commit()
    row = store.find_match(session, "Phone", "tel")
    assert row.verified_submission_count == 0

    store.upsert(session, "Phone", "tel", "555", sensitive=False,
                 provenance=Provenance.VERIFIED.value, verified=True)
    session.commit()
    row = store.find_match(session, "Phone", "tel")
    assert row.verified_submission_count == 1
    assert row.last_validated_at is not None


# --- capture_from_page tagging --------------------------------------------


def test_capture_tags_model_filled_fields_as_guesses(session, monkeypatch):
    monkeypatch.setattr("jobbot.submit.values.read_field_value", lambda page, spec: "Yes")
    field = FieldSpec(field_id=7, label="Do you require sponsorship?", field_type="radio",
                      options=["Yes", "No"])

    store.capture_from_page(
        session, page=object(), fields=[field],
        verified_submission=False, model_filled_ids={7},
    )
    session.commit()

    row = store.find_match(session, "Do you require sponsorship?", "radio")
    assert row.provenance == Provenance.MODEL_GUESS.value
    assert row.human_confirmed is False
    assert may_autofill_sensitive(row.provenance, row.human_confirmed) is False


def test_capture_tags_human_filled_fields_as_human_entered(session, monkeypatch):
    monkeypatch.setattr("jobbot.submit.values.read_field_value", lambda page, spec: "No")
    field = FieldSpec(field_id=7, label="Do you require sponsorship?", field_type="radio",
                      options=["Yes", "No"])

    store.capture_from_page(
        session, page=object(), fields=[field],
        verified_submission=False, model_filled_ids=set(),  # the human typed it
    )
    session.commit()

    row = store.find_match(session, "Do you require sponsorship?", "radio")
    assert row.provenance == Provenance.HUMAN_ENTERED.value
    assert may_autofill_sensitive(row.provenance, row.human_confirmed) is True


def test_verified_submission_promotes_even_model_filled_values(session, monkeypatch):
    """Once an application is positively verified as submitted, the values
    on it were actually accepted — that is real evidence."""
    monkeypatch.setattr("jobbot.submit.values.read_field_value", lambda page, spec: "Ada")
    field = FieldSpec(field_id=1, label="First name", field_type="text")

    store.capture_from_page(
        session, page=object(), fields=[field],
        verified_submission=True, model_filled_ids={1},
    )
    session.commit()

    row = store.find_match(session, "First name", "text")
    assert row.provenance == Provenance.VERIFIED.value
    assert row.verified_submission_count == 1

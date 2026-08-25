import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.learning import store
from jobbot.models import Base
from jobbot.submit.form_scan import FieldSpec


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, future=True)
    s = Session()
    yield s
    s.close()


def test_eligible_for_learning_rejects_file_and_empty():
    assert not store.eligible_for_learning("file", "resume.pdf")
    assert not store.eligible_for_learning("text", "")
    assert not store.eligible_for_learning("text", "   ")


def test_eligible_for_learning_rejects_long_textarea():
    long_text = "x" * 300
    assert not store.eligible_for_learning("textarea", long_text)
    assert store.eligible_for_learning("textarea", "x" * 100)


def test_upsert_then_exact_match(session):
    store.upsert(session, "First name", "text", "Jane", sensitive=False)
    session.commit()

    match = store.find_match(session, "First name", "text")
    assert match is not None
    assert match.value == "Jane"
    assert match.times_used == 1


def test_upsert_twice_increments_usage_and_updates_value(session):
    store.upsert(session, "First name", "text", "Jane", sensitive=False)
    session.commit()
    store.upsert(session, "First Name", "text", "Janet", sensitive=False)  # different case, same question
    session.commit()

    match = store.find_match(session, "first name", "text")
    assert match.value == "Janet"
    assert match.times_used == 2


def test_fuzzy_match_catches_reworded_question(session):
    store.upsert(
        session,
        "Are you legally authorized to work in the United States?",
        "radio",
        "Yes",
        sensitive=True,
    )
    session.commit()

    match = store.find_match(session, "Are you authorized to work in the US?", "radio")
    assert match is not None
    assert match.value == "Yes"
    assert match.sensitive is True


def test_no_match_for_unrelated_question(session):
    store.upsert(session, "First name", "text", "Jane", sensitive=False)
    session.commit()

    match = store.find_match(session, "Desired salary", "text")
    assert match is None


def test_match_fields_only_matches_known_fields(session):
    store.upsert(session, "First name", "text", "Jane", sensitive=False)
    session.commit()

    fields = [
        FieldSpec(field_id=0, label="First name", field_type="text"),
        FieldSpec(field_id=1, label="Cover letter", field_type="textarea"),
        FieldSpec(field_id=2, label="Resume", field_type="file"),
    ]
    matches = store.match_fields(session, fields)
    assert set(matches.keys()) == {0}
    assert matches[0].value == "Jane"


# --- value_still_offerable: guards against reusing a learned select/radio
# value when this posting's option list doesn't actually contain it --------


def test_value_still_offerable_true_when_option_present():
    field = FieldSpec(field_id=0, label="Work auth", field_type="radio", options=["Yes", "No"])
    assert store.value_still_offerable(field, "Yes")


def test_value_still_offerable_false_when_option_missing():
    field = FieldSpec(field_id=0, label="Visa type", field_type="select", options=["H1B", "L1", "None"])
    assert not store.value_still_offerable(field, "OPT")  # this employer's form doesn't offer it


def test_value_still_offerable_case_insensitive():
    field = FieldSpec(field_id=0, label="Work auth", field_type="radio", options=["Yes", "No"])
    assert store.value_still_offerable(field, "yes")


def test_value_still_offerable_always_true_for_free_text_types():
    field = FieldSpec(field_id=0, label="Email", field_type="text", options=[])
    assert store.value_still_offerable(field, "anything at all")


# --- circuit breaker: a question that keeps failing to auto-fill stops
# being retried automatically after CIRCUIT_BREAKER_THRESHOLD failures ------


def test_circuit_not_broken_before_threshold(session):
    store.record_failure(session, "School combobox", "option not found")
    session.commit()
    assert not store.is_circuit_broken(session, "School combobox")


def test_circuit_breaks_at_threshold(session):
    for _ in range(store.CIRCUIT_BREAKER_THRESHOLD):
        store.record_failure(session, "School combobox", "option not found")
        session.commit()
    assert store.is_circuit_broken(session, "School combobox")


def test_circuit_breaker_matches_reworded_question(session):
    for _ in range(store.CIRCUIT_BREAKER_THRESHOLD):
        store.record_failure(session, "Are you willing to relocate?", "option not found")
        session.commit()
    assert store.is_circuit_broken(session, "Are you willing to relocate for this role?")


def test_clear_failure_resets_circuit_breaker(session):
    for _ in range(store.CIRCUIT_BREAKER_THRESHOLD):
        store.record_failure(session, "School combobox", "option not found")
        session.commit()
    assert store.is_circuit_broken(session, "School combobox")

    store.clear_failure(session, "School combobox")
    session.commit()
    assert not store.is_circuit_broken(session, "School combobox")


def test_capture_from_page_clears_failure_for_successfully_filled_field(session, monkeypatch):
    for _ in range(store.CIRCUIT_BREAKER_THRESHOLD):
        store.record_failure(session, "First name", "fill did not verify after retry")
        session.commit()
    assert store.is_circuit_broken(session, "First name")

    field = FieldSpec(field_id=0, label="First name", field_type="text")
    monkeypatch.setattr("jobbot.submit.values.read_field_value", lambda page, spec: "Ada")

    count = store.capture_from_page(session, page=object(), fields=[field])
    session.commit()

    assert count == 1
    assert not store.is_circuit_broken(session, "First name")

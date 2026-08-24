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

from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from jobbot.models import Base
from jobbot.resume import multi
from jobbot.resume.multi import tag_from_filename
from jobbot.resume.schema import Profile


def test_tag_from_filename_lowercases_and_hyphenates():
    assert tag_from_filename(Path("Python_Developer.pdf")) == "python-developer"


def test_tag_from_filename_handles_spaces():
    assert tag_from_filename(Path("AI Engineer.docx")) == "ai-engineer"


def test_tag_from_filename_already_hyphenated():
    assert tag_from_filename(Path("full-stack.pdf")) == "full-stack"


@pytest.fixture
def isolated_db(monkeypatch):
    """Points jobbot.resume.multi's session_scope at a throwaway in-memory
    DB instead of the real one, so import_folder can be tested without
    touching any real state or requiring an ANTHROPIC_API_KEY (parse_resume
    is mocked separately per test)."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def fake_session_scope():
        session = Session()
        try:
            yield session
            session.commit()
        finally:
            session.close()

    monkeypatch.setattr(multi, "session_scope", fake_session_scope)
    yield


def _fake_profile(name: str) -> Profile:
    return Profile(name=name, email=f"{name.lower()}@example.com")


def test_import_folder_parses_every_resume_and_tags_by_filename(tmp_path, isolated_db, monkeypatch):
    (tmp_path / "python-developer.pdf").write_bytes(b"dummy")
    (tmp_path / "ai_engineer.pdf").write_bytes(b"dummy")
    (tmp_path / "notes.txt").write_text("not a resume, but a valid extension")
    (tmp_path / "ignore-me.png").write_bytes(b"not a resume format")

    monkeypatch.setattr(multi, "parse_resume", lambda path: _fake_profile(path.stem))

    tags = multi.import_folder(tmp_path)

    assert set(tags) == {"python-developer", "ai-engineer", "notes"}
    assert len(multi.list_profiles()) == 3

    row = multi.get_profile("python-developer")
    assert row is not None
    assert row.profile_json["name"] == "python-developer"
    assert Path(row.resume_path).name == "python-developer.pdf"


def test_reimporting_same_filename_updates_in_place(tmp_path, isolated_db, monkeypatch):
    resume = tmp_path / "python-developer.pdf"
    resume.write_bytes(b"v1")
    monkeypatch.setattr(multi, "parse_resume", lambda path: _fake_profile("v1"))
    multi.import_folder(tmp_path)

    monkeypatch.setattr(multi, "parse_resume", lambda path: _fake_profile("v2"))
    multi.import_folder(tmp_path)

    assert len(multi.list_profiles()) == 1  # updated, not duplicated
    row = multi.get_profile("python-developer")
    assert row.profile_json["name"] == "v2"


def test_get_profile_returns_none_for_unknown_tag(isolated_db):
    assert multi.get_profile("does-not-exist") is None


def test_one_resume_failing_to_parse_does_not_abort_the_rest(tmp_path, isolated_db, monkeypatch):
    """Real bug hit live: one unusually dense resume exhausted llm.py's
    retries with a malformed/empty tool-call generation, and the raised
    exception aborted the whole import-folder run — every resume after it
    in sorted order was silently never attempted."""
    for name in ("a-first", "b-fails", "c-third"):
        (tmp_path / f"{name}.pdf").write_bytes(b"dummy")

    def flaky_parse(path):
        if path.stem == "b-fails":
            raise RuntimeError("tool_use_failed even after retries")
        return _fake_profile(path.stem)

    monkeypatch.setattr(multi, "parse_resume", flaky_parse)

    tags = multi.import_folder(tmp_path)

    assert set(tags) == {"a-first", "c-third"}  # b-fails skipped, not fatal
    assert multi.get_profile("a-first") is not None
    assert multi.get_profile("c-third") is not None
    assert multi.get_profile("b-fails") is None


def test_daily_quota_exhaustion_stops_the_whole_import_instead_of_wasting_calls(tmp_path, isolated_db, monkeypatch):
    from jobbot.llm import DailyQuotaExceeded

    for name in ("a-first", "b-fails", "c-third"):
        (tmp_path / f"{name}.pdf").write_bytes(b"dummy")

    calls = {"n": 0}

    def always_quota(path):
        calls["n"] += 1
        raise DailyQuotaExceeded("daily quota exhausted for today")

    monkeypatch.setattr(multi, "parse_resume", always_quota)

    with pytest.raises(DailyQuotaExceeded):
        multi.import_folder(tmp_path)

    assert calls["n"] == 1  # stopped immediately on the first resume, didn't burn through the rest

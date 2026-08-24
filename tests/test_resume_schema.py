from jobbot.resume.schema import Profile


def test_sensitive_fields_default_to_none():
    profile = Profile(name="Jane Doe", email="jane@example.com")
    assert profile.work_authorization is None
    assert profile.veteran_status is None
    assert profile.disability_status is None
    assert profile.gender is None
    assert profile.race_ethnicity is None


def test_facts_json_excludes_none_fields():
    profile = Profile(name="Jane Doe", email="jane@example.com")
    dumped = profile.facts_json_for_llm()
    assert "work_authorization" not in dumped
    assert "veteran_status" not in dumped
    assert "Jane Doe" in dumped


def test_facts_json_includes_set_fields():
    profile = Profile(name="Jane Doe", email="jane@example.com", work_authorization="US Citizen")
    dumped = profile.facts_json_for_llm()
    assert "US Citizen" in dumped

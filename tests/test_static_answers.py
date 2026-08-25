"""static_answers.py resolves the handful of near-universal fields (name,
email, phone, links, current job, school, ...) straight from the profile
with no LLM call — see the module docstring for why (Simplify.jobs and
similar tools do the same thing). These lock in what it does and, just as
importantly, what it deliberately leaves alone.
"""
from jobbot.resume.schema import Education, Experience, Links, Profile
from jobbot.submit.form_scan import FieldSpec
from jobbot.submit.static_answers import resolve_static_fields

FULL_PROFILE = Profile(
    name="Ada Lovelace",
    email="ada@example.com",
    phone="+1-555-0100",
    location="Bangalore, India",
    links=Links(linkedin="https://linkedin.com/in/ada", github="https://github.com/ada", portfolio="https://ada.dev"),
    experience=[Experience(company="Analytical Engines Inc", title="Senior Engineer")],
    education=[Education(school="Trinity College", degree="B.Sc. Mathematics")],
    salary_expectation_usd=150000,
    willing_to_relocate=True,
)

EMPTY_PROFILE = Profile(name="Ada Lovelace", email="ada@example.com")


def _field(field_id: int, label: str, field_type: str = "text") -> FieldSpec:
    return FieldSpec(field_id=field_id, label=label, field_type=field_type)


def test_resolves_every_covered_field_from_a_full_profile():
    fields = [
        _field(1, "First Name*"),
        _field(2, "Last Name*"),
        _field(3, "Email*"),
        _field(4, "Phone Number"),
        _field(5, "LinkedIn URL"),
        _field(6, "GitHub"),
        _field(7, "Portfolio / Personal Website"),
        _field(8, "Current Company"),
        _field(9, "Current Job Title"),
        _field(10, "University"),
        _field(11, "Degree"),
        _field(12, "Desired Salary"),
        _field(13, "Are you willing to relocate?", "radio"),
        _field(14, "Current Location"),
    ]
    plan = resolve_static_fields(FULL_PROFILE, fields)

    assert plan[1]["value"] == "Ada"
    assert plan[2]["value"] == "Lovelace"
    assert plan[3]["value"] == "ada@example.com"
    assert plan[4]["value"] == "+1-555-0100"
    assert plan[5]["value"] == "https://linkedin.com/in/ada"
    assert plan[6]["value"] == "https://github.com/ada"
    assert plan[7]["value"] == "https://ada.dev"
    assert plan[8]["value"] == "Analytical Engines Inc"
    assert plan[9]["value"] == "Senior Engineer"
    assert plan[10]["value"] == "Trinity College"
    assert plan[11]["value"] == "B.Sc. Mathematics"
    assert plan[12]["value"] == "150000"
    assert plan[13]["value"] == "Yes"
    assert plan[14]["value"] == "Bangalore, India"
    for fid in plan:
        assert plan[fid]["needs_human"] is False


def test_full_name_field_uses_whole_name_not_just_first_token():
    plan = resolve_static_fields(FULL_PROFILE, [_field(1, "Full Name")])
    assert plan[1]["value"] == "Ada Lovelace"


def test_fields_with_no_profile_data_are_left_unresolved():
    """A field the taxonomy covers but the profile has no answer for (e.g.
    no GitHub link) must be absent from the result, not filled with an
    empty string — the caller falls back to the LLM/human for it."""
    fields = [_field(1, "GitHub"), _field(2, "Portfolio"), _field(3, "Desired Salary")]
    plan = resolve_static_fields(EMPTY_PROFILE, fields)
    assert plan == {}


def test_uncovered_freeform_question_is_left_for_the_llm():
    fields = [_field(1, "Why do you want to work here?", "textarea")]
    assert resolve_static_fields(FULL_PROFILE, fields) == {}


def test_sensitive_fields_are_never_resolved_here_even_if_a_pattern_would_match():
    """Defense in depth: fill_planner.is_sensitive() is checked again here
    independently of the taxonomy, so a mislabeled or unusually-worded
    sensitive question can never slip through this fast path."""
    fields = [
        _field(1, "Are you authorized to work in the United States?", "radio"),
        _field(2, "Will you require visa sponsorship?", "radio"),
        _field(3, "Gender", "select"),
    ]
    assert resolve_static_fields(FULL_PROFILE, fields) == {}


def test_file_fields_are_skipped():
    plan = resolve_static_fields(FULL_PROFILE, [_field(1, "Resume/CV", "file")])
    assert plan == {}


def test_last_name_absent_for_single_word_name():
    single = Profile(name="Cher", email="cher@example.com")
    plan = resolve_static_fields(single, [_field(1, "Last Name")])
    assert plan == {}

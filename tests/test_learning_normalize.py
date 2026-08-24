from jobbot.learning.normalize import normalize_label


def test_case_and_whitespace_insensitive():
    assert normalize_label("First   Name") == normalize_label("first name")


def test_strips_trailing_required_marker():
    assert normalize_label("Phone Number *") == normalize_label("Phone Number")


def test_strips_punctuation():
    assert normalize_label("Are you authorized to work in the US?") == normalize_label(
        "Are you authorized to work in the US"
    )


def test_different_questions_stay_different():
    assert normalize_label("First name") != normalize_label("Last name")

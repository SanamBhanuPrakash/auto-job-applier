from jobbot.utils.textclean import strip_html


def test_strips_real_greenhouse_style_markup():
    # Confirmed live: this is what Greenhouse's `content` field actually
    # looks like — see jobbot/discovery/greenhouse.py.
    raw = '&lt;div class=&quot;content-intro&quot;&gt;&lt;p&gt;&lt;strong&gt;Who we are&amp;nbsp; &lt;/strong&gt;&lt;/p&gt;'
    cleaned = strip_html(raw)
    assert "<div" not in cleaned
    assert "&lt;" not in cleaned
    assert "&quot;" not in cleaned
    assert "Who we are" in cleaned


def test_strips_plain_html_tags():
    assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"


def test_decodes_html_entities():
    assert strip_html("Tom &amp; Jerry") == "Tom & Jerry"


def test_empty_string_stays_empty():
    assert strip_html("") == ""
    assert strip_html(None) == ""


def test_already_plain_text_is_unchanged():
    text = "We are looking for a backend engineer with Python experience."
    assert strip_html(text) == text


def test_collapses_excess_whitespace_from_stripped_tags():
    raw = "<div>Line one</div><div>Line two</div>"
    cleaned = strip_html(raw)
    assert cleaned == "Line one Line two"

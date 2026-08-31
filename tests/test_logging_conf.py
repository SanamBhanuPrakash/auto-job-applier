"""Real bug hit live importing a dense resume on Windows: logging a message
containing an en-dash/non-breaking-hyphen (common in scraped job postings
and resume text) crashed inside logging's own error handler, because the
default console stream on Windows (cp1252 or similar) can't encode it.
Not fatal to the caller — logging.Handler.handleError() swallows it — but
it dumps a multi-page traceback to the console on every single occurrence.
setup_logging() now reconfigures stdout/stderr to replace instead of raise.
"""
from __future__ import annotations

import io
import logging

import pytest

from jobbot.logging_conf import _make_console_unicode_safe

_UNREPRESENTABLE = "worker‑null"  # U+2011 non-breaking hyphen: genuinely absent from cp1252


def _cp1252_stream() -> io.TextIOWrapper:
    return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict", write_through=True)


def test_unencodable_character_crashes_a_strict_cp1252_stream():
    """Sanity check that the fixture actually reproduces the real failure
    mode before testing the fix against it."""
    stream = _cp1252_stream()
    with pytest.raises(UnicodeEncodeError):
        stream.write(_UNREPRESENTABLE)


def test_make_console_unicode_safe_prevents_the_crash(monkeypatch):
    out, err = _cp1252_stream(), _cp1252_stream()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)

    _make_console_unicode_safe()

    out.write(_UNREPRESENTABLE)  # must not raise
    err.write(_UNREPRESENTABLE)  # must not raise


def test_make_console_unicode_safe_tolerates_a_stream_without_reconfigure(monkeypatch):
    """Some stream stand-ins (captured test output, certain redirections)
    don't implement reconfigure() at all — must be a no-op, not a crash."""
    class NoReconfigure:
        pass

    monkeypatch.setattr("sys.stdout", NoReconfigure())
    monkeypatch.setattr("sys.stderr", NoReconfigure())

    _make_console_unicode_safe()  # must not raise


def test_logging_a_problem_string_does_not_raise_after_setup(monkeypatch, tmp_path):
    """End-to-end: the exact failure shape from the live crash — a log
    call whose formatted message contains a character the console stream
    can't natively encode — must not propagate out of logging.warning."""
    from jobbot import logging_conf

    monkeypatch.setattr(logging_conf, "get_settings", lambda: type("S", (), {"data_dir": tmp_path})())
    stream = _cp1252_stream()
    monkeypatch.setattr("sys.stderr", stream)

    logging_conf.setup_logging()
    log = logging.getLogger("test_logging_conf")
    log.warning("resume text contained: %s", _UNREPRESENTABLE)  # must not raise

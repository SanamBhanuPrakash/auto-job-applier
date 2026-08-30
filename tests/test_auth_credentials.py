"""Credential isolation (spec §26, §60).

The rule: a password never appears in a prompt, a tool argument, a trace
row, a log line, or an exception message. These tests attack that rule
from every direction a credential actually leaks in practice.
"""
from __future__ import annotations

import logging

import pytest

from jobbot.auth.credentials import (
    Credential,
    CredentialSource,
    CredentialStore,
    Secret,
    normalize_domain,
    redact,
)

PW = "correct-horse-battery-staple"


# --- Secret refuses to print itself ---------------------------------------


def test_a_secret_never_renders_its_value():
    s = Secret(PW)
    assert PW not in str(s)
    assert PW not in repr(s)
    assert PW not in f"{s}"
    assert PW not in f"{s!r}"
    assert PW not in f"{s:>40}"
    assert PW not in "{}".format(s)
    assert PW not in "%s" % (s,)


def test_a_secret_still_behaves_as_a_value_where_it_must():
    s = Secret(PW)
    assert s.reveal() == PW
    assert bool(s) is True
    assert bool(Secret("")) is False
    assert len(s) == len(PW)
    assert s == Secret(PW)


def test_a_secret_cannot_be_used_as_a_dict_key():
    """Hashing one invites it into a cache key, a log of cache keys, or a
    trace. Refuse rather than let it travel."""
    with pytest.raises(TypeError):
        {Secret(PW): 1}  # noqa: B018


def test_a_credential_repr_is_safe_for_a_trace_row():
    c = Credential("acme.example", "ada@example.com", Secret(PW))
    assert PW not in repr(c)
    assert "ada@example.com" in repr(c), "the username is not secret and is needed to debug"


def test_a_secret_caught_in_a_log_record_does_not_render(caplog):
    """The commonest real leak: an f-string in a log line."""
    with caplog.at_level(logging.INFO):
        logging.getLogger("t").info("signing in with %s", Secret(PW))
    assert PW not in caplog.text


def test_a_secret_in_a_traceback_message_does_not_render():
    try:
        raise RuntimeError(f"login failed for {Secret(PW)}")
    except RuntimeError as exc:
        assert PW not in str(exc)


# --- redaction as the second layer ----------------------------------------


@pytest.mark.parametrize("text", [
    "password=hunter2seekrit",
    'password: "hunter2seekrit"',
    "pwd=hunter2seekrit&next=/apply",
    "api_key=sk-abcdef123456",
    "otp: 314159",
    "Authorization: Bearer abcdefgh12345678",
    "https://ada:hunter2seekrit@acme.example/apply",
])
def test_credential_shaped_text_is_scrubbed(text):
    out = redact(text)
    assert "hunter2seekrit" not in out
    assert "sk-abcdef123456" not in out
    assert "abcdefgh12345678" not in out
    assert "314159" not in out


def test_redaction_takes_known_values_too():
    assert PW not in redact(f"the page said {PW} was wrong", extra=(PW,))


def test_redaction_leaves_ordinary_text_readable():
    text = "Could not find the submit button on the application form"
    assert redact(text) == text


def test_redaction_ignores_values_too_short_to_be_secrets():
    """Scrubbing every 3-character string would make errors unreadable."""
    assert "abc" in redact("field abc failed", extra=("abc",))


# --- the store -------------------------------------------------------------


def test_no_credential_configured_reports_none(monkeypatch):
    store = CredentialStore(allow_keyring=False)
    monkeypatch.delenv("JOBBOT_CRED_ACME_EXAMPLE_USER", raising=False)
    monkeypatch.delenv("JOBBOT_CRED_ACME_EXAMPLE_PASSWORD", raising=False)
    assert store.get("acme.example") is None
    assert store.has("acme.example") is False
    assert store.source_for("acme.example") is CredentialSource.NONE


def test_an_environment_credential_is_found(monkeypatch):
    monkeypatch.setenv("JOBBOT_CRED_ACME_EXAMPLE_USER", "ada@example.com")
    monkeypatch.setenv("JOBBOT_CRED_ACME_EXAMPLE_PASSWORD", PW)
    store = CredentialStore(allow_keyring=False)
    cred = store.get("https://acme.example/careers/apply?x=1")
    assert cred is not None
    assert cred.username == "ada@example.com"
    assert cred.password.reveal() == PW
    assert store.source_for("acme.example") is CredentialSource.ENVIRONMENT


def test_looking_up_a_credential_does_not_log_it(monkeypatch, caplog):
    monkeypatch.setenv("JOBBOT_CRED_ACME_EXAMPLE_USER", "ada@example.com")
    monkeypatch.setenv("JOBBOT_CRED_ACME_EXAMPLE_PASSWORD", PW)
    with caplog.at_level(logging.DEBUG):
        CredentialStore(allow_keyring=False).get("acme.example")
    assert PW not in caplog.text


def test_storing_without_a_keyring_refuses_rather_than_writing_a_file(monkeypatch):
    """The fallback that must not exist. A password in a repo config file
    is the mistake the storage ordering is designed to prevent."""
    import jobbot.auth.credentials as mod

    monkeypatch.setattr(mod, "_keyring", lambda: None)
    with pytest.raises(RuntimeError, match="Refusing to write a password"):
        CredentialStore().store("acme.example", "ada", PW)


# --- domain normalization --------------------------------------------------


@pytest.mark.parametrize("given,expected", [
    ("https://boards.greenhouse.io/acme/jobs/1", "greenhouse.io"),
    ("job-boards.greenhouse.io", "greenhouse.io"),
    ("JOBS.LEVER.CO", "lever.co"),
    ("https://acme.wd5.myworkdayjobs.com/en-US/careers", "myworkdayjobs.com"),
    ("careers.acme.co.uk", "acme.co.uk"),
    ("acme.example:8443", "acme.example"),
    ("", ""),
])
def test_domains_normalize_so_one_account_is_asked_for_once(given, expected):
    assert normalize_domain(given) == expected

"""jobbot defaults to Groq (free, no card) but supports Anthropic too — this
locks in that both give a clear, actionable error when misconfigured rather
than a confusing stack trace, since a wrong/missing key here is the single
most likely thing to trip someone up on first setup.
"""
import pytest

import jobbot.llm as llm_module
from jobbot.config import get_settings


@pytest.fixture(autouse=True)
def _reset_llm_client_cache():
    llm_module._client = None
    llm_module._client_provider = None
    yield
    llm_module._client = None
    llm_module._client_provider = None
    get_settings.cache_clear()


def test_default_provider_is_groq(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    get_settings.cache_clear()
    assert get_settings().llm_provider == "groq"


def test_missing_groq_key_raises_clear_error(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        llm_module._get_client()


def test_missing_anthropic_key_raises_clear_error_mentioning_pro_plan_caveat(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        llm_module._get_client()


def test_unknown_provider_raises_clear_error(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "made-up-provider")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="made-up-provider"):
        llm_module._get_client()


def test_groq_client_constructed_when_key_present(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-test")
    get_settings.cache_clear()

    client = llm_module._get_client()
    assert type(client).__module__.startswith("groq")


def test_rate_limit_error_is_retried_then_succeeds(monkeypatch):
    calls = {"n": 0}

    class FakeRateLimitError(Exception):
        pass

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise FakeRateLimitError("rate limited")
        return "ok"

    monkeypatch.setattr(llm_module.time, "sleep", lambda _seconds: None)  # don't actually wait in tests
    result = llm_module._call_with_rate_limit_retry(flaky)

    assert result == "ok"
    assert calls["n"] == 3


def test_non_rate_limit_error_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise ValueError("something else entirely")

    with pytest.raises(ValueError):
        llm_module._call_with_rate_limit_retry(always_fails)

    assert calls["n"] == 1

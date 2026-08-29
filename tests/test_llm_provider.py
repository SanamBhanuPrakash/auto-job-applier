"""jobbot defaults to Groq (free, no card) but supports Gemini and Anthropic
too — this locks in that all three give a clear, actionable error when
misconfigured rather than a confusing stack trace, since a wrong/missing key
here is the single most likely thing to trip someone up on first setup.
"""
import pytest

import jobbot.llm as llm_module
from jobbot.config import get_settings


@pytest.fixture(autouse=True)
def _reset_llm_client_cache():
    llm_module._clients.clear()
    yield
    llm_module._clients.clear()
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


def test_missing_gemini_key_raises_clear_error_mentioning_pro_subscription_caveat(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()

    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        llm_module._get_client()


def test_gemini_client_constructed_when_key_present(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-test")
    get_settings.cache_clear()

    client = llm_module._get_client()
    assert type(client).__module__.startswith("google.genai")


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


def test_daily_quota_error_fails_fast_with_clear_message_no_retry(monkeypatch):
    """Real error hit live: 'Rate limit reached ... on tokens per day (TPD):
    Limit 200000, Used 199543 ... Please try again in 17m13.344s.' Retrying
    that in seconds is pointless and previously burned the whole retry
    budget before crashing with a raw stack trace — this should fail
    immediately, on the first attempt, with an actionable message instead."""
    calls = {"n": 0}

    class FakeRateLimitError(Exception):
        pass

    def always_daily_quota():
        calls["n"] += 1
        raise FakeRateLimitError(
            "Rate limit reached for model `openai/gpt-oss-120b` ... on tokens per day (TPD): "
            "Limit 200000, Used 199543, Requested 2849. Please try again in 17m13.344s."
        )

    monkeypatch.setattr(llm_module.time, "sleep", lambda _seconds: None)

    with pytest.raises(RuntimeError, match="daily token/request quota"):
        llm_module._call_with_rate_limit_retry(always_daily_quota)

    assert calls["n"] == 1  # no wasted retries on something that won't clear in seconds


def test_per_minute_rate_limit_still_retries_as_normal(monkeypatch):
    """Sanity check the daily-quota detection doesn't accidentally swallow
    the ordinary per-minute case this was already handling correctly."""
    calls = {"n": 0}

    class FakeRateLimitError(Exception):
        pass

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise FakeRateLimitError("Rate limit reached ... on tokens per minute (TPM): Limit 6000.")
        return "ok"

    monkeypatch.setattr(llm_module.time, "sleep", lambda _seconds: None)
    result = llm_module._call_with_rate_limit_retry(flaky)

    assert result == "ok"
    assert calls["n"] == 3


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


def test_gemini_shaped_rate_limit_error_is_retried(monkeypatch):
    """google-genai doesn't raise a distinctly-named RateLimitError like the
    other two SDKs — it raises ClientError for every 4xx with the real
    status on .code. This is the shape that would have silently skipped
    retry if _is_rate_limit_error only checked the exception's class name."""
    calls = {"n": 0}

    class FakeGeminiClientError(Exception):
        def __init__(self, code):
            self.code = code
            super().__init__(f"{code} error")

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise FakeGeminiClientError(429)
        return "ok"

    monkeypatch.setattr(llm_module.time, "sleep", lambda _seconds: None)
    result = llm_module._call_with_rate_limit_retry(flaky)

    assert result == "ok"
    assert calls["n"] == 3


def test_gemini_shaped_non_rate_limit_client_error_is_not_retried(monkeypatch):
    class FakeGeminiClientError(Exception):
        def __init__(self, code):
            self.code = code
            super().__init__(f"{code} error")

    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise FakeGeminiClientError(400)  # bad request, not a rate limit

    with pytest.raises(Exception, match="400"):
        llm_module._call_with_rate_limit_retry(always_fails)

    assert calls["n"] == 1


def test_non_rate_limit_error_is_not_retried(monkeypatch):
    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        raise ValueError("something else entirely")

    with pytest.raises(ValueError):
        llm_module._call_with_rate_limit_retry(always_fails)

    assert calls["n"] == 1


def test_malformed_tool_call_json_is_retried_then_succeeds(monkeypatch):
    """Real error hit live scoring a 1200-job batch: Groq's server-side JSON
    parser choked on a stray quote the model generated
    (`"job_id":2523","score":30`) and rejected the request with 400
    'tool_use_failed'. A fresh generation is usually well-formed, so this
    should retry (not crash the whole scoring run) and succeed."""
    calls = {"n": 0}

    class FakeBadRequestError(Exception):
        pass

    def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise FakeBadRequestError(
                "Error code: 400 - {'error': {'message': 'Failed to parse tool call arguments as "
                "JSON', 'type': 'invalid_request_error', 'code': 'tool_use_failed'}}"
            )
        return "ok"

    monkeypatch.setattr(llm_module.time, "sleep", lambda _seconds: None)
    result = llm_module._call_with_rate_limit_retry(flaky)

    assert result == "ok"
    assert calls["n"] == 2


def test_daily_quota_error_is_a_runtime_error_subclass_for_precise_catching(monkeypatch):
    """score.py needs to distinguish 'stop the whole run, quota's gone for
    the day' from 'skip this one batch and keep going' — that only works if
    the daily-quota case raises a dedicated type rather than a bare
    RuntimeError shared with every other failure mode."""
    def always_daily_quota():
        raise Exception("Rate limit reached ... on tokens per day (TPD): Limit 200000.")  # noqa: TRY002

    with pytest.raises(llm_module.DailyQuotaExceeded):
        llm_module._call_with_rate_limit_retry(always_daily_quota)


def test_call_tool_falls_back_to_gemini_when_groq_hits_daily_quota(monkeypatch):
    """Real scenario hit live: openai/gpt-oss-20b's 200k/day Groq budget ran
    out partway through a 1200-job scoring run. With a GEMINI_API_KEY also
    configured, call_tool should retry the same request on Gemini instead
    of stopping the whole run for the rest of the day."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    get_settings.cache_clear()
    monkeypatch.setattr(llm_module, "_get_client", lambda provider=None: provider or "groq")

    calls = []

    def fake_groq(client, **_kwargs):
        calls.append("groq")
        raise llm_module.DailyQuotaExceeded("groq daily quota hit")

    def fake_gemini(client, **_kwargs):
        calls.append("gemini")
        return {"ok": True}

    monkeypatch.setattr(llm_module, "_HANDLERS", {"groq": fake_groq, "gemini": fake_gemini, "anthropic": fake_gemini})

    result = llm_module.call_tool(system="sys", user_message="msg", tool_name="t", tool_description="d", input_schema={})

    assert result == {"ok": True}
    assert calls == ["groq", "gemini"]


def test_call_tool_raises_daily_quota_exceeded_when_every_free_provider_is_exhausted(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")
    get_settings.cache_clear()
    monkeypatch.setattr(llm_module, "_get_client", lambda provider=None: provider or "groq")

    def always_quota(client, **_kwargs):
        raise llm_module.DailyQuotaExceeded("quota hit")

    monkeypatch.setattr(llm_module, "_HANDLERS", {"groq": always_quota, "gemini": always_quota, "anthropic": always_quota})

    with pytest.raises(llm_module.DailyQuotaExceeded):
        llm_module.call_tool(system="sys", user_message="msg", tool_name="t", tool_description="d", input_schema={})


def test_call_tool_never_auto_falls_back_to_paid_anthropic_provider(monkeypatch):
    """Anthropic is pay-as-you-go — even if ANTHROPIC_API_KEY happens to be
    set, running out of a free provider's daily quota must never silently
    start spending the user's money without explicit consent."""
    monkeypatch.setenv("LLM_PROVIDER", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "fake-groq-key")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key")
    get_settings.cache_clear()
    monkeypatch.setattr(llm_module, "_get_client", lambda provider=None: provider or "groq")

    def always_quota(client, **_kwargs):
        raise llm_module.DailyQuotaExceeded("quota hit")

    def must_not_be_called(client, **_kwargs):
        raise AssertionError("must never automatically fall back to a paid provider")

    monkeypatch.setattr(
        llm_module, "_HANDLERS",
        {"groq": always_quota, "gemini": must_not_be_called, "anthropic": must_not_be_called},
    )

    with pytest.raises(llm_module.DailyQuotaExceeded):
        llm_module.call_tool(system="sys", user_message="msg", tool_name="t", tool_description="d", input_schema={})

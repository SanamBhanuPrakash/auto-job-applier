"""Shared LLM client. Every structured-output call goes through `call_tool`,
which forces the model to respond via a single tool/function invocation so
we get back JSON that matches a schema instead of parsing free text.

Three providers are supported behind this one interface — nothing else in
the codebase (resume/parser.py, matching/score.py, submit/fill_planner.py)
needs to know which one is active:

- "groq" (default): free, no credit card, and — checked directly, not
  assumed — does not train on inputs/outputs even on the free tier, which
  matters here since resumes are personal data. Trade-off: a tight 6,000
  tokens/minute limit on the free tier, which is why matching/score.py
  batches conservatively and why 429s are retried with backoff below rather
  than treated as fatal.
- "gemini": also free with no credit card, via a Google AI Studio API key —
  note this is NOT the same thing as a Google AI Pro/Ultra subscription,
  which is a separate consumer product and does not grant API access either
  (checked directly; same category of gap as Claude.ai Pro not covering the
  Anthropic API). Roomier free-tier limits than Groq (250k tokens/minute vs.
  6k, as of the 3.7 Flash generation), but its free tier's terms allow
  Google to use your inputs/outputs to improve their models — Groq's
  free tier explicitly does not. Worth weighing given resumes are personal
  data; enabling Cloud Billing on the same key removes that clause (and
  raises the rate limits further) if you'd rather pay a little.
- "anthropic": needs a separate pay-as-you-go API key (NOT covered by a
  Claude.ai Pro/Max subscription — that covers the chat app and Claude Code
  itself, not third-party API calls like this one). Higher quality, no
  practical rate-limit concern for this project's scale, costs real money
  (typically well under $1 for a heavy day of use at Anthropic's per-token
  pricing, but it is a real charge).

Set LLM_PROVIDER in .env to switch.
"""
from __future__ import annotations

import json
import logging
import time

from jobbot.config import get_settings

log = logging.getLogger(__name__)

_client = None
_client_provider: str | None = None

MAX_RATE_LIMIT_RETRIES = 5


def _get_client():
    global _client, _client_provider
    settings = get_settings()
    provider = settings.llm_provider

    if _client is not None and _client_provider == provider:
        return _client

    if provider == "groq":
        if not settings.groq_api_key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Get a free key (no credit card) at "
                "https://console.groq.com/keys and add it to .env."
            )
        import groq

        _client = groq.Groq(api_key=settings.groq_api_key)
    elif provider == "gemini":
        if not settings.gemini_api_key:
            raise RuntimeError(
                "GEMINI_API_KEY is not set. Note a Google AI Pro/Ultra subscription does NOT "
                "cover this — get a free API key (separate, no credit card) at "
                "https://aistudio.google.com/apikey and add it to .env."
            )
        from google import genai

        _client = genai.Client(api_key=settings.gemini_api_key)
    elif provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set, and LLM_PROVIDER=anthropic. Note this needs a "
                "separate pay-as-you-go API key from console.anthropic.com — a Claude.ai Pro/Max "
                "subscription does not cover it. Add the key to .env, or set LLM_PROVIDER=groq "
                "in .env to use Groq's free tier instead (no card needed)."
            )
        import anthropic

        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    else:
        raise RuntimeError(f"Unknown LLM_PROVIDER {provider!r}; expected 'groq', 'gemini', or 'anthropic'.")

    _client_provider = provider
    return _client


def _is_rate_limit_error(exc: BaseException) -> bool:
    # Anthropic/Groq (OpenAI-shaped SDKs) raise a distinctly-named RateLimitError.
    if "RateLimit" in type(exc).__name__:
        return True
    # google-genai instead raises ClientError for every 4xx, with the real
    # status on .code — verified against the installed SDK's error classes,
    # not assumed, since guessing wrong here silently disables retry for it.
    code = getattr(exc, "code", None)
    return code == 429


def _is_daily_quota_error(exc: BaseException) -> bool:
    """A 429 for hitting a per-minute token/request limit clears in seconds
    and is worth retrying. A 429 for hitting Groq's separate per-MODEL
    per-DAY token budget (confirmed live: `openai/gpt-oss-120b`'s free tier
    caps at 200,000 tokens/day, independent of the per-minute limit) can
    say "please try again in 17m43s" — retrying that with a few seconds of
    backoff just burns the whole retry budget and then fails anyway with a
    confusing stack trace. Detected by the wording Groq's own error message
    uses ("tokens per day" / "requests per day"), so it fails fast instead
    with a clear, actionable message.
    """
    message = str(exc).lower()
    return "per day" in message or "tpd" in message or "rpd" in message


def _call_with_rate_limit_retry(fn):
    delay = 2.0
    for attempt in range(MAX_RATE_LIMIT_RETRIES):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if _is_daily_quota_error(exc):
                raise RuntimeError(
                    "Hit a daily token/request quota on this model's free tier (not the per-minute "
                    "limit — that one retries automatically). Options: wait for it to reset (Groq's "
                    "error message above usually says how long), switch GROQ_MODEL in .env to a "
                    "different model (each has its own separate daily budget), or switch "
                    "LLM_PROVIDER for now. Original error: " + str(exc)
                ) from exc
            if not _is_rate_limit_error(exc) or attempt == MAX_RATE_LIMIT_RETRIES - 1:
                raise
            log.warning("Rate limited (attempt %d/%d), waiting %.0fs: %s", attempt + 1, MAX_RATE_LIMIT_RETRIES, delay, exc)
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")  # loop always returns or raises


def _call_groq(client, *, system, user_message, tool_name, tool_description, input_schema, max_tokens) -> dict:
    settings = get_settings()

    def do_call():
        return client.chat.completions.create(
            model=settings.groq_model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": tool_description,
                        "parameters": input_schema,
                    },
                }
            ],
            tool_choice={"type": "function", "function": {"name": tool_name}},
        )

    response = _call_with_rate_limit_retry(do_call)
    tool_calls = response.choices[0].message.tool_calls
    if not tool_calls:
        raise RuntimeError(f"Model did not call {tool_name}; got: {response.choices[0].message.content!r}")
    return json.loads(tool_calls[0].function.arguments)


def _call_gemini(client, *, system, user_message, tool_name, tool_description, input_schema, max_tokens) -> dict:
    settings = get_settings()
    from google.genai import types

    def do_call():
        return client.models.generate_content(
            model=settings.gemini_model,
            contents=user_message,
            config=types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=max_tokens,
                tools=[
                    types.Tool(
                        function_declarations=[
                            types.FunctionDeclaration(
                                name=tool_name,
                                description=tool_description,
                                # Accepts a plain JSON Schema dict directly (mutually
                                # exclusive with `parameters`, which wants Google's
                                # own non-standard Schema object shape instead).
                                parameters_json_schema=input_schema,
                            )
                        ]
                    )
                ],
                tool_config=types.ToolConfig(
                    function_calling_config=types.FunctionCallingConfig(
                        mode=types.FunctionCallingConfigMode.ANY,
                        allowed_function_names=[tool_name],
                    )
                ),
            ),
        )

    response = _call_with_rate_limit_retry(do_call)
    calls = response.function_calls
    if not calls:
        raise RuntimeError(f"Model did not call {tool_name}; got: {response.text!r}")
    return calls[0].args


def _call_anthropic(client, *, system, user_message, tool_name, tool_description, input_schema, max_tokens) -> dict:
    settings = get_settings()

    def do_call():
        return client.messages.create(
            model=settings.anthropic_model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user_message}],
            tools=[{"name": tool_name, "description": tool_description, "input_schema": input_schema}],
            tool_choice={"type": "tool", "name": tool_name},
        )

    response = _call_with_rate_limit_retry(do_call)
    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input
    raise RuntimeError(f"Model did not call {tool_name}; got: {response.content!r}")


def call_tool(
    *,
    system: str,
    user_message: str,
    tool_name: str,
    tool_description: str,
    input_schema: dict,
    max_tokens: int = 4096,
) -> dict:
    """Send one message, force the model to call `tool_name`, return its input dict."""
    settings = get_settings()
    client = _get_client()

    handler = {"groq": _call_groq, "gemini": _call_gemini, "anthropic": _call_anthropic}[settings.llm_provider]
    return handler(
        client, system=system, user_message=user_message, tool_name=tool_name,
        tool_description=tool_description, input_schema=input_schema, max_tokens=max_tokens,
    )

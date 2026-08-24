"""Shared Anthropic client helper. Every structured-output call goes through
`call_tool`, which forces the model to respond via a single tool invocation
so we get back JSON that matches a schema instead of parsing free text.
"""
from __future__ import annotations

import json
import logging

import anthropic

from jobbot.config import get_settings

log = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.anthropic_api_key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set. Copy .env.example to .env and fill it in.")
        _client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    return _client


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
    client = get_client()

    response = client.messages.create(
        model=settings.anthropic_model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_message}],
        tools=[
            {
                "name": tool_name,
                "description": tool_description,
                "input_schema": input_schema,
            }
        ],
        tool_choice={"type": "tool", "name": tool_name},
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == tool_name:
            return block.input

    raise RuntimeError(f"Model did not call {tool_name}; got: {response.content!r}")

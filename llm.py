"""Thin, provider-agnostic wrapper over an OpenAI-compatible chat endpoint.

Everything here speaks the standard OpenAI chat-completions format
(``tools`` with ``type: function``, ``tool_calls``, ``role: tool``). No
provider-specific features are used, so any OpenAI-compatible gateway works.

Also hosts the shared JSON parser: models love to wrap JSON in markdown
fences or add a sentence of commentary, so every JSON path goes through
``parse_json_safe``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from openai import OpenAI

import config

_fence_re = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.DOTALL)


def client() -> OpenAI:
    """Build an OpenAI client pointed at the configured gateway."""
    return OpenAI(base_url=config.LLM_BASE_URL, api_key=config.LLM_API_KEY, timeout=60.0, max_retries=0)


def _first_balanced(text: str, open_ch: str, close_ch: str) -> str | None:
    """Return the first balanced ``{...}`` or ``[...]`` span, ignoring strings."""
    start = text.find(open_ch)
    if start == -1:
        return None
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_json_safe(text: str) -> Any:
    """Parse JSON from model output, tolerating fences and commentary.

    Raises ``ValueError`` (with the offending text) when nothing parses.
    """
    if text is None:
        raise ValueError("empty model output")
    raw = text.strip()

    # 1. Strip markdown fences if present.
    fence = _fence_re.search(raw)
    if fence:
        raw = fence.group(1).strip()

    # 2. Straight parse.
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    # 3. Pull the first balanced object, then array, out of surrounding prose.
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        span = _first_balanced(raw, open_ch, close_ch)
        if span:
            try:
                return json.loads(span)
            except json.JSONDecodeError:
                continue

    raise ValueError(f"could not parse JSON from model output:\n{text[:1000]}")


def complete(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    timeout: float = 60.0,
) -> Any:
    """One chat-completion round trip. Returns the assistant message object."""
    kwargs: dict[str, Any] = {
        "model": config.require_llm_model(),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "timeout": timeout,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    resp = client().chat.completions.create(**kwargs)
    return resp.choices[0].message


def chat_text(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.0,
    max_tokens: int = 2048,
) -> str:
    """Convenience: one call, return the text content only."""
    return complete(messages, temperature=temperature, max_tokens=max_tokens).content or ""


def chat_json(
    messages: list[dict[str, Any]],
    *,
    temperature: float = 0.0,
    max_tokens: int = 2048,
    retries: int = 1,
) -> Any:
    """Ask the model for JSON and parse it, retrying once on a parse failure.

    The retry grows the message history with a blunt correction request, which
    is enough for most stubborn models.
    """
    convo = list(messages)
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        text = chat_text(convo, temperature=temperature, max_tokens=max_tokens)
        try:
            return parse_json_safe(text)
        except ValueError as err:
            last_err = err
            convo = convo + [
                {"role": "assistant", "content": text},
                {
                    "role": "user",
                    "content": (
                        "That was not valid JSON. Reply with raw JSON only -- no "
                        "markdown fences, no commentary."
                    ),
                },
            ]
    raise ValueError(f"model did not return valid JSON after {retries + 1} attempts: {last_err}")

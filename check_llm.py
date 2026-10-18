"""Step 0: prove the LLM endpoint works before anything else is built.

Checks, in order:
  1. GET /v1/models lists models (and that LLM_MODEL is among them, if listed).
  2. A tiny tool-calling round trip returns a tool call.
  3. A strict-JSON round trip parses through the shared parser.

Run:  python check_llm.py
"""

from __future__ import annotations

import sys

import httpx

import config
import llm


def ok(msg: str) -> None:
    print(f"  [ok]   {msg}")


def fail(msg: str) -> None:
    print(f"  [FAIL] {msg}")


def list_models() -> list[str]:
    url = config.LLM_BASE_URL.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {config.LLM_API_KEY}"}
    resp = httpx.get(url, headers=headers, timeout=config.HTTP_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return [m["id"] for m in data.get("data", [])]


def check_tool_call() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "get_temperature",
                "description": "Get the current temperature for a city.",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
        }
    ]
    msg = llm.complete(
        [
            {"role": "system", "content": "You are a weather bot. Use tools when asked."},
            {"role": "user", "content": "What is the temperature in Paris? Use the tool."},
        ],
        tools=tools,
        max_tokens=256,
    )
    calls = getattr(msg, "tool_calls", None)
    if not calls:
        raise RuntimeError(
            "model did not return a tool call; got content: " + repr((msg.content or "")[:300])
        )
    name = calls[0].function.name
    print(f"         tool call -> {name}({calls[0].function.arguments})")


def check_json() -> object:
    data = llm.chat_json(
        [
            {
                "role": "user",
                "content": (
                    "Reply with raw JSON only, no markdown: "
                    '{"status": "ok", "items": [1, 2, 3]}'
                ),
            }
        ],
        max_tokens=256,
    )
    if not isinstance(data, dict) or data.get("status") != "ok":
        raise RuntimeError(f"unexpected JSON payload: {data!r}")
    return data


def main() -> int:
    print(f"LLM gateway: {config.LLM_BASE_URL}")
    print(f"LLM model:   {config.LLM_MODEL or '(unset)'}")
    try:
        config.require_llm_model()
    except SystemExit as err:
        fail(str(err))
        return 2

    print("\n1. listing models")
    try:
        models = list_models()
        ok(f"{len(models)} models available")
        if config.LLM_MODEL in models:
            ok(f"LLM_MODEL '{config.LLM_MODEL}' is listed")
        else:
            print(f"         note: '{config.LLM_MODEL}' not in the list (gateways often alias)")
    except Exception as err:
        fail(f"could not list models: {err}")
        return 1

    print("\n2. tool-calling round trip")
    try:
        check_tool_call()
        ok("tool calling works")
    except Exception as err:
        fail(f"tool calling failed: {err}")
        print(
            "\nEverything downstream depends on tool calling. Stopping here.\n"
            "Try another LLM_MODEL (e.g. a larger coding model) and re-run."
        )
        return 1

    print("\n3. strict-JSON round trip")
    try:
        check_json()
        ok("strict JSON parsed cleanly")
    except Exception as err:
        fail(f"JSON round trip failed: {err}")
        return 1

    print("\nAll checks passed. You are good to build.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

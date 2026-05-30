"""Anthropic adapter.

Same shape as the OpenAI adapter — extract_schemas + auto_repair — adapted
to Anthropic's `input_schema` field and `tool_result` content blocks.
"""

from __future__ import annotations

import json
from typing import Any

from cruxial.errors import RepairExhausted
from cruxial.types import Failure


def extract_schemas(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pull {tool_name: input_schema} from a native Anthropic tools list.

    Anthropic's tool format:
      [{"name": "...", "description": "...", "input_schema": {... JSON Schema ...}}]
    """
    out: dict[str, dict[str, Any]] = {}
    for t in tools:
        name = t.get("name")
        schema = t.get("input_schema")
        if name and isinstance(schema, dict):
            out[name] = schema
    return out


def auto_repair(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    failure: Failure,
    failed_args: dict[str, Any],
    repair_prompt: str,
    max_attempts: int = 1,
    max_tokens: int = 1024,
    tool_use_id: str | None = None,
) -> dict[str, Any]:
    """Round-trip a repair prompt through Anthropic and return corrected args.

    Args:
        client: An anthropic.Anthropic instance.
        model: Same model id used in the original call.
        messages: Conversation up to and including the assistant turn that
                  emitted the bad tool_use block.
        tools: Same tools list passed to the original call.
        failure, failed_args, repair_prompt: from the validator + builder.
        max_attempts: Max repair rounds. Default 1.
        max_tokens: Anthropic requires this; pass through.
        tool_use_id: id of the bad tool_use block, if known. Synthesized
                     otherwise.

    Raises:
        RepairExhausted on max_attempts of invalid output.
    """
    tu_id = tool_use_id or f"toolu_cruxial_repair_{failure.tool}"

    # Anthropic format: feed the failure as a tool_result with is_error=true.
    repair_messages = list(messages) + [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tu_id,
                    "is_error": True,
                    "content": repair_prompt,
                }
            ],
        }
    ]

    last_exception: Exception | None = None
    for _attempt in range(max_attempts):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=repair_messages,
                tools=tools,
                tool_choice={"type": "tool", "name": failure.tool},
            )
        except Exception as exc:  # noqa: BLE001
            last_exception = exc
            continue

        new_args = _extract_tool_use_args(resp, expected_name=failure.tool)
        if new_args is not None:
            return new_args

    raise RepairExhausted(
        f"could not repair {failure.tool!r} after {max_attempts} attempt(s). "
        + (f"last error: {last_exception}" if last_exception else "")
    )


# ─── helpers ──────────────────────────────────────────────────────────────


def _extract_tool_use_args(resp: Any, expected_name: str) -> dict[str, Any] | None:
    """Pull the first matching tool_use block's input out of an Anthropic response."""
    try:
        content = getattr(resp, "content", []) or []
        for block in content:
            btype = getattr(block, "type", None)
            if btype != "tool_use":
                continue
            name = getattr(block, "name", None)
            if name != expected_name:
                continue
            inp = getattr(block, "input", None)
            if isinstance(inp, dict):
                return inp
            if isinstance(inp, str):
                return json.loads(inp)
    except Exception:
        return None
    return None

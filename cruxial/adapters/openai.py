"""OpenAI adapter.

Two exports:
  - extract_schemas: pull {name: json_schema} from native OpenAI tools list.
  - auto_repair:     run the structured-retry round-trip against an OpenAI
                     client. Returns corrected args or raises RepairExhausted.

The openai SDK is an optional dependency — import this module only when you
need it. `pip install cruxial[openai]` if you want it bundled.
"""

from __future__ import annotations

import json
from typing import Any

from cruxial.errors import RepairExhausted
from cruxial.types import Failure


def extract_schemas(tools: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Pull {tool_name: parameters_schema} from a native OpenAI tools list.

    OpenAI's tool format:
      [{"type": "function",
        "function": {
          "name": "...",
          "parameters": {... JSON Schema ...}
        }}]
    """
    out: dict[str, dict[str, Any]] = {}
    for t in tools:
        if t.get("type") != "function":
            continue
        fn = t.get("function", {})
        name = fn.get("name")
        params = fn.get("parameters")
        if name and isinstance(params, dict):
            out[name] = params
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
    tool_call_id: str | None = None,
) -> dict[str, Any]:
    """Round-trip a repair prompt and return the LLM's corrected args.

    Args:
        client: An openai.OpenAI instance (or compatible — LiteLLM works,
                AzureOpenAI works).
        model:  Same model id used in the original call. On Azure, this is
                the deployment name.
        messages: The conversation up to and including the assistant turn
                  that emitted the bad tool call. If you intend to pass
                  `tool_call_id`, the assistant turn with that tool_call
                  MUST be present in messages (OpenAI API requirement).
        tools:  The same tools list passed to the original call.
        failure: The Cruxial Failure from the validator.
        failed_args: The args the model originally produced (used to build
                     the repair prompt and to log a diff).
        repair_prompt: The text built by `cruxial.build_repair_prompt()`.
        max_attempts: Max repair rounds. Default 1.
        tool_call_id: The id of the failed tool_call from the assistant turn.
                      When provided, we use the canonical `role:"tool"`
                      message format which models respond to most reliably.
                      When None, we fall back to a `role:"user"` message —
                      less reliable but works without history coordination.

    Returns:
        Corrected args dict.

    Raises:
        RepairExhausted: After max_attempts the model still emits invalid
                         args (or no tool call at all).
    """
    # Choose the message-injection format. The tool-result format is
    # preferred but requires a real tool_call_id matching the preceding
    # assistant turn. Without one, fall back to a plain user message.
    if tool_call_id is not None:
        repair_turn = [
            {
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": repair_prompt,
            }
        ]
    else:
        repair_turn = [
            {
                "role": "user",
                "content": (
                    f"Your previous `{failure.tool}` tool call was rejected "
                    "by the schema validator. Details:\n\n"
                    + repair_prompt
                    + "\n\nRe-emit the tool call with corrected arguments."
                ),
            }
        ]

    repair_messages = list(messages) + repair_turn

    last_exception: Exception | None = None
    for _attempt in range(max_attempts):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=repair_messages,
                tools=tools,
                tool_choice={
                    "type": "function",
                    "function": {"name": failure.tool},
                },
            )
        except Exception as exc:  # noqa: BLE001
            last_exception = exc
            continue

        new_args = _extract_tool_call_args(resp, expected_name=failure.tool)
        if new_args is not None:
            return new_args

    raise RepairExhausted(
        f"could not repair {failure.tool!r} after {max_attempts} attempt(s). "
        + (f"last error: {last_exception}" if last_exception else "")
    )


def auto_repair_batch(
    client: Any,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_call_outcomes: list[dict[str, Any]],
    max_attempts: int = 1,
) -> dict[str, dict[str, Any]]:
    """Repair every failed call from a multi-tool-call assistant response in one round-trip.

    Background: when an LLM emits N>1 tool_calls in a single response, OpenAI's
    API requires `role:"tool"` messages for EVERY tool_call_id from that turn
    before any further request will be accepted. The single-call `auto_repair`
    cannot satisfy that constraint when N>1 — it provides a tool_result for
    one id and the API returns 400 because the others are unanswered.

    This function provides tool_results for every prior tool_call (success or
    failure), then asks the model to re-emit corrected versions of the failed
    ones. Single repair round-trip, regardless of how many calls failed.

    Args:
        client: An OpenAI / AzureOpenAI / LiteLLM-compatible client.
        model:  Same model id used in the original call.
        messages: Conversation up to AND INCLUDING the assistant turn whose
                  tool_calls we're answering. Must contain the full
                  ``{role: "assistant", content: None, tool_calls: [...]}`` turn
                  with every tool_call_id referenced in ``tool_call_outcomes``.
        tools: Same tools list passed to the original call.
        tool_call_outcomes: One entry per tool_call from the assistant turn.
            Each entry must have:
              - "tool_call_id": str  — original id from the assistant turn
              - "name": str          — tool name
              - "ok": bool           — did cruxial.execute pass?
              - "value": Any         — return value (if ok)
              - "failure": Failure   — the Failure object (if not ok)
              - "repair_prompt": str — built via cruxial.build_repair_prompt (if not ok)
        max_attempts: Repair rounds. Default 1.

    Returns:
        dict mapping original tool_call_id → corrected args dict, ONLY for
        the calls that were failed AND got re-emitted by the model. Calls
        the model declined to re-emit are omitted (caller decides whether to
        treat as fatal).

    Raises:
        RepairExhausted if the model produces no usable tool_calls across all
        attempts AND there were failed outcomes to repair. Never raises if
        there were no failures in the first place — returns {}.
    """
    if not tool_call_outcomes:
        return {}

    failed = [o for o in tool_call_outcomes if not o.get("ok", True)]
    if not failed:
        return {}

    # Provide tool_result for every prior tool_call — OpenAI requires 1:1
    # coverage of the previous assistant turn's tool_calls.
    tool_result_msgs: list[dict[str, Any]] = []
    for o in tool_call_outcomes:
        tc_id = o["tool_call_id"]
        if o.get("ok", True):
            payload = {"ok": True, "result": _safe_jsonable(o.get("value"))}
            content = json.dumps(payload)
        else:
            content = o.get("repair_prompt") or _fallback_failure_text(o)
        tool_result_msgs.append({
            "role": "tool",
            "tool_call_id": tc_id,
            "content": content,
        })

    # Enumerate the failures so the model knows exactly how many corrected
    # calls to re-emit.
    failure_summary = "\n".join(
        f"  {i+1}. tool_call_id={o['tool_call_id']!r}, name={o['name']!r}, "
        f"failure={o['failure'].category}: {o['failure'].message}"
        for i, o in enumerate(failed)
    )

    user_repair_prompt = (
        f"The following {len(failed)} tool call(s) were rejected by the schema "
        f"validator:\n{failure_summary}\n\n"
        "See the tool messages above for the full validation details and the "
        "schema fragments that were violated. Re-emit corrected versions of "
        f"these call(s). Emit exactly {len(failed)} tool call(s)."
    )

    repair_messages = list(messages) + tool_result_msgs + [
        {"role": "user", "content": user_repair_prompt}
    ]

    # Back-match the model's new tool_calls to original failed ids by name.
    # When multiple originals share a name (e.g. 4 deploys), match in
    # emission order — best the API surface allows.
    remaining_by_name: dict[str, list[str]] = {}
    for o in failed:
        remaining_by_name.setdefault(o["name"], []).append(o["tool_call_id"])

    last_exception: Exception | None = None
    for _attempt in range(max_attempts):
        try:
            resp = client.chat.completions.create(
                model=model,
                messages=repair_messages,
                tools=tools,
            )
        except Exception as exc:  # noqa: BLE001
            last_exception = exc
            continue

        new_tool_calls = getattr(resp.choices[0].message, "tool_calls", None) or []
        corrected: dict[str, dict[str, Any]] = {}
        for tc in new_tool_calls:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            name = getattr(fn, "name", None)
            if not name or name not in remaining_by_name or not remaining_by_name[name]:
                continue
            try:
                args = json.loads(getattr(fn, "arguments", "{}") or "{}")
            except json.JSONDecodeError:
                continue
            original_id = remaining_by_name[name].pop(0)
            corrected[original_id] = args

        if corrected:
            return corrected

    raise RepairExhausted(
        f"could not repair {len(failed)} tool call(s) after {max_attempts} attempt(s)"
        + (f". last error: {last_exception}" if last_exception else "")
    )


def _safe_jsonable(value: Any, max_chars: int = 500) -> Any:
    """Make a value safely JSON-serialisable for inclusion in tool_result content."""
    try:
        s = json.dumps(value, default=str)
        return value if len(s) <= max_chars else f"<truncated {len(s)} chars>"
    except Exception:
        return repr(value)[:max_chars]


def _fallback_failure_text(outcome: dict[str, Any]) -> str:
    """If caller forgot repair_prompt, build a minimal one inline."""
    failure = outcome.get("failure")
    if failure is None:
        return "Tool call was rejected. No further details available."
    return (
        f"REJECTED ({failure.category}): {failure.message}\n"
        "Re-emit with corrected arguments."
    )


# ─── helpers ──────────────────────────────────────────────────────────────


def _extract_tool_call_args(resp: Any, expected_name: str) -> dict[str, Any] | None:
    """Pull the first matching tool_call's args out of an openai response."""
    try:
        choice = resp.choices[0]
        tool_calls = getattr(choice.message, "tool_calls", None) or []
        for tc in tool_calls:
            fn = getattr(tc, "function", None)
            if fn is None:
                continue
            name = getattr(fn, "name", None)
            if name != expected_name:
                continue
            raw_args = getattr(fn, "arguments", "{}") or "{}"
            return json.loads(raw_args)
    except Exception:
        return None
    return None

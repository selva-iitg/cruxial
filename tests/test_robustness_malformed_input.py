"""Malformed-input robustness — every test grounded in a cited real incident.

Sources (see BENCHMARKS.md / DEFENSIVE.md):
  - openai/codex#19765                  truncated JSON args in tool_call.arguments
  - openai/openai-agents-python#2061    bad JSON poisons session-history reload
  - openai/openai-agents-js#664         empty-string arguments crash parser
  - community.openai.com #1333519       trailing comma + mixed quotes from GPT-4.1
  - anthropics/claude-code#5504         object args double-encoded as JSON strings

Every test asserts ONE of:
  (a) Cruxial intercepts cleanly with a typed Failure and helpful message
  (b) Cruxial fails open (returns ok=True / treats as unknown_tool) without
      raising from internal code
  (c) Cruxial raises a typed CruxialError that a host can catch

NEVER acceptable: unhandled exception leaks out, stack trace from
jsonschema internals, or silent pass when the input is clearly wrong.
"""

from __future__ import annotations

import json

import pytest

from cruxial import GuardConfig, guard
from cruxial.telemetry import NullSink
from cruxial.types import Failure
from cruxial.validator import validate


_EMAIL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "to": {"type": "string", "format": "email"},
        "subject": {"type": "string", "maxLength": 200},
        "body": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
    },
    "required": ["to", "subject", "body"],
}


def _cx(extra=None):
    return guard(
        schemas={"send_email": _EMAIL_SCHEMA, **(extra or {})},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )


# ─── 1. Empty / null / type-confused args ────────────────────────────


def test_args_is_none_treated_as_empty_dict():
    """Cruxial should normalize None → {} rather than crashing."""
    cx = _cx()
    res = cx.check("send_email", None)  # type: ignore[arg-type]
    assert not res.ok
    assert res.failure.category == "missing_required"


def test_args_is_empty_dict_returns_missing_required():
    cx = _cx()
    res = cx.check("send_email", {})
    assert not res.ok
    assert res.failure.category == "missing_required"
    # All 3 required fields should be surfaced via siblings
    paths = {f.path for f in res.failure.all_violations()}
    assert "to" in paths


def test_args_with_completely_wrong_top_level_type():
    """Args MUST be a dict for an object schema. List/string/int should not crash."""
    cx = _cx()
    for bad in ([], [1, 2, 3], "not-a-dict", 42, True):
        res = cx.check("send_email", bad)  # type: ignore[arg-type]
        # Either intercepts as type_mismatch OR raises a typed error.
        # Never an unhandled jsonschema exception.
        if not res.ok:
            assert res.failure.category in (
                "type_mismatch", "missing_required", "constraint_violation",
            ), f"unexpected category for {bad!r}: {res.failure.category}"
        else:
            pytest.fail(f"args={bad!r} silently passed — should have failed")


def test_args_with_nested_None_value():
    """A None inside a string field should be classified as type_mismatch."""
    cx = _cx()
    res = cx.check(
        "send_email",
        {"to": None, "subject": "hi", "body": "x"},
    )
    assert not res.ok
    assert res.failure.category in ("type_mismatch", "format_violation")


def test_args_with_extra_fields_when_additional_properties_false():
    """The 'model invented a field' case must trip extra_field, not silently pass."""
    cx = _cx()
    res = cx.check(
        "send_email",
        {"to": "a@b.com", "subject": "hi", "body": "x", "cc": "x@y.com"},
    )
    assert not res.ok
    assert res.failure.category == "extra_field"


def test_args_with_unicode_strings_pass_validation():
    """Unicode in valid string fields must not break validation."""
    cx = _cx()
    res = cx.check(
        "send_email",
        {"to": "test@example.com", "subject": "日本語 ✨", "body": "Hello 🚀"},
    )
    assert res.ok, f"unicode args should pass: {res.failure}"


def test_args_with_control_characters_in_string():
    """\\n / \\t / \\0 in strings are valid JSON; must not break our pipeline."""
    cx = _cx()
    res = cx.check(
        "send_email",
        {"to": "test@example.com", "subject": "x\n\t", "body": "x\x00y"},
    )
    # Either passes (strings are valid) or trips a specific category — never crash.
    assert res.ok or isinstance(res.failure, Failure)


# ─── 2. Double-encoded object args (claude-code#5504) ────────────────


def test_object_arg_double_encoded_as_json_string_trips_type_mismatch():
    """Anthropic clients have shipped tool calls where an OBJECT param arrived
    as a JSON STRING. Cruxial must reject as type_mismatch, not silently pass."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "options": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
            },
        },
        "required": ["options"],
    }
    cx = guard(
        schemas={"tool": schema},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    res = cx.check("tool", {"options": '{"key":"value"}'})  # string, not object
    assert not res.ok
    assert res.failure.category == "type_mismatch"


# ─── 3. Truncated / malformed JSON in tool_call.arguments string ─────
#
# This happens at the LLM-response level, not inside cruxial.execute.
# But our adapter (auto_repair_batch in cruxial/adapters/openai.py) must
# handle bad JSON in tool_calls without crashing the host.


def test_adapter_handles_bad_json_arguments_string():
    """If tool_call.arguments is unparseable JSON, the demo loop pattern
    catches the JSONDecodeError and continues. Cruxial itself never sees
    such args — but if a host hands them in as already-parsed garbage,
    our validator must classify rather than crash."""
    # Simulate the args a host might construct AFTER a JSONDecodeError fallback:
    # they default to {} and pass on. We verify that path is robust.
    cx = _cx()
    res = cx.check("send_email", {})  # fallback empty dict from a parser error
    assert not res.ok  # missing required


def test_adapter_handles_truncated_json_in_args_field():
    """Codex incident #19765: model emitted unclosed JSON string in arguments.

    Cruxial doesn't parse the wire-level arguments string — that's the host's
    responsibility — but we document and verify the recommended pattern:
    catch JSONDecodeError at the dispatch site, hand {} to cruxial.check,
    let cruxial surface 'missing_required' which the host can then surface
    as 'malformed tool call' or repair via auto_repair_batch."""
    truncated = '{"to": "a@b.com", "subject": "Q3 update'  # missing closing
    try:
        args = json.loads(truncated)
    except json.JSONDecodeError:
        args = {}  # the recommended fallback shown in our examples

    cx = _cx()
    res = cx.check("send_email", args)
    assert not res.ok
    assert res.failure.category == "missing_required"


def test_adapter_handles_arguments_value_null_in_args_field():
    """openai-agents-js#664 reported `arguments: ""`. Sister case: model emits
    `arguments: null`. Host parses it. Both paths must end up in a clean
    Failure, never an unhandled crash inside cruxial."""
    for null_like in (None, {}):
        cx = _cx()
        res = cx.check("send_email", null_like)  # type: ignore[arg-type]
        assert not res.ok
        assert res.failure.category in ("missing_required", "type_mismatch")


# ─── 4. JSON edge values (NaN, Infinity, large numbers) ──────────────


def test_args_with_nan_or_infinity_is_handled_gracefully():
    """JSON spec forbids NaN/Infinity but Python json.loads can emit them
    via `parse_constant`. If they reach cruxial, we shouldn't crash."""
    cx = guard(
        schemas={"calc": {
            "type": "object",
            "properties": {"x": {"type": "number"}},
            "required": ["x"],
        }},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    for weird in (float("nan"), float("inf"), float("-inf")):
        res = cx.check("calc", {"x": weird})
        # Spec says jsonschema may or may not flag these — we just MUST NOT crash.
        assert res.ok or res.failure.category in (
            "type_mismatch", "constraint_violation", "format_violation",
        ), f"weird value {weird} caused unexpected outcome"


def test_args_with_very_large_integer():
    """Python ints have unbounded precision; jsonschema should handle gracefully."""
    cx = guard(
        schemas={"calc": {
            "type": "object",
            "properties": {"x": {"type": "integer", "maximum": 100}},
            "required": ["x"],
        }},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    res = cx.check("calc", {"x": 10**100})
    assert not res.ok
    assert res.failure.category == "constraint_violation"


# ─── 5. Tool-name edge cases ────────────────────────────────────────


def test_unknown_tool_name_is_intercepted_not_crashed():
    cx = _cx()
    for bad_name in ("nonexistent", "", "send_email_xyz"):
        res = cx.check(bad_name, {"to": "a@b.com", "subject": "x", "body": "y"})
        assert not res.ok
        assert res.failure.category == "unknown_tool"


def test_tool_name_with_special_characters_does_not_crash():
    """Some MCP servers expose `tool.with.dots` or `tool/with/slashes`.
    These should classify as unknown_tool (registered name doesn't match),
    not crash."""
    cx = _cx()
    for weird in ("send.email", "send/email", "send email", "send-email", "🚀"):
        res = cx.check(weird, {})
        assert not res.ok
        assert res.failure.category == "unknown_tool"


def test_registered_tool_name_with_unusual_chars_round_trips():
    """If the consumer registers an unusually-named tool, .check() should still find it."""
    weird_name = "vendor.gmail.send_email"
    cx = guard(
        schemas={weird_name: {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    assert cx.knows(weird_name)
    res = cx.check(weird_name, {"x": "ok"})
    assert res.ok


# ─── 6. Multiple-violations-at-once is reported with all siblings ────


def test_args_with_three_simultaneous_violations_surfaces_all_three():
    """Per the multi-error-fix shipped earlier: validator returns up to 5
    violations as siblings so the model fixes them in one repair pass."""
    cx = _cx()
    bad = {
        # missing 'body' → missing_required
        "to": "not-an-email",          # → format_violation
        "subject": "x" * 300,          # → constraint_violation (maxLength=200)
        "priority": "urgent",          # → enum_violation
        "extra": "field",              # → extra_field (additionalProperties=false)
    }
    res = cx.check("send_email", bad)
    assert not res.ok
    cats = {f.category for f in res.failure.all_violations()}
    # We don't require ALL 5 to fire (validator caps at 5 deduped per (cat, path))
    # but at LEAST 3 of the distinct failure modes must surface.
    assert len(cats) >= 3, f"only got {cats} from multi-violation args"


# ─── 7. Args that are valid but look suspicious ─────────────────────


def test_args_that_are_valid_pass_cleanly():
    """Sanity check — clean args shouldn't get false-positive intercepts."""
    cx = _cx()
    res = cx.check(
        "send_email",
        {"to": "founders@cruxial.ai", "subject": "Ship it", "body": "Today."},
    )
    assert res.ok, f"clean args were intercepted: {res.failure}"


def test_args_with_only_required_fields_pass():
    """Optional fields are optional — omitting them must not trip missing_required."""
    cx = _cx()
    res = cx.check(
        "send_email",
        {"to": "a@b.com", "subject": "x", "body": "y"},  # no priority
    )
    assert res.ok

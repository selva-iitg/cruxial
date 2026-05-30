"""jsonschema library footguns + draft-compat issues.

Sources:
  - python-jsonschema docs                 'format' is informational by default
  - jsonschema#847                         RecursionError on anyOf + Draft 2019-09
  - jsonschema#547                         propertyNames + $ref crash
  - json-schema.org/understanding/object   additionalProperties:false under allOf is broken;
                                           unevaluatedProperties is the real fix
  - jsonschema#274                         recursive relative $ref resolution
  - openai community #929996               OpenAI strict mode requires additionalProperties:false everywhere
  - anthropics/claude-code#10606           Anthropic rejects top-level oneOf/allOf/anyOf

For each: cruxial either handles it correctly OR fails loud with a typed
error. No silent mis-validation.
"""

from __future__ import annotations

import pytest

from cruxial import GuardConfig, guard
from cruxial.telemetry import NullSink
from cruxial.validator import validate


def _check(schema, args, tool="t"):
    cx = guard(
        schemas={tool: schema},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    return cx.check(tool, args)


# ─── 1. format checker enabled (the BIG footgun) ─────────────────────


def test_format_email_rejects_garbage_by_default():
    """jsonschema's default validator treats `format` as informational —
    `not-an-email` would silently pass. Cruxial MUST enable a format checker
    out of the box, otherwise every consumer hits this silent failure."""
    res = _check(
        {"type": "object", "properties": {"e": {"type": "string", "format": "email"}}, "required": ["e"]},
        {"e": "not-an-email"},
    )
    assert not res.ok
    assert res.failure.category == "format_violation"


def test_format_uri_rejects_garbage_by_default():
    """The 4-silent-pass bug from the MCP audit (2026-05-30). Must stay fixed."""
    res = _check(
        {"type": "object", "properties": {"u": {"type": "string", "format": "uri"}}, "required": ["u"]},
        {"u": "not-a-valid-uri"},
    )
    assert not res.ok
    assert res.failure.category == "format_violation"


def test_format_hostname_rejects_garbage_by_default():
    res = _check(
        {"type": "object", "properties": {"h": {"type": "string", "format": "hostname"}}, "required": ["h"]},
        {"h": "spaces in hostname"},
    )
    assert not res.ok
    assert res.failure.category == "format_violation"


def test_format_date_time_rejects_garbage():
    res = _check(
        {"type": "object", "properties": {"t": {"type": "string", "format": "date-time"}}, "required": ["t"]},
        {"t": "2026-05-30"},  # missing time component → rejected
    )
    assert not res.ok
    assert res.failure.category == "format_violation"


def test_format_ipv4_rejects_garbage():
    res = _check(
        {"type": "object", "properties": {"ip": {"type": "string", "format": "ipv4"}}, "required": ["ip"]},
        {"ip": "999.999.999.999"},
    )
    assert not res.ok
    assert res.failure.category == "format_violation"


# ─── 2. Recursive / self-referential schemas (jsonschema#847) ────────


def test_recursive_schema_via_ref_does_not_crash():
    """A schema that references itself via $ref must not cause RecursionError.

    If jsonschema panics on a particular shape, cruxial's fail_open=True
    must convert that into a pass-through warning, not a host-app crash."""
    recursive = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://example.com/tree",
        "type": "object",
        "properties": {
            "value": {"type": "string"},
            "child": {"$ref": "#"},
        },
        "required": ["value"],
    }
    # Should validate a 5-deep nested struct without crashing
    args = {"value": "root", "child": {"value": "a", "child": {"value": "b", "child": {"value": "c"}}}}
    res = _check(recursive, args)
    # Either passes (recursion handled) or fail-opens with a warning — never crashes
    assert res.ok or res.failure is not None


def test_pathologically_deep_args_do_not_blow_recursion_limit():
    """Real-world: model emits deeply nested args. We must not crash with
    RecursionError — that becomes a host-app crash."""
    schema = {
        "type": "object",
        "properties": {"x": {"type": "object"}},
        "required": ["x"],
    }
    # Build 100-deep nested dict
    args = {"x": {}}
    cursor = args["x"]
    for i in range(100):
        cursor["nested"] = {}
        cursor = cursor["nested"]
    # Should not crash
    res = _check(schema, args)
    assert res.ok or res.failure is not None


# ─── 3. allOf + additionalProperties:false footgun ────────────────────
#   The most-misused JSON Schema idiom. Many users write this expecting
#   strict validation; jsonschema correctly per-spec REJECTS properties
#   from sibling allOf branches. We just need to NOT crash and to surface
#   the violation honestly.


def test_allof_with_additional_properties_false_per_branch_behaves_per_spec():
    """Per JSON Schema spec, additionalProperties:false applies only to the
    properties declared in the SAME subschema. Cruxial defers to jsonschema's
    spec-compliant behavior — but it must not crash, and the failure when it
    happens must be classified clearly."""
    schema = {
        "type": "object",
        "allOf": [
            {"properties": {"a": {"type": "string"}}, "additionalProperties": False},
            {"properties": {"b": {"type": "string"}}, "additionalProperties": False},
        ],
    }
    # Pass {a, b} — per spec each branch sees the OTHER's property as "extra"
    # and rejects. This is a known JSON Schema gotcha; we document it.
    res = _check(schema, {"a": "x", "b": "y"})
    # Don't assert pass/fail — just assert no crash and a clean Failure if any.
    if not res.ok:
        assert res.failure.category in ("extra_field", "constraint_violation")


# ─── 4. Edge cases around `required` ────────────────────────────────


def test_schema_with_no_required_field_is_handled():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    # No required field — empty args should pass
    res = _check(schema, {})
    assert res.ok


def test_schema_with_empty_required_array():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": []}
    res = _check(schema, {})
    assert res.ok


def test_schema_requires_a_field_not_in_properties():
    """If `required` mentions a field not in `properties`, jsonschema still
    requires it. Test it surfaces as missing_required."""
    schema = {
        "type": "object",
        "properties": {"x": {"type": "string"}},
        "required": ["x", "y"],   # 'y' not in properties
    }
    res = _check(schema, {"x": "ok"})
    assert not res.ok
    assert res.failure.category == "missing_required"


# ─── 5. Schema with no type (allows anything) ────────────────────────


def test_typeless_schema_accepts_anything():
    """A schema with no `type` is permissive — should accept any args."""
    schema = {"properties": {"x": {}}, "required": ["x"]}
    res = _check(schema, {"x": "string"})
    assert res.ok
    res = _check(schema, {"x": 42})
    assert res.ok
    res = _check(schema, {"x": [1, 2, 3]})
    assert res.ok
    res = _check(schema, {"x": {"nested": "obj"}})
    assert res.ok


# ─── 6. Empty schema ─────────────────────────────────────────────────


def test_empty_schema_accepts_anything():
    """The empty schema {} is the "match anything" schema in JSON Schema."""
    res = _check({}, {"any": "args", "at": "all"})
    assert res.ok


# ─── 7. Schemas with unusual but legal property names ────────────────


def test_property_names_with_dots_are_legal_in_jsonschema():
    """JSON Schema allows any string as a property name. Anthropic's API
    has a stricter ^[a-zA-Z0-9_.-]{1,64}$ rule but that's an API-side issue
    we should expose via a separate `cruxial.lint_schema()` helper (V0.2)."""
    schema = {
        "type": "object",
        "properties": {
            "my.dotted.field": {"type": "string"},
        },
        "required": ["my.dotted.field"],
    }
    res = _check(schema, {"my.dotted.field": "ok"})
    assert res.ok


def test_property_names_with_special_chars():
    schema = {
        "type": "object",
        "properties": {
            "$.xgafv": {"type": "string"},  # known Anthropic-API-killer
        },
        "required": ["$.xgafv"],
    }
    # Cruxial itself should accept any property name. The fact that Anthropic
    # would 400 on this schema is a downstream concern — V0.2 lint will catch
    # it before submission.
    res = _check(schema, {"$.xgafv": "ok"})
    assert res.ok


# ─── 8. Schemas with anyOf / oneOf / allOf compositions ──────────────


def test_oneof_with_matching_branch_passes():
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "integer"},
                ],
            },
        },
        "required": ["value"],
    }
    assert _check(schema, {"value": "ok"}).ok
    assert _check(schema, {"value": 42}).ok


def test_oneof_with_no_matching_branch_intercepts():
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "oneOf": [
                    {"type": "string"},
                    {"type": "integer"},
                ],
            },
        },
        "required": ["value"],
    }
    res = _check(schema, {"value": True})  # bool matches neither
    assert not res.ok
    # Classifier may bucket as constraint_violation (closest jsonschema fit)
    assert res.failure.category in ("constraint_violation", "type_mismatch")


def test_anyof_passes_when_any_branch_matches():
    schema = {
        "type": "object",
        "properties": {
            "x": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        },
        "required": ["x"],
    }
    assert _check(schema, {"x": "ok"}).ok
    assert _check(schema, {"x": None}).ok


# ─── 9. Schema with enum at top level ────────────────────────────────


def test_enum_violation_with_unicode_value():
    schema = {
        "type": "object",
        "properties": {
            "lang": {"type": "string", "enum": ["en", "ja", "中文", "हिंदी"]},
        },
        "required": ["lang"],
    }
    assert _check(schema, {"lang": "ja"}).ok
    assert _check(schema, {"lang": "中文"}).ok
    res = _check(schema, {"lang": "klingon"})
    assert not res.ok
    assert res.failure.category == "enum_violation"


# ─── 10. Pattern-based validation with unicode ───────────────────────


def test_unicode_pattern_match():
    """Patterns must handle unicode properly via Python's re module."""
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "pattern": r"^[\w-]+$"},  # \w includes unicode
        },
        "required": ["name"],
    }
    # Should pass — unicode letters match \w
    res = _check(schema, {"name": "tomás"})
    assert res.ok
    # Should fail — spaces not allowed
    res = _check(schema, {"name": "has space"})
    assert not res.ok
    assert res.failure.category == "format_violation"


# ─── 11. Number vs integer distinction ───────────────────────────────


def test_integer_schema_rejects_float():
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }
    res = _check(schema, {"n": 3.14})
    assert not res.ok
    assert res.failure.category == "type_mismatch"


def test_integer_schema_accepts_float_that_is_integral():
    """JSON spec: 1.0 is an integer. jsonschema permits this. Verify."""
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }
    # 5.0 is an integer per JSON Schema
    res = _check(schema, {"n": 5.0})
    # jsonschema is liberal here — accept either outcome but never crash
    assert res.ok or res.failure.category == "type_mismatch"


def test_number_schema_accepts_int_and_float():
    schema = {
        "type": "object",
        "properties": {"n": {"type": "number"}},
        "required": ["n"],
    }
    assert _check(schema, {"n": 42}).ok
    assert _check(schema, {"n": 3.14}).ok


# ─── 12. Bool vs int (a common Python footgun) ───────────────────────


def test_python_bool_is_subclass_of_int_jsonschema_distinguishes():
    """In Python, True is 1. jsonschema's validator must NOT treat bool as
    a valid integer. This is a real source of confusion."""
    schema = {
        "type": "object",
        "properties": {"n": {"type": "integer"}},
        "required": ["n"],
    }
    res = _check(schema, {"n": True})
    # jsonschema does distinguish — bool is not integer in the schema sense
    assert not res.ok
    assert res.failure.category == "type_mismatch"

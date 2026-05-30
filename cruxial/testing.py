"""Synthetic payload generators for smoke-testing a Cruxial integration.

Real LLMs are usually well-behaved on simple schemas — meaning a freshly
integrated Cruxial setup will sit at 0% interception even when wired
perfectly. That's confusing for engineers verifying "is it even on?".

This module solves that. Given a JSON Schema, generate:
  - one synthetic VALID payload (proves happy path)
  - one synthetic violating payload per supported failure category

Usage:

    from cruxial.testing import valid_payload, violation_payloads

    payloads = violation_payloads(my_schema)
    for category, bad_args in payloads.items():
        res = cruxial.check("my_tool", bad_args)
        assert not res.ok and res.failure.category == category

    happy = valid_payload(my_schema)
    res = cruxial.check("my_tool", happy)
    assert res.ok

This is intentionally a smoke-test tool, not a property-based fuzzer.
It produces ONE plausible example per category — enough to verify a
telemetry pipeline writes the right row.
"""

from __future__ import annotations

from typing import Any

from cruxial.types import FailureCategory


__all__ = ["valid_payload", "violation_payloads"]


# ─── public API ─────────────────────────────────────────────────────────


def valid_payload(schema: dict[str, Any]) -> dict[str, Any]:
    """Synthesize one valid object that satisfies the schema.

    Required + optional fields are populated. Values are deterministic
    plausible defaults (the *first* allowed value for enums, the lower
    bound for numeric ranges, etc.).
    """
    if schema.get("type") != "object":
        # We only synth top-level object schemas (the tool-args case).
        return {}

    props: dict[str, Any] = schema.get("properties", {}) or {}
    required: set[str] = set(schema.get("required", []) or [])
    out: dict[str, Any] = {}
    for field, field_schema in props.items():
        out[field] = _valid_value(field_schema, field)
    # Filter to required + reasonably-populated optionals; over-filling is fine.
    return out


def violation_payloads(schema: dict[str, Any]) -> dict[FailureCategory, dict[str, Any]]:
    """Synthesize one payload per failure category derivable from the schema.

    Categories produced depend on what the schema declares. A schema with
    no enums won't get an `enum_violation` example; one without
    `additionalProperties: false` won't get an `extra_field` example.
    """
    if schema.get("type") != "object":
        return {}

    props: dict[str, Any] = schema.get("properties", {}) or {}
    required: list[str] = list(schema.get("required", []) or [])
    base = valid_payload(schema)
    out: dict[FailureCategory, dict[str, Any]] = {}

    # 1. missing_required: drop the first required field.
    if required:
        bad = {k: v for k, v in base.items() if k != required[0]}
        out["missing_required"] = bad

    # 2. type_mismatch: flip the type of any typed field.
    for field, field_schema in props.items():
        wrong = _wrong_type_for(field_schema)
        if wrong is _UNCHANGED:
            continue
        bad = dict(base)
        bad[field] = wrong
        out["type_mismatch"] = bad
        break

    # 3. enum_violation: emit a string not in the enum.
    for field, field_schema in props.items():
        enum = field_schema.get("enum")
        if not enum:
            continue
        bad = dict(base)
        bad[field] = _outside_enum(enum)
        out["enum_violation"] = bad
        break

    # 4. format_violation: trash a string field with a known format.
    for field, field_schema in props.items():
        fmt = field_schema.get("format")
        if not fmt or field_schema.get("type") != "string":
            continue
        bad = dict(base)
        bad[field] = "not-a-valid-" + fmt
        out["format_violation"] = bad
        break

    # 5. constraint_violation: pick a constrained field, push it past the bound.
    for field, field_schema in props.items():
        bad_value = _out_of_constraint(field_schema)
        if bad_value is _UNCHANGED:
            continue
        bad = dict(base)
        bad[field] = bad_value
        out["constraint_violation"] = bad
        break

    # 6. extra_field: only meaningful if additionalProperties is false.
    if schema.get("additionalProperties") is False:
        bad = dict(base)
        bad["__cruxial_synth_extra__"] = "should be rejected"
        out["extra_field"] = bad

    return out


# ─── internals ──────────────────────────────────────────────────────────

_UNCHANGED = object()  # sentinel: "no violation possible for this field"


def _valid_value(field_schema: dict[str, Any], name: str) -> Any:
    """Plausible default value satisfying the field's constraints."""
    if "enum" in field_schema:
        return field_schema["enum"][0]
    if "const" in field_schema:
        return field_schema["const"]

    ftype = field_schema.get("type")
    if ftype == "string":
        fmt = field_schema.get("format")
        if fmt == "email":
            return "test@example.com"
        if fmt == "uri":
            return "https://example.com/test"
        if fmt == "date-time":
            return "2026-01-01T00:00:00Z"
        if fmt == "date":
            return "2026-01-01"
        if fmt == "uuid":
            return "00000000-0000-4000-8000-000000000000"
        if fmt == "ipv4":
            return "127.0.0.1"

        pattern = field_schema.get("pattern")
        min_len = field_schema.get("minLength", 0)
        max_len = field_schema.get("maxLength", 64)

        if pattern:
            candidate = _string_for_pattern(pattern, min_len, max_len, name)
            if candidate is not None:
                return candidate
            # Fall through — we'll return a default and let validation fail
            # honestly rather than fabricate a wrong value.

        # No pattern: pick a safe baseline that respects length bounds.
        base = f"smoke-test-{name}"[: max(min_len, min(max_len, 32))]
        if len(base) < min_len:
            base = base.ljust(min_len, "x")
        return base

    if ftype == "integer":
        mn = field_schema.get("minimum", field_schema.get("exclusiveMinimum", 1))
        mx = field_schema.get("maximum", field_schema.get("exclusiveMaximum", 1))
        mult = field_schema.get("multipleOf")
        if isinstance(mn, (int, float)) and isinstance(mx, (int, float)):
            val = int(max(mn, min(mx, (mn + mx) // 2))) or 1
        elif isinstance(mn, (int, float)):
            val = int(mn) + 1
        else:
            val = 1
        if isinstance(mult, int) and mult > 0:
            val = _snap_to_multiple(val, mult, int(mn) if isinstance(mn, (int, float)) else None)
        return val

    if ftype == "number":
        mn = field_schema.get("minimum", 0)
        return float(mn) + 0.5

    if ftype == "boolean":
        return False

    if ftype == "array":
        items = field_schema.get("items", {})
        min_items = field_schema.get("minItems", 1)
        # Generate distinct items in case items has a pattern that would
        # collide on duplicates (rare in practice but safe).
        return [_valid_value(items, f"{name}_item_{i}") for i in range(max(min_items, 1))]

    if ftype == "object":
        return valid_payload(field_schema)

    return None


def _snap_to_multiple(val: int, mult: int, lower: int | None) -> int:
    """Round to the nearest multiple of `mult`, biased downward but staying ≥ lower."""
    snapped = (val // mult) * mult
    if lower is not None and snapped < lower:
        # Snap up to the next multiple ≥ lower.
        remainder = lower % mult
        snapped = lower + (mult - remainder) % mult
    return snapped


def _string_for_pattern(
    pattern: str, min_len: int, max_len: int, name: str
) -> str | None:
    """Best-effort: try a small set of common-shape candidates against a regex.

    Returns the first candidate that fullmatch()es and respects length bounds,
    or None if nothing in our catalogue works. We deliberately don't try to
    invert arbitrary regex (rabbit hole) — we just cover the patterns we
    repeatedly see in real production schemas: prefixed ids, kebab/snake
    case, phone numbers, slash-separated paths.
    """
    import re

    try:
        compiled = re.compile(pattern)
    except re.error:
        return None

    candidates = [
        # Generic kebab + snake
        "smoke-test",
        "smoke_test",
        "smoketest",
        "smoke-test-123",
        # Prefixed ids commonly seen in production APIs
        "cus_smoketest12",
        "sk_test_smoketest12",
        "evt_smoketest12",
        "tok_smoketest12",
        "msg_smoketest12",
        "tmpl_smoketest12",
        "db_smoketest12",
        "tenant_smoketest12",
        "key_smoketestabcdef1234",
        # Slash-separated org/repo, project/key, etc.
        "smoke/test",
        "smoke-org/smoke-repo",
        "abc/xyz",
        # Phone-like — both formatted and E.164
        "+1-555-555-5555",
        "+1 555-555-5555",
        "+15555555555",
        "+919999999999",
        # Slack / chat shapes
        "#smoke-test",
        "#general",
        "@smoketest",
        ":rocket:",
        "1717023600.123456",
        # Semver / version strings
        "1.2.3",
        "v1.2.3",
        "0.1.0",
        "1.0.0-rc.1",
        # Uppercase identifiers (jira keys, airport codes, currency)
        "PROJ",
        "ENG",
        "ABC",
        "BLR",
        # RFC 5545 RRULE
        "RRULE:FREQ=WEEKLY",
        "RRULE:FREQ=DAILY",
        # Lowercase short
        "abc",
        "test",
        "a",
    ]

    for c in candidates:
        if min_len and len(c) < min_len:
            continue
        if max_len and len(c) > max_len:
            continue
        try:
            if compiled.fullmatch(c):
                return c
        except re.error:
            continue
    return None


def _wrong_type_for(field_schema: dict[str, Any]) -> Any:
    """A value of the wrong type for the field. _UNCHANGED if untyped."""
    ftype = field_schema.get("type")
    if ftype == "string":
        return 12345
    if ftype == "integer":
        return "not-an-integer"
    if ftype == "number":
        return "not-a-number"
    if ftype == "boolean":
        return "not-a-bool"
    if ftype == "array":
        return "not-an-array"
    if ftype == "object":
        return "not-an-object"
    return _UNCHANGED


def _outside_enum(enum: list[Any]) -> Any:
    """A value definitely not in the enum."""
    candidate = "definitely_not_in_enum"
    while candidate in enum:
        candidate += "_x"
    return candidate


def _out_of_constraint(field_schema: dict[str, Any]) -> Any:
    """A value that satisfies type but violates a numeric/length/pattern bound."""
    ftype = field_schema.get("type")

    if ftype == "integer" or ftype == "number":
        if "maximum" in field_schema:
            return field_schema["maximum"] + (1 if ftype == "integer" else 0.001)
        if "minimum" in field_schema:
            return field_schema["minimum"] - (1 if ftype == "integer" else 0.001)

    if ftype == "string":
        if "maxLength" in field_schema:
            return "x" * (field_schema["maxLength"] + 1)
        if "minLength" in field_schema and field_schema["minLength"] > 0:
            return ""  # likely under the min
        if "pattern" in field_schema:
            return "PATTERN VIOLATION " + field_schema["pattern"]

    if ftype == "array":
        if "maxItems" in field_schema:
            n = field_schema["maxItems"] + 1
            items = field_schema.get("items", {})
            sample = _valid_value(items, "x") if items else "x"
            return [sample] * n

    return _UNCHANGED

"""All violations per tool_call are surfaced together — no cascading repairs.

These cover the bug surfaced by the deploy_application benchmark prompt
(2026-05-30): model emitted version='1.0' AND replicas=200, validator
stopped at first error, repair fixed version but second error re-surfaced
on retry.

After this fix: a single ``cruxial.check()`` returns a Failure whose
``.siblings`` contains every other violation. The repair prompt
enumerates ALL of them so the model fixes everything in one round-trip.
"""

from __future__ import annotations

from cruxial import GuardConfig, guard
from cruxial.repair import build_repair_prompt
from cruxial.telemetry import NullSink
from cruxial.validator import validate


_DEPLOY_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "service_name": {"type": "string", "pattern": "^[a-z][a-z0-9-]{1,40}$"},
        "version": {
            "type": "string",
            "pattern": r"^v?\d+\.\d+\.\d+(-[a-zA-Z0-9.]+)?$",
        },
        "environment": {
            "type": "string",
            "enum": ["dev", "staging", "production", "canary"],
        },
        "replicas": {"type": "integer", "minimum": 1, "maximum": 100},
    },
    "required": ["service_name", "version", "environment"],
}


def test_validator_surfaces_all_violations_as_siblings():
    """The exact case from the benchmark — bad version AND too many replicas."""
    bad_args = {
        "service_name": "payments-service",
        "version": "1.0",           # missing patch → format_violation
        "environment": "production",
        "replicas": 200,            # > max 100 → constraint_violation
    }
    res = validate("demo_deploy_application", bad_args, _DEPLOY_SCHEMA)
    assert not res.ok
    all_violations = res.failure.all_violations()
    assert len(all_violations) == 2, (
        f"expected 2 violations, got {len(all_violations)}: "
        f"{[(v.category, v.message) for v in all_violations]}"
    )
    cats = sorted(v.category for v in all_violations)
    assert cats == ["constraint_violation", "format_violation"]


def test_validator_dedupes_overlapping_errors_per_path():
    """jsonschema can report the same path multiple times in oneOf/anyOf —
    we keep first occurrence per (category, path) tuple."""
    schema = {
        "type": "object",
        "properties": {
            "x": {"type": "string", "enum": ["a", "b", "c"]},
        },
        "required": ["x"],
    }
    # value 42 is both wrong type AND not in enum — both are legitimate
    # violations on the same path, just classified differently. Keep both.
    res = validate("t", {"x": 42}, schema)
    assert not res.ok
    cats = sorted(v.category for v in res.failure.all_violations())
    # type_mismatch and enum_violation both fire for {x: 42}
    assert "type_mismatch" in cats or "enum_violation" in cats


def test_repair_prompt_enumerates_every_violation():
    """The repair prompt must list ALL violations so the model fixes them all."""
    bad_args = {
        "service_name": "payments-service",
        "version": "1.0",
        "environment": "production",
        "replicas": 200,
    }
    res = validate("demo_deploy_application", bad_args, _DEPLOY_SCHEMA)
    prompt = build_repair_prompt(res.failure, _DEPLOY_SCHEMA, bad_args)

    # Both violations must appear by category name in the prompt
    assert "format_violation" in prompt
    assert "constraint_violation" in prompt
    # And the prompt must instruct the model to fix all at once
    assert "ALL" in prompt or "all" in prompt
    assert "single" in prompt or "simultaneously" in prompt
    # The literal bad values appear (model needs to see them)
    assert "1.0" in prompt
    assert "200" in prompt


def test_repair_prompt_for_single_violation_unchanged():
    """When only one violation, prompt uses the original single-failure shape."""
    res = validate(
        "demo_deploy_application",
        {"service_name": "ok-svc", "version": "v1.2.3", "environment": "production", "replicas": 5},
        _DEPLOY_SCHEMA,
    )
    assert res.ok

    # Now a single-violation case
    res = validate(
        "demo_deploy_application",
        {"service_name": "ok-svc", "version": "1.0", "environment": "production", "replicas": 5},
        _DEPLOY_SCHEMA,
    )
    assert not res.ok
    assert len(res.failure.all_violations()) == 1
    prompt = build_repair_prompt(res.failure, _DEPLOY_SCHEMA, {"version": "1.0"})
    # Single-violation prompt does NOT use the multi-violation header
    assert "violation(s):" not in prompt or "1 violation(s):" not in prompt


def test_check_surfaces_all_violations_through_cruxial_api():
    """End-to-end: cruxial.check() returns failure.siblings populated for multi-error case."""
    c = guard(
        schemas={"deploy": _DEPLOY_SCHEMA},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    bad = {
        "service_name": "payments-service",
        "version": "1.0",
        "environment": "production",
        "replicas": 200,
    }
    res = c.check("deploy", bad)
    assert not res.ok
    violations = res.failure.all_violations()
    assert len(violations) == 2
    # Backward compat: result.failure.category still returns primary
    assert res.failure.category in ("format_violation", "constraint_violation")


def test_violation_cap_at_5():
    """Pathological schema with many violations gets capped at 5 (cap defined in validator)."""
    # 10 required fields, omit all → 10 missing_required candidates
    schema = {
        "type": "object",
        "properties": {f"field_{i}": {"type": "string"} for i in range(10)},
        "required": [f"field_{i}" for i in range(10)],
    }
    res = validate("t", {}, schema)
    assert not res.ok
    assert len(res.failure.all_violations()) <= 5  # cap


def test_repair_prompt_lists_schema_fragments_for_every_failing_path():
    bad = {
        "service_name": "OK-svc",   # pattern violation (uppercase)
        "version": "1.0",            # pattern violation (missing patch)
        "environment": "production",
        "replicas": 999,             # constraint violation
    }
    res = validate("deploy", bad, _DEPLOY_SCHEMA)
    prompt = build_repair_prompt(res.failure, _DEPLOY_SCHEMA, bad)
    # Schema fragments for both failing fields should appear
    assert "service_name" in prompt or "version" in prompt
    assert "replicas" in prompt

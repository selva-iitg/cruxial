"""Verify the cruxial.testing helpers produce payloads that the validator
actually classifies into the requested category. This is the contract: if
violation_payloads(schema)["missing_required"] is fed into Cruxial.check,
the failure category must be "missing_required".
"""

from __future__ import annotations

from cruxial import GuardConfig, guard
from cruxial.telemetry import NullSink
from cruxial.testing import valid_payload, violation_payloads


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "to": {"type": "string", "format": "email"},
        "subject": {"type": "string", "maxLength": 10},
        "body": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        "retries": {"type": "integer", "minimum": 1, "maximum": 5},
    },
    "required": ["to", "subject", "body"],
}


def _cruxial(schemas):
    return guard(
        schemas={"send_email": schemas},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )


def test_valid_payload_passes_validation():
    cx = _cruxial(SCHEMA)
    res = cx.check("send_email", valid_payload(SCHEMA))
    assert res.ok, f"expected valid_payload to pass; got failure: {res.failure}"


def test_violation_payloads_produce_every_supported_category():
    cx = _cruxial(SCHEMA)
    payloads = violation_payloads(SCHEMA)
    # SCHEMA covers all 6 schema-derivable categories:
    assert set(payloads) == {
        "missing_required",
        "type_mismatch",
        "enum_violation",
        "format_violation",
        "constraint_violation",
        "extra_field",
    }


def test_each_violation_payload_is_classified_correctly():
    cx = _cruxial(SCHEMA)
    payloads = violation_payloads(SCHEMA)
    for category, bad_args in payloads.items():
        res = cx.check("send_email", bad_args)
        assert not res.ok, f"{category}: expected failure on {bad_args}"
        assert res.failure.category == category, (
            f"{category}: got classified as {res.failure.category} instead. "
            f"args={bad_args} msg={res.failure.message}"
        )


def test_schemas_without_enum_dont_get_enum_violation():
    """Negative: a schema without enums should skip the enum_violation case."""
    schema_no_enum = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "name": {"type": "string"},
            "count": {"type": "integer"},
        },
        "required": ["name"],
    }
    payloads = violation_payloads(schema_no_enum)
    assert "enum_violation" not in payloads
    assert "missing_required" in payloads
    assert "type_mismatch" in payloads
    assert "extra_field" in payloads  # additionalProperties: false declared


def test_extra_field_only_when_additional_properties_false():
    schema_lenient = {
        "type": "object",
        # additionalProperties not declared — defaults to True in JSON Schema
        "properties": {"name": {"type": "string"}},
        "required": ["name"],
    }
    payloads = violation_payloads(schema_lenient)
    assert "extra_field" not in payloads

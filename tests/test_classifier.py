"""Each of the 7 failure categories must be classified correctly.

These tests pin the contract — if a refactor changes how jsonschema errors
get bucketed, the affected category test will catch it.
"""

from __future__ import annotations

from cruxial.validator import validate


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


def test_happy_path_validates_ok():
    res = validate("send_email", {"to": "a@b.com", "subject": "hi", "body": "x"}, SCHEMA)
    assert res.ok
    assert res.failure is None


def test_missing_required():
    res = validate("send_email", {"subject": "hi", "body": "x"}, SCHEMA)
    assert not res.ok
    assert res.failure.category == "missing_required"
    assert "to" in res.failure.message


def test_type_mismatch():
    res = validate("send_email", {"to": 42, "subject": "hi", "body": "x"}, SCHEMA)
    assert not res.ok
    assert res.failure.category == "type_mismatch"


def test_enum_violation():
    res = validate(
        "send_email",
        {"to": "a@b.com", "subject": "hi", "body": "x", "priority": "urgent"},
        SCHEMA,
    )
    assert not res.ok
    assert res.failure.category == "enum_violation"
    assert "urgent" in res.failure.message


def test_format_violation():
    res = validate(
        "send_email",
        {"to": "definitely-not-an-email", "subject": "hi", "body": "x"},
        SCHEMA,
    )
    assert not res.ok
    assert res.failure.category == "format_violation"


def test_constraint_violation_maxlength():
    res = validate(
        "send_email",
        {"to": "a@b.com", "subject": "way too long subject line", "body": "x"},
        SCHEMA,
    )
    assert not res.ok
    assert res.failure.category == "constraint_violation"


def test_constraint_violation_minimum():
    res = validate(
        "send_email",
        {"to": "a@b.com", "subject": "hi", "body": "x", "retries": 0},
        SCHEMA,
    )
    assert not res.ok
    assert res.failure.category == "constraint_violation"


def test_extra_field():
    res = validate(
        "send_email",
        {"to": "a@b.com", "subject": "hi", "body": "x", "made_up_field": True},
        SCHEMA,
    )
    assert not res.ok
    assert res.failure.category == "extra_field"
    assert "made_up_field" in res.failure.message

"""Maps a raw jsonschema validation error into one of the 7 Cruxial categories.

The category drives both the repair prompt copy AND the telemetry bucket. A
miscategorized failure produces a less effective repair prompt, which is the
single biggest knob on the auto-repair success rate.
"""

from __future__ import annotations

from typing import Any

from jsonschema.exceptions import ValidationError

from cruxial.types import Failure, FailureCategory

# jsonschema 'validator' field maps roughly to our categories.
# We translate then refine using context (path, message) for ambiguous cases.
_VALIDATOR_TO_CATEGORY: dict[str, FailureCategory] = {
    "required": "missing_required",
    "type": "type_mismatch",
    "enum": "enum_violation",
    "const": "enum_violation",
    "format": "format_violation",
    "pattern": "format_violation",
    "minimum": "constraint_violation",
    "maximum": "constraint_violation",
    "exclusiveMinimum": "constraint_violation",
    "exclusiveMaximum": "constraint_violation",
    "minLength": "constraint_violation",
    "maxLength": "constraint_violation",
    "minItems": "constraint_violation",
    "maxItems": "constraint_violation",
    "minProperties": "constraint_violation",
    "maxProperties": "constraint_violation",
    "multipleOf": "constraint_violation",
    "additionalProperties": "extra_field",
    "unevaluatedProperties": "extra_field",
}


def classify(error: ValidationError, tool: str) -> Failure:
    """Convert a jsonschema ValidationError into a Cruxial Failure."""

    validator = getattr(error, "validator", None) or ""
    category: FailureCategory = _VALIDATOR_TO_CATEGORY.get(
        validator, "constraint_violation"
    )

    path = _format_path(error)
    received = _safe_received(error.instance)
    expected = _safe_expected(error.validator_value)

    # Refine messages for the most common failures so the repair prompt is crisp.
    if category == "missing_required":
        missing_field = _extract_missing_field(error)
        path = missing_field or path
        message = (
            f"required field {missing_field!r} is missing"
            if missing_field
            else error.message
        )
        received = None
        expected = "field present"

    elif category == "type_mismatch":
        message = (
            f"{path or 'value'}: expected {expected!r}, got "
            f"{type(error.instance).__name__}"
        )

    elif category == "enum_violation":
        message = f"{path or 'value'} = {received!r} not in allowed values {expected!r}"

    elif category == "format_violation":
        message = f"{path or 'value'} = {received!r} does not match {validator!r}={expected!r}"

    elif category == "extra_field":
        extra = _extract_extra_field(error)
        path = extra or path
        message = (
            f"unexpected field {extra!r} — not in schema properties"
            if extra
            else error.message
        )
        received = None

    else:  # constraint_violation
        # Always name WHICH constraint + its threshold so the model
        # (and human debugger) can fix it. Without this, messages like
        # "subject: 'xxxx' is too long" leave the consumer guessing
        # the actual max value.
        if validator and error.validator_value is not None:
            message = (
                f"{path or 'value'}: {error.message} "
                f"({validator}={error.validator_value!r})"
            )
        else:
            message = f"{path or 'value'}: {error.message}"

    return Failure(
        category=category,
        tool=tool,
        message=message,
        path=path,
        expected=expected,
        received=received,
    )


def unknown_tool(name: str, known: list[str]) -> Failure:
    """A separate constructor for the tool-not-in-registry case."""
    return Failure(
        category="unknown_tool",
        tool=name,
        message=(
            f"tool {name!r} is not registered — known: {sorted(known)!r}"
        ),
        expected=sorted(known),
        received=name,
    )


# ─── helpers ──────────────────────────────────────────────────────────────


def _format_path(error: ValidationError) -> str | None:
    parts = [str(p) for p in error.absolute_path]
    return ".".join(parts) if parts else None


def _extract_missing_field(error: ValidationError) -> str | None:
    """jsonschema's 'required' error: message is like \"'foo' is a required property\"."""
    msg = error.message or ""
    if "is a required property" in msg:
        return msg.split("'", 2)[1] if "'" in msg else None
    return None


def _extract_extra_field(error: ValidationError) -> str | None:
    """For additionalProperties violations."""
    msg = error.message or ""
    if "Additional properties are not allowed" in msg or "were unexpected" in msg:
        # message format: "Additional properties are not allowed ('foo', 'bar' were unexpected)"
        if "(" in msg and "'" in msg:
            inside = msg.split("(", 1)[1]
            return inside.split("'", 2)[1] if "'" in inside else None
    return None


def _safe_received(value: Any, max_len: int = 80) -> Any:
    """Stringify (truncated) so the repair prompt has a concrete example.

    Note: this is the only place a raw arg value enters memory. We keep it for
    the repair prompt itself; telemetry separately hashes args and stores only
    the hash. The raw value never gets persisted.
    """
    try:
        s = repr(value)
        if len(s) > max_len:
            return s[: max_len - 1] + "…"
        return value
    except Exception:
        return "<unrepr-able>"


def _safe_expected(value: Any, max_len: int = 120) -> Any:
    try:
        s = repr(value)
        if len(s) > max_len:
            return s[: max_len - 1] + "…"
        return value
    except Exception:
        return None

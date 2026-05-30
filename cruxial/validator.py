"""Schema validation. Wraps `jsonschema` with format-checker enabled and
returns Cruxial's structured ValidationResult.
"""

from __future__ import annotations

from typing import Any

import re

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError

from cruxial.classifier import classify
from cruxial.types import ValidationResult

# jsonschema's default FormatChecker handles email/uuid/ipv4/date* etc., but
# `uri`, `iri`, `hostname`, and `idn-hostname` only validate if you've
# installed the `rfc3987` / `idna` extras. Most users won't, and silently
# passing a URL field that's clearly not a URL is a real product bug
# (we caught 4 of these in the MCP schema audit on 2026-05-30). So we
# register lightweight regex-based checkers here — strict enough to catch
# obvious garbage like 'not-a-valid-uri', lenient enough to accept any
# scheme-host shape.
_format_checker = FormatChecker()


@_format_checker.checks("uri", raises=ValueError)
def _check_uri(value):
    if not isinstance(value, str):
        return True
    # RFC 3986 minimum: scheme ":" [scheme-specific]. Scheme must start with
    # a letter, can have letters/digits/+ - . — enforce at least scheme + ":"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:", value):
        raise ValueError(f"{value!r} is not a valid URI (missing or invalid scheme)")
    return True


@_format_checker.checks("iri", raises=ValueError)
def _check_iri(value):
    # Same minimum as URI; IRI just allows unicode chars in the rest.
    return _check_uri(value)


@_format_checker.checks("uri-reference", raises=ValueError)
def _check_uri_reference(value):
    if not isinstance(value, str):
        return True
    # uri-reference allows relative refs, so a bare string IS sometimes
    # valid. But blatant garbage (whitespace, control chars) shouldn't be.
    if "\x00" in value or "\n" in value or " " in value.strip("/"):
        raise ValueError(f"{value!r} contains characters illegal in a URI reference")
    return True


@_format_checker.checks("hostname", raises=ValueError)
def _check_hostname(value):
    if not isinstance(value, str):
        return True
    # RFC 1123: labels of 1-63 chars, each label [a-zA-Z0-9-] not starting/
    # ending with hyphen; whole thing under 253 chars.
    if len(value) > 253:
        raise ValueError(f"hostname too long: {len(value)} chars")
    label = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    if not re.fullmatch(rf"{label}(\.{label})*", value):
        raise ValueError(f"{value!r} is not a valid hostname")
    return True


@_format_checker.checks("idn-hostname", raises=ValueError)
def _check_idn_hostname(value):
    # Permissive — IDN allows unicode. Just reject obvious garbage.
    if not isinstance(value, str):
        return True
    if "\x00" in value or "\n" in value or " " in value:
        raise ValueError(f"{value!r} contains characters illegal in a hostname")
    return True


# jsonschema's default date-time check has implementation-defined strictness
# (often accepts a bare date when the rfc3339-validator package isn't
# installed — a silent false-pass). Same for date and time. We register
# explicit RFC 3339 regex checkers so the validator never silently accepts
# half-specified timestamps.

_RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RE_TIME = re.compile(
    r"^\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})?$"
)
_RE_DATE_TIME = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


@_format_checker.checks("date-time", raises=ValueError)
def _check_date_time(value):
    if not isinstance(value, str):
        return True
    if not _RE_DATE_TIME.fullmatch(value):
        raise ValueError(
            f"{value!r} is not a valid RFC 3339 date-time "
            "(expected YYYY-MM-DDTHH:MM:SS[.frac](Z|±HH:MM))"
        )
    return True


@_format_checker.checks("date", raises=ValueError)
def _check_date(value):
    if not isinstance(value, str):
        return True
    if not _RE_DATE.fullmatch(value):
        raise ValueError(f"{value!r} is not a valid RFC 3339 date (expected YYYY-MM-DD)")
    return True


@_format_checker.checks("time", raises=ValueError)
def _check_time(value):
    if not isinstance(value, str):
        return True
    if not _RE_TIME.fullmatch(value):
        raise ValueError(f"{value!r} is not a valid RFC 3339 time")
    return True


@_format_checker.checks("uuid", raises=ValueError)
def _check_uuid(value):
    if not isinstance(value, str):
        return True
    # RFC 4122 — 8-4-4-4-12 lowercase or uppercase hex
    if not re.fullmatch(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
        value,
    ):
        raise ValueError(f"{value!r} is not a valid UUID")
    return True


_MAX_VIOLATIONS_PER_CALL = 5


def validate(tool: str, args: dict[str, Any], schema: dict[str, Any]) -> ValidationResult:
    """Validate args against schema. Returns ALL violations as one Failure tree.

    The primary violation is the first one detected (returned at
    ``result.failure``). Any additional violations are at
    ``result.failure.siblings``. The repair prompt builder enumerates all
    of them so the model fixes everything in one round-trip — eliminates
    the cascade-into-second-error problem that surfaces with deploy /
    supabase / pinecone-style multi-constraint failures.

    Capped at 5 violations per call. jsonschema's iter_errors can produce
    overlapping reports for the same path (e.g. ``oneOf`` failures yielding
    sub-errors at each branch); we dedupe by ``(category, path)`` and keep
    the first occurrence. 5 is the sweet spot empirically: enough to give
    the model a complete picture, few enough that the repair prompt stays
    tight.
    """
    validator = Draft202012Validator(schema, format_checker=_format_checker)

    seen: set[tuple[str, str]] = set()
    failures = []
    for err in validator.iter_errors(args):
        f = classify(err, tool=tool)
        key = (f.category, f.path or "")
        if key in seen:
            continue
        seen.add(key)
        failures.append(f)
        if len(failures) >= _MAX_VIOLATIONS_PER_CALL:
            break

    if not failures:
        return ValidationResult(ok=True)

    primary = failures[0]
    primary.siblings = failures[1:]
    return ValidationResult(ok=False, failure=primary)

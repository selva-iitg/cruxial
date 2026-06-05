"""Schema validation. Wraps `jsonschema` with a hardened validator + format
checkers and returns Cruxial's structured ValidationResult.

Hardening (vs. stock jsonschema), each closing a finding from the 0.2.0
security/correctness review:
  - **No network during validation.** The validator is pinned to an empty
    ``referencing.Registry()``: an external ``$ref`` (``http://``, ``file://``)
    raises ``Unresolvable`` instead of being fetched. In-schema refs still work.
  - **No hangs.** ``uniqueItems`` is an O(n) set-based check (stock is O(n²));
    ``pattern`` is routed through ``re2`` when installed, else a static guard
    refuses to run catastrophic-backtracking patterns rather than hang.
  - **Calendar/clock validity** on date/time/date-time (stock is regex-shape
    only — it accepts ``2026-02-30`` / ``25:61:61``).
  - **Finite numbers** — ``NaN``/``Infinity`` are rejected by ``type: number``
    (stock accepts them; they then serialise to invalid JSON).
  - **Exact ``multipleOf``** via ``Decimal`` (stock float modulo rejects a valid
    ``0.3`` against ``0.1``).
  - **Structural email** + **dangerous-scheme deny** on ``uri``.
"""

from __future__ import annotations

import datetime
import json
import math
import re
import warnings
from decimal import Decimal, InvalidOperation
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from jsonschema.validators import extend
from referencing import Registry

from cruxial.classifier import classify
from cruxial.types import ValidationResult

# Optional linear-time regex engine. If `cruxial[re2]` is installed, `pattern`
# is matched with re2 (no backtracking → no ReDoS). Otherwise the static guard
# below refuses to run patterns that look catastrophic.
try:  # pragma: no cover - presence is environment-dependent
    import re2 as _re2
except Exception:  # pragma: no cover
    _re2 = None


# ─── format checkers ────────────────────────────────────────────────────────
#
# jsonschema's default FormatChecker handles email/uuid/ipv4/date* etc., but
# `uri`/`hostname`/`idn-hostname` only validate if you've installed the
# `rfc3987`/`idna` extras, and its temporal/email checks are lenient. We
# register strict, ReDoS-safe checkers here.
_format_checker = FormatChecker()

# Pseudo-schemes that are NEVER a legitimate tool argument and are dangerous if
# an agent opens/renders the value. Denied on `format: uri` by default. `file:`
# is deliberately NOT here — it's legitimate for file-handling tools, so denying
# it by default would be a false positive (precision first). Use the opt-in
# `uri_schemes` allowlist to exclude it.
_DANGEROUS_URI_SCHEMES = {"javascript", "vbscript", "data"}
_URI_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):")


@_format_checker.checks("uri", raises=ValueError)
def _check_uri(value):
    if not isinstance(value, str):
        return True
    m = _URI_SCHEME_RE.match(value)
    if not m:
        raise ValueError(f"{value!r} is not a valid URI (missing or invalid scheme)")
    if m.group(1).lower() in _DANGEROUS_URI_SCHEMES:
        raise ValueError(f"{value!r} uses a disallowed scheme {m.group(1)!r}")
    return True


@_format_checker.checks("iri", raises=ValueError)
def _check_iri(value):
    return _check_uri(value)


@_format_checker.checks("uri-reference", raises=ValueError)
def _check_uri_reference(value):
    if not isinstance(value, str):
        return True
    if "\x00" in value or "\n" in value or " " in value.strip("/"):
        raise ValueError(f"{value!r} contains characters illegal in a URI reference")
    return True


@_format_checker.checks("hostname", raises=ValueError)
def _check_hostname(value):
    if not isinstance(value, str):
        return True
    if len(value) > 253:
        raise ValueError(f"hostname too long: {len(value)} chars")
    label = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
    if not re.fullmatch(rf"{label}(\.{label})*", value):
        raise ValueError(f"{value!r} is not a valid hostname")
    return True


@_format_checker.checks("idn-hostname", raises=ValueError)
def _check_idn_hostname(value):
    if not isinstance(value, str):
        return True
    if "\x00" in value or "\n" in value or " " in value:
        raise ValueError(f"{value!r} contains characters illegal in a hostname")
    return True


# Temporal checkers: shape regex first, then real calendar/clock validity, so
# impossible values (2026-02-30, 25:61:61) are rejected. Leap day (2024-02-29)
# and leap second (23:59:60) stay valid.
_RE_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_RE_TIME = re.compile(r"^(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?$")
_RE_DATE_TIME = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
)


def _valid_clock(hh: int, mm: int, ss: int) -> bool:
    return hh <= 23 and mm <= 59 and ss <= 60  # ss == 60 allows a leap second


def _valid_offset(off: str | None) -> bool:
    if not off or off == "Z":
        return True
    return int(off[1:3]) <= 23 and int(off[4:6]) <= 59


@_format_checker.checks("date", raises=ValueError)
def _check_date(value):
    if not isinstance(value, str):
        return True
    m = _RE_DATE.fullmatch(value)
    if not m:
        raise ValueError(f"{value!r} is not a valid RFC 3339 date (expected YYYY-MM-DD)")
    try:
        datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise ValueError(f"{value!r} is not a real calendar date")
    return True


@_format_checker.checks("time", raises=ValueError)
def _check_time(value):
    if not isinstance(value, str):
        return True
    m = _RE_TIME.fullmatch(value)
    if not m:
        raise ValueError(f"{value!r} is not a valid RFC 3339 time")
    if not _valid_clock(int(m.group(1)), int(m.group(2)), int(m.group(3))):
        raise ValueError(f"{value!r} has an out-of-range time component")
    if not _valid_offset(m.group(5)):
        raise ValueError(f"{value!r} has an out-of-range UTC offset")
    return True


@_format_checker.checks("date-time", raises=ValueError)
def _check_date_time(value):
    if not isinstance(value, str):
        return True
    m = _RE_DATE_TIME.fullmatch(value)
    if not m:
        raise ValueError(
            f"{value!r} is not a valid RFC 3339 date-time "
            "(expected YYYY-MM-DDTHH:MM:SS[.frac](Z|±HH:MM))"
        )
    try:
        datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        raise ValueError(f"{value!r} is not a real calendar date")
    if not _valid_clock(int(m.group(4)), int(m.group(5)), int(m.group(6))):
        raise ValueError(f"{value!r} has an out-of-range time component")
    if not _valid_offset(m.group(8)):
        raise ValueError(f"{value!r} has an out-of-range UTC offset")
    return True


# Structural email: split on the single '@', validate each side with bounded
# (provably linear, ReDoS-safe) regexes. A `format: email` field does NOT arm
# any timeout, so the checker itself must be safe under adversarial input.
_RE_EMAIL_LOCAL = re.compile(r"[^@\s]{1,64}")
_RE_EMAIL_LABEL = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
_RE_EMAIL_DOMAIN = re.compile(rf"{_RE_EMAIL_LABEL}(\.{_RE_EMAIL_LABEL})+")


@_format_checker.checks("email", raises=ValueError)
def _check_email(value):
    if not isinstance(value, str):
        return True
    if value.count("@") != 1:
        raise ValueError(f"{value!r} is not a valid email (need exactly one '@')")
    local, _, domain = value.partition("@")
    if not _RE_EMAIL_LOCAL.fullmatch(local):
        raise ValueError(f"{value!r} has an invalid local part")
    if not _RE_EMAIL_DOMAIN.fullmatch(domain):
        raise ValueError(f"{value!r} has an invalid domain")
    return True


@_format_checker.checks("uuid", raises=ValueError)
def _check_uuid(value):
    if not isinstance(value, str):
        return True
    if not re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        value,
    ):
        raise ValueError(f"{value!r} is not a valid UUID")
    return True


def _format_checker_for(uri_schemes: tuple[str, ...] | None) -> FormatChecker:
    """Return the shared checker, or a copy with `uri`/`iri` restricted to an
    opt-in scheme allowlist (GuardConfig.uri_schemes). Only built when the
    allowlist is set, so the normal path keeps the shared, zero-alloc checker.
    """
    if not uri_schemes:
        return _format_checker
    allowed = {s.lower() for s in uri_schemes}

    def _uri_allowlisted(value):
        if not isinstance(value, str):
            return True
        m = _URI_SCHEME_RE.match(value)
        if not m:
            raise ValueError(f"{value!r} is not a valid URI (missing or invalid scheme)")
        if m.group(1).lower() not in allowed:
            raise ValueError(f"{value!r} scheme not in allowed {sorted(allowed)}")
        return True

    fc = FormatChecker()
    fc.checkers = dict(_format_checker.checkers)
    fc.checkers["uri"] = (_uri_allowlisted, (ValueError,))
    fc.checkers["iri"] = (_uri_allowlisted, (ValueError,))
    return fc


# ─── hardened keyword + type checks ─────────────────────────────────────────


def _is_number(checker, instance) -> bool:
    if isinstance(instance, bool):
        return False
    if not isinstance(instance, (int, float)):
        return False
    return math.isfinite(instance)


def _is_integer(checker, instance) -> bool:
    if isinstance(instance, bool):
        return False
    if isinstance(instance, int):
        return True
    if isinstance(instance, float):
        return math.isfinite(instance) and instance.is_integer()
    return False


def _validate_unique_items(validator, uI, instance, schema):
    """O(n) uniqueness via serialised keys (stock jsonschema is O(n²) deep-eq,
    which hangs on large arrays). Yields one error on the first duplicate."""
    if uI and validator.is_type(instance, "array"):
        seen: set[str] = set()
        for item in instance:
            try:
                key = json.dumps(item, sort_keys=True, default=str)
            except Exception:
                key = repr(item)
            if key in seen:
                yield ValidationError(
                    f"{instance!r} has non-unique elements",
                    validator="uniqueItems",
                    validator_value=uI,
                    instance=instance,
                )
                return
            seen.add(key)


def _validate_multiple_of(validator, dB, instance, schema):
    """Exact decimal modulo so a valid 0.3 isn't rejected against 0.1."""
    if not validator.is_type(instance, "number"):
        return
    if isinstance(dB, bool) or dB == 0:
        return
    try:
        quotient = Decimal(str(instance)) / Decimal(str(dB))
        ok = quotient == quotient.to_integral_value()
    except (InvalidOperation, ValueError, ArithmeticError):
        try:
            ok = (instance % dB) == 0
        except Exception:
            return
    if not ok:
        yield ValidationError(
            f"{instance!r} is not a multiple of {dB!r}",
            validator="multipleOf",
            validator_value=dB,
            instance=instance,
        )


# Heuristic for catastrophic backtracking: a quantified group — ( (...)+, (...)*,
# (...){n}, (...){n,}, (...){n,m} ) — whose body contains a quantifier (+ * ?) OR
# an alternation (|). That covers the shapes that actually blow up: nested
# quantifiers ((a+)+), alternation overlap ((a|aa)+, (x|x)*, (ab|a|b)+), and
# fixed/open counts ((.*a){20}, (.*a){20,}). A plain quantified group like (abc)+
# is linear and stays validated (not over-suppressed). Not exhaustive — re2 is
# the real guarantee — so when re2 is absent we refuse to RUN a flagged pattern
# (skip + a loud guard() warning) rather than hang.
_DANGEROUS_PATTERN = re.compile(r"\([^)]*[+*?|][^)]*\)\s*(?:[+*]|\{\d+,?\d*\})")

def _has_nested_quantifier(pattern: str) -> bool:
    """Depth-aware scan for nested-quantifier ReDoS — the ((...)Q)Q shape.

    The flat ``_DANGEROUS_PATTERN`` regex uses ``[^)]*`` and so cannot cross a
    nested ``)``; when the inner unbounded quantifier lives one group deeper it
    never sees it. This walks paren depth and flags an unbounded-quantified
    group (``(...)+`` / ``(...)*``) that itself contains an unbounded-quantified
    sub-expression — the structural cause of exponential backtracking.

    Only ``+`` and ``*`` count here: a bounded ``{n}`` repetition (``(\\d{3})+``)
    is linear and must stay validated, and the genuinely-dangerous brace cases
    (``(.*a){20}``) are single-level and already caught by the flat regex.
    Escapes and character classes are skipped so ``\\(`` and ``[)]`` don't
    perturb the depth count.
    """
    stack = [False]  # per-group: does this group contain an unbounded quantifier?
    i, n = 0, len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":  # skip a character class wholesale
            i += 1
            while i < n:
                if pattern[i] == "\\":
                    i += 1
                elif pattern[i] == "]":
                    break
                i += 1
            i += 1
            continue
        if ch == "(":
            stack.append(False)
        elif ch == ")":
            if len(stack) > 1:
                inner = stack.pop()
                j = i + 1
                if j < n and pattern[j] == "?":  # non-greedy / possessive suffix
                    j += 1
                quantified = j < n and pattern[j] in "+*"
                if quantified and inner:
                    return True
                if quantified or inner:  # propagate an unbounded child upward
                    stack[-1] = True
            else:
                stack = [False]  # unbalanced ) — reset rather than under/overflow
        elif ch in "+*":
            stack[-1] = True
        i += 1
    return False


def is_dangerous_pattern(pattern: str) -> bool:
    try:
        if len(pattern) > 1000:
            return True  # absurdly long pattern — don't risk running it at all
        # Flat check first (cheap; catches alternation + top-level shapes), then
        # the depth-aware scan for nested ((...)Q)Q that the regex can't reach.
        return bool(_DANGEROUS_PATTERN.search(pattern)) or _has_nested_quantifier(pattern)
    except Exception:
        return False


def _validate_pattern(validator, patrn, instance, schema):
    if not validator.is_type(instance, "string"):
        return
    if _re2 is not None:  # pragma: no cover - depends on optional dep
        try:
            if _re2.search(patrn, instance) is None:
                yield _pattern_error(patrn, instance)
            return
        except Exception:
            pass  # re2 rejected the syntax — fall through to the python path
    if is_dangerous_pattern(patrn):
        # Refuse to run a catastrophic pattern (it would hang). Validation of
        # this one constraint is skipped; guard() already warned loudly at
        # construction telling the user to fix it or install cruxial[re2].
        return
    if re.search(patrn, instance) is None:
        yield _pattern_error(patrn, instance)


def _pattern_error(patrn, instance) -> ValidationError:
    return ValidationError(
        f"{instance!r} does not match {patrn!r}",
        validator="pattern",
        validator_value=patrn,
        instance=instance,
    )


_TYPE_CHECKER = (
    Draft202012Validator.TYPE_CHECKER
    .redefine("number", _is_number)
    .redefine("integer", _is_integer)
)

_CruxialValidator = extend(
    Draft202012Validator,
    validators={
        "uniqueItems": _validate_unique_items,
        "multipleOf": _validate_multiple_of,
        "pattern": _validate_pattern,
    },
    type_checker=_TYPE_CHECKER,
)

# Empty registry: external $refs raise Unresolvable (no urlopen, no file read),
# in-schema refs (#/$defs/…) still resolve against the schema document.
_EMPTY_REGISTRY: Registry = Registry()


# ─── object-closure transform (GuardConfig.strict_properties) ───────────────

_OPENNESS_KEYS = ("additionalProperties", "patternProperties", "unevaluatedProperties")
_SCHEMA_MAP_KEYS = ("properties", "$defs", "definitions", "patternProperties", "dependentSchemas")
_SCHEMA_LIST_KEYS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_VALUE_KEYS = ("items", "additionalProperties", "contains", "propertyNames",
                      "not", "if", "then", "else", "unevaluatedItems")


def close_open_objects(node: Any) -> Any:
    """Recursively inject ``additionalProperties: false`` into every object
    subschema that enumerates ``properties`` and hasn't declared openness — so a
    hallucinated extra field is caught even when the author didn't close the
    schema. Opt-in (GuardConfig.strict_properties); never mutates the input.
    """
    if not isinstance(node, dict):
        if isinstance(node, list):
            return [close_open_objects(x) for x in node]
        return node
    out: dict[str, Any] = {}
    for k, v in node.items():
        if k in _SCHEMA_MAP_KEYS and isinstance(v, dict):
            out[k] = {kk: close_open_objects(vv) for kk, vv in v.items()}
        elif k in _SCHEMA_LIST_KEYS and isinstance(v, list):
            out[k] = [close_open_objects(x) for x in v]
        elif k in _SCHEMA_VALUE_KEYS:
            if isinstance(v, list):
                out[k] = [close_open_objects(x) for x in v]
            elif isinstance(v, dict):
                out[k] = close_open_objects(v)
            else:
                out[k] = v
        else:
            out[k] = v
    if "properties" in out and not any(key in node for key in _OPENNESS_KEYS):
        out["additionalProperties"] = False
    return out


def external_refs(schema: Any) -> list[str]:
    """All ``$ref`` values that point outside this document (not ``#...``).
    These no longer resolve (no network, by design), so guard() warns on them.
    """
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#"):
                found.append(ref)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for x in node:
                walk(x)

    walk(schema)
    return found


_MAX_VIOLATIONS_PER_CALL = 5


def validate(
    tool: str,
    args: dict[str, Any],
    schema: dict[str, Any],
    *,
    uri_schemes: tuple[str, ...] | None = None,
) -> ValidationResult:
    """Validate args against schema. Returns ALL violations as one Failure tree.

    The primary violation is the first one detected (returned at
    ``result.failure``); additional violations are at ``result.failure.siblings``
    so the repair prompt can fix everything in one round-trip. Capped at 5,
    deduped by ``(category, path)``.
    """
    validator = _CruxialValidator(
        schema,
        format_checker=_format_checker_for(uri_schemes),
        registry=_EMPTY_REGISTRY,
    )

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

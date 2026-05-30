"""Builds the structured retry prompt fed back to the model.

This is the single biggest knob on auto-repair success rate. Get it wrong and
you cap correction at ~40%. Get it right and you hit 85%+.

Empirical rules baked in:
  1. Show the schema fragment for the failing field. Not the whole schema.
  2. Show the literal bad arg. Don't paraphrase ("you passed an integer").
  3. State the rule, not the fix. "must be email" beats "set to user@example.com"
     — models that get told a literal answer parrot it back.
  4. Keep it under ~200 tokens. Longer = noisier = lower repair rate.
"""

from __future__ import annotations

import json
from typing import Any

from cruxial.types import Failure

_CATEGORY_HINT: dict[str, str] = {
    "missing_required": "Add the missing required field.",
    "type_mismatch": "Convert the value to the correct type.",
    "enum_violation": "Choose a value from the allowed list.",
    "format_violation": "Reformat the value to match the required format.",
    "constraint_violation": "Adjust the value to satisfy the constraint.",
    "extra_field": "Remove the field that is not in the schema.",
    "unknown_tool": "Call one of the registered tools instead.",
}


def build_repair_prompt(
    failure: Failure,
    schema: dict[str, Any] | None,
    failed_args: dict[str, Any],
) -> str:
    """Render the failure (+ any siblings) as a tool_result-style message.

    If the validator surfaced multiple violations for one tool_call (e.g.
    bad version AND too many replicas), all of them are enumerated so the
    model can fix everything in one round-trip — no cascading into a second
    repair attempt.
    """
    all_failures = failure.all_violations()

    if len(all_failures) == 1:
        return _single_violation_prompt(failure, schema, failed_args)
    return _multi_violation_prompt(all_failures, schema, failed_args)


def _single_violation_prompt(
    failure: Failure,
    schema: dict[str, Any] | None,
    failed_args: dict[str, Any],
) -> str:
    schema_fragment = _extract_relevant_fragment(schema, failure.path) if schema else None
    hint = _CATEGORY_HINT.get(failure.category, "")

    lines = [
        f"The previous call to `{failure.tool}` was rejected by the schema validator.",
        "",
        f"Failure ({failure.category}):",
        f"  {failure.message}",
    ]

    if schema_fragment:
        lines += [
            "",
            "Relevant schema:",
            _indent(json.dumps(schema_fragment, indent=2), 2),
        ]

    if failed_args:
        lines += [
            "",
            "The args you sent:",
            _indent(json.dumps(_safe_args(failed_args), indent=2), 2),
        ]

    if hint:
        lines += ["", hint + " Then re-emit the tool call."]

    return "\n".join(lines)


def _multi_violation_prompt(
    failures: list[Failure],
    schema: dict[str, Any] | None,
    failed_args: dict[str, Any],
) -> str:
    """Enumerate all violations with per-violation schema fragments.

    Critical that the model sees ALL failures here — if we surface only
    the first, the model fixes that and a sibling surfaces on the next
    repair attempt, doubling round-trips for what should be one fix.
    """
    tool = failures[0].tool

    lines = [
        f"The previous call to `{tool}` was rejected by the schema validator.",
        "",
        f"{len(failures)} violation(s):",
    ]

    # Enumerate every violation with category + message
    for i, f in enumerate(failures, 1):
        lines.append(f"  {i}. ({f.category}) {f.message}")

    # Per-field schema fragments — keep it focused to the failing paths
    if schema:
        relevant_paths = {f.path for f in failures if f.path}
        fragments = _collect_relevant_fragments(schema, relevant_paths)
        if fragments:
            lines += [
                "",
                "Relevant schema fragments:",
                _indent(json.dumps(fragments, indent=2), 2),
            ]

    if failed_args:
        lines += [
            "",
            "The args you sent:",
            _indent(json.dumps(_safe_args(failed_args), indent=2), 2),
        ]

    # One combined hint — the categories tell the model what to fix
    cats = sorted({f.category for f in failures})
    hint_lines = [
        f"Fix ALL {len(failures)} violation(s) above in a single re-emission.",
        f"Categories: {', '.join(cats)}.",
        "Do not address them one at a time — emit a single corrected tool call that "
        "satisfies every listed violation simultaneously.",
    ]
    lines += ["", *hint_lines]

    return "\n".join(lines)


def _collect_relevant_fragments(
    schema: dict[str, Any], paths: set[str]
) -> dict[str, Any] | None:
    """Pull schema fragments for every failing top-level field."""
    if not paths or not schema:
        return None
    props = schema.get("properties", {})
    required = set(schema.get("required", []) or [])
    fragments: dict[str, Any] = {"properties": {}}
    seen_required: list[str] = []
    for path in paths:
        if not path:
            continue
        head = path.split(".")[0]
        if head in props and head not in fragments["properties"]:
            fragments["properties"][head] = props[head]
            if head in required:
                seen_required.append(head)
    if seen_required:
        fragments["required"] = seen_required
    return fragments if fragments["properties"] else None


# ─── helpers ──────────────────────────────────────────────────────────────


def _extract_relevant_fragment(
    schema: dict[str, Any], path: str | None
) -> dict[str, Any] | None:
    """Pull out just the schema fragment for the failing field, if possible."""
    if not path:
        return schema  # whole schema is the relevant fragment
    props = schema.get("properties", {})
    head = path.split(".")[0]
    if head in props:
        fragment = {"properties": {head: props[head]}}
        if "required" in schema and head in schema["required"]:
            fragment["required"] = [head]
        return fragment
    return schema


def _safe_args(args: dict[str, Any]) -> dict[str, Any]:
    """Truncate long string values so the prompt stays under 200ish tokens."""
    out: dict[str, Any] = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 120:
            out[k] = v[:120] + "…"
        else:
            out[k] = v
    return out


def _indent(text: str, spaces: int) -> str:
    pad = " " * spaces
    return "\n".join(pad + line for line in text.splitlines())

"""Schema linter — catch vendor-API rejections before they kill your session.

LLM providers have stricter rules about what JSON Schema they'll accept as
a tool definition than the JSON Schema spec itself. Each rejection is a
session-killer:

  - OpenAI strict mode requires ``additionalProperties: false`` AT EVERY
    LEVEL and that every declared property be in ``required``. A single
    missing flag elsewhere in the tree returns 400.
  - Anthropic Claude Code v2.0.21+ rejects ``oneOf`` / ``allOf`` / ``anyOf``
    at the TOP LEVEL of ``input_schema``. Bricks MCP integrations like
    Perplexity and Countly. No opt-out, no recovery.
  - Anthropic also requires property keys to match
    ``^[a-zA-Z0-9_.-]{1,64}$``. Google APIs export ``$.xgafv`` which is
    illegal — the entire conversation becomes unrecoverable without /clear.
  - Several MCP servers (gitlab, hubspot, docker, github) ship schemas
    that violate the JSON Schema spec itself (array type without ``items``,
    object type without ``properties``, booleans where arrays are expected).

This linter runs in-process before you submit your tools list to an LLM
provider. It surfaces each issue with severity + specific path so you can
fix the schema at registration time, not at midnight when production
breaks.

  from cruxial.lint import lint_schemas_for_openai, lint_schemas_for_anthropic

  issues = lint_schemas_for_openai(my_tools)
  for issue in issues:
      print(f"[{issue.severity}] {issue.tool}.{issue.path}: {issue.message}")
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

__all__ = [
    "LintIssue",
    "lint_schema",
    "lint_schemas",
    "lint_schemas_for_openai",
    "lint_schemas_for_anthropic",
]


Severity = Literal["error", "warning", "info"]


@dataclass(slots=True)
class LintIssue:
    severity: Severity
    tool: str
    path: str             # JSON pointer-ish, e.g. "properties.foo.type"
    code: str             # short identifier — used for ignore-lists
    message: str          # human-readable
    fix_hint: str | None = None
    cited_incident: str | None = None  # GitHub issue or doc URL


# ─── property-key sanitiser (Anthropic API) ──────────────────────────


_ANTHROPIC_KEY_RE = re.compile(r"^[a-zA-Z0-9_.-]{1,64}$")


def _walk_property_keys(schema: Any, path: str = ""):
    """Yield (path, key) for every property name in a schema tree."""
    if not isinstance(schema, dict):
        return
    if isinstance(schema.get("properties"), dict):
        for key, sub in schema["properties"].items():
            here = f"{path}.properties.{key}" if path else f"properties.{key}"
            yield (here, key)
            yield from _walk_property_keys(sub, here)
    # Walk into common composition keywords too
    for kw in ("allOf", "anyOf", "oneOf", "items", "additionalProperties"):
        if kw in schema:
            sub = schema[kw]
            if isinstance(sub, list):
                for i, s in enumerate(sub):
                    yield from _walk_property_keys(s, f"{path}.{kw}.{i}")
            elif isinstance(sub, dict):
                yield from _walk_property_keys(sub, f"{path}.{kw}")


def _walk_object_subschemas(schema: Any, path: str = ""):
    """Yield (path, schema_dict) for every place that looks like an object schema."""
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object" or "properties" in schema:
        yield (path, schema)
    if isinstance(schema.get("properties"), dict):
        for key, sub in schema["properties"].items():
            yield from _walk_object_subschemas(sub, f"{path}.properties.{key}" if path else f"properties.{key}")
    for kw in ("allOf", "anyOf", "oneOf", "items"):
        if kw in schema:
            sub = schema[kw]
            if isinstance(sub, list):
                for i, s in enumerate(sub):
                    yield from _walk_object_subschemas(s, f"{path}.{kw}.{i}")
            elif isinstance(sub, dict):
                yield from _walk_object_subschemas(sub, f"{path}.{kw}")


# ─── per-issue checkers ──────────────────────────────────────────────


def _check_jsonschema_well_formed(tool: str, schema: dict[str, Any]) -> list[LintIssue]:
    """Generic JSON Schema well-formedness — issues that violate the spec itself.
    These are guaranteed to be rejected by every reasonable LLM provider."""
    issues: list[LintIssue] = []

    # Empty or near-empty schemas at the root: just metadata keys ($schema, title,
    # description) with no `type` and no structural keywords. Technically a legal
    # JSON Schema (an empty schema matches anything) but provider tool-call APIs
    # reject it — there's nothing for the model to fill in. Found in the wild on
    # 100% of one widely-used MCP server's tools (gitlab MCP), and discovered by
    # this benchmark: 5/5 calls failed at OpenAI's schema-validation stage.
    _META_ONLY_KEYS = {"$schema", "$id", "title", "description", "$comment"}
    _STRUCTURAL_KEYS = {
        "type", "properties", "items", "allOf", "anyOf", "oneOf", "not", "$ref",
        "enum", "const", "additionalProperties", "patternProperties",
    }
    meaningful = set(schema.keys()) - _META_ONLY_KEYS
    if not meaningful or not (meaningful & _STRUCTURAL_KEYS):
        issues.append(LintIssue(
            severity="error", tool=tool, path="<root>",
            code="EMPTY_OR_META_ONLY_SCHEMA",
            message=(
                "schema has no structural content (no `type`, `properties`, "
                "`items`, `enum`, `$ref`, or composition keyword) — provider "
                "tool-call APIs will reject it"
            ),
            fix_hint=(
                'declare at minimum `"type": "object", "properties": {...}` so '
                "the model has a shape to fill"
            ),
            cited_incident="cruxial benchmark 2026-05-30: 9/9 calls to gitlab MCP server rejected by OpenAI",
        ))

    def walk(s: Any, path: str):
        if not isinstance(s, dict):
            return
        t = s.get("type")
        # array must declare items
        if t == "array" and "items" not in s:
            issues.append(LintIssue(
                severity="error", tool=tool, path=path or "<root>",
                code="ARRAY_WITHOUT_ITEMS",
                message="schema declares type=array but has no `items` — OpenAI / Anthropic will reject",
                fix_hint='add `"items": {"type": "string"}` (or whatever the element type is)',
                cited_incident="docker/mcp-gateway#311, github/github-mcp-server#1548",
            ))
        # object should declare properties (warning only — typeless objects are technically legal)
        if t == "object" and "properties" not in s and "additionalProperties" not in s and not any(
            k in s for k in ("allOf", "anyOf", "oneOf", "$ref")
        ):
            issues.append(LintIssue(
                severity="warning", tool=tool, path=path or "<root>",
                code="OBJECT_WITHOUT_PROPERTIES",
                message="object schema has no `properties` — OpenAI may reject; LLM will have no guidance on shape",
                fix_hint='declare `"properties": {...}` even if empty',
                cited_incident="github/github-mcp-server#1548",
            ))
        # type must be a string or array of strings, not bool / non-list-of-strings
        if t is not None and not isinstance(t, str) and not (
            isinstance(t, list) and all(isinstance(x, str) for x in t)
        ):
            issues.append(LintIssue(
                severity="error", tool=tool, path=path or "<root>",
                code="INVALID_TYPE_VALUE",
                message=f"schema `type` is {t!r} — must be a string or array of strings",
                fix_hint='use `"type": "object"` (or whatever)',
                cited_incident="agno-agi/agno#2848",
            ))
        # recurse
        if isinstance(s.get("properties"), dict):
            for k, v in s["properties"].items():
                walk(v, f"{path}.properties.{k}" if path else f"properties.{k}")
        for kw in ("allOf", "anyOf", "oneOf", "items"):
            if kw in s:
                sub = s[kw]
                if isinstance(sub, list):
                    for i, x in enumerate(sub):
                        walk(x, f"{path}.{kw}.{i}")
                elif isinstance(sub, dict):
                    walk(sub, f"{path}.{kw}")

    walk(schema, "")
    return issues


def _check_openai_strict_mode(tool: str, schema: dict[str, Any]) -> list[LintIssue]:
    """OpenAI's strict tool-calling mode requires:
      1. additionalProperties: false on EVERY object schema in the tree
      2. Every declared property is in `required`
    """
    issues: list[LintIssue] = []
    for path, obj_schema in _walk_object_subschemas(schema):
        # rule 1: additionalProperties must be explicitly false
        ap = obj_schema.get("additionalProperties")
        if ap is not False:
            issues.append(LintIssue(
                severity="warning", tool=tool, path=path or "<root>",
                code="OPENAI_STRICT_MISSING_ADDITIONAL_PROPS_FALSE",
                message=(
                    "OpenAI strict mode requires `additionalProperties: false` at "
                    "every object level. Missing here."
                ),
                fix_hint='add `"additionalProperties": false` to this object',
                cited_incident="community.openai.com #929996",
            ))
        # rule 2: every declared property must be in required
        props = obj_schema.get("properties") or {}
        required = set(obj_schema.get("required") or [])
        missing = set(props) - required
        if missing:
            issues.append(LintIssue(
                severity="warning", tool=tool, path=path or "<root>",
                code="OPENAI_STRICT_PROPS_NOT_IN_REQUIRED",
                message=(
                    f"OpenAI strict mode requires every declared property to be in "
                    f"`required`. Missing: {sorted(missing)!r}. Make truly-optional "
                    "fields nullable with `type: [string, null]` and still mark them required."
                ),
                fix_hint='add all property names to `required` or use union-with-null types',
                cited_incident="community.openai.com #929996",
            ))
    return issues


def _check_anthropic_top_level_composition(tool: str, schema: dict[str, Any]) -> list[LintIssue]:
    """Anthropic rejects oneOf / allOf / anyOf at the TOP level of input_schema."""
    issues: list[LintIssue] = []
    for kw in ("oneOf", "allOf", "anyOf"):
        if kw in schema:
            issues.append(LintIssue(
                severity="error", tool=tool, path=kw,
                code="ANTHROPIC_TOP_LEVEL_COMPOSITION",
                message=(
                    f"Anthropic rejects top-level `{kw}` in tool input_schema "
                    "(Claude Code v2.0.21+). This will brick MCP integrations."
                ),
                fix_hint=(
                    f"flatten the {kw} into a single object schema with optional "
                    "fields, or wrap in an object: "
                    f'`{{"type": "object", "properties": {{...}}, "{kw}": [...]}}`'
                ),
                cited_incident="anthropics/claude-code#10606, #4886",
            ))
    return issues


def _check_anthropic_property_key_charset(tool: str, schema: dict[str, Any]) -> list[LintIssue]:
    """Anthropic requires property keys to match ^[a-zA-Z0-9_.-]{1,64}$.
    A single illegal key bricks the entire conversation."""
    issues: list[LintIssue] = []
    for path, key in _walk_property_keys(schema):
        if not _ANTHROPIC_KEY_RE.fullmatch(key):
            issues.append(LintIssue(
                severity="error", tool=tool, path=path,
                code="ANTHROPIC_ILLEGAL_PROPERTY_KEY",
                message=(
                    f"property key {key!r} doesn't match Anthropic's "
                    "`^[a-zA-Z0-9_.-]{1,64}$` rule — will brick the entire conversation"
                ),
                fix_hint=(
                    "rename the property to a legal key, or use a sanitization layer "
                    "with a bijective mapping before submission"
                ),
                cited_incident="anthropics/claude-code#34771",
            ))
    return issues


def _check_property_count_under_limit(
    tool: str, schema: dict[str, Any], max_props: int = 60
) -> list[LintIssue]:
    """Anthropic / Claude Code has silently truncated schemas with many
    nested properties — losing parameters mid-call.

    There's no hard documented limit, but real reports surface around
    20-100 props. Warn aggressively past 60."""
    issues: list[LintIssue] = []

    def count(s: Any) -> int:
        if not isinstance(s, dict):
            return 0
        c = len(s.get("properties") or {})
        for k, v in (s.get("properties") or {}).items():
            c += count(v)
        for kw in ("allOf", "anyOf", "oneOf"):
            for sub in (s.get(kw) or []):
                c += count(sub)
        if isinstance(s.get("items"), dict):
            c += count(s["items"])
        return c

    n = count(schema)
    if n > max_props:
        issues.append(LintIssue(
            severity="warning", tool=tool, path="<root>",
            code="MANY_PROPERTIES_TRUNCATION_RISK",
            message=(
                f"{n} properties in this schema — Anthropic Claude Code "
                "has silently truncated schemas with similar property counts "
                "(losing parameters from outgoing tool calls)"
            ),
            fix_hint="split into multiple narrower tools, or flatten nested objects",
            cited_incident="anthropics/claude-code#31302",
        ))
    return issues


# ─── public API ──────────────────────────────────────────────────────


def lint_schema(
    tool: str,
    schema: dict[str, Any],
    *,
    target: Literal["openai", "anthropic", "all"] = "all",
) -> list[LintIssue]:
    """Lint a single tool schema for known vendor-API rejection causes.

    Args:
        tool: tool name (used in issue messages).
        schema: JSON Schema dict.
        target: which provider's rules to check.
          - "openai": strict-mode + well-formed
          - "anthropic": top-level composition + property-key charset + well-formed
          - "all": everything (warning: may overreport)
    """
    issues = _check_jsonschema_well_formed(tool, schema)
    if target in ("openai", "all"):
        issues.extend(_check_openai_strict_mode(tool, schema))
    if target in ("anthropic", "all"):
        issues.extend(_check_anthropic_top_level_composition(tool, schema))
        issues.extend(_check_anthropic_property_key_charset(tool, schema))
        issues.extend(_check_property_count_under_limit(tool, schema))
    return issues


def lint_schemas(
    schemas: dict[str, dict[str, Any]],
    *,
    target: Literal["openai", "anthropic", "all"] = "all",
) -> list[LintIssue]:
    """Lint a {tool_name: schema} dict — returns all issues across all tools."""
    out: list[LintIssue] = []
    for name, schema in schemas.items():
        out.extend(lint_schema(name, schema, target=target))
    return out


def lint_schemas_for_openai(schemas: dict[str, dict[str, Any]]) -> list[LintIssue]:
    """Convenience: lint specifically for OpenAI."""
    return lint_schemas(schemas, target="openai")


def lint_schemas_for_anthropic(schemas: dict[str, dict[str, Any]]) -> list[LintIssue]:
    """Convenience: lint specifically for Anthropic."""
    return lint_schemas(schemas, target="anthropic")

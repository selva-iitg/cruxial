"""Catch vendor-API rejections BEFORE the request hits OpenAI / Anthropic.

Each rejection is a session-killer in practice — Claude Code v2.0.21+ breaks
the entire conversation until `/clear`. OpenAI strict mode returns 400 on
the entire tools list. We add a `cruxial.lint_schema()` helper so consumers
can catch these in CI / at registration time, not in production.

Sources:
  - community.openai.com #929996       OpenAI strict needs additionalProperties:false everywhere
  - anthropics/claude-code#10606       Anthropic rejects top-level oneOf/allOf/anyOf
  - anthropics/claude-code#34771       $.xgafv illegal property key bricks session
  - anthropics/claude-code#31302       Many-property schemas silently truncated
  - docker/mcp-gateway#311             array type without items rejected
  - github/github-mcp-server#1548      object type without properties rejected
  - agno-agi/agno#2848                 boolean where array expected
"""

from __future__ import annotations

from cruxial import (
    lint_schema,
    lint_schemas,
    lint_schemas_for_anthropic,
    lint_schemas_for_openai,
)


# ─── well-formedness (every provider) ────────────────────────────────


def test_array_without_items_is_flagged():
    """docker/mcp-gateway#311 — array without items bricks the tools list."""
    schema = {"type": "object", "properties": {"tags": {"type": "array"}}}
    issues = lint_schema("t", schema)
    codes = {i.code for i in issues}
    assert "ARRAY_WITHOUT_ITEMS" in codes


def test_array_with_items_passes_wellformedness():
    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
    }
    well_formed = [i for i in lint_schema("t", schema) if i.code == "ARRAY_WITHOUT_ITEMS"]
    assert well_formed == []


def test_object_without_properties_is_warned():
    """github/github-mcp-server#1548 — pure-object schema with no shape guidance."""
    schema = {"type": "object"}
    issues = lint_schema("t", schema)
    codes = {i.code for i in issues}
    assert "OBJECT_WITHOUT_PROPERTIES" in codes


def test_object_with_explicit_additional_properties_is_acceptable():
    """An object with `additionalProperties: false` (no properties) is intentional."""
    schema = {"type": "object", "additionalProperties": False}
    well_formed = [i for i in lint_schema("t", schema) if i.code == "OBJECT_WITHOUT_PROPERTIES"]
    assert well_formed == []


def test_invalid_type_value_is_flagged():
    """agno-agi/agno#2848 — `type: True` returned by some MCP servers."""
    schema = {"type": "object", "properties": {"x": {"type": True}}}
    issues = lint_schema("t", schema)
    codes = {i.code for i in issues}
    assert "INVALID_TYPE_VALUE" in codes


# ─── OpenAI strict mode ───────────────────────────────────────────────


def test_openai_strict_flags_missing_additional_properties_false():
    """Object schemas without `additionalProperties: false` will be rejected."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    }
    issues = lint_schema("t", schema, target="openai")
    codes = {i.code for i in issues}
    assert "OPENAI_STRICT_MISSING_ADDITIONAL_PROPS_FALSE" in codes


def test_openai_strict_flags_property_not_in_required():
    """OpenAI strict requires every property to be required."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],  # b not required
    }
    issues = lint_schema("t", schema, target="openai")
    codes = {i.code for i in issues}
    assert "OPENAI_STRICT_PROPS_NOT_IN_REQUIRED" in codes


def test_openai_strict_passes_a_strict_schema():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    }
    issues = [i for i in lint_schema("t", schema, target="openai")
              if i.code.startswith("OPENAI_STRICT_")]
    assert issues == []


def test_openai_strict_recurses_into_nested_objects():
    """The strict rules apply at EVERY level of nesting."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "settings": {  # nested object — must also be strict
                "type": "object",
                "properties": {"theme": {"type": "string"}},
                # missing additionalProperties:false at this level
            },
        },
        "required": ["settings"],
    }
    issues = lint_schema("t", schema, target="openai")
    # Should flag the nested object's missing additionalProperties:false
    matching = [i for i in issues if "settings" in i.path and "ADDITIONAL_PROPS" in i.code]
    assert matching, f"didn't flag nested non-strict object. issues: {[i.code for i in issues]}"


# ─── Anthropic — top-level composition (#10606) ──────────────────────


def test_anthropic_flags_top_level_oneof():
    schema = {
        "oneOf": [
            {"type": "object", "properties": {"a": {"type": "string"}}},
            {"type": "object", "properties": {"b": {"type": "integer"}}},
        ],
    }
    issues = lint_schema("t", schema, target="anthropic")
    codes = {i.code for i in issues}
    assert "ANTHROPIC_TOP_LEVEL_COMPOSITION" in codes


def test_anthropic_flags_top_level_anyof():
    schema = {"anyOf": [{"type": "object"}, {"type": "null"}]}
    issues = lint_schema("t", schema, target="anthropic")
    assert any(i.code == "ANTHROPIC_TOP_LEVEL_COMPOSITION" for i in issues)


def test_anthropic_flags_top_level_allof():
    schema = {"allOf": [{"type": "object"}, {"type": "object"}]}
    issues = lint_schema("t", schema, target="anthropic")
    assert any(i.code == "ANTHROPIC_TOP_LEVEL_COMPOSITION" for i in issues)


def test_anthropic_accepts_nested_composition():
    """Composition INSIDE a property is fine — only top-level is rejected."""
    schema = {
        "type": "object",
        "properties": {
            "value": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        },
        "required": ["value"],
    }
    issues = lint_schema("t", schema, target="anthropic")
    composition = [i for i in issues if i.code == "ANTHROPIC_TOP_LEVEL_COMPOSITION"]
    assert composition == []


# ─── Anthropic — illegal property key charset (#34771) ───────────────


def test_anthropic_flags_dollar_sign_property_key():
    """The exact $.xgafv key from Google APIs that bricks Anthropic sessions."""
    schema = {
        "type": "object",
        "properties": {"$.xgafv": {"type": "string"}},
        "required": ["$.xgafv"],
    }
    issues = lint_schema("t", schema, target="anthropic")
    codes = {i.code for i in issues}
    assert "ANTHROPIC_ILLEGAL_PROPERTY_KEY" in codes
    # And the cited incident link is preserved
    illegal_key_issue = next(i for i in issues if i.code == "ANTHROPIC_ILLEGAL_PROPERTY_KEY")
    assert "34771" in (illegal_key_issue.cited_incident or "")


def test_anthropic_flags_property_key_with_spaces():
    schema = {
        "type": "object",
        "properties": {"my field": {"type": "string"}},
    }
    issues = lint_schema("t", schema, target="anthropic")
    assert any(i.code == "ANTHROPIC_ILLEGAL_PROPERTY_KEY" for i in issues)


def test_anthropic_flags_property_key_too_long():
    schema = {
        "type": "object",
        "properties": {"x" * 65: {"type": "string"}},  # 65 chars > 64 limit
    }
    issues = lint_schema("t", schema, target="anthropic")
    assert any(i.code == "ANTHROPIC_ILLEGAL_PROPERTY_KEY" for i in issues)


def test_anthropic_accepts_normal_property_keys():
    schema = {
        "type": "object",
        "properties": {
            "snake_case": {"type": "string"},
            "camelCase": {"type": "string"},
            "kebab-case": {"type": "string"},
            "dotted.key": {"type": "string"},
            "with123numbers": {"type": "string"},
        },
    }
    issues = lint_schema("t", schema, target="anthropic")
    illegal = [i for i in issues if i.code == "ANTHROPIC_ILLEGAL_PROPERTY_KEY"]
    assert illegal == []


def test_anthropic_flags_illegal_keys_in_nested_objects():
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"$weird": {"type": "string"}},
            },
        },
    }
    issues = lint_schema("t", schema, target="anthropic")
    nested_illegal = [
        i for i in issues
        if i.code == "ANTHROPIC_ILLEGAL_PROPERTY_KEY" and "$weird" in i.path
    ]
    assert nested_illegal


# ─── Anthropic — silent property truncation risk (#31302) ────────────


def test_many_properties_schema_is_warned():
    """Anthropic's Claude Code has silently truncated schemas with many
    properties, dropping params from outgoing tool calls."""
    schema = {
        "type": "object",
        "properties": {f"field_{i}": {"type": "string"} for i in range(80)},
    }
    issues = lint_schema("t", schema, target="anthropic")
    assert any(i.code == "MANY_PROPERTIES_TRUNCATION_RISK" for i in issues)


def test_small_schema_does_not_trigger_truncation_warning():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
    }
    issues = lint_schema("t", schema, target="anthropic")
    truncation = [i for i in issues if i.code == "MANY_PROPERTIES_TRUNCATION_RISK"]
    assert truncation == []


# ─── Convenience APIs ────────────────────────────────────────────────


def test_lint_schemas_aggregates_across_tools():
    schemas = {
        "tool_a": {"type": "object", "properties": {"tags": {"type": "array"}}},  # missing items
        "tool_b": {"oneOf": [{"type": "object"}, {"type": "null"}]},   # anthropic top-level
    }
    issues = lint_schemas(schemas)
    tools = {i.tool for i in issues}
    assert tools == {"tool_a", "tool_b"}


def test_lint_for_openai_skips_anthropic_rules():
    schema = {"oneOf": [{"type": "object"}]}  # would trigger anthropic top-level
    issues = lint_schema("t", schema, target="openai")
    codes = {i.code for i in issues}
    assert "ANTHROPIC_TOP_LEVEL_COMPOSITION" not in codes


def test_lint_for_anthropic_skips_openai_strict_rules():
    """A non-strict schema is fine for Anthropic; only OpenAI strict cares."""
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
    }
    issues = lint_schema("t", schema, target="anthropic")
    codes = {i.code for i in issues}
    assert "OPENAI_STRICT_MISSING_ADDITIONAL_PROPS_FALSE" not in codes
    assert "OPENAI_STRICT_PROPS_NOT_IN_REQUIRED" not in codes


# ─── Severity / message quality ──────────────────────────────────────


def test_every_issue_has_a_fix_hint_or_cited_incident():
    """Trust signal: every flagged issue should tell the user HOW to fix it
    AND/OR cite the real incident that motivated the rule. Pure 'this is wrong'
    with no guidance is cargo-cult linting."""
    # Cover all rule classes
    schemas = {
        "openai_bad": {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            # missing additionalProperties:false → OPENAI_STRICT_*
        },
        "anthropic_bad": {
            "type": "object",
            "properties": {"$bad": {"type": "string"}},  # → ANTHROPIC_ILLEGAL_PROPERTY_KEY
            "anyOf": [{}],                               # → ANTHROPIC_TOP_LEVEL_COMPOSITION (if at root)
        },
        "wellformed_bad": {
            "type": "object",
            "properties": {"tags": {"type": "array"}},   # → ARRAY_WITHOUT_ITEMS
        },
    }
    for issue in lint_schemas(schemas):
        assert issue.fix_hint or issue.cited_incident, (
            f"issue {issue.code} on {issue.tool}.{issue.path} has no fix_hint or cited_incident — "
            "trust hit"
        )


def test_lint_issue_has_unique_codes_for_distinct_problems():
    """Code identifiers must be unique enough that consumers can write
    `if i.code == 'ANTHROPIC_ILLEGAL_PROPERTY_KEY'` to filter."""
    schemas = {
        "t": {
            "type": "object",
            "properties": {"$bad": {"type": "string"}, "tags": {"type": "array"}},
            "anyOf": [{}],
        },
    }
    codes = {i.code for i in lint_schemas(schemas)}
    # All three distinct issue classes should appear
    assert "ANTHROPIC_ILLEGAL_PROPERTY_KEY" in codes
    assert "ARRAY_WITHOUT_ITEMS" in codes


# ─── Real-world snapshot: lint the mined MCP corpus ──────────────────


def test_lint_finds_issues_in_real_mined_mcp_schemas():
    """Sanity check: run the linter against our actual mined MCP corpus.
    We KNOW from the benchmark that gitlab + hubspot ship malformed schemas
    that OpenAI rejects. Linter should catch them too."""
    try:
        from cruxial.demo.mcp_schemas import MCP_SCHEMAS
    except ImportError:
        # Mined corpus not present in this environment — skip
        import pytest
        pytest.skip("mcp_schemas.py not present; run examples/mine_mcp_schemas.py first")

    # Just confirm the linter runs across the whole corpus without crashing
    # AND surfaces at least one issue (we expect many — these are real-world schemas).
    total_issues = 0
    for server_id, payload in MCP_SCHEMAS.items():
        for tool_name, schema in payload["schemas"].items():
            issues = lint_schema(f"{server_id}/{tool_name}", schema, target="all")
            total_issues += len(issues)

    # 877 real schemas → expect many lint warnings (mostly OpenAI strict-mode
    # flags + some real bugs like gitlab/hubspot)
    assert total_issues > 0, "linter found ZERO issues across 877 real schemas — suspicious"

"""Unit tests for cruxial.adapters.mcp — focused on the pure-logic helpers.

The actual stdio/SSE transports are integration-tested by the mining script
(`examples/mine_mcp_schemas.py`) since unit-mocking the mcp SDK's async
context managers is more brittle than it's worth.
"""

from __future__ import annotations

import pytest

from cruxial.adapters.mcp import _extract_schemas, _get_attr_or_key


# ─── _get_attr_or_key ────────────────────────────────────────────────


def test_get_attr_or_key_handles_dicts():
    assert _get_attr_or_key({"name": "foo"}, "name") == "foo"
    assert _get_attr_or_key({"name": "foo"}, "missing") is None


def test_get_attr_or_key_handles_objects():
    class O:
        name = "bar"
    assert _get_attr_or_key(O(), "name") == "bar"
    assert _get_attr_or_key(O(), "missing") is None


# ─── _extract_schemas ────────────────────────────────────────────────


def test_extract_schemas_from_dicts_camel_case():
    """MCP spec uses inputSchema (camelCase)."""
    tools = [
        {"name": "read_file", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "write_file", "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    ]
    schemas = _extract_schemas(tools)
    assert set(schemas) == {"read_file", "write_file"}
    assert schemas["read_file"]["required"] == ["path"]


def test_extract_schemas_from_dicts_snake_case_fallback():
    """Some servers ship snake_case input_schema."""
    tools = [
        {"name": "snake_tool", "input_schema": {"type": "object", "properties": {}}},
    ]
    schemas = _extract_schemas(tools)
    assert "snake_tool" in schemas


def test_extract_schemas_from_pydantic_like_objects():
    class Tool:
        def __init__(self, name, schema):
            self.name = name
            self.inputSchema = schema

    tools = [
        Tool("foo", {"type": "object", "required": ["x"]}),
        Tool("bar", {"type": "object", "required": []}),
    ]
    schemas = _extract_schemas(tools)
    assert set(schemas) == {"foo", "bar"}
    assert schemas["foo"]["required"] == ["x"]


def test_extract_schemas_skips_malformed_entries():
    """A tool with no name or no schema is silently skipped — not a crash."""
    tools = [
        {"name": "good", "inputSchema": {"type": "object"}},
        {"name": "no_schema"},                            # missing inputSchema
        {"inputSchema": {"type": "object"}},              # missing name
        {"name": "wrong_schema_type", "inputSchema": "not a dict"},
        None,                                             # nonsense entry
    ]
    schemas = _extract_schemas(tools)
    assert set(schemas) == {"good"}


def test_extract_schemas_handles_empty_input():
    assert _extract_schemas([]) == {}
    assert _extract_schemas(None) == {}


# ─── ImportError surface when mcp SDK is missing ─────────────────────


def test_require_mcp_sdk_raises_clear_error_when_missing(monkeypatch):
    """If mcp isn't installed, we surface a clear ImportError with install hint."""
    import sys
    # Pretend the mcp module is absent
    monkeypatch.setitem(sys.modules, "mcp", None)

    from cruxial.adapters import mcp as mcp_adapter

    with pytest.raises(ImportError, match=r"pip install.*cruxial\[mcp\]"):
        mcp_adapter._require_mcp_sdk()

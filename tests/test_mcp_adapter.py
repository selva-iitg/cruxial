"""Unit tests for cruxial.adapters.mcp — focused on the pure-logic helpers.

The actual stdio/SSE transports are integration-tested by the mining script
(`examples/mine_mcp_schemas.py`) since unit-mocking the mcp SDK's async
context managers is more brittle than it's worth.
"""

from __future__ import annotations

import pytest

from cruxial.adapters.mcp import (
    _extract_schemas,
    _get_attr_or_key,
    _validate_args,
    _validate_command,
    import_server_stdio_sync,
)


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


# ─── Subprocess command/args validation ──────────────────────────────
#
# The MCP stdio transport spawns child processes. We harden the trust
# boundary at the cruxial adapter layer: invalid types, empty strings,
# and shell metacharacters are rejected BEFORE we ever hand the values
# to the MCP SDK / subprocess. None of these tests spawn a real process.


class TestValidateCommand:
    """Defense-in-depth tests for _validate_command."""

    def test_accepts_simple_command(self):
        _validate_command("npx")
        _validate_command("uvx")
        _validate_command("/usr/bin/python3")
        _validate_command("python")

    def test_accepts_command_with_spaces_in_path(self):
        # spaces are not metacharacters — paths like "/Applications/My App/bin/x"
        # are valid since args are passed via execvp positionally
        _validate_command("/Applications/My App/bin/server")

    def test_rejects_none(self):
        with pytest.raises(TypeError, match="must be a str"):
            _validate_command(None)

    def test_rejects_list(self):
        with pytest.raises(TypeError, match="must be a str"):
            _validate_command(["npx", "-y"])

    def test_rejects_int(self):
        with pytest.raises(TypeError, match="got int"):
            _validate_command(42)

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_command("")

    def test_rejects_whitespace_only(self):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_command("   \t\n  ")

    @pytest.mark.parametrize(
        "bad",
        [
            "npx | tee log",      # pipe
            "npx ; rm -rf /",     # semicolon
            "npx && other",       # logical-and
            "npx `whoami`",       # command substitution (backtick)
            "npx $(whoami)",      # command substitution
            "npx > out.txt",      # redirect
            "npx < in.txt",       # redirect
            "npx \n other",       # newline injection
        ],
    )
    def test_rejects_shell_metacharacters(self, bad):
        with pytest.raises(ValueError, match="shell metacharacter"):
            _validate_command(bad)

    def test_error_message_explains_why(self):
        """The error should tell the user why their command is rejected
        and how to legitimately accomplish what they wanted."""
        with pytest.raises(ValueError) as exc:
            _validate_command("npx | head")
        msg = str(exc.value)
        assert "shell" in msg.lower()
        assert "wrapper script" in msg.lower()


class TestValidateArgs:
    """args = positional arguments, passed via execvp — metacharacters OK."""

    def test_accepts_none(self):
        _validate_args(None)

    def test_accepts_empty_list(self):
        _validate_args([])

    def test_accepts_normal_args(self):
        _validate_args(["-y", "@modelcontextprotocol/server-filesystem", "/tmp"])

    def test_accepts_args_with_special_chars(self):
        # Pipes, ampersands etc. ARE valid here — args go through execvp,
        # not a shell. A file path or URL might legitimately contain them.
        _validate_args(["--url", "https://api.example.com/?key=abc&v=2", "/tmp/dir|with|bars"])

    def test_rejects_non_list(self):
        with pytest.raises(TypeError, match="must be a list"):
            _validate_args("not a list")

    def test_rejects_non_string_element(self):
        with pytest.raises(TypeError, match=r"args\[1\] must be str"):
            _validate_args(["-y", 42, "/tmp"])

    def test_rejects_none_element(self):
        with pytest.raises(TypeError, match=r"args\[0\] must be str"):
            _validate_args([None, "ok"])


# ─── Validation fires at the public entry points ─────────────────────


class TestPublicEntryPointsValidate:
    """The validation must run BEFORE we touch the MCP SDK, so the user
    sees the clean TypeError/ValueError even without `mcp` installed."""

    def test_sync_stdio_entry_validates_before_spawn(self):
        """Bad command never reaches the subprocess spawn — fails fast."""
        with pytest.raises(ValueError, match="shell metacharacter"):
            import_server_stdio_sync(command="npx | tee log")

    def test_sync_stdio_entry_rejects_non_string_command(self):
        with pytest.raises(TypeError, match="must be a str"):
            import_server_stdio_sync(command=["npx", "-y"])  # type: ignore[arg-type]

    def test_sync_stdio_entry_rejects_bad_args(self):
        with pytest.raises(TypeError, match=r"args\[0\] must be str"):
            import_server_stdio_sync(
                command="npx", args=[42, "/tmp"]  # type: ignore[list-item]
            )

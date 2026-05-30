"""MCP (Model Context Protocol) adapter.

Connect to any MCP-compliant server, pull its tool list, and register every
tool's JSON Schema with a Cruxial guard. Works with both transports:

  - **stdio** — the dominant pattern for npm/local servers like
    ``npx @modelcontextprotocol/server-filesystem /tmp``
  - **SSE / HTTP** — for remote MCP servers reachable over HTTPS

Why this matters: MCP exposes tool schemas as part of its protocol, so any
team running an MCP server gets cruxial validation for free — no manual
schema registration, no copy-paste. Two-line integration:

    from cruxial.adapters.mcp import guard_mcp_server_stdio
    cruxial = await guard_mcp_server_stdio(
        command="npx", args=["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
    )
    # cruxial.schemas now has every tool the server exposes

Optional dependency: requires ``pip install cruxial[mcp]``. The import is
lazy — code that doesn't use this module doesn't pay for it.
"""

from __future__ import annotations

import asyncio
from typing import Any

from cruxial.core import Cruxial, GuardConfig, guard
from cruxial.telemetry import Sink


__all__ = [
    "import_server_stdio",
    "import_server_sse",
    "guard_mcp_server_stdio",
    "guard_mcp_server_sse",
    "import_server_stdio_sync",
    "guard_mcp_server_stdio_sync",
]


# ─── async API (preferred) ────────────────────────────────────────────


async def import_server_stdio(
    *,
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, dict[str, Any]]:
    """Spawn a stdio MCP server, list its tools, return ``{name: input_schema}``.

    Does NOT register with a guard — use ``guard_mcp_server_stdio`` for that.
    This is the low-level primitive useful for snapshotting + auditing.

    Args:
        command: Executable to spawn (typically ``npx`` for npm packages,
                 or ``uvx`` / ``python`` for Python servers, or a bare
                 binary path).
        args: Arguments to pass after ``command``.
        env: Environment variables to set for the subprocess. None = inherit.
        timeout_seconds: Total time budget for spawn + initialize + list_tools.
                         If exceeded, the subprocess is terminated and
                         TimeoutError is raised.

    Returns:
        Mapping from tool name to the tool's JSON Schema dict, suitable to
        pass directly as ``schemas=`` to ``cruxial.guard()``.

    Raises:
        ImportError: if ``mcp`` Python SDK isn't installed.
        TimeoutError: if discovery exceeds ``timeout_seconds``.
        RuntimeError: if the server starts but exposes zero tools.
        Whatever the underlying transport raises on connection failure.
    """
    _require_mcp_sdk()
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=command,
        args=list(args or []),
        env=env,
    )

    async def _discover() -> dict[str, dict[str, Any]]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                resp = await session.list_tools()
                return _extract_schemas(resp.tools)

    try:
        return await asyncio.wait_for(_discover(), timeout=timeout_seconds)
    except asyncio.TimeoutError as e:
        raise TimeoutError(
            f"MCP server discovery exceeded {timeout_seconds}s "
            f"(command={command!r}, args={args!r})"
        ) from e


async def import_server_sse(
    *,
    url: str,
    timeout_seconds: float = 30.0,
) -> dict[str, dict[str, Any]]:
    """Connect to a remote SSE-based MCP server, list tools, return schemas.

    Args:
        url: HTTPS URL of the MCP SSE endpoint (e.g.
             ``https://mcp.example.com/sse``).
        timeout_seconds: Budget for connect + initialize + list_tools.

    Returns:
        ``{tool_name: input_schema_dict}``.
    """
    _require_mcp_sdk()
    from mcp import ClientSession
    from mcp.client.sse import sse_client

    async def _discover() -> dict[str, dict[str, Any]]:
        async with sse_client(url) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                resp = await session.list_tools()
                return _extract_schemas(resp.tools)

    try:
        return await asyncio.wait_for(_discover(), timeout=timeout_seconds)
    except asyncio.TimeoutError as e:
        raise TimeoutError(
            f"MCP server discovery exceeded {timeout_seconds}s (url={url!r})"
        ) from e


async def guard_mcp_server_stdio(
    *,
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
    config: GuardConfig | None = None,
    sink: Sink | None = None,
) -> Cruxial:
    """Discover an MCP server's tools and return a Cruxial guard wired to them.

    Validate-only mode (no executors registered — use ``.check()`` /
    ``.knows()``). The intended pattern is: your existing MCP client code
    handles tool execution; Cruxial sits beside it and validates every call.
    """
    schemas = await import_server_stdio(
        command=command, args=args, env=env, timeout_seconds=timeout_seconds,
    )
    if not schemas:
        raise RuntimeError(
            f"MCP server at command={command!r} exposed 0 tools — "
            "nothing to register with cruxial."
        )
    return guard(schemas=schemas, config=config, sink=sink)


async def guard_mcp_server_sse(
    *,
    url: str,
    timeout_seconds: float = 30.0,
    config: GuardConfig | None = None,
    sink: Sink | None = None,
) -> Cruxial:
    """Discover a remote MCP server's tools and return a wired Cruxial guard."""
    schemas = await import_server_sse(url=url, timeout_seconds=timeout_seconds)
    if not schemas:
        raise RuntimeError(
            f"MCP server at {url!r} exposed 0 tools — nothing to register."
        )
    return guard(schemas=schemas, config=config, sink=sink)


# ─── sync convenience wrappers ────────────────────────────────────────


def import_server_stdio_sync(
    *,
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
) -> dict[str, dict[str, Any]]:
    """Synchronous wrapper for ``import_server_stdio``.

    Runs the async coroutine in a fresh event loop. Do NOT call from inside
    an already-running event loop — use the async version instead.
    """
    return asyncio.run(
        import_server_stdio(
            command=command, args=args, env=env, timeout_seconds=timeout_seconds,
        )
    )


def guard_mcp_server_stdio_sync(
    *,
    command: str,
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    timeout_seconds: float = 30.0,
    config: GuardConfig | None = None,
    sink: Sink | None = None,
) -> Cruxial:
    """Synchronous wrapper for ``guard_mcp_server_stdio``."""
    return asyncio.run(
        guard_mcp_server_stdio(
            command=command, args=args, env=env,
            timeout_seconds=timeout_seconds, config=config, sink=sink,
        )
    )


# ─── helpers ──────────────────────────────────────────────────────────


def _require_mcp_sdk() -> None:
    """Lazy-check: the `mcp` SDK is installed."""
    try:
        import mcp  # noqa: F401
    except ImportError as e:
        raise ImportError(
            "cruxial.adapters.mcp requires the `mcp` Python SDK. "
            "Install it with: pip install 'cruxial[mcp]' "
            "(or: pip install mcp)"
        ) from e


def _extract_schemas(tools: Any) -> dict[str, dict[str, Any]]:
    """Normalize MCP Tool objects into ``{name: json_schema_dict}``.

    Handles both raw dicts and pydantic Tool models. Skips any tool whose
    schema is missing or malformed (logs would be nice but we'd need a
    logger dep — caller can wrap with their own logging).
    """
    out: dict[str, dict[str, Any]] = {}
    for t in tools or []:
        name = _get_attr_or_key(t, "name")
        # MCP spec uses camelCase `inputSchema`; some servers ship snake_case.
        schema = (
            _get_attr_or_key(t, "inputSchema")
            or _get_attr_or_key(t, "input_schema")
        )
        if not name or not isinstance(schema, dict):
            continue
        out[name] = schema
    return out


def _get_attr_or_key(obj: Any, key: str) -> Any:
    """Support both pydantic model attributes and plain dict keys."""
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)

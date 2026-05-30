"""Cruxial demo data — hand-crafted tools + mined MCP server schemas.

Two sources of schemas live here:

- **Hand-crafted** (15 tools, 70 prompts) — synthetic but realistic.
  Available as ``cruxial.demo.DEMO_TOOL_SCHEMAS`` / ``DEMO_PROMPTS`` / etc.
- **MCP-mined** (variable count) — real schemas from public MCP servers,
  snapshot via ``examples/mine_mcp_schemas.py``. Available as
  ``cruxial.demo.MCP_SCHEMAS`` if mining has been run.
"""

# Re-export the hand-crafted set so existing imports keep working.
from cruxial._demo_handcrafted import (
    DEMO_ANTHROPIC_TOOLS,
    DEMO_OPENAI_TOOLS,
    DEMO_PROMPTS,
    DEMO_TOOL_DESCRIPTIONS,
    DEMO_TOOL_EXECUTORS,
    DEMO_TOOL_EXECUTORS_SYNC,
    DEMO_TOOL_SCHEMAS,
)

__all__ = [
    "DEMO_TOOL_SCHEMAS",
    "DEMO_TOOL_EXECUTORS",
    "DEMO_TOOL_EXECUTORS_SYNC",
    "DEMO_TOOL_DESCRIPTIONS",
    "DEMO_OPENAI_TOOLS",
    "DEMO_ANTHROPIC_TOOLS",
    "DEMO_PROMPTS",
]

# Lazy re-export of MCP_SCHEMAS — only if mining has produced the file.
try:
    from cruxial.demo.mcp_schemas import MCP_SCHEMAS, by_domain, flat_schemas  # noqa: F401
    __all__ += ["MCP_SCHEMAS", "by_domain", "flat_schemas"]
except ImportError:
    pass

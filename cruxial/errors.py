"""Typed exceptions surfaced to library users.

Every error class can be `except`'d explicitly. None of these are raised
internally by Cruxial's own machinery — that path is always fail-open. They
are raised only when the user explicitly calls `.raise_on_failure()` or
similar opt-in paths.
"""

from __future__ import annotations


class CruxialError(Exception):
    """Base class. except this to catch any Cruxial-raised exception."""


class SchemaViolation(CruxialError):
    """A tool call failed schema validation."""

    def __init__(
        self,
        tool: str,
        category: str,
        message: str,
        path: str | None = None,
    ):
        self.tool = tool
        self.category = category
        self.message = message
        self.path = path
        super().__init__(f"[{category}] {tool}: {message}")


class ToolUnknown(CruxialError):
    """LLM called a tool name not present in the guarded registry."""


class RepairExhausted(CruxialError):
    """Auto-repair tried its max attempts and still failed validation."""


class ExecutorError(CruxialError):
    """The user-provided executor itself raised. Wraps the original."""

    def __init__(self, tool: str, original: BaseException):
        self.tool = tool
        self.original = original
        super().__init__(f"executor for {tool!r} raised {type(original).__name__}: {original}")

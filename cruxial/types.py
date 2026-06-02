"""Core data shapes — no logic, no deps.

These are the values flowing through the interceptor. Kept dependency-free
so users importing only types don't pull in jsonschema or anything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from cruxial.errors import SchemaViolation, ToolUnknown

# The 7 failure categories. Order matters only for stable enumeration.
FailureCategory = Literal[
    "missing_required",
    "type_mismatch",
    "enum_violation",
    "format_violation",
    "constraint_violation",
    "extra_field",
    "unknown_tool",
    "tool_bypass",  # model claimed an action in prose but emitted no matching tool call
]


@dataclass(slots=True)
class Failure:
    """A classified validation failure. Drives repair prompt + telemetry.

    A single tool_call may produce multiple violations (e.g. wrong version
    AND too many replicas AND an enum mismatch). The primary Failure is the
    one returned at .failure; the rest are at .siblings. Both are normal
    Failure objects with their own category/message/path — same level of
    detail, same as_exception(), same telemetry shape.

    The repair prompt builder uses ALL of them (primary + siblings) so the
    model fixes everything in one repair round-trip — eliminates the
    cascade-into-second-error problem.
    """

    category: FailureCategory
    tool: str
    message: str
    path: str | None = None
    expected: Any = None
    received: Any = None
    siblings: list["Failure"] = field(default_factory=list)

    def all_violations(self) -> list["Failure"]:
        """Primary + siblings, in detection order. Always non-empty."""
        return [self] + list(self.siblings)

    def as_exception(self) -> SchemaViolation | ToolUnknown:
        """Convert into the matching typed exception."""
        if self.category == "unknown_tool":
            return ToolUnknown(f"{self.tool}: {self.message}")
        return SchemaViolation(
            tool=self.tool,
            category=self.category,
            message=self.message,
            path=self.path,
        )


@dataclass(slots=True)
class ValidationResult:
    """Outcome of validating a tool call against its schema."""

    ok: bool
    failure: Failure | None = None


@dataclass(slots=True)
class ExecutionResult:
    """Final outcome surfaced to library users."""

    ok: bool
    tool: str
    value: Any = None
    failure: Failure | None = None
    error: BaseException | None = None
    latency_ms: float = 0.0
    repaired: bool = False
    # If repair was attempted, the corrected args that actually ran.
    repaired_args: dict[str, Any] | None = None

    def raise_on_failure(self) -> Any:
        """Convenience: raise the typed exception if not ok, else return value."""
        if not self.ok:
            if self.failure is not None:
                raise self.failure.as_exception()
            if self.error is not None:
                raise self.error
        return self.value


@dataclass(slots=True)
class InterceptionRecord:
    """One row in the telemetry sink. No raw arg values, by design."""

    timestamp: str
    tool: str
    status: Literal["passed", "intercepted", "corrected", "failed", "executor_error"]
    failure_category: FailureCategory | None
    failure_path: str | None
    args_hash: str
    schema_hash: str
    latency_ms: float
    repaired: bool
    cruxial_version: str
    # Whether the registered schema matches what the LLM was actually shown.
    # "model_visible" is the safe default; "canonical" warns of possible
    # false-positive interceptions (see GuardConfig.schema_origin).
    schema_origin: str = "model_visible"
    # Extra context for cloud sink; not used locally.
    extras: dict[str, Any] = field(default_factory=dict)

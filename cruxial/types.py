"""Core data shapes — no logic, no deps.

These are the values flowing through the interceptor. Kept dependency-free
so users importing only types don't pull in jsonschema or anything else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from cruxial.errors import CruxialError, SchemaViolation, ToolUnknown

# Failure categories. Order matters only for stable enumeration.
FailureCategory = Literal[
    "missing_required",
    "type_mismatch",
    "enum_violation",
    "format_violation",
    "constraint_violation",
    "extra_field",
    "unknown_tool",
    "tool_bypass",  # model claimed an action in prose but emitted no matching tool call
    "executor_error",  # the user's executor itself raised (raw exception at .error)
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

    def as_exception(self) -> CruxialError:
        """Convert into the matching typed exception."""
        if self.category == "unknown_tool":
            return ToolUnknown(f"{self.tool}: {self.message}")
        if self.category == "executor_error":
            # The raw exception lives on ExecutionResult.error; raise_on_failure()
            # prefers it. This is the fallback for a direct as_exception() call.
            return CruxialError(f"executor for {self.tool!r} raised: {self.message}")
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

    def __post_init__(self) -> None:
        # Contract: a not-ok result ALWAYS carries a Failure. Executor exceptions
        # set .error (the raw exception) but historically left .failure None, so the
        # natural `result.failure.category` raised AttributeError. Synthesize an
        # ``executor_error`` Failure so `not ok` ⟹ `failure is not None` holds at
        # every call site — the real executor path and no-op mode alike. The raw
        # exception stays on .error for callers that want it.
        if not self.ok and self.failure is None and self.error is not None:
            self.failure = Failure(
                category="executor_error",
                tool=self.tool,
                message=str(self.error) or type(self.error).__name__,
            )

    def raise_on_failure(self) -> Any:
        """Convenience: raise the typed exception if not ok, else return value."""
        if not self.ok:
            # Prefer the raw executor exception — it's more informative than the
            # synthesized executor_error Failure and preserves the original type.
            if self.error is not None:
                raise self.error
            if self.failure is not None:
                raise self.failure.as_exception()
        return self.value

    @property
    def failure_category(self) -> str | None:
        """Why the call is not ok — safe to read without a None check.

        Returns the validation category (``missing_required``, ``type_mismatch``,
        …), or ``"executor_error"`` if the tool itself raised, else ``None``.
        Equivalent to ``self.failure.category`` now that a not-ok result always
        carries a Failure, but stays null-safe on an ok result.
        """
        if self.failure is not None:
            return self.failure.category
        if self.error is not None:
            return "executor_error"
        return None


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

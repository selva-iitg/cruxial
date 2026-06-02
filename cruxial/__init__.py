"""Cruxial — the reliability layer for LLM tool calls."""

from __future__ import annotations

__version__ = "0.1.3"

from cruxial.core import Cruxial, GuardConfig, NoopCruxial, guard
from cruxial.run import RunResult, run
from cruxial.lint import (
    LintIssue,
    lint_schema,
    lint_schemas,
    lint_schemas_for_anthropic,
    lint_schemas_for_openai,
)
from cruxial import demo  # importable as `from cruxial import demo`; module-level access
from cruxial.errors import (
    CruxialError,
    ExecutorError,
    ProviderUnsupported,
    RepairExhausted,
    SchemaViolation,
    ToolUnknown,
)
from cruxial.types import (
    ExecutionResult,
    Failure,
    FailureCategory,
    InterceptionRecord,
    ValidationResult,
)

__all__ = [
    "__version__",
    # Core
    "guard",
    "Cruxial",
    "NoopCruxial",
    "GuardConfig",
    # Managed turn
    "run",
    "RunResult",
    # Schema linter
    "LintIssue",
    "lint_schema",
    "lint_schemas",
    "lint_schemas_for_openai",
    "lint_schemas_for_anthropic",
    # Submodules
    "demo",
    # Types
    "ExecutionResult",
    "ValidationResult",
    "Failure",
    "FailureCategory",
    "InterceptionRecord",
    # Errors
    "CruxialError",
    "SchemaViolation",
    "ToolUnknown",
    "RepairExhausted",
    "ExecutorError",
    "ProviderUnsupported",
]

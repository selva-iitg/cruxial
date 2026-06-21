"""Cruxial — the action layer for AI agents."""

from __future__ import annotations

__version__ = "0.5.2"

from cruxial.core import Cruxial, GuardConfig, NoopCruxial, guard
from cruxial.run import RunResult, arun, run
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
    Operation,
    OpState,
    Receipt,
    ValidationResult,
    Verdict,
)
from cruxial.actions import ActionRegistry, FLAG, HALT, PASS, action, verify
from cruxial.receipts import ReceiptRegistry, http_receipt, id_field, receipt

__all__ = [
    "__version__",
    # Core
    "guard",
    "Cruxial",
    "NoopCruxial",
    "GuardConfig",
    # Managed turn
    "run",
    "arun",
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
    # Action layer (v0.5)
    "action",
    "verify",
    "receipt",
    "PASS",
    "FLAG",
    "HALT",
    "Receipt",
    "Operation",
    "OpState",
    "Verdict",
    "id_field",
    "http_receipt",
    "ReceiptRegistry",
    "ActionRegistry",
    # Errors
    "CruxialError",
    "SchemaViolation",
    "ToolUnknown",
    "RepairExhausted",
    "ExecutorError",
    "ProviderUnsupported",
]

"""The `guard()` primitive — the public 2-line API.

Wraps a tool registry. Every .execute() call validates args against the
captured schema, executes if valid, classifies the failure if not. Repair
is the caller's responsibility (use cruxial.adapters.openai.auto_repair or
similar) — keeps the core dependency-free.

Hard invariant: if Cruxial's own machinery raises, the tool STILL EXECUTES.
A reliability tool that takes down prod is worse than no reliability tool.
This is enforced by `_fail_open` everywhere validation/telemetry runs.
"""

from __future__ import annotations

import time
import warnings
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from cruxial import __version__
from cruxial.classifier import unknown_tool
from cruxial.repair import build_repair_prompt
from cruxial.telemetry import (
    MultiSink,
    NullSink,
    Sink,
    SqliteSink,
    StdoutSink,
    hash_args,
    hash_schema,
    perf_ms_since,
    utc_now,
)
from cruxial.types import ExecutionResult, Failure, InterceptionRecord
from cruxial.validator import validate as _validate


# ─── public surface ─────────────────────────────────────────────────────


@dataclass
class GuardConfig:
    fail_open: bool = True
    strict: bool = True  # if False, construction errors return a no-op guard
    sinks: tuple[str, ...] = ("sqlite",)  # "sqlite", "stdout", "null"
    sqlite_path: str | None = None  # defaults to ~/.cruxial/telemetry.sqlite
    capture_args: bool = False  # if True, store raw args (off by default)
    # What schema you're registering — used to suppress false-positive
    # interception alarms when the LLM saw a trimmed view of the schema.
    #   "model_visible" (default) — the schema you passed is what the LLM
    #                               actually saw. Interceptions are real.
    #   "canonical"               — the LLM may have been shown a trimmed
    #                               schema (fewer fields, looser constraints).
    #                               Cruxial will warn at construction because
    #                               "missing_required" or "extra_field"
    #                               failures may not be the model's fault.
    schema_origin: str = "model_visible"


def guard(
    schemas: Mapping[str, dict[str, Any]],
    executors: Mapping[str, Callable[..., Any]] | None = None,
    *,
    config: GuardConfig | None = None,
    sink: Sink | None = None,
) -> "Cruxial":
    """Wrap a tool registry. Returns a Cruxial instance.

    Args:
        schemas:   {tool_name: json_schema_dict} — the JSON Schema parameters
                   from your tool definitions.
        executors: {tool_name: callable} OR None. If provided, you can call
                   `.execute()` to validate-and-run. If None, only `.check()`
                   is available (validation + telemetry, no execution).
                   This is the right mode for codebases where execution lives
                   inside an existing async/middleware/wrapped path that
                   Cruxial shouldn't take ownership of.
        config:    Optional config knobs.
        sink:      Custom telemetry sink. Tests pass NullSink. If omitted,
                   constructed from config.

    Raises:
        ValueError if both schemas and executors are provided and disagree on
        tool names.
    """
    cfg = config or GuardConfig()

    # schema_origin sanity warning — fires regardless of strict mode because
    # it's a config-correctness signal, not a construction error.
    if cfg.schema_origin == "canonical":
        warnings.warn(
            "cruxial: schema_origin='canonical' — you're validating against the "
            "FULL schema, but the LLM may have been shown a trimmed view (fewer "
            "required fields, looser constraints). 'missing_required' and "
            "'extra_field' interceptions may not be the model's fault. Prefer "
            "registering the schema the LLM actually saw (schema_origin='model_visible', "
            "the default).",
            stacklevel=2,
        )
    elif cfg.schema_origin not in ("model_visible", "canonical"):
        warnings.warn(
            f"cruxial: unknown schema_origin={cfg.schema_origin!r}. "
            "Use 'model_visible' (default) or 'canonical'. Treating as 'model_visible'.",
            stacklevel=2,
        )

    try:
        if executors is None:
            executors = {}  # check-only mode
        else:
            if set(schemas) != set(executors):
                only_schemas = sorted(set(schemas) - set(executors))
                only_executors = sorted(set(executors) - set(schemas))
                raise ValueError(
                    "schemas and executors must declare the same tool names. "
                    f"In schemas only: {only_schemas}. In executors only: {only_executors}."
                )

        if sink is None:
            sink = _build_sink(cfg)

        return Cruxial(
            schemas=dict(schemas),
            executors=dict(executors),
            sink=sink,
            config=cfg,
        )
    except Exception as exc:  # noqa: BLE001 — extend fail-open to construction
        if cfg.strict:
            raise
        warnings.warn(
            f"cruxial: guard() construction failed ({type(exc).__name__}: {exc}). "
            "Returning no-op guard — interception is disabled but app boot is unaffected.",
            stacklevel=2,
        )
        return NoopCruxial()


class Cruxial:
    """The guarded tool registry. Returned by `guard()`."""

    __slots__ = ("schemas", "executors", "sink", "config", "_schema_hashes")

    def __init__(
        self,
        schemas: dict[str, dict[str, Any]],
        executors: dict[str, Callable[..., Any]],
        sink: Sink,
        config: GuardConfig,
    ):
        self.schemas = schemas
        self.executors = executors
        self.sink = sink
        self.config = config
        # Precompute schema hashes once (drift detection later).
        self._schema_hashes: dict[str, str] = {
            name: hash_schema(schema) for name, schema in schemas.items()
        }
        # Publish the registry to the sink so `cruxial stats` can show
        # what's wired up even before any traffic flows. Fail-open: a sink
        # that doesn't implement register_tools just no-ops.
        try:
            if hasattr(sink, "register_tools"):
                sink.register_tools(
                    self._schema_hashes,
                    schema_origin=config.schema_origin,
                )
        except Exception:
            pass

    # ─── public API ─────────────────────────────────────────────────

    def execute(self, name: str, args: dict[str, Any]) -> ExecutionResult:
        """Validate `args` against `name`'s schema, then execute.

        Never raises from Cruxial's internals (when fail_open=True). The
        result object surfaces success/failure typed.
        """
        start_ns = time.perf_counter_ns()
        args = args or {}

        # 1. Unknown tool — pre-validation check.
        if name not in self.schemas:
            failure = unknown_tool(name, list(self.schemas))
            self._record(
                tool=name,
                status="intercepted",
                failure=failure,
                args=args,
                latency_ns=start_ns,
                schema_hash="-",
                repaired=False,
            )
            return ExecutionResult(
                ok=False, tool=name, failure=failure,
                latency_ms=perf_ms_since(start_ns),
            )

        schema = self.schemas[name]

        # 2. Validate. Fail-open: if our own validator crashes, pass through.
        validation = self._fail_open_validate(name, args, schema)

        if validation is not None and not validation.ok:
            failure = validation.failure
            assert failure is not None
            self._record(
                tool=name,
                status="intercepted",
                failure=failure,
                args=args,
                latency_ns=start_ns,
                schema_hash=self._schema_hashes[name],
                repaired=False,
            )
            return ExecutionResult(
                ok=False, tool=name, failure=failure,
                latency_ms=perf_ms_since(start_ns),
            )

        # 3. Execute the user's function.
        try:
            value = self.executors[name](**args)
        except BaseException as exc:  # noqa: BLE001 — surface anything the user's fn does
            self._record(
                tool=name,
                status="executor_error",
                failure=None,
                args=args,
                latency_ns=start_ns,
                schema_hash=self._schema_hashes[name],
                repaired=False,
            )
            return ExecutionResult(
                ok=False, tool=name, error=exc,
                latency_ms=perf_ms_since(start_ns),
            )

        # 4. Happy path.
        self._record(
            tool=name,
            status="passed",
            failure=None,
            args=args,
            latency_ns=start_ns,
            schema_hash=self._schema_hashes[name],
            repaired=False,
        )
        return ExecutionResult(
            ok=True, tool=name, value=value,
            latency_ms=perf_ms_since(start_ns),
        )

    def check(self, name: str, args: dict[str, Any]) -> ExecutionResult:
        """Validate args + classify + record telemetry. Never execute.

        Use this when the executor lives inside an existing async,
        middleware-wrapped, or context-injected path that Cruxial shouldn't
        own. You call this at the dispatch point, log/handle any failure,
        then let your existing executor path run as before.

        Behavior:
          - Returns ExecutionResult with ok=True and value=None on a clean call
            (because we did not execute — `value` is meaningless here).
          - Returns ok=False with .failure populated on a schema violation.
          - Records a telemetry row in either case, same shape as `.execute()`.
          - Unknown tool name returns ok=False with category="unknown_tool".
            If you don't want Cruxial to flag tools whose schemas aren't
            registered (e.g. dynamically-discovered MCP tools),
            guard the call: `if name in cruxial.schemas: cruxial.check(...)`.
        """
        start_ns = time.perf_counter_ns()
        args = args or {}

        if name not in self.schemas:
            failure = unknown_tool(name, list(self.schemas))
            self._record(
                tool=name,
                status="intercepted",
                failure=failure,
                args=args,
                latency_ns=start_ns,
                schema_hash="-",
                repaired=False,
            )
            return ExecutionResult(
                ok=False, tool=name, failure=failure,
                latency_ms=perf_ms_since(start_ns),
            )

        schema = self.schemas[name]
        validation = self._fail_open_validate(name, args, schema)

        if validation is not None and not validation.ok:
            failure = validation.failure
            assert failure is not None
            self._record(
                tool=name,
                status="intercepted",
                failure=failure,
                args=args,
                latency_ns=start_ns,
                schema_hash=self._schema_hashes[name],
                repaired=False,
            )
            return ExecutionResult(
                ok=False, tool=name, failure=failure,
                latency_ms=perf_ms_since(start_ns),
            )

        # Validation passed. Record but do not execute.
        self._record(
            tool=name,
            status="passed",
            failure=None,
            args=args,
            latency_ns=start_ns,
            schema_hash=self._schema_hashes[name],
            repaired=False,
        )
        return ExecutionResult(
            ok=True, tool=name, value=None,
            latency_ms=perf_ms_since(start_ns),
        )

    def knows(self, name: str) -> bool:
        """True if a schema is registered for this tool name.

        Useful for dynamic tool registries (MCP / runtime
        discovery) where you only want to check tools Cruxial has seen.
        """
        return name in self.schemas

    def register_schemas(
        self,
        schemas: Mapping[str, dict[str, Any]],
        *,
        schema_origin: str | None = None,
    ) -> int:
        """Register additional tool schemas at runtime. Returns count touched.

        For hosts that have schemas in hand from their own discovery path
        (custom MCP transports, runtime tool registries, plugin loaders,
        dynamic tool spawning). Eliminates the need to poke
        ``cx.schemas`` / ``cx._schema_hashes`` / ``cx.sink.register_tools``
        directly.

        Idempotent — re-registering an existing tool updates its schema and
        rehashes it. Fail-open — if the telemetry sink errors during
        registry update, the in-memory schema is still set.

        Args:
            schemas: ``{tool_name: json_schema_dict}`` to register.
            schema_origin: ``"model_visible"`` (default) or ``"canonical"``.
                If omitted, uses the value already configured on this
                Cruxial instance.

        Returns:
            Number of schemas processed (new + updated).

        Example (integration where the host's MCP discovery
        already yielded ``{tool_name: schema}``):

            from cruxial.adapters.openai import extract_schemas
            new_schemas = extract_schemas(my_runtime_mcp_tools_list)
            cruxial.register_schemas(new_schemas)
        """
        if not schemas:
            return 0

        origin = schema_origin or self.config.schema_origin

        for name, schema in schemas.items():
            self.schemas[name] = schema
            self._schema_hashes[name] = hash_schema(schema)

        try:
            if hasattr(self.sink, "register_tools"):
                self.sink.register_tools(
                    {n: self._schema_hashes[n] for n in schemas},
                    schema_origin=origin,
                )
        except Exception:
            # Fail-open — telemetry update failure must not break registration.
            pass

        return len(schemas)

    def execute_repaired(
        self,
        name: str,
        repaired_args: dict[str, Any],
    ) -> ExecutionResult:
        """Same as execute() but logs status=corrected on success.

        Used by adapter auto_repair helpers after they've round-tripped
        the model and gotten new args.
        """
        result = self.execute(name, repaired_args)
        if result.ok:
            # Re-record as "corrected" so stats CLI separates first-pass passes
            # from repaired-then-passed.
            self._record(
                tool=name,
                status="corrected",
                failure=None,
                args=repaired_args,
                latency_ns=time.perf_counter_ns(),
                schema_hash=self._schema_hashes.get(name, "-"),
                repaired=True,
            )
            result.repaired = True
            result.repaired_args = repaired_args
        return result

    def build_repair_prompt(
        self,
        failure: Failure,
        failed_args: dict[str, Any],
    ) -> str:
        """Public convenience for adapter authors."""
        schema = self.schemas.get(failure.tool)
        return build_repair_prompt(failure, schema, failed_args)

    def record_bypass(self, tool: str) -> None:
        """Log a tool_bypass interception so `cruxial stats` counts it.

        Called by ``cruxial.run`` when the model claimed an action in prose,
        emitted no matching call, and confirmed the bypass on a neutral
        re-prompt (then got corrected). Fail-open like all telemetry.
        """
        failure = Failure(
            category="tool_bypass",
            tool=tool,
            message=f"assistant claimed a {tool!r} action with no matching tool call",
        )
        self._record(
            tool=tool,
            status="intercepted",
            failure=failure,
            args={},
            latency_ns=time.perf_counter_ns(),
            schema_hash=self._schema_hashes.get(tool, "-"),
            repaired=True,
        )

    def close(self) -> None:
        try:
            self.sink.close()
        except Exception:
            pass

    # ─── internals ──────────────────────────────────────────────────

    def _fail_open_validate(self, name, args, schema):
        try:
            return _validate(name, args, schema)
        except BaseException as exc:  # noqa: BLE001
            if not self.config.fail_open:
                raise
            warnings.warn(
                f"cruxial: validator crashed for tool {name!r} "
                f"({type(exc).__name__}: {exc}). passing through.",
                stacklevel=2,
            )
            return None  # signals "treat as ok, let executor run"

    def _record(
        self,
        tool: str,
        status: str,
        failure: Failure | None,
        args: dict[str, Any],
        latency_ns: int,
        schema_hash: str,
        repaired: bool,
    ) -> None:
        try:
            record = InterceptionRecord(
                timestamp=utc_now(),
                tool=tool,
                status=status,  # type: ignore[arg-type]
                failure_category=failure.category if failure else None,
                failure_path=failure.path if failure else None,
                args_hash=hash_args(args),
                schema_hash=schema_hash,
                latency_ms=perf_ms_since(latency_ns),
                repaired=repaired,
                cruxial_version=__version__,
                schema_origin=self.config.schema_origin,
            )
            self.sink.record(record)
        except Exception:
            # Fail-open: never let telemetry sink-down the host app.
            pass


class NoopCruxial:
    """Returned by `guard()` when construction errors and `strict=False`.

    Mirrors the Cruxial public API surface. Every method is a no-op:
      - .check() / .execute() / .execute_repaired() → ok=True, value=None
      - .knows() → False (so `if cruxial.knows(name): check(...)` skips cleanly)
      - .build_repair_prompt() → empty string
      - .close() → nothing

    Designed so that an integration that follows the recommended
    `if cruxial.knows(name): cruxial.check(name, args)` pattern silently
    bypasses cruxial entirely if setup fails — preserving fail-open at
    construction, not just at runtime.
    """

    __slots__ = ("schemas", "executors")

    def __init__(self) -> None:
        self.schemas: dict[str, dict[str, Any]] = {}
        self.executors: dict[str, Callable[..., Any]] = {}

    def check(self, name: str, args: dict[str, Any]) -> ExecutionResult:
        return ExecutionResult(ok=True, tool=name, value=None, latency_ms=0.0)

    def execute(self, name: str, args: dict[str, Any]) -> ExecutionResult:
        # Without executors registered, we can't actually run anything.
        # Surface a clear error rather than silently mis-behaving.
        return ExecutionResult(
            ok=False,
            tool=name,
            error=RuntimeError(
                "cruxial is in no-op mode (construction failed with strict=False). "
                "Cannot .execute() — handle execution in your own dispatch path."
            ),
            latency_ms=0.0,
        )

    def execute_repaired(self, name: str, args: dict[str, Any]) -> ExecutionResult:
        return self.execute(name, args)

    def knows(self, name: str) -> bool:
        return False

    def build_repair_prompt(self, failure: Failure, failed_args: dict[str, Any]) -> str:
        return ""

    def close(self) -> None:
        pass


# ─── sink construction ─────────────────────────────────────────────────


def _build_sink(cfg: GuardConfig) -> Sink:
    sinks: list[Sink] = []
    for kind in cfg.sinks:
        if kind == "sqlite":
            sinks.append(
                SqliteSink(cfg.sqlite_path) if cfg.sqlite_path else SqliteSink()
            )
        elif kind == "stdout":
            sinks.append(StdoutSink())
        elif kind == "null":
            sinks.append(NullSink())
    if not sinks:
        return NullSink()
    if len(sinks) == 1:
        return sinks[0]
    return MultiSink(sinks)

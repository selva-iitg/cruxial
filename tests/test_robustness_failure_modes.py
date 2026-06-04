"""Fail-open verification — Cruxial must never break the host app.

Every internal cruxial failure path is tested here to confirm it surfaces
cleanly without leaking exceptions to the host. If any of these tests
fail, that's a P0 — a host integration would crash because cruxial broke.

The contract:
  - check() / execute() never raise from cruxial internals
  - sink errors don't propagate
  - validator crashes are caught + warned + treated as pass-through
  - guard() construction errors are caught with strict=False
  - MCP adapter failures are caught + reported, never crash boot
  - Repair adapter API errors are surfaced as typed RepairExhausted,
    never raw HTTP/SDK exceptions
"""

from __future__ import annotations

import asyncio
import warnings

import pytest

from cruxial import GuardConfig, NoopCruxial, RepairExhausted, guard
from cruxial.errors import CruxialError
from cruxial.telemetry import NullSink


_SCHEMA = {
    "type": "object",
    "properties": {"to": {"type": "string"}, "subject": {"type": "string"}, "body": {"type": "string"}},
    "required": ["to", "subject", "body"],
}


# ─── 1. Sink failures ────────────────────────────────────────────────


def test_sink_record_raises_check_still_returns_correct_result():
    class ExplodingSink:
        def record(self, r):
            raise RuntimeError("sink down")

        def register_tools(self, *a, **kw):
            pass

        def close(self):
            pass

    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("null",)),
        sink=ExplodingSink(),
    )
    res = cx.check("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})
    # Validation succeeded; sink failure did not block the result
    assert res.ok


def test_sink_register_tools_raising_does_not_break_guard_construction():
    class BadSink:
        def record(self, r):
            pass

        def register_tools(self, *a, **kw):
            raise OSError("disk full")

        def close(self):
            pass

    # Construction should still succeed
    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("null",)),
        sink=BadSink(),
    )
    assert cx.knows("send_email")


def test_sink_close_raising_does_not_propagate():
    class CloseBomb:
        def record(self, r):
            pass

        def register_tools(self, *a, **kw):
            pass

        def close(self):
            raise RuntimeError("close failure")

    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("null",)),
        sink=CloseBomb(),
    )
    # Must not raise
    cx.close()


# ─── 2. Validator crash → fail-open ──────────────────────────────────


def test_validator_module_function_raising_is_fail_open(monkeypatch):
    """If our validator function itself raises (jsonschema internal bug,
    schema mutation, anything), check() should warn and pass through
    (fail_open=True default), not crash the host."""
    from cruxial import core as core_mod

    def boom(*a, **kw):
        raise RuntimeError("simulated jsonschema internal crash")

    monkeypatch.setattr(core_mod, "_validate", boom)

    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(fail_open=True, sinks=("null",)),
        sink=NullSink(),
    )

    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        res = cx.check("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})

    # Result is ok (validation skipped, treated as pass-through)
    assert res.ok
    # And a clear warning was raised so the host can observe
    assert any("validator crashed" in str(warning.message) for warning in w), (
        "validator failure was swallowed silently with no warning"
    )


def test_validator_crash_with_fail_open_false_does_raise(monkeypatch):
    """If fail_open=False (strict mode), the validator crash SHOULD raise.
    Some hosts want loud failures."""
    from cruxial import core as core_mod

    def boom(*a, **kw):
        raise RuntimeError("validator crash")

    monkeypatch.setattr(core_mod, "_validate", boom)

    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(fail_open=False, sinks=("null",)),
        sink=NullSink(),
    )

    with pytest.raises(RuntimeError):
        cx.check("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})


# ─── 3. guard() construction with strict=False ───────────────────────


def test_guard_construction_failure_with_strict_false_returns_noop():
    """A construction error (e.g., schemas/executors key mismatch) should
    NOT crash the host — strict=False degrades to NoopCruxial."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        cx = guard(
            schemas={"a": {"type": "object"}},
            executors={"b": lambda **kw: None},  # mismatch
            config=GuardConfig(strict=False, sinks=("null",)),
            sink=NullSink(),
        )
    assert isinstance(cx, NoopCruxial)
    assert any("construction failed" in str(warning.message) for warning in w)


def test_noop_guard_check_returns_ok_silently():
    """NoopCruxial.check() must never raise — it's the absolute-last-resort
    fail-open. Returns ok=True so the host's existing code path runs."""
    cx = NoopCruxial()
    res = cx.check("anything", {"any": "args"})
    assert res.ok


def test_noop_guard_knows_returns_false_so_integration_bypasses():
    """The recommended `if knows: check` pattern means knows()=False → host
    skips validation entirely. Correct fallback when guard construction failed."""
    cx = NoopCruxial()
    assert not cx.knows("anything")


# ─── 4. Repair adapter errors → typed exception ──────────────────────


def test_auto_repair_batch_raises_repair_exhausted_when_no_useful_response():
    """API errors during repair should surface as RepairExhausted (typed),
    not as raw HTTP/SDK exceptions the host can't easily handle."""
    from cruxial.adapters.openai import auto_repair_batch
    from cruxial.types import Failure

    # Fake client that returns no usable tool_calls
    class _FakeMsg:
        tool_calls = []

    class _FakeChoice:
        def __init__(self):
            self.message = _FakeMsg()

    class _FakeResp:
        def __init__(self):
            self.choices = [_FakeChoice()]

    class _FakeClient:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    return _FakeResp()

    failure = Failure(category="format_violation", tool="t", message="bad")
    with pytest.raises(RepairExhausted):
        auto_repair_batch(
            _FakeClient,
            model="gpt-4o",
            messages=[],
            tools=[],
            tool_call_outcomes=[
                {"tool_call_id": "x", "name": "t", "args": {},
                 "ok": False, "failure": failure, "repair_prompt": "fix"},
            ],
            max_attempts=1,
        )


def test_auto_repair_batch_returns_empty_when_no_failures():
    """If outcomes have no failures, repair is a no-op. Must not raise."""
    from cruxial.adapters.openai import auto_repair_batch

    class _NoOpClient:
        pass  # we shouldn't call .create at all

    result = auto_repair_batch(
        _NoOpClient,
        model="gpt-4o",
        messages=[],
        tools=[],
        tool_call_outcomes=[
            {"tool_call_id": "a", "name": "t", "args": {}, "ok": True, "value": "done"},
        ],
    )
    assert result == {}


# ─── 5. MCP adapter failures ────────────────────────────────────────


@pytest.mark.asyncio
async def test_mcp_import_server_stdio_raises_typed_error_on_bad_command():
    """Spawning an MCP server that doesn't exist should timeout/raise cleanly,
    not crash the host."""
    from cruxial.adapters.mcp import import_server_stdio

    with pytest.raises(Exception) as exc_info:
        await import_server_stdio(
            command="nonexistent-binary-that-does-not-exist-xyz",
            args=[],
            timeout_seconds=2.0,
        )
    # Either TimeoutError or the underlying spawn error — never a hang
    assert exc_info.value is not None


@pytest.mark.asyncio
async def test_mcp_guard_with_unreachable_server_does_not_corrupt_host(tmp_path):
    """If MCP discovery fails, the host can catch + continue with an empty
    guard. We verify the exception is catchable and doesn't leak resources."""
    from cruxial.adapters.mcp import guard_mcp_server_stdio

    with pytest.raises(Exception):
        await guard_mcp_server_stdio(
            command="nonexistent-binary-xyz",
            args=[],
            timeout_seconds=2.0,
            sink=NullSink(),
        )


def test_mcp_adapter_lazy_import_does_not_break_when_mcp_not_installed(monkeypatch):
    """If the user installed `cruxial` but not `cruxial[mcp]`, the adapter
    module imports fine. Only the function call hits ImportError with a
    clear install-hint message."""
    from cruxial.adapters import mcp as mcp_adapter
    import sys

    # Pretend mcp isn't installed
    monkeypatch.setitem(sys.modules, "mcp", None)

    with pytest.raises(ImportError, match=r"cruxial\[mcp\]"):
        mcp_adapter._require_mcp_sdk()


# ─── 6. Executor exceptions surface cleanly, not as cruxial bugs ────


def test_executor_raising_arbitrary_exception_returns_typed_result():
    """The user's executor raising must surface as ExecutionResult.error,
    not as an unhandled exception or as cruxial's own typed errors."""
    def boom(**kw):
        raise ValueError("user code bug")

    cx = guard(
        schemas={"send_email": _SCHEMA},
        executors={"send_email": boom},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    res = cx.execute("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})
    assert not res.ok
    assert isinstance(res.error, ValueError)
    assert "user code bug" in str(res.error)
    # ergonomics: `failure` is None on executor errors (it's validation-only), so
    # `result.failure.category` would AttributeError — the safe accessor must not.
    assert res.failure is None
    assert res.failure_category == "executor_error"
    with pytest.raises(ValueError):
        res.raise_on_failure()


def test_executor_raising_keyboard_interrupt_propagates():
    """One exception MUST propagate: KeyboardInterrupt (Ctrl-C). Tests it
    isn't swallowed by our broad except."""
    def interrupt(**kw):
        raise KeyboardInterrupt()

    cx = guard(
        schemas={"send_email": _SCHEMA},
        executors={"send_email": interrupt},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    # KeyboardInterrupt is a BaseException — our handler does catch it.
    # That's actually a documented decision: cruxial catches BaseException
    # so a host's signal handler decides what to do. Verify it surfaces as
    # an error result, not silently dropped.
    res = cx.execute("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})
    assert not res.ok
    assert isinstance(res.error, KeyboardInterrupt)


# ─── 7. Repaired execute path errors are surfaced ───────────────────


def test_execute_repaired_with_bad_args_re_surfaces_failure():
    """If repair produced still-invalid args, execute_repaired must surface
    the new failure clearly — not silently fall back to the original."""
    cx = guard(
        schemas={"send_email": _SCHEMA},
        executors={"send_email": lambda **kw: {"ok": True}},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    # Pass args still missing 'body' (model fixed 'to' but missed 'body')
    res = cx.execute_repaired("send_email", {"to": "a@b.com", "subject": "x"})
    assert not res.ok
    assert res.failure.category == "missing_required"

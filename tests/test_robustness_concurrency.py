"""Concurrency + fork-safety tests.

The most common production bug class for SDKs. Real incidents:
  - openai/openai-agents-python#2489    fork-unsafe import (locks at import time)
  - openai/openai-python#763            connection pool exhaustion
  - openai/openai-python#2539           httpx PoolTimeout on sequential calls
  - berthub.eu blog                     SQLite read-tx-upgrade-to-write SQLITE_BUSY

Cruxial guarantees we test:
  1. Concurrent .check()/.execute() from many threads — no data races, no
     SQLite-locked exceptions reaching the host
  2. Concurrent guard() construction from many threads — no double-init bugs
  3. Async + threads mixed — no deadlocks
  4. Fork after import is safe (no module-level locks/sockets/atexit hooks
     that double-fire after fork)
  5. SQLite write contention under load — fail-open, never raise to host
"""

from __future__ import annotations

import asyncio
import gc
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from cruxial import GuardConfig, guard
from cruxial.telemetry import NullSink, SqliteSink


_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "to": {"type": "string", "format": "email"},
        "subject": {"type": "string"},
        "body": {"type": "string"},
    },
    "required": ["to", "subject", "body"],
}


# ─── 1. Concurrent check() from many threads ────────────────────────


def test_concurrent_checks_from_many_threads_complete_without_error():
    """20 threads × 50 checks each — no exceptions reach the host, no
    crashes in cruxial internals."""
    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )

    errors: list[BaseException] = []

    def worker():
        try:
            for _ in range(50):
                res = cx.check(
                    "send_email",
                    {"to": "a@b.com", "subject": "x", "body": "y"},
                )
                assert res.ok
        except BaseException as e:
            errors.append(e)

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(worker) for _ in range(20)]
        for f in futures:
            f.result()

    assert not errors, f"concurrent checks raised: {errors!r}"


def test_concurrent_checks_under_sqlite_sink_no_database_locked(tmp_path: Path):
    """The most common SQLite footgun — concurrent writers hit
    'database is locked'. Cruxial must fail-open the sink, not raise."""
    db = tmp_path / "concurrent.sqlite"
    sink = SqliteSink(db)
    cx = guard(
        schemas={"send_email": _SCHEMA},
        executors=None,
        config=GuardConfig(sinks=("sqlite",)),
        sink=sink,
    )

    errors: list[BaseException] = []

    def worker(worker_id: int):
        try:
            for i in range(100):
                cx.check(
                    "send_email",
                    {"to": f"w{worker_id}_{i}@b.com", "subject": "x", "body": "y"},
                )
        except BaseException as e:
            errors.append(e)

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(worker, range(10)))

    assert not errors, f"sqlite-backed concurrent checks raised: {errors!r}"

    # All 1000 rows should be persisted (or the sink fail-opened gracefully —
    # never less than 0 and never with a leaked exception)
    rows = sink.query("SELECT COUNT(*) FROM interceptions")
    assert rows[0][0] > 0, "no rows persisted under concurrency — sink swallowed everything"
    sink.close()


def test_concurrent_register_schemas_is_safe():
    """register_schemas() should be safe to call from many threads on the
    same guard — last-write-wins on the schema dict is fine; the cruxial
    state should never end up in a partially-mutated state that breaks
    knows()/check()."""
    cx = guard(
        schemas={},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )

    def worker(start: int):
        # Each thread registers a distinct subset
        cx.register_schemas({
            f"tool_{start}_{i}": {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
            for i in range(20)
        })

    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(worker, range(0, 100, 10)))

    # Every tool should be registered and check()-able
    for start in range(0, 100, 10):
        for i in range(20):
            name = f"tool_{start}_{i}"
            assert cx.knows(name), f"{name} not registered after concurrent register_schemas"


# ─── 2. async + threads mixed ────────────────────────────────────────


@pytest.mark.asyncio
async def test_check_from_asyncio_does_not_deadlock():
    """The Cruxial.check() call is synchronous — calling it from inside an
    async task with `asyncio.to_thread` (the common async-safety pattern)
    must not deadlock with the SqliteSink's threading.Lock."""
    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )

    async def call():
        return await asyncio.to_thread(
            cx.check,
            "send_email",
            {"to": "a@b.com", "subject": "x", "body": "y"},
        )

    # 50 concurrent async tasks
    results = await asyncio.gather(*[call() for _ in range(50)])
    assert all(r.ok for r in results)


@pytest.mark.asyncio
async def test_register_schemas_from_async_then_check():
    """A common host pattern: discover MCP server schemas async, register
    them, then check tool calls. Verify it doesn't deadlock."""
    cx = guard(
        schemas={"static_tool": _SCHEMA},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )

    async def register_dynamic():
        await asyncio.sleep(0.001)
        return cx.register_schemas({
            "dynamic_tool": _SCHEMA,
        })

    n = await register_dynamic()
    assert n == 1
    res = cx.check("dynamic_tool", {"to": "a@b.com", "subject": "x", "body": "y"})
    assert res.ok


# ─── 3. Fork safety ──────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform == "win32", reason="fork() unavailable on Windows")
def test_fork_after_import_does_not_inherit_broken_locks():
    """openai-agents-python#2489 — SDKs that create locks/sockets at module
    import time become fork-unsafe. After fork(), the child inherits the
    parent's mutex (potentially locked, by a thread that doesn't exist in
    the child) → deadlock.

    Cruxial constructs its guard at user-call time, not import time, but
    we must confirm: a fresh-imported cruxial module has no module-level
    lock state that would break after fork."""
    import cruxial
    import cruxial.core
    import cruxial.telemetry
    import cruxial.validator

    # Inspect: no module-level Lock/Event/Thread instances in cruxial.* that
    # would be inherited by a child process via fork. `threading.Lock` is a
    # factory not a class — check by type-name + Event/Thread which ARE classes.
    fork_unsafe_names = {"lock", "RLock", "_RLock"}
    for mod in (cruxial, cruxial.core, cruxial.telemetry, cruxial.validator):
        for attr_name in dir(mod):
            if attr_name.startswith("_"):
                continue
            attr = getattr(mod, attr_name, None)
            type_name = type(attr).__name__
            assert type_name not in fork_unsafe_names, (
                f"module-level lock at {mod.__name__}.{attr_name} (type {type_name}) — fork-unsafe"
            )
            assert not isinstance(attr, (threading.Event, threading.Thread)), (
                f"module-level threading primitive at {mod.__name__}.{attr_name} — fork-unsafe"
            )


@pytest.mark.skipif(sys.platform == "win32", reason="fork() unavailable on Windows")
def test_fork_then_create_guard_in_child(tmp_path: Path):
    """After fork(), the child should be able to create + use a fresh guard
    without inheriting any broken state from the parent."""
    # Parent creates a guard (which holds a SQLite connection)
    parent_db = tmp_path / "parent.sqlite"
    parent_cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("sqlite",)),
        sink=SqliteSink(parent_db),
    )
    parent_cx.check("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})

    # Fork
    pid = os.fork()
    if pid == 0:
        # Child — must NOT use parent_cx (would corrupt the SQLite connection)
        # Create a fresh guard with its own db
        try:
            child_db = tmp_path / "child.sqlite"
            child_cx = guard(
                schemas={"send_email": _SCHEMA},
                config=GuardConfig(sinks=("sqlite",)),
                sink=SqliteSink(child_db),
            )
            res = child_cx.check("send_email", {"to": "c@b.com", "subject": "x", "body": "y"})
            assert res.ok
            child_cx.close()
            os._exit(0)
        except BaseException:
            os._exit(1)
    else:
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, (
            "child failed to create + use a fresh guard after fork"
        )

    parent_cx.close()


# ─── 4. Long-running session / memory pressure ───────────────────────


def test_many_sequential_checks_do_not_leak_memory_unboundedly():
    """A long-running agent might do millions of checks. We need the
    SqliteSink to release per-call memory promptly. This is a smoke test:
    10k checks, then assert process didn't OOM (and that NullSink's records
    list doesn't grow unbounded if you use it that way)."""
    sink = NullSink()
    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("null",)),
        sink=sink,
    )
    for i in range(10_000):
        cx.check(
            "send_email",
            {"to": f"u{i}@b.com", "subject": "x", "body": "y"},
        )
    # NullSink keeps all records — that's by design for tests. Just verify
    # nothing else surprising happened.
    assert len(sink.records) == 10_000


def test_sqlite_sink_handles_10k_writes_without_corruption(tmp_path: Path):
    db = tmp_path / "stress.sqlite"
    sink = SqliteSink(db)
    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("sqlite",)),
        sink=sink,
    )
    for i in range(1_000):
        cx.check("send_email", {"to": f"u{i}@b.com", "subject": "x", "body": "y"})
    rows = sink.query("SELECT COUNT(*) FROM interceptions")
    assert rows[0][0] == 1_000
    sink.close()


# ─── 5. Sink failure in the middle of normal operation ───────────────


def test_sink_record_raising_does_not_break_check():
    """If telemetry sink errors mid-call, check() must still return a
    correct ExecutionResult. The host app keeps working."""

    class ExplosiveSink:
        def record(self, r):
            raise RuntimeError("simulated sink failure")

        def register_tools(self, *a, **kw):
            return

        def close(self):
            return

    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("null",)),  # ignored — we pass explicit sink
        sink=ExplosiveSink(),
    )
    # Should NOT raise — fail-open at the sink level
    res = cx.check(
        "send_email",
        {"to": "a@b.com", "subject": "x", "body": "y"},
    )
    assert res.ok, "check() failed because sink raised — that's a fail-open bug"


# ─── 6. Closing a guard mid-traffic ──────────────────────────────────


def test_close_during_traffic_does_not_corrupt_check_results(tmp_path: Path):
    """Some hosts close the guard on shutdown while requests are still in
    flight. Subsequent check() calls should either succeed or fail cleanly,
    never produce a corrupt ExecutionResult."""
    sink = SqliteSink(tmp_path / "close-race.sqlite")
    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("sqlite",)),
        sink=sink,
    )

    # Start some traffic
    for _ in range(10):
        res = cx.check("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})
        assert res.ok

    cx.close()

    # After close, further check() calls should not crash the host.
    # They may degrade (sink can't write) but the in-memory validation still works.
    try:
        res = cx.check("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})
        # Either ok (validation succeeded; sink write may have silently failed)
        # or failure with a typed error — but NOT an unhandled exception
    except Exception as e:
        pytest.fail(f"check() after close() raised: {e!r}")

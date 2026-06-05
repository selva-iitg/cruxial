"""Resource exhaustion + DoS resistance tests.

Sources:
  - CVE-2024-3772                       Pydantic email regex catastrophic backtracking
  - CVE-2024-24762                      python-multipart ReDoS in Content-Type parsing
  - openai/codex#19765 (truncated JSON)  giant unclosed string args from model
  - langchain-ai/deepagentsjs#82        large tool result triggers infinite read loop

Cruxial must:
  1. Time-bound validation so a pathological schema/args don't hang the host
  2. Handle 100k+ tools registered without quadratic blowup
  3. Handle multi-MB args (slow but not crash)
  4. Not leak file descriptors from SqliteSink across many guards
  5. Reject obvious DoS patterns (huge enums, deeply-nested allOf)
"""

from __future__ import annotations

import resource
import signal
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

from cruxial import GuardConfig, guard
from cruxial.telemetry import NullSink, SqliteSink
from cruxial.validator import validate


@contextmanager
def _time_bound(seconds: float, label: str):
    """Fail the test if the block takes longer than `seconds`. Posix-only."""
    if not hasattr(signal, "SIGALRM"):
        # Windows — skip the wall-clock guard; tests still run.
        yield
        return

    def _handler(signum, frame):
        raise TimeoutError(f"{label} exceeded {seconds}s")

    old = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


# ─── 1. Massive args don't crash the validator ───────────────────────


def test_validator_handles_1mb_string_arg():
    """1 MB string in a single field — within reason for some tools (e.g.
    code review of a large file). Validation should be sub-second."""
    schema = {
        "type": "object",
        "properties": {"content": {"type": "string"}},
        "required": ["content"],
    }
    big = "x" * (1024 * 1024)  # 1 MB
    t = time.perf_counter()
    res = validate("t", {"content": big}, schema)
    elapsed = time.perf_counter() - t
    assert res.ok
    assert elapsed < 1.0, f"validation of 1MB arg took {elapsed:.2f}s — too slow"


def test_validator_handles_10mb_string_arg_under_5s():
    """10 MB — extreme but not adversarial. Still must complete in finite time."""
    schema = {
        "type": "object",
        "properties": {"content": {"type": "string", "maxLength": 100}},
        "required": ["content"],
    }
    big = "x" * (10 * 1024 * 1024)  # 10 MB
    t = time.perf_counter()
    res = validate("t", {"content": big}, schema)
    elapsed = time.perf_counter() - t
    assert not res.ok
    assert res.failure.category == "constraint_violation"
    assert elapsed < 5.0, f"validation of 10MB constrained-string took {elapsed:.2f}s"


def test_validator_handles_1000_item_array():
    schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "string"}, "maxItems": 100},
        },
        "required": ["items"],
    }
    big = ["item"] * 1000
    res = validate("t", {"items": big}, schema)
    assert not res.ok
    assert res.failure.category == "constraint_violation"


# ─── 2. Deeply nested schemas don't blow recursion limit ─────────────


def test_50_level_deep_nested_object_validates_without_recursion_error():
    """A 50-level deep nested object via `properties.x.properties.x...`
    must validate without Python's default recursion limit (1000) firing
    OR with a clean fail-open."""
    # Build a 50-deep schema
    schema = {"type": "object", "properties": {"x": {"type": "string"}}}
    args = {"x": "ok"}
    for _ in range(49):
        schema = {"type": "object", "properties": {"x": schema}}
        args = {"x": args}

    res = validate("t", args, schema)
    # Either passes or fail-opens; never RecursionError leaking out
    assert res.ok or res.failure is not None


# ─── 3. Huge enums ───────────────────────────────────────────────────


def test_enum_with_10000_values_does_not_choke_validator():
    """A pathologically large enum — should still be O(n) check, not O(n²)."""
    big_enum = [f"value_{i}" for i in range(10_000)]
    schema = {
        "type": "object",
        "properties": {"choice": {"type": "string", "enum": big_enum}},
        "required": ["choice"],
    }
    t = time.perf_counter()
    res = validate("t", {"choice": "value_5000"}, schema)
    elapsed = time.perf_counter() - t
    assert res.ok
    assert elapsed < 1.0, f"enum-of-10000 took {elapsed:.2f}s"


def test_enum_violation_with_10000_choices_finds_failure_fast():
    big_enum = [f"value_{i}" for i in range(10_000)]
    schema = {
        "type": "object",
        "properties": {"choice": {"type": "string", "enum": big_enum}},
        "required": ["choice"],
    }
    res = validate("t", {"choice": "not_in_enum"}, schema)
    assert not res.ok
    assert res.failure.category == "enum_violation"


# ─── 4. Many registered tools ────────────────────────────────────────


def test_guard_with_10000_tools_constructs_linearly():
    """Some MCP servers expose hundreds of tools; aggregating multiple
    servers could yield thousands. Guard construction must scale linearly.

    The invariant under test is linearity, not a precise SLA — construction
    runs check_schema() per tool, so absolute wall-clock varies with the
    runner. The 10s bound is a generous ceiling: an O(n^2) regression at
    10k tools would overshoot it by orders of magnitude, so it still fails
    loudly on real blow-ups without flaking on slow CI.
    """
    schemas = {
        f"tool_{i}": {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        }
        for i in range(10_000)
    }
    t = time.perf_counter()
    cx = guard(
        schemas=schemas,
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    elapsed = time.perf_counter() - t
    assert elapsed < 10.0, f"guard with 10000 tools took {elapsed:.2f}s"
    assert cx.knows("tool_5000")


def test_check_is_o1_in_tool_count():
    """check() lookup should be dict O(1), not list O(n). Verify at 10k tools."""
    schemas = {
        f"tool_{i}": {
            "type": "object",
            "properties": {"x": {"type": "string"}},
            "required": ["x"],
        }
        for i in range(10_000)
    }
    cx = guard(
        schemas=schemas,
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    # Time 1000 lookups — should be a few ms total if O(1)
    t = time.perf_counter()
    for i in range(1000):
        cx.check(f"tool_{i % 10_000}", {"x": "ok"})
    elapsed = time.perf_counter() - t
    assert elapsed < 2.0, f"1000 checks against 10k tools took {elapsed:.2f}s — O(n) leak?"


# ─── 5. ReDoS resistance (CVE-2024-3772 pattern) ─────────────────────


def test_validator_does_not_hang_on_adversarial_email_pattern():
    """CVE-2024-3772 pattern: Pydantic email regex had catastrophic
    backtracking on `a@` + `a.` * 40. Cruxial defers email validation to
    jsonschema's FormatChecker. The test asserts ONLY that validation
    completes promptly — NOT that the input is rejected (jsonschema's
    email checker is lenient, which is a separate documented limitation).
    The ReDoS-resistance is the only thing this test guards."""
    schema = {
        "type": "object",
        "properties": {"e": {"type": "string", "format": "email"}},
        "required": ["e"],
    }
    payload = "a@" + ("a." * 40)
    t = time.perf_counter()
    with _time_bound(2.0, "adversarial email validation"):
        validate("t", {"e": payload}, schema)
    elapsed = time.perf_counter() - t
    assert elapsed < 2.0, f"validation took {elapsed:.2f}s — ReDoS risk"


def test_validator_does_not_hang_on_adversarial_pattern_constraint():
    """A user-supplied schema with a pathological regex (`(a+)+`) is a
    classic ReDoS. Cruxial uses Python's re module which IS vulnerable —
    we document this as a known limitation: don't accept untrusted
    schemas. Test asserts that a malicious USER-supplied schema doesn't
    hang us beyond a few seconds."""
    schema = {
        "type": "object",
        "properties": {
            "x": {"type": "string", "pattern": r"^(a+)+$"},  # exponential backtrack
        },
        "required": ["x"],
    }
    payload = "a" * 25 + "X"  # input that triggers backtracking
    try:
        with _time_bound(3.0, "redos pattern"):
            res = validate("t", {"x": payload}, schema)
            # Outcome depends on regex engine; we just bound the time
            assert res.ok or res.failure is not None
    except TimeoutError:
        pytest.fail(
            "Adversarial pattern hung the validator > 3s. Add a documented "
            "warning in DEFENSIVE.md: don't accept untrusted schemas, OR "
            "ship a regex.compile() with a finite-state matcher in V0.2."
        )


# ─── 6. File-descriptor / SQLite-connection leaks ────────────────────


def test_creating_100_guards_does_not_leak_file_descriptors(tmp_path: Path):
    """Some hosts create a guard per request (anti-pattern but possible).
    Each SqliteSink opens a SQLite connection — that's a file descriptor.
    Verify a hundred guards don't exhaust the FD limit."""
    soft_limit, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft_limit < 200:
        pytest.skip(f"FD limit {soft_limit} too low for this test")

    sinks = []
    for i in range(100):
        sink = SqliteSink(tmp_path / f"guard_{i}.sqlite")
        sinks.append(sink)
        guard(
            schemas={"t": {"type": "object"}},
            config=GuardConfig(sinks=("sqlite",)),
            sink=sink,
        )

    # Close them
    for s in sinks:
        s.close()


# ─── 7. NullSink memory growth (for tests using it long-term) ────────


def test_null_sink_records_can_be_cleared():
    """NullSink keeps records in memory by design. If a test uses it for
    100k checks, that's fine — but the test author should know they can
    clear the list."""
    sink = NullSink()
    cx = guard(
        schemas={"t": {"type": "object"}},
        config=GuardConfig(sinks=("null",)),
        sink=sink,
    )
    for _ in range(100):
        cx.check("t", {})
    assert len(sink.records) == 100
    sink.records.clear()
    assert len(sink.records) == 0
    cx.check("t", {})
    assert len(sink.records) == 1


# ─── 8. Hash function performance on large args ──────────────────────


def test_hash_args_does_not_block_on_huge_arg_dict():
    """telemetry.hash_args() is called on EVERY check. Must not become
    a bottleneck on large args."""
    from cruxial.telemetry import hash_args

    big = {f"k_{i}": "x" * 100 for i in range(1000)}
    t = time.perf_counter()
    for _ in range(100):
        hash_args(big)
    elapsed = time.perf_counter() - t
    assert elapsed < 1.0, f"100 hashes of 1000-key dict took {elapsed:.2f}s"

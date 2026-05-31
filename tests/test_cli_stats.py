"""End-to-end CLI: build a real sqlite, run `cruxial stats`, assert output."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from cruxial import GuardConfig, guard
from cruxial.telemetry import SqliteSink


def _run_stats(db: Path, since: str = "all") -> str:
    out = subprocess.run(
        [sys.executable, "-m", "cruxial.cli", "stats", "--db", str(db), "--since", since],
        capture_output=True, text=True, check=True,
    )
    return out.stdout


def _run_demo(cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "cruxial.cli", "demo"],
        capture_output=True, text=True, cwd=str(cwd),
    )


def test_demo_runs_offline_and_catches_every_category(tmp_path: Path):
    """`cruxial demo` works with no API key and shows each failure category caught."""
    proc = _run_demo(tmp_path)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout
    # A valid call passes, and each schema-derivable category is demonstrated.
    assert "valid call" in out
    for category in (
        "missing_required",
        "type_mismatch",
        "enum_violation",
        "format_violation",
        "constraint_violation",
        "extra_field",
        "unknown_tool",
    ):
        assert category in out, f"demo did not exercise {category}"
    assert "violation types caught" in out
    assert "cruxial stats" in out  # points the user at the next step


def test_demo_writes_no_telemetry(tmp_path: Path):
    """The demo must never pollute the user's telemetry — runs on a null sink."""
    proc = _run_demo(tmp_path)
    assert proc.returncode == 0, proc.stderr
    # Running from an empty tmp dir, no project-local .cruxial/ should appear.
    assert not (tmp_path / ".cruxial").exists()
    assert "0 telemetry rows written" in proc.stdout


def test_stats_with_no_traffic_shows_registry_hint(tmp_path: Path, schemas, executors):
    """guard() registers tools → stats shows them even with zero traffic."""
    db = tmp_path / "telemetry.sqlite"
    sink = SqliteSink(db)
    guard(
        schemas=schemas, executors=executors,
        config=GuardConfig(sinks=("null",)), sink=sink,
    )
    sink.close()

    output = _run_stats(db)
    assert "tool(s) registered" in output
    assert "send_email" in output
    assert "no traffic has hit cruxial yet" in output


def test_stats_with_traffic_shows_registry_line(tmp_path: Path, schemas, executors):
    db = tmp_path / "telemetry.sqlite"
    sink = SqliteSink(db)
    c = guard(
        schemas=schemas, executors=executors,
        config=GuardConfig(sinks=("null",)), sink=sink,
    )
    c.execute("send_email", {"to": "a@b.com", "subject": "hi", "body": "x"})
    c.execute("send_email", {"subject": "hi", "body": "x"})  # intercepted
    sink.close()

    output = _run_stats(db)
    assert "registry" in output
    assert "registered" in output
    assert "fired" in output
    assert "intercepted" in output
    assert "total calls" in output


def test_stats_hint_appears_when_quiet_but_active(tmp_path: Path, schemas, executors):
    """Traffic flowing but 0 interceptions → user gets a how-to-verify hint."""
    db = tmp_path / "telemetry.sqlite"
    sink = SqliteSink(db)
    c = guard(
        schemas=schemas, executors=executors,
        config=GuardConfig(sinks=("null",)), sink=sink,
    )
    # All happy-path calls
    for _ in range(3):
        c.execute("send_email", {"to": "a@b.com", "subject": "hi", "body": "x"})
    sink.close()

    output = _run_stats(db)
    assert "no interceptions in this window" in output
    assert "violation_payloads" in output  # the hint references the testing helper


def test_stats_warns_when_canonical_mode_used(tmp_path: Path, schemas, executors):
    import warnings as _w
    db = tmp_path / "telemetry.sqlite"
    sink = SqliteSink(db)
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        c = guard(
            schemas=schemas, executors=executors,
            config=GuardConfig(sinks=("null",), schema_origin="canonical"),
            sink=sink,
        )
    c.execute("send_email", {"to": "a@b.com", "subject": "hi", "body": "x"})
    sink.close()

    output = _run_stats(db)
    assert "schema_origin='canonical'" in output

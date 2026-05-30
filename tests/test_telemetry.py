"""Telemetry sinks must persist correctly and respect privacy."""

from __future__ import annotations

from pathlib import Path

import pytest

from cruxial import GuardConfig, guard
from cruxial.telemetry import SqliteSink, hash_args, hash_schema


def test_hash_args_is_stable_and_collision_safe():
    h1 = hash_args({"a": 1, "b": 2})
    h2 = hash_args({"b": 2, "a": 1})  # different insertion order
    h3 = hash_args({"a": 1, "b": 3})
    assert h1 == h2  # order-independent
    assert h1 != h3
    assert len(h1) == 16


def test_hash_schema_is_stable():
    s = {"type": "object", "properties": {"x": {"type": "string"}}}
    assert hash_schema(s) == hash_schema(s)


def test_sqlite_sink_persists_records(tmp_path: Path, schemas, executors):
    db = tmp_path / "telemetry.sqlite"
    sink = SqliteSink(db)

    c = guard(
        schemas=schemas,
        executors=executors,
        config=GuardConfig(sinks=("null",)),  # ignored; we pass sink directly
        sink=sink,
    )
    c.execute("send_email", {"to": "a@b.com", "subject": "hi", "body": "x"})
    c.execute("send_email", {"subject": "hi", "body": "x"})  # missing 'to'

    rows = sink.query("SELECT status, failure_category, args_hash FROM interceptions ORDER BY id")
    assert len(rows) == 2
    assert rows[0][0] == "passed"
    assert rows[1][0] == "intercepted"
    assert rows[1][1] == "missing_required"
    assert rows[0][2]  # args_hash present
    sink.close()


def test_sqlite_sink_persists_tool_registry(tmp_path: Path, schemas, executors):
    """guard() should publish the registered tools to sqlite so `cruxial stats`
    can show what's wired up even before any traffic flows."""
    db = tmp_path / "telemetry.sqlite"
    sink = SqliteSink(db)

    guard(
        schemas=schemas,
        executors=executors,
        config=GuardConfig(sinks=("null",)),
        sink=sink,
    )

    rows = sink.query("SELECT name, schema_origin FROM tools ORDER BY name")
    names = [r[0] for r in rows]
    assert names == sorted(schemas.keys())
    # default schema_origin
    assert all(r[1] == "model_visible" for r in rows)
    sink.close()


def test_sqlite_sink_registry_upsert_is_idempotent(tmp_path: Path, schemas, executors):
    """Calling guard() multiple times with the same schemas should not duplicate rows."""
    db = tmp_path / "telemetry.sqlite"
    sink = SqliteSink(db)

    for _ in range(3):
        guard(
            schemas=schemas, executors=executors,
            config=GuardConfig(sinks=("null",)), sink=sink,
        )

    rows = sink.query("SELECT COUNT(*) FROM tools")
    assert rows[0][0] == len(schemas)
    sink.close()


def test_sqlite_sink_schema_origin_canonical_persists(tmp_path: Path, schemas, executors):
    """schema_origin='canonical' should flow into the tools and interceptions tables."""
    db = tmp_path / "telemetry.sqlite"
    sink = SqliteSink(db)

    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        c = guard(
            schemas=schemas, executors=executors,
            config=GuardConfig(sinks=("null",), schema_origin="canonical"),
            sink=sink,
        )
    c.execute("send_email", {"to": "a@b.com", "subject": "hi", "body": "x"})

    tool_origins = sink.query("SELECT DISTINCT schema_origin FROM tools")
    assert tool_origins[0][0] == "canonical"

    int_origins = sink.query("SELECT DISTINCT schema_origin FROM interceptions")
    assert int_origins[0][0] == "canonical"
    sink.close()


def test_sqlite_sink_never_stores_raw_args(tmp_path: Path, schemas, executors):
    """Privacy contract: no raw arg value should appear in the DB file."""
    db = tmp_path / "telemetry.sqlite"
    sink = SqliteSink(db)

    c = guard(
        schemas=schemas,
        executors=executors,
        config=GuardConfig(sinks=("null",)),
        sink=sink,
    )
    secret = "supersecret-canary-token-12345"
    c.execute(
        "send_email",
        {"to": "a@b.com", "subject": secret, "body": "x"},
    )
    sink.close()

    # Open the raw file and grep for the canary.
    contents = db.read_bytes()
    assert secret.encode() not in contents, (
        "raw arg value found in telemetry db — privacy contract broken"
    )

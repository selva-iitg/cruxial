"""schema_origin: warn when 'canonical', record per-row, default 'model_visible'."""

from __future__ import annotations

import warnings as _w

import pytest

from cruxial import GuardConfig, guard
from cruxial.telemetry import NullSink


def test_default_is_model_visible(schemas, executors):
    c = guard(
        schemas=schemas, executors=executors,
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    assert c.config.schema_origin == "model_visible"


def test_canonical_warns_at_construction(schemas, executors):
    with _w.catch_warnings(record=True) as warns:
        _w.simplefilter("always")
        guard(
            schemas=schemas, executors=executors,
            config=GuardConfig(sinks=("null",), schema_origin="canonical"),
            sink=NullSink(),
        )
    matching = [w for w in warns if "schema_origin='canonical'" in str(w.message)]
    assert len(matching) == 1, f"expected exactly one canonical warning, got {[str(w.message) for w in warns]}"


def test_unknown_value_warns_at_construction(schemas, executors):
    with _w.catch_warnings(record=True) as warns:
        _w.simplefilter("always")
        guard(
            schemas=schemas, executors=executors,
            config=GuardConfig(sinks=("null",), schema_origin="something-else"),
            sink=NullSink(),
        )
    matching = [w for w in warns if "unknown schema_origin" in str(w.message)]
    assert len(matching) == 1


def test_schema_origin_is_recorded_on_every_row(schemas, executors):
    sink = NullSink()
    c = guard(
        schemas=schemas, executors=executors,
        config=GuardConfig(sinks=("null",)),
        sink=sink,
    )
    c.execute("send_email", {"to": "a@b.com", "subject": "hi", "body": "x"})
    c.execute("send_email", {"subject": "hi", "body": "x"})  # intercepted
    assert all(r.schema_origin == "model_visible" for r in sink.records)


def test_schema_origin_canonical_flows_into_records(schemas, executors):
    sink = NullSink()
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        c = guard(
            schemas=schemas, executors=executors,
            config=GuardConfig(sinks=("null",), schema_origin="canonical"),
            sink=sink,
        )
    c.execute("send_email", {"to": "a@b.com", "subject": "hi", "body": "x"})
    assert all(r.schema_origin == "canonical" for r in sink.records)


def test_canonical_warning_fires_even_with_strict_false(schemas, executors):
    """schema_origin is a correctness signal, fires regardless of strict mode."""
    with _w.catch_warnings(record=True) as warns:
        _w.simplefilter("always")
        guard(
            schemas=schemas, executors=executors,
            config=GuardConfig(sinks=("null",), schema_origin="canonical", strict=False),
            sink=NullSink(),
        )
    assert any("canonical" in str(w.message) for w in warns)

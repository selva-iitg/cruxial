"""Smoke-test the demo module so it can't ship broken.

These don't hit any LLM — they just verify the demo schemas are
self-consistent and integrate cleanly with the rest of the SDK.
"""

from __future__ import annotations

import asyncio
import inspect

from cruxial import GuardConfig, guard
from cruxial.demo import (
    DEMO_ANTHROPIC_TOOLS,
    DEMO_OPENAI_TOOLS,
    DEMO_PROMPTS,
    DEMO_TOOL_DESCRIPTIONS,
    DEMO_TOOL_EXECUTORS,
    DEMO_TOOL_EXECUTORS_SYNC,
    DEMO_TOOL_SCHEMAS,
)
from cruxial.telemetry import NullSink
from cruxial.testing import valid_payload, violation_payloads


def test_all_collections_share_the_same_tool_names():
    names = set(DEMO_TOOL_SCHEMAS)
    assert names == set(DEMO_TOOL_EXECUTORS)
    assert names == set(DEMO_TOOL_EXECUTORS_SYNC)
    assert names == set(DEMO_TOOL_DESCRIPTIONS)
    assert {t["function"]["name"] for t in DEMO_OPENAI_TOOLS} == names
    assert {t["name"] for t in DEMO_ANTHROPIC_TOOLS} == names


def test_schemas_are_loadable_into_guard():
    """The 5 demo schemas must build a valid Cruxial guard without raising."""
    c = guard(
        schemas=DEMO_TOOL_SCHEMAS,
        executors=DEMO_TOOL_EXECUTORS_SYNC,
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    for name in DEMO_TOOL_SCHEMAS:
        assert c.knows(name)


def test_valid_payload_passes_for_every_demo_tool():
    """The auto-generator should satisfy every demo schema's required fields."""
    c = guard(
        schemas=DEMO_TOOL_SCHEMAS,
        executors=DEMO_TOOL_EXECUTORS_SYNC,
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    failures: list[str] = []
    for name, schema in DEMO_TOOL_SCHEMAS.items():
        good = valid_payload(schema)
        res = c.check(name, good)
        if not res.ok:
            failures.append(f"{name}: {res.failure.category} → {res.failure.message}")
    assert not failures, (
        "valid_payload failed for: " + "; ".join(failures)
        + " — fix _valid_value in cruxial/testing.py to handle these cases"
    )


def test_violation_payloads_yield_at_least_3_categories_per_tool():
    """Each demo tool's schema is rich enough to support ≥3 violation classes."""
    weak: list[str] = []
    for name, schema in DEMO_TOOL_SCHEMAS.items():
        cats = set(violation_payloads(schema))
        if len(cats) < 3:
            weak.append(f"{name}: only {sorted(cats)}")
    assert not weak, "schemas too simple: " + "; ".join(weak)


def test_async_executors_run_cleanly():
    """The async no-op executors should be awaitable and return demo=True."""
    for name, fn in DEMO_TOOL_EXECUTORS.items():
        assert inspect.iscoroutinefunction(fn), f"{name} must be async"
        result = asyncio.run(fn(any_arg="x"))
        assert result["ok"] is True
        assert result["demo"] is True
        assert result["tool"] == name


def test_sync_executors_run_cleanly():
    for name, fn in DEMO_TOOL_EXECUTORS_SYNC.items():
        assert not inspect.iscoroutinefunction(fn), f"{name} sync version must NOT be async"
        result = fn(any_arg="x")
        assert result["ok"] is True


def test_prompt_set_covers_every_demo_tool():
    expected = {p["expected_tool"] for p in DEMO_PROMPTS}
    assert expected == set(DEMO_TOOL_SCHEMAS), (
        f"prompt coverage gap. expected: {sorted(expected)}, "
        f"schemas: {sorted(DEMO_TOOL_SCHEMAS)}"
    )


def test_each_prompt_has_required_keys():
    for i, p in enumerate(DEMO_PROMPTS):
        assert set(p) >= {"text", "expected_tool", "designed_to_trip"}, (
            f"prompt {i} missing keys: {set(p)}"
        )
        assert p["expected_tool"] in DEMO_TOOL_SCHEMAS
        assert len(p["text"]) > 20  # non-trivial prompts


def test_openai_tools_format_is_well_formed():
    for t in DEMO_OPENAI_TOOLS:
        assert t["type"] == "function"
        assert "name" in t["function"]
        assert "description" in t["function"]
        assert isinstance(t["function"]["parameters"], dict)


def test_anthropic_tools_format_is_well_formed():
    for t in DEMO_ANTHROPIC_TOOLS:
        assert "name" in t
        assert "description" in t
        assert isinstance(t["input_schema"], dict)

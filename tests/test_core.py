"""End-to-end behaviour of the guard() primitive."""

from __future__ import annotations

import pytest

from cruxial import GuardConfig, ToolUnknown, guard
from cruxial.telemetry import NullSink


def test_guard_rejects_mismatched_registries(schemas):
    with pytest.raises(ValueError):
        guard(
            schemas=schemas,
            executors={"send_email": lambda **kw: None},  # missing create_event
            sink=NullSink(),
        )


def test_happy_path_executes_and_returns_value(cruxial, executors):
    res = cruxial.execute(
        "send_email", {"to": "a@b.com", "subject": "hi", "body": "x"}
    )
    assert res.ok
    assert res.value == {"ok": True, "id": "msg_1"}
    assert res.failure is None
    assert res.latency_ms >= 0


def test_validation_failure_does_not_execute(cruxial, executors):
    res = cruxial.execute("send_email", {"subject": "hi", "body": "x"})
    assert not res.ok
    assert res.failure.category == "missing_required"
    # Executor must not have run.
    assert executors["send_email"]._sent == []


def test_unknown_tool_intercepted_and_raises_correctly(cruxial):
    res = cruxial.execute("nonexistent_tool", {})
    assert not res.ok
    assert res.failure.category == "unknown_tool"
    with pytest.raises(ToolUnknown):
        res.raise_on_failure()


def test_telemetry_records_each_call(sink, cruxial):
    canary_subject = "supersecret-canary-subject-token-9999"
    canary_email = "canary-recipient-7777@privacy-test.invalid"
    cruxial.execute("send_email", {"to": canary_email, "subject": canary_subject, "body": "x"})
    cruxial.execute("send_email", {"subject": "hi", "body": "x"})  # missing 'to'
    cruxial.execute("ghost_tool", {})

    statuses = [r.status for r in sink.records]
    assert statuses == ["passed", "intercepted", "intercepted"]
    # Privacy: args_hash is set, raw arg VALUES never appear in the record.
    for rec in sink.records:
        assert rec.args_hash
        as_str = str(rec)
        assert canary_subject not in as_str
        assert canary_email not in as_str


def test_execute_repaired_records_corrected(sink, cruxial):
    # First call fails validation
    bad = cruxial.execute("send_email", {"to": 42, "subject": "hi", "body": "x"})
    assert not bad.ok

    # User repairs externally and re-executes via the repaired path
    good = cruxial.execute_repaired(
        "send_email", {"to": "a@b.com", "subject": "hi", "body": "x"}
    )
    assert good.ok
    assert good.repaired
    assert good.repaired_args == {"to": "a@b.com", "subject": "hi", "body": "x"}

    statuses = [r.status for r in sink.records]
    assert statuses == ["intercepted", "passed", "corrected"]
    assert sink.records[-1].repaired is True


def test_executor_error_is_classified_separately(schemas):
    def broken(**kw):
        raise RuntimeError("upstream API down")

    c = guard(
        schemas={"send_email": schemas["send_email"]},
        executors={"send_email": broken},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    res = c.execute("send_email", {"to": "a@b.com", "subject": "hi", "body": "x"})
    assert not res.ok
    assert res.error is not None
    assert isinstance(res.error, RuntimeError)


def test_fail_open_when_validator_errors(schemas, executors, monkeypatch):
    """If our own validator crashes, the executor must still run."""
    from cruxial import core as core_mod

    def boom(*a, **kw):
        raise RuntimeError("simulated cruxial internal bug")

    monkeypatch.setattr(core_mod, "_validate", boom)

    c = guard(
        schemas=schemas,
        executors=executors,
        config=GuardConfig(fail_open=True, sinks=("null",)),
        sink=NullSink(),
    )
    import warnings as _w
    with _w.catch_warnings(record=True) as warns:
        _w.simplefilter("always")
        res = c.execute(
            "send_email", {"to": "a@b.com", "subject": "hi", "body": "x"}
        )
    assert res.ok  # tool ran despite validator crashing
    assert any("validator crashed" in str(w.message) for w in warns)


def test_check_only_mode_works_without_executors(schemas, sink):
    """Check-only mode — guard() without executors. Used when execution
    lives in an existing async/middleware path Cruxial shouldn't own."""
    c = guard(
        schemas=schemas,
        # no executors
        config=GuardConfig(sinks=("null",)),
        sink=sink,
    )

    res = c.check("send_email", {"to": "a@b.com", "subject": "hi", "body": "x"})
    assert res.ok
    assert res.value is None  # we did not execute
    assert res.failure is None

    res2 = c.check("send_email", {"subject": "hi", "body": "x"})  # missing 'to'
    assert not res2.ok
    assert res2.failure.category == "missing_required"

    statuses = [r.status for r in sink.records]
    assert statuses == ["passed", "intercepted"]


def test_check_validates_without_executing(cruxial, executors):
    """Even when executors are registered, .check() must NOT call them."""
    res = cruxial.check(
        "send_email", {"to": "a@b.com", "subject": "hi", "body": "x"}
    )
    assert res.ok
    assert res.value is None
    # Critically: the executor must NOT have run.
    assert executors["send_email"]._sent == []


def test_check_unknown_tool_is_intercepted(cruxial):
    res = cruxial.check("nope", {})
    assert not res.ok
    assert res.failure.category == "unknown_tool"


def test_knows_returns_true_for_registered_tools(cruxial):
    assert cruxial.knows("send_email")
    assert not cruxial.knows("not_a_tool")


def test_guard_without_executors_does_not_validate_keys(schemas):
    """When executors are None, schemas/executors key mismatch check is skipped."""
    c = guard(schemas=schemas, sink=NullSink())
    # Should not raise even though no executors were provided.
    assert c.knows("send_email")
    assert c.knows("create_event")


def test_strict_false_returns_noop_on_construction_error(schemas):
    """When strict=False, a key mismatch yields a no-op guard instead of raising."""
    from cruxial import NoopCruxial
    import warnings as _w
    with _w.catch_warnings(record=True) as warns:
        _w.simplefilter("always")
        # Deliberately mismatched executors → would raise with strict=True (default)
        c = guard(
            schemas=schemas,
            executors={"only_one": lambda **kw: None},
            config=GuardConfig(strict=False, sinks=("null",)),
        )
    assert isinstance(c, NoopCruxial)
    assert any("construction failed" in str(w.message) for w in warns)

    # All API methods are safe no-ops
    assert not c.knows("send_email")
    assert c.check("send_email", {"to": "a@b.com"}).ok  # silently passes
    assert c.build_repair_prompt(None, {}) == ""  # type: ignore[arg-type]


def test_strict_true_default_still_raises(schemas):
    """Default behavior unchanged — strict=True raises on key mismatch."""
    import pytest as _pytest
    with _pytest.raises(ValueError):
        guard(
            schemas=schemas,
            executors={"only_one": lambda **kw: None},
            # config omitted → defaults to strict=True
            sink=NullSink(),
        )


def test_noop_guard_in_recommended_integration_pattern(schemas):
    """The `if knows: check` pattern should silently bypass when guard is no-op."""
    from cruxial import NoopCruxial
    noop = NoopCruxial()

    # The recommended integration shape — `if knows: check`:
    bypassed = True
    if noop.knows("send_email"):
        res = noop.check("send_email", {})
        if not res.ok:
            bypassed = False  # would have entered the warning branch
    # knows() returned False → we never entered the check branch → bypassed cleanly
    assert bypassed


def test_register_schemas_adds_new_tools_at_runtime(schemas, executors, sink):
    """The Cruxial.register_schemas() public API — for hosts that have
    schemas in hand from their own discovery path (custom MCP transports,
    runtime tool registries, etc.)."""
    cx = guard(
        schemas=schemas, executors=executors,
        config=GuardConfig(sinks=("null",)),
        sink=sink,
    )
    starting = set(cx.schemas)

    extra = {
        "runtime_tool_a": {"type": "object", "properties": {"q": {"type": "string"}}, "required": ["q"]},
        "runtime_tool_b": {"type": "object", "properties": {"n": {"type": "integer", "minimum": 0}}, "required": ["n"]},
    }
    n = cx.register_schemas(extra)
    assert n == 2
    assert cx.knows("runtime_tool_a")
    assert cx.knows("runtime_tool_b")
    assert set(cx.schemas) == starting | {"runtime_tool_a", "runtime_tool_b"}

    # Sink got told about the new tools
    assert "runtime_tool_a" in sink.registered
    assert "runtime_tool_b" in sink.registered


def test_register_schemas_is_idempotent_and_updates_hashes(schemas, executors, sink):
    cx = guard(
        schemas=schemas, executors=executors,
        config=GuardConfig(sinks=("null",)),
        sink=sink,
    )
    cx.register_schemas({"x": {"type": "object", "properties": {"a": {"type": "string"}}}})
    h1 = cx._schema_hashes["x"]

    # Re-register with a different schema for the same name — hash should change
    cx.register_schemas({"x": {"type": "object", "properties": {"b": {"type": "integer"}}}})
    h2 = cx._schema_hashes["x"]
    assert h1 != h2


def test_register_schemas_then_check_validates_correctly(schemas, executors, sink):
    """The full flow: register a runtime tool, then call .check() on it."""
    cx = guard(
        schemas=schemas, executors=executors,
        config=GuardConfig(sinks=("null",)),
        sink=sink,
    )
    cx.register_schemas({
        "mcp_send_email": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "to": {"type": "string", "format": "email"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            "required": ["to", "subject", "body"],
        }
    })

    ok = cx.check("mcp_send_email", {"to": "a@b.com", "subject": "hi", "body": "x"})
    assert ok.ok

    bad = cx.check("mcp_send_email", {"to": "not-an-email", "subject": "hi", "body": "x"})
    assert not bad.ok
    assert bad.failure.category == "format_violation"


def test_register_schemas_honors_schema_origin_override(schemas, executors, sink):
    cx = guard(
        schemas=schemas, executors=executors,
        config=GuardConfig(sinks=("null",)),  # default schema_origin="model_visible"
        sink=sink,
    )
    cx.register_schemas(
        {"canonical_tool": {"type": "object"}},
        schema_origin="canonical",
    )
    # NullSink records the schema_origin it was told
    assert "canonical_tool" in sink.registered


def test_register_schemas_empty_is_noop(cruxial):
    assert cruxial.register_schemas({}) == 0


def test_build_repair_prompt_uses_schema_fragment_and_actual_args(cruxial):
    res = cruxial.execute(
        "send_email", {"to": "not-an-email", "subject": "hi", "body": "x"}
    )
    assert not res.ok
    prompt = cruxial.build_repair_prompt(res.failure, {"to": "not-an-email", "subject": "hi", "body": "x"})
    assert "send_email" in prompt
    assert "format_violation" in prompt
    assert "not-an-email" in prompt  # the literal bad value, not paraphrased
    # The schema fragment for the 'to' field should be inlined.
    assert "email" in prompt

"""Observability tests — cruxial must never be a blackbox.

The user's explicit ask: "The clearer it is while failing as well as
succeeding, more trust is gained. Never be a blackbox or partially opaque."

These tests verify that every cruxial behavior produces a clear, actionable
signal — either via the returned ExecutionResult, the telemetry record, a
typed exception, or a warning.
"""

from __future__ import annotations

import warnings
from dataclasses import asdict

from cruxial import GuardConfig, guard
from cruxial.telemetry import NullSink
from cruxial.types import Failure, InterceptionRecord


_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "to": {"type": "string", "format": "email"},
        "subject": {"type": "string", "maxLength": 200},
        "body": {"type": "string"},
        "priority": {"type": "string", "enum": ["low", "normal", "high"]},
    },
    "required": ["to", "subject", "body"],
}


def _cx():
    return guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )


# ─── 1. Failure objects carry everything a host needs to debug ───────


def test_failure_has_category_message_and_path():
    """Every Failure must have the 3 things a host needs to act:
    category (machine-readable), message (human-readable), path (which field)."""
    cx = _cx()
    res = cx.check("send_email", {"to": "not-an-email", "subject": "x", "body": "y"})
    assert not res.ok
    f = res.failure
    assert f.category, "category missing"
    assert f.message, "message missing"
    assert f.path == "to", f"path should be 'to', got {f.path!r}"


def test_failure_message_includes_the_actual_bad_value():
    """Trust signal: the failure message must show what the model emitted,
    so the host can debug without re-running."""
    cx = _cx()
    res = cx.check("send_email", {"to": "not-an-email", "subject": "x", "body": "y"})
    assert "not-an-email" in res.failure.message, (
        f"failure message {res.failure.message!r} doesn't include the bad value"
    )


def test_missing_required_failure_names_the_specific_field():
    cx = _cx()
    res = cx.check("send_email", {"subject": "x", "body": "y"})  # missing 'to'
    assert not res.ok
    # Either in path or in message — must name "to" specifically
    assert "to" in (res.failure.path or "") or "to" in res.failure.message


def test_enum_violation_message_lists_allowed_values():
    """The repair prompt depends on knowing what's allowed. Verify it's surfaced."""
    cx = _cx()
    res = cx.check("send_email", {"to": "a@b.com", "subject": "x", "body": "y", "priority": "urgent"})
    assert not res.ok
    # Allowed enum values appear in message
    msg = res.failure.message.lower()
    assert "low" in msg and "normal" in msg and "high" in msg, (
        f"enum message {res.failure.message!r} doesn't list allowed values"
    )


def test_constraint_violation_message_includes_the_constraint():
    """e.g., 'maxLength=200' should appear in a maxLength violation message."""
    cx = _cx()
    res = cx.check("send_email", {"to": "a@b.com", "subject": "x" * 300, "body": "y"})
    assert not res.ok
    msg = res.failure.message
    assert "200" in msg or "maxLength" in msg, (
        f"constraint message {msg!r} doesn't name the violated constraint"
    )


def test_unknown_tool_failure_lists_known_tools_for_easy_debug():
    """If a model calls an unknown tool, the failure should help the host
    figure out the typo — list the registered names."""
    cx = _cx()
    res = cx.check("send_emial", {})  # typo
    assert not res.ok
    assert res.failure.category == "unknown_tool"
    assert "send_email" in res.failure.message, (
        f"unknown_tool message {res.failure.message!r} doesn't list known tools"
    )


# ─── 2. ExecutionResult carries latency + repair status ─────────────


def test_execution_result_has_latency_in_milliseconds():
    cx = _cx()
    res = cx.check("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})
    assert res.latency_ms >= 0
    assert res.latency_ms < 1000, "validation took > 1s for trivial schema — perf regression"


def test_execution_result_repaired_flag_is_truthful():
    cx = _cx()
    # Plain check — not a repair
    res = cx.check("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})
    assert res.repaired is False

    # execute_repaired sets repaired=True on success
    cx = guard(
        schemas={"send_email": _SCHEMA},
        executors={"send_email": lambda **kw: {"ok": True}},
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )
    res = cx.execute_repaired("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})
    assert res.ok and res.repaired is True


def test_execution_result_raise_on_failure_surfaces_typed_exception():
    cx = _cx()
    res = cx.check("send_email", {"to": "not-an-email", "subject": "x", "body": "y"})
    import pytest
    with pytest.raises(Exception) as exc:
        res.raise_on_failure()
    # Typed CruxialError subclass — not raw ValueError
    from cruxial.errors import CruxialError
    assert isinstance(exc.value, CruxialError)


# ─── 3. Telemetry records carry diagnostic info ──────────────────────


def test_telemetry_record_has_all_diagnostic_fields():
    """An InterceptionRecord must include everything needed to debug a
    failure without re-running the agent."""
    sink = NullSink()
    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("null",)),
        sink=sink,
    )
    cx.check("send_email", {"to": "not-an-email", "subject": "x", "body": "y"})

    assert len(sink.records) == 1
    rec = sink.records[0]

    # Required fields for triage
    assert rec.timestamp
    assert rec.tool == "send_email"
    assert rec.status == "intercepted"
    assert rec.failure_category == "format_violation"
    assert rec.failure_path == "to"
    assert rec.args_hash  # not the raw value, but a stable identifier
    assert rec.schema_hash
    assert rec.latency_ms >= 0
    assert rec.cruxial_version  # so bug reports include the SDK version


def test_telemetry_record_distinguishes_passed_from_intercepted():
    sink = NullSink()
    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("null",)),
        sink=sink,
    )
    # 2 passes, 1 intercept
    for _ in range(2):
        cx.check("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})
    cx.check("send_email", {"to": "bad", "subject": "x", "body": "y"})

    statuses = [r.status for r in sink.records]
    assert statuses.count("passed") == 2
    assert statuses.count("intercepted") == 1


def test_telemetry_record_passes_also_log_so_stats_can_compute_rate():
    """Without 'passed' rows, you can't compute intercept-rate from telemetry.
    Verify the happy path emits a row."""
    sink = NullSink()
    cx = guard(
        schemas={"send_email": _SCHEMA},
        config=GuardConfig(sinks=("null",)),
        sink=sink,
    )
    cx.check("send_email", {"to": "a@b.com", "subject": "x", "body": "y"})
    assert len(sink.records) == 1
    assert sink.records[0].status == "passed"


# ─── 4. Warnings, not silent failures ────────────────────────────────


def test_construction_with_unknown_schema_origin_warns():
    """A typo'd `schema_origin` should warn, not silently default."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        guard(
            schemas={"t": {"type": "object"}},
            config=GuardConfig(
                sinks=("null",),
                schema_origin="typo",  # not in {"model_visible", "canonical"}
            ),
            sink=NullSink(),
        )
    assert any("unknown schema_origin" in str(warning.message) for warning in w)


def test_canonical_schema_origin_warns_about_false_positive_risk():
    """The schema_origin='canonical' warning must mention the false-positive
    risk, so consumers know why they're being warned."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        guard(
            schemas={"t": {"type": "object"}},
            config=GuardConfig(sinks=("null",), schema_origin="canonical"),
            sink=NullSink(),
        )
    matching = [warning for warning in w if "canonical" in str(warning.message)]
    assert matching, "schema_origin='canonical' didn't surface a warning"
    msg = str(matching[0].message)
    assert "missing_required" in msg or "false" in msg or "model" in msg, (
        f"warning {msg!r} doesn't explain the false-positive risk"
    )


# ─── 5. CLI diagnostic surface is honest ────────────────────────────


def test_cli_diagnostic_includes_version_and_db_path(capsys, tmp_path, monkeypatch):
    """`cruxial diagnostic` must tell the user:
      - cruxial version (for bug reports)
      - which sqlite db is in use
      - whether that db exists
      - HOW that path was resolved (env var / project-local / home fallback)
    """
    from cruxial.cli import main

    monkeypatch.setenv("CRUXIAL_DB_PATH", str(tmp_path / "test.sqlite"))
    code = main(["diagnostic"])
    assert code == 0

    out = capsys.readouterr().out
    assert "cruxial" in out
    assert "0.1.0" in out  # version
    assert "db source" in out
    assert "CRUXIAL_DB_PATH" in out  # surface WHY this path was chosen


def test_cli_stats_with_no_db_explains_what_to_do(capsys, tmp_path, monkeypatch):
    """`cruxial stats` with no db must NOT just say "no db" — it must tell
    the user how to point at one."""
    from cruxial.cli import main

    # Point at a guaranteed-empty path
    nonexistent = tmp_path / "nope" / "telemetry.sqlite"
    monkeypatch.setenv("CRUXIAL_DB_PATH", str(nonexistent))
    code = main(["stats"])
    assert code == 1

    err = capsys.readouterr().err
    assert "no telemetry database" in err
    assert "--db" in err or "CRUXIAL_DB_PATH" in err  # tell them how to fix it


# ─── 6. Repair prompt content is debuggable ─────────────────────────


def test_repair_prompt_includes_tool_name():
    cx = _cx()
    res = cx.check("send_email", {"to": "bad", "subject": "x", "body": "y"})
    prompt = cx.build_repair_prompt(res.failure, {"to": "bad"})
    assert "send_email" in prompt


def test_repair_prompt_includes_actual_failed_args():
    """Critical: the model needs to see what IT sent, not paraphrased."""
    cx = _cx()
    res = cx.check("send_email", {"to": "bad-email", "subject": "x", "body": "y"})
    prompt = cx.build_repair_prompt(res.failure, {"to": "bad-email", "subject": "x", "body": "y"})
    assert "bad-email" in prompt


def test_repair_prompt_includes_failure_category():
    cx = _cx()
    res = cx.check("send_email", {"to": "bad-email", "subject": "x", "body": "y"})
    prompt = cx.build_repair_prompt(res.failure, {"to": "bad-email"})
    assert "format_violation" in prompt


def test_repair_prompt_for_multi_violation_enumerates_each():
    """The model needs to see ALL failures so it fixes them in one go."""
    cx = _cx()
    res = cx.check(
        "send_email",
        {
            "to": "bad-email",
            "subject": "x" * 300,
            "body": "y",
            "priority": "urgent",
        },
    )
    assert not res.ok
    prompt = cx.build_repair_prompt(res.failure, {})
    # Multiple violations enumerated
    assert "format_violation" in prompt or "constraint_violation" in prompt
    # And the prompt tells the model to fix them all
    assert "all" in prompt.lower() or "every" in prompt.lower()


# ─── 7. Cruxial version + diagnostic-ability ────────────────────────


def test_cruxial_exposes_version_for_bug_reports():
    import cruxial
    assert cruxial.__version__
    assert cruxial.__version__ != ""


def test_cruxial_has_public_diagnostic_command():
    """`cruxial diagnostic` is the documented entry-point for bug reports."""
    from cruxial.cli import main

    code = main(["diagnostic"])
    assert code == 0

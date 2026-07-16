"""Fail-open must be VISIBLE.

When Cruxial's own action-layer stage crashes (a genuine Cruxial-internal
failure — receipt adapters and verify hooks already fail-open one level down),
the op is recorded as `unknown` / "unattested", NOT a silent success — while the
host tool's value still returns.

Double-validated by two independent production reviewers: a fail-open that records
success hides the audit hole exactly on the runs where Cruxial itself misbehaved,
which is when you most want the record.
"""
from __future__ import annotations

import pytest

from cruxial.core import guard, GuardConfig
from cruxial.actions import ActionRegistry
from cruxial.receipts import ReceiptRegistry, id_field

RESULT = {"id": "ch_1", "amount": 999}


def _boom(*a, **k):
    raise RuntimeError("stage boom")


def _charge_guard(fail_open=True, executor=None):
    ar = ActionRegistry(); ar.mark_action("charge")
    rr = ReceiptRegistry(); rr.register("charge", id_field("id", kind="charge"))
    return guard(
        {"charge": {"type": "object"}},
        {"charge": executor or (lambda **k: RESULT)},
        receipt_registry=rr, action_registry=ar,
        config=GuardConfig(sinks=("null",), fail_open=fail_open),
    )


def test_stage_crash_records_unattested_unknown(monkeypatch):
    cx = _charge_guard()
    monkeypatch.setattr(ReceiptRegistry, "adapt", _boom)   # force a Cruxial-internal crash in _finalize
    with pytest.warns(UserWarning, match="unattested"):
        r = cx.execute("charge", {"amount": 5})
    # host still runs — value flows, ok keeps its 0.4 meaning
    assert r.ok and r.value == RESULT
    # but the hole is VISIBLE (unknown), never dressed as a silent success
    assert r.state == "unknown"
    assert r.operation is not None and "unattested" in (r.operation.note or "")


def test_stage_crash_fail_closed_reraises(monkeypatch):
    # fail_open=False (money-path strict mode) still re-raises, unchanged.
    cx = _charge_guard(fail_open=False)
    monkeypatch.setattr(ReceiptRegistry, "adapt", _boom)
    with pytest.raises(RuntimeError, match="stage boom"):
        cx.execute("charge", {"amount": 5})


def test_unattested_backstop_never_breaks_host(monkeypatch):
    # Even if recording the hole ITSELF fails, the host must still get its value.
    cx = _charge_guard()
    monkeypatch.setattr(ReceiptRegistry, "adapt", _boom)
    monkeypatch.setattr(type(cx), "_record_unattested", _boom)
    with pytest.warns(UserWarning):
        r = cx.execute("charge", {"amount": 5})
    assert r.ok and r.value == RESULT   # fail-open is absolute


async def test_astage_crash_records_unattested_unknown(monkeypatch):
    async def acharge(**k):
        return RESULT

    cx = _charge_guard(executor=acharge)
    monkeypatch.setattr(ReceiptRegistry, "adapt", _boom)
    with pytest.warns(UserWarning, match="unattested"):
        r = await cx.aexecute("charge", {"amount": 5})
    assert r.ok and r.value == RESULT
    assert r.state == "unknown"
    assert r.operation is not None and "unattested" in (r.operation.note or "")

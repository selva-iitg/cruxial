"""The action-layer stage wired into Cruxial.execute()/aexecute().

Verifies: 0.4 behaviour is byte-identical for an uninstrumented tool; an
@action tool resolves to posted/failed/unknown/needs_review from its receipt;
the stage is fail-open; and the async path works. Uses explicit registries
(no global decorator state) for isolation.
"""
from __future__ import annotations

import pytest

from cruxial import GuardConfig, guard
from cruxial.actions import ActionRegistry, HALT, PASS
from cruxial.ledger import Ledger
from cruxial.receipts import ReceiptRegistry, id_field
from cruxial.telemetry import NullSink, SqliteSink
from cruxial.types import Receipt


def build(executors, ar, rr, sink, config=None):
    schemas = {n: {"type": "object"} for n in executors}
    return guard(schemas, executors, sink=sink, config=config,
                 receipt_registry=rr, action_registry=ar)


# ─── 0.4 compatibility ───────────────────────────────────────────────────────


def test_uninstrumented_tool_is_exact_0_4():
    ar, rr, sink = ActionRegistry(), ReceiptRegistry(), NullSink()
    cx = build({"lookup": lambda **k: {"row": 1}}, ar, rr, sink)
    r = cx.execute("lookup", {"id": "a"})
    assert r.ok and r.value == {"row": 1}
    assert r.state is None and r.receipt is None and r.op_id is None
    assert sink.operations == []  # no ledger row for an uninstrumented tool


# ─── action resolution ───────────────────────────────────────────────────────


def test_action_with_receipt_posts():
    ar, rr, sink = ActionRegistry(), ReceiptRegistry(), NullSink()
    ar.mark_action("send_email")
    rr.register("send_email", id_field("message_id", kind="email"))
    cx = build({"send_email": lambda **k: {"message_id": "m1"}}, ar, rr, sink)
    r = cx.execute("send_email", {"to": "x"})
    assert r.ok and r.state == "posted"
    assert r.receipt.id == "m1" and r.op_id
    assert len(sink.operations) == 1 and sink.operations[0].state == "posted"


def test_action_with_no_evidence_is_unknown_with_guided_hint():
    ar, rr, sink = ActionRegistry(), ReceiptRegistry(), NullSink()
    ar.mark_action("send_email")  # no adapter registered → default → no id
    cx = build({"send_email": lambda **k: {"ok": True}}, ar, rr, sink)
    r = cx.execute("send_email", {"to": "x"})
    assert r.state == "unknown"
    note = sink.operations[0].note or ""
    assert "cruxial.receipt" in note  # the guided one-liner is persisted


def test_action_returning_empty_is_unknown():
    ar, rr, sink = ActionRegistry(), ReceiptRegistry(), NullSink()
    ar.mark_action("send_email")
    cx = build({"send_email": lambda **k: None}, ar, rr, sink)
    assert cx.execute("send_email", {}).state == "unknown"


def test_action_with_not_ok_receipt_is_failed():
    ar, rr, sink = ActionRegistry(), ReceiptRegistry(), NullSink()
    ar.mark_action("send_email")
    rr.register("send_email", lambda raw: Receipt(ok=False, id="m1", kind="email"))
    cx = build({"send_email": lambda **k: object()}, ar, rr, sink)
    assert cx.execute("send_email", {}).state == "failed"


def test_verify_halt_with_evidence_is_needs_review():
    ar, rr, sink = ActionRegistry(), ReceiptRegistry(), NullSink()
    ar.mark_action("send_email")
    rr.register("send_email", id_field("message_id", kind="email"))
    ar.register_verify("send_email", lambda a, r: HALT("recipient blocked"))
    cx = build({"send_email": lambda **k: {"message_id": "m1"}}, ar, rr, sink)
    r = cx.execute("send_email", {})
    assert r.state == "needs_review"
    assert "recipient blocked" in (sink.operations[0].note or "")


def test_non_action_with_verify_hook_engages_and_posts():
    ar, rr, sink = ActionRegistry(), ReceiptRegistry(), NullSink()
    ar.register_verify("lookup", lambda a, r: PASS)  # hook but not an action
    cx = build({"lookup": lambda **k: {"x": 1}}, ar, rr, sink)
    r = cx.execute("lookup", {})
    assert r.state == "posted" and len(sink.operations) == 1


# ─── fail-open ───────────────────────────────────────────────────────────────


def test_adapter_crash_resolves_unknown_not_posted():
    ar, rr, sink = ActionRegistry(), ReceiptRegistry(), NullSink()
    ar.mark_action("send_email")

    def boom(raw):
        raise RuntimeError("adapter crash")

    rr.register("send_email", boom)
    cx = build({"send_email": lambda **k: {"message_id": "m1"}}, ar, rr, sink)
    # adapt() is fail-open → adapter_error receipt (no evidence) → unknown,
    # never a silent posted.
    assert cx.execute("send_email", {}).state == "unknown"


def test_stage_failure_degrades_to_0_4(monkeypatch):
    from cruxial import ledger as ledger_mod

    ar, rr, sink = ActionRegistry(), ReceiptRegistry(), NullSink()
    ar.mark_action("send_email")
    rr.register("send_email", id_field("message_id"))
    cx = build({"send_email": lambda **k: {"message_id": "m1"}}, ar, rr, sink)

    def explode(*a, **k):
        raise RuntimeError("stage boom")

    # crash the stage at state resolution (past the fail-open sub-layers) to
    # exercise _finalize's own fallback.
    monkeypatch.setattr(ledger_mod.StateResolver, "resolve", staticmethod(explode))
    with pytest.warns(UserWarning):
        r = cx.execute("send_email", {})
    assert r.ok and r.value == {"message_id": "m1"}  # tool result still returns
    assert r.state is None  # stage didn't complete


def test_ledger_off_resolves_but_does_not_persist():
    ar, rr, sink = ActionRegistry(), ReceiptRegistry(), NullSink()
    ar.mark_action("send_email")
    rr.register("send_email", id_field("message_id"))
    cx = build(
        {"send_email": lambda **k: {"message_id": "m1"}}, ar, rr, sink,
        config=GuardConfig(ledger=False),
    )
    r = cx.execute("send_email", {})
    assert r.state == "posted"  # still resolved in-process
    assert sink.operations == []  # but not written to the ledger


# ─── async path ──────────────────────────────────────────────────────────────


async def test_aexecute_action_posts():
    ar, rr, sink = ActionRegistry(), ReceiptRegistry(), NullSink()
    ar.mark_action("send_email")
    rr.register("send_email", id_field("message_id"))
    cx = build({"send_email": lambda **k: {"message_id": "m1"}}, ar, rr, sink)
    r = await cx.aexecute("send_email", {})
    assert r.state == "posted" and r.receipt.id == "m1"


async def test_aexecute_async_verify_halts():
    ar, rr, sink = ActionRegistry(), ReceiptRegistry(), NullSink()
    ar.mark_action("send_email")
    rr.register("send_email", id_field("message_id"))

    async def ahook(a, r):
        return HALT("async blocked")

    ar.register_verify("send_email", ahook)
    cx = build({"send_email": lambda **k: {"message_id": "m1"}}, ar, rr, sink)
    r = await cx.aexecute("send_email", {})
    assert r.state == "needs_review"


# ─── end-to-end over sqlite (the real ledger + reads) ────────────────────────


def test_protection_registry_persisted(tmp_path):
    ar, rr = ActionRegistry(), ReceiptRegistry()
    ar.mark_action("send_email")
    rr.register("send_email", id_field("message_id"))
    ar.register_verify("send_email", lambda a, r: PASS, label="non-empty body")
    sink = SqliteSink(tmp_path / "t.sqlite")
    build({"send_email": lambda **k: {"message_id": "m1"}, "lookup": lambda **k: {"x": 1}},
          ar, rr, sink)  # guard() persists the action registry at construction
    prot = {p["tool"]: p for p in Ledger(sink).protection()}
    assert prot["send_email"] == {
        "tool": "send_email", "is_action": True, "has_receipt": True,
        "verify_count": 1, "verify_labels": ["non-empty body"]}
    assert prot["lookup"]["is_action"] is False
    sink.close()


def test_end_to_end_sqlite_ledger(tmp_path):
    ar, rr = ActionRegistry(), ReceiptRegistry()
    ar.mark_action("send_email")
    rr.register("send_email", id_field("message_id", kind="email"))
    sink = SqliteSink(tmp_path / "t.sqlite")
    cx = build({"send_email": lambda **k: {"message_id": "m1"}}, ar, rr, sink)
    cx.execute("send_email", {"to": "x@y.com"})
    led = Ledger(sink)
    recent = led.recent()
    assert len(recent) == 1 and recent[0].state == "posted"
    assert recent[0].receipt.id == "m1"
    assert led.state_counts().get("posted") == 1
    sink.close()

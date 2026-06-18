"""The action ledger — StateResolver invariant + append/read round-trip.

The load-bearing test is the invariant: an action NEVER reaches `posted`
without a real receipt — that's the whole "only a receipt advances state".
"""
from __future__ import annotations

from cruxial.actions import FLAG, HALT, PASS
from cruxial.ledger import Ledger, StateResolver, new_op_id
from cruxial.telemetry import NullSink, SqliteSink
from cruxial.types import Operation, Receipt

resolve = StateResolver.resolve

EVID_OK = Receipt(ok=True, id="msg_1", kind="email")
EVID_FAIL = Receipt(ok=False, id="msg_1", kind="email")
GENERIC = Receipt(ok=True, kind="generic")  # weak success, NO evidence


# ─── op id ───────────────────────────────────────────────────────────────────


def test_new_op_id_format_and_uniqueness():
    ids = {new_op_id() for _ in range(1000)}
    assert len(ids) == 1000  # no collisions in-process
    assert all(i.startswith("op_") for i in ids)


# ─── StateResolver truth table ───────────────────────────────────────────────


def test_non_action_posts_or_reviews():
    assert resolve(False, None, PASS) == "posted"
    assert resolve(False, GENERIC, PASS) == "posted"
    assert resolve(False, None, HALT("x")) == "needs_review"


def test_action_with_evidence_resolves_on_receipt_ok():
    assert resolve(True, EVID_OK, PASS) == "posted"
    assert resolve(True, EVID_FAIL, PASS) == "failed"


def test_action_halt_with_evidence_is_needs_review():
    assert resolve(True, EVID_OK, HALT("blocked")) == "needs_review"


def test_flag_is_advisory_does_not_block_state():
    assert resolve(True, EVID_OK, FLAG("suspicious")) == "posted"
    assert resolve(True, EVID_FAIL, FLAG("suspicious")) == "failed"


def test_resolver_accepts_bare_string_verdict():
    assert resolve(True, EVID_OK, "pass") == "posted"
    assert resolve(True, EVID_OK, "halt") == "needs_review"


# THE INVARIANT — no posted without a real receipt
def test_invariant_action_without_evidence_is_always_unknown():
    for verdict in (PASS, FLAG("x"), HALT("x"), "pass", "flag"):
        assert resolve(True, None, verdict) == "unknown"
        assert resolve(True, GENERIC, verdict) == "unknown"  # weak success ≠ proof


def test_invariant_never_posts_without_receipt_ok():
    # exhaustive-ish: an action only posts when has_evidence AND receipt.ok
    assert resolve(True, GENERIC, PASS) != "posted"          # no evidence
    assert resolve(True, EVID_FAIL, PASS) != "posted"        # evidence but not ok
    assert resolve(True, EVID_OK, PASS) == "posted"          # the only posting path


# ─── Ledger append + read (sqlite) ───────────────────────────────────────────


def _op(tool="send_email", state="posted", actor="bot", receipt=EVID_OK, note=None):
    return Operation(
        op_id=new_op_id(), tool=tool, state=state, ts_intent="2026-06-12T00:00:00.000+00:00",
        actor=actor, requested={"to": "x@y.com"}, receipt=receipt, note=note,
    )


def test_ledger_append_and_recent(tmp_path):
    sink = SqliteSink(tmp_path / "t.sqlite")
    led = Ledger(sink)
    op = _op()
    led.append(op)
    led.append(_op(tool="charge", state="unknown", receipt=None))
    recent = led.recent()
    assert len(recent) == 2
    got = led.get(op.op_id)
    assert got is not None and got.tool == "send_email" and got.state == "posted"
    assert got.receipt is not None and got.receipt.id == "msg_1"
    sink.close()


def test_ledger_privacy_no_raw_args_persisted(tmp_path):
    sink = SqliteSink(tmp_path / "t.sqlite")
    led = Ledger(sink)
    led.append(_op())
    # raw recipient must NOT be stored; only a hash column exists
    rows = sink.query("SELECT requested_hash FROM operations")
    assert rows and rows[0][0] is not None
    assert "x@y.com" not in str(rows)
    # the reconstructed Operation does not carry raw args back
    assert led.recent()[0].requested is None
    sink.close()


def test_ledger_state_counts(tmp_path):
    sink = SqliteSink(tmp_path / "t.sqlite")
    led = Ledger(sink)
    led.append(_op(state="posted"))
    led.append(_op(tool="a", state="unknown", receipt=None))
    led.append(_op(tool="b", state="unknown", receipt=None))
    counts = led.state_counts()
    assert counts["posted"] == 1 and counts["unknown"] == 2
    sink.close()


def test_ledger_append_is_fail_open_on_dumb_sink():
    class Dumb:  # no append_operation / no query
        pass

    led = Ledger(Dumb())
    led.append(_op())  # must not raise
    assert led.recent() == []
    assert led.state_counts() == {}


def test_ledger_works_over_nullsink():
    sink = NullSink()
    led = Ledger(sink)
    led.append(_op())
    assert len(sink.operations) == 1

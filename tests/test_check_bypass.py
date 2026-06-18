"""cx.check_bypass() — the safe detect+record primitive for hand-rolled loops.

It must do exactly what run() does internally: detect a claimed-but-never-called
action AND record it as a deterministic `unknown` op, in one call — so a manual
loop can't detect-then-forget-to-record (the footgun the bare detector invites).
"""
from __future__ import annotations

import cruxial
from cruxial import GuardConfig, Receipt, guard
from cruxial.actions import ActionRegistry
from cruxial.receipts import ReceiptRegistry, id_field
from cruxial.telemetry import SqliteSink


def _cx(tmp_path, capture_args=True):
    db = tmp_path / "cb.sqlite"
    ar = ActionRegistry(); ar.mark_action("send_email"); ar.mark_action("send_sms")
    rr = ReceiptRegistry()
    rr.register("send_email", id_field("id", kind="email"))
    rr.register("send_sms", id_field("id", kind="sms"))
    return guard(
        {"send_email": {"type": "object"}, "send_sms": {"type": "object"}},
        {"send_email": lambda **k: {"id": "m1"}, "send_sms": lambda **k: {"id": "s1"}},
        sink=SqliteSink(db), receipt_registry=rr, action_registry=ar,
        config=GuardConfig(actor="bot", capture_args=capture_args),
    )


def _unknowns(cx):
    return cx._ledger.state_counts().get("unknown", 0)


def test_detects_and_records_in_one_call(tmp_path):
    cx = _cx(tmp_path)
    sus = cx.check_bypass("I've sent the email to a@b.com.", called_tools=[])
    assert sus is not None and sus.tool == "send_email"
    assert _unknowns(cx) == 1                      # recorded, not just detected
    op = cx._ledger.recent(1)[0]
    assert op.state == "unknown" and op.tool == "send_email"


def test_sibling_not_flagged_when_action_already_ran(tmp_path):
    # send_email actually ran → a generic "sent" claim must NOT flag send_sms
    cx = _cx(tmp_path)
    assert cx.check_bypass("The email has been sent.", called_tools=["send_email"]) is None
    assert _unknowns(cx) == 0


def test_no_flag_when_a_tool_was_called_this_turn(tmp_path):
    cx = _cx(tmp_path)
    assert cx.check_bypass("Done, sent it.", tool_calls_this_turn=["send_email"]) is None
    assert _unknowns(cx) == 0


def test_no_flag_without_a_completion_claim(tmp_path):
    cx = _cx(tmp_path)
    assert cx.check_bypass("Sure — what should I do next?") is None
    assert _unknowns(cx) == 0


def test_tool_names_default_to_guard_registry(tmp_path):
    # no tool_names passed → uses the guard's registered tools
    cx = _cx(tmp_path)
    sus = cx.check_bypass("Email sent ✅", called_tools=[])
    assert sus is not None and sus.tool in ("send_email", "send_sms")


def test_capture_args_controls_claim_text(tmp_path):
    cx_on = _cx(tmp_path / "on", capture_args=True)
    cx_on.check_bypass("I've sent the email to alice@x.com.", called_tools=[])
    assert "alice@x.com" in (cx_on._ledger.recent(1)[0].claim or "")

    cx_off = _cx(tmp_path / "off", capture_args=False)
    cx_off.check_bypass("I've sent the email to alice@x.com.", called_tools=[])
    claim = cx_off._ledger.recent(1)[0].claim or ""
    assert "alice@x.com" not in claim and "send" in claim   # structured, non-PII


def test_explicit_tool_names_and_side_effecting(tmp_path):
    cx = _cx(tmp_path)
    sus = cx.check_bypass("I've charged the card.", tool_names=["charge_card"],
                          side_effecting=["charge_card"], called_tools=[])
    assert sus is not None and sus.tool == "charge_card"


def test_noop_guard_check_bypass_is_none():
    from cruxial.core import NoopCruxial
    assert NoopCruxial().check_bypass("I've sent the email.") is None

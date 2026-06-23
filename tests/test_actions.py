"""The action layer — @action / @verify, PASS/FLAG/HALT, built-in verifiers.

Covers verdict combination, the built-in receipt_required (HALT) + zero_latency
(FLAG), fail-open on a buggy/async hook, the async averify path, and the
decorators writing to the default registry.
"""
from __future__ import annotations

import pytest

from cruxial.types import Receipt
from cruxial.actions import (
    ActionRegistry,
    FLAG,
    HALT,
    PASS,
    action,
    default_action_registry,
    verify,
)

EVID = Receipt(ok=True, id="msg_1", kind="email")          # has evidence
GENERIC = Receipt(ok=True, kind="generic")                 # weak success, no proof


# ─── verdicts ────────────────────────────────────────────────────────────────


def test_verdict_helpers():
    assert PASS.kind == "pass"
    assert HALT("x").is_halt and HALT("x").reason == "x"
    assert FLAG("y").is_flag and FLAG("y").reason == "y"


# ─── built-in: receipt_required (the deterministic no-proof HALT) ────────────


def test_action_with_no_evidence_halts_with_guided_hint():
    reg = ActionRegistry()
    reg.mark_action("send_email")
    v = reg.verify("send_email", {}, GENERIC)
    assert v.is_halt
    # the HALT reason is a guided one-liner naming the fix
    assert "send_email" in v.reason and "cruxial.receipt" in v.reason


def test_action_with_evidence_passes():
    reg = ActionRegistry()
    reg.mark_action("send_email")
    assert reg.verify("send_email", {}, EVID).kind == "pass"


def test_non_action_never_halts_on_missing_receipt():
    reg = ActionRegistry()  # "lookup" not marked as an action
    assert reg.verify("lookup", {}, GENERIC).kind == "pass"
    assert reg.verify("lookup", {}, None).kind == "pass"


def test_action_with_no_receipt_object_halts():
    reg = ActionRegistry()
    reg.mark_action("charge")
    assert reg.verify("charge", {}, None).is_halt


# ─── built-in: zero_latency (advisory FLAG) ──────────────────────────────────


def test_zero_latency_flags_when_fast_with_evidence():
    reg = ActionRegistry()
    reg.mark_action("send_email")
    v = reg.verify("send_email", {}, EVID, latency_ms=0.01)
    assert v.is_flag and "real I/O" in v.reason


def test_normal_latency_passes():
    reg = ActionRegistry()
    reg.mark_action("send_email")
    assert reg.verify("send_email", {}, EVID, latency_ms=42.0).kind == "pass"


def test_no_evidence_halts_even_when_fast():
    # HALT (no proof) outranks the FLAG (fast) — severity wins.
    reg = ActionRegistry()
    reg.mark_action("send_email")
    assert reg.verify("send_email", {}, GENERIC, latency_ms=0.01).is_halt


# ─── user hooks + combination ────────────────────────────────────────────────


def test_user_hook_halt_combines_with_builtin():
    reg = ActionRegistry()
    reg.mark_action("send_email")
    reg.register_verify("send_email", lambda a, r: HALT("recipient not allowed"))
    v = reg.verify("send_email", {}, EVID)
    assert v.is_halt and "recipient not allowed" in v.reason


def test_user_hook_return_types():
    reg = ActionRegistry()
    reg.mark_action("t")
    # bool False -> halt ; True -> pass
    reg.register_verify("t", lambda a, r: False)
    assert reg.verify("t", {}, EVID).is_halt
    reg2 = ActionRegistry(); reg2.mark_action("t")
    reg2.register_verify("t", lambda a, r: True)
    assert reg2.verify("t", {}, EVID).kind == "pass"
    # string verdict
    reg3 = ActionRegistry(); reg3.mark_action("t")
    reg3.register_verify("t", lambda a, r: "flag")
    assert reg3.verify("t", {}, EVID).is_flag
    # None -> no objection
    reg4 = ActionRegistry(); reg4.mark_action("t")
    reg4.register_verify("t", lambda a, r: None)
    assert reg4.verify("t", {}, EVID).kind == "pass"


def test_flag_plus_halt_picks_halt():
    reg = ActionRegistry()
    reg.mark_action("t")
    reg.register_verify("t", lambda a, r: FLAG("minor"))
    reg.register_verify("t", lambda a, r: HALT("major"))
    v = reg.verify("t", {}, EVID)
    assert v.is_halt and "major" in v.reason and "minor" not in v.reason


# ─── fail-open ───────────────────────────────────────────────────────────────


def test_buggy_hook_is_fail_open():
    reg = ActionRegistry()
    reg.mark_action("t")

    def boom(a, r):
        raise RuntimeError("hook bug")

    reg.register_verify("t", boom)
    with pytest.warns(UserWarning):
        assert reg.verify("t", {}, EVID).kind == "pass"


def test_async_hook_in_sync_verify_is_skipped():
    reg = ActionRegistry()
    reg.mark_action("t")

    async def ahook(a, r):
        return HALT("should be skipped in sync path")

    reg.register_verify("t", ahook)
    with pytest.warns(UserWarning):
        assert reg.verify("t", {}, EVID).kind == "pass"


# ─── async averify ───────────────────────────────────────────────────────────


async def test_averify_awaits_async_hook():
    reg = ActionRegistry()
    reg.mark_action("t")

    async def ahook(a, r):
        return HALT("async halt")

    reg.register_verify("t", ahook)
    v = await reg.averify("t", {}, EVID)
    assert v.is_halt and "async halt" in v.reason


async def test_averify_runs_builtins_and_sync_hooks():
    reg = ActionRegistry()
    reg.mark_action("send_email")
    v = await reg.averify("send_email", {}, GENERIC)
    assert v.is_halt  # built-in receipt_required still fires


# ─── raw tool output threaded to opt-in hooks (3rd positional arg) ────────────


def test_accepts_value_arity_detection():
    from cruxial.actions import _accepts_value
    assert not _accepts_value(lambda a, r: PASS)            # 2 positional -> no
    assert _accepts_value(lambda a, r, out: PASS)           # 3 positional -> yes
    assert _accepts_value(lambda a, r, *rest: PASS)         # *args -> yes
    assert not _accepts_value(len)                          # un-mappable -> fail-open no


def test_verify_passes_raw_output_when_hook_opts_in():
    reg = ActionRegistry(); reg.mark_action("charge")
    seen = {}

    def check(args, receipt, output):
        seen["output"] = output
        return HALT("amount drift") if output["amount"] != args["amount"] else PASS

    reg.register_verify("charge", check)
    v = reg.verify("charge", {"amount": 5}, EVID, value={"id": "ch_1", "amount": 999})
    assert seen["output"] == {"id": "ch_1", "amount": 999}  # hook saw the RAW return
    assert v.is_halt
    # matching amount -> the same hook passes
    assert reg.verify("charge", {"amount": 5}, EVID, value={"amount": 5}).kind == "pass"


def test_two_arg_hook_unaffected_when_value_supplied():
    reg = ActionRegistry(); reg.mark_action("charge")
    reg.register_verify("charge", lambda a, r: PASS)        # legacy 2-arg hook
    # value is supplied but the 2-arg hook is still called the old way (no crash)
    assert reg.verify("charge", {}, EVID, value={"anything": 1}).kind == "pass"


def test_opt_in_hook_without_value_fails_open():
    # A 3-arg hook called with no value (direct verify, value=_UNSET) can't get
    # its 3rd arg; the call fails open to PASS rather than crashing.
    reg = ActionRegistry(); reg.mark_action("charge")
    reg.register_verify("charge", lambda a, r, out: HALT("x"))
    with pytest.warns(UserWarning):
        assert reg.verify("charge", {}, EVID).kind == "pass"


def test_execute_threads_raw_output_to_hook():
    from cruxial.core import guard, GuardConfig
    from cruxial.actions import ActionRegistry as AR
    from cruxial.receipts import ReceiptRegistry, id_field
    ar = AR(); ar.mark_action("charge")
    rr = ReceiptRegistry(); rr.register("charge", id_field("id", kind="charge"))
    ar.register_verify("charge",
                       lambda args, receipt, output: HALT("drift") if output["amount"] != args["amount"] else PASS)
    cx = guard({"charge": {"type": "object"}},
               {"charge": lambda **k: {"id": "ch_1", "amount": 999}},
               receipt_registry=rr, action_registry=ar, config=GuardConfig(sinks=("null",)))
    r = cx.execute("charge", {"amount": 5})
    assert r.state == "needs_review"   # hook HALTed on the raw-output drift


async def test_aexecute_threads_raw_output_to_async_hook():
    from cruxial.core import guard, GuardConfig
    from cruxial.actions import ActionRegistry as AR
    from cruxial.receipts import ReceiptRegistry, id_field
    ar = AR(); ar.mark_action("charge")
    rr = ReceiptRegistry(); rr.register("charge", id_field("id", kind="charge"))

    async def acheck(args, receipt, output):
        return HALT("drift") if output["amount"] != args["amount"] else PASS

    ar.register_verify("charge", acheck)

    async def aexec(**k):
        return {"id": "ch_1", "amount": 999}

    cx = guard({"charge": {"type": "object"}}, {"charge": aexec},
               receipt_registry=rr, action_registry=ar, config=GuardConfig(sinks=("null",)))
    r = await cx.aexecute("charge", {"amount": 5})
    assert r.state == "needs_review"


# ─── decorators ──────────────────────────────────────────────────────────────


def test_action_decorator_bare_and_param():
    reg = default_action_registry()
    reg.clear()
    try:

        @action
        def send_email(to, body):
            return None

        @action(tool="charge_card")
        def _charge(x):
            return None

        assert reg.is_action("send_email")
        assert reg.is_action("charge_card")
        assert send_email.__cruxial_action__ == "send_email"
    finally:
        reg.clear()


def test_verify_labels():
    reg = ActionRegistry()
    reg.mark_action("t")
    reg.register_verify("t", lambda a, r: PASS, label="explicit rule")

    def named_rule(a, r):
        return PASS

    reg.register_verify("t", named_rule)            # falls back to the function name
    reg.register_verify("t", lambda a, r: PASS)     # anonymous + no label → omitted
    assert reg.verify_labels("t") == ["explicit rule", "named_rule"]
    assert reg.verify_count("t") == 3               # count still reflects all three


def test_verify_decorator_registers_and_returns_hook():
    reg = default_action_registry()
    reg.clear()
    try:
        reg.mark_action("issue_refund")

        @verify("issue_refund")
        def check(args, receipt):
            return HALT("over policy") if args["amount"] > 1000 else PASS

        # registered onto the default registry
        assert reg.verify("issue_refund", {"amount": 50}, Receipt(ok=True, id="x", kind="refund")).kind == "pass"
        # and still independently callable
        assert check({"amount": 5000}, Receipt(ok=True, id="x", kind="refund")).is_halt
    finally:
        reg.clear()

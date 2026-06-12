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


def test_verify_decorator_registers_and_returns_hook():
    reg = default_action_registry()
    reg.clear()
    try:
        reg.mark_action("send_email")

        @verify("send_email")
        def check(args, receipt):
            return HALT("no id") if receipt.id is None else PASS

        # registered onto the default registry
        assert reg.verify("send_email", {}, Receipt(ok=True, id="x", kind="email")).kind == "pass"
        # and still independently callable
        assert check({}, Receipt(ok=False, kind="generic")).is_halt
    finally:
        reg.clear()

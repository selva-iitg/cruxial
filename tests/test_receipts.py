"""Receipt registry — normalize raw executor returns into typed Receipts.

Covers the default "null-is-not-success" adapter, the False/0 trap, the
fail-open behaviour of a buggy adapter, the reusable factories, and the
decorator writing to the default registry.
"""
from __future__ import annotations

import pytest

from cruxial.types import Receipt
from cruxial.receipts import (
    ReceiptRegistry,
    default_adapter,
    default_registry,
    has_evidence,
    http_receipt,
    id_field,
    receipt,
)


# ─── default adapter: prove-it-worked, not assume ────────────────────────────


@pytest.mark.parametrize("raw", [None, "", [], {}, (), set(), False])
def test_default_adapter_empty_is_not_ok(raw):
    r = default_adapter(raw)
    assert r.ok is False
    assert r.kind == "generic"
    assert r.id is None


@pytest.mark.parametrize("raw", [0, 0.0, "x", {"a": 1}, [1], 42])
def test_default_adapter_nonempty_is_weak_ok(raw):
    # A non-empty return is a WEAK ok — no evidence id, kind="generic".
    r = default_adapter(raw)
    assert r.ok is True
    assert r.kind == "generic"
    assert r.id is None


def test_zero_is_not_false_trap():
    # 0 == False in Python; the adapter must not treat 0 as empty.
    assert default_adapter(0).ok is True
    assert default_adapter(False).ok is False


def test_default_adapter_passes_through_a_receipt():
    given = Receipt(ok=True, id="msg_1", kind="email")
    assert default_adapter(given) is given


# ─── has_evidence ────────────────────────────────────────────────────────────


def test_has_evidence():
    assert has_evidence(None) is False
    assert has_evidence(Receipt(ok=True, kind="generic")) is False  # weak success
    assert has_evidence(Receipt(ok=True, id="x", kind="generic")) is True
    assert has_evidence(Receipt(ok=True, evidence={"webhook": "delivered"})) is True
    assert has_evidence(Receipt(ok=True, kind="email")) is False  # kind alone is not proof
    assert has_evidence(Receipt(ok=False, kind="adapter_error")) is False  # a crash is not proof


# ─── registry ────────────────────────────────────────────────────────────────


def test_registry_uses_registered_adapter():
    reg = ReceiptRegistry()
    reg.register("send_email", id_field("message_id", kind="email"))
    assert reg.has("send_email") is True
    r = reg.adapt("send_email", {"message_id": "abc123"})
    assert r.ok is True and r.id == "abc123" and r.kind == "email"


def test_registry_falls_back_to_default():
    reg = ReceiptRegistry()
    assert reg.has("unknown_tool") is False
    assert reg.adapt("unknown_tool", "something").ok is True
    assert reg.adapt("unknown_tool", None).ok is False


def test_registry_adapt_is_fail_open_on_raise():
    reg = ReceiptRegistry()

    def boom(raw):
        raise RuntimeError("adapter blew up")

    reg.register("t", boom)
    r = reg.adapt("t", {"x": 1})
    assert r.ok is False and r.kind == "adapter_error"


def test_registry_adapt_rejects_non_receipt_return():
    reg = ReceiptRegistry()
    reg.register("t", lambda raw: {"not": "a receipt"})  # type: ignore[arg-type,return-value]
    r = reg.adapt("t", {"x": 1})
    assert r.ok is False and r.kind == "adapter_error"


def test_registry_clear():
    reg = ReceiptRegistry()
    reg.register("t", id_field("id"))
    reg.clear()
    assert reg.has("t") is False


# ─── factories ───────────────────────────────────────────────────────────────


def test_id_field_no_id_is_not_ok():
    adapter = id_field("message_id", kind="email")
    assert adapter({"message_id": "m1"}).ok is True
    out = adapter({"other": "x"})
    assert out.ok is False and out.id is None


def test_id_field_duck_types_attr_and_key():
    adapter = id_field("message_id")

    class Resp:
        message_id = "from_attr"

    assert adapter(Resp()).id == "from_attr"
    assert adapter({"message_id": "from_key"}).id == "from_key"


def test_id_field_multiple_names():
    adapter = id_field("message_id", "id", kind="email")
    assert adapter({"id": "fallback"}).id == "fallback"


def test_http_receipt_status():
    assert http_receipt({"status_code": 200}).ok is True
    assert http_receipt({"status_code": 204, "Location": "/r/1"}).id == "/r/1"
    assert http_receipt({"status_code": 500}).ok is False
    assert http_receipt({}).ok is False  # no status → not ok


# ─── decorator + default registry ────────────────────────────────────────────


def test_receipt_decorator_registers_on_default_registry():
    reg = default_registry()
    reg.clear()
    try:

        @receipt("charge_card")
        def _(raw) -> Receipt:
            return Receipt(ok=raw.get("paid", False), id=raw.get("event_id"), kind="payment")

        out = reg.adapt("charge_card", {"paid": True, "event_id": "evt_9"})
        assert out.ok is True and out.id == "evt_9" and out.kind == "payment"
    finally:
        reg.clear()


def test_receipt_decorator_returns_fn_unchanged():
    reg = default_registry()
    reg.clear()
    try:

        @receipt("t")
        def my_adapter(raw) -> Receipt:
            return Receipt(ok=True, id="x")

        # still independently callable
        assert my_adapter({"any": 1}).id == "x"
    finally:
        reg.clear()

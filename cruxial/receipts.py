"""Receipt registry — turn a raw executor return value into a typed `Receipt`.

The receipt is the EXECUTOR's proof a side effect happened (a provider
message-id, an exit code, a committed row version) — never the model's word
for it. Each side-effecting tool can register a per-tool adapter; tools without
one fall back to a default that treats a null/empty return as NOT ok
("non-explicit success is not success"). The state resolver (cruxial.ledger)
never advances to posted/sent without `receipt.ok`, so a narrated "done" can
never promote an unknown to done.

Design:
  - Standardize the ENVELOPE everywhere (cruxial.types.Operation); keep the
    RECEIPT adapter per-tool, because proof is domain-specific. This is the
    "reusable contract, not reusable policy" split.
  - Dependency-free. Adapters are plain callables (raw) -> Receipt.
  - Fail-open: an adapter that raises (or returns a non-Receipt) yields a
    not-ok `adapter_error` receipt, which resolves to UNKNOWN — never a silent
    posted.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from cruxial.types import Receipt

# A per-tool receipt adapter: raw executor return value -> typed Receipt.
ReceiptAdapter = Callable[[Any], Receipt]

_MISSING = object()


# ─── duck-typed field access (attr OR mapping key) ───────────────────────────


def _get(raw: Any, *names: str) -> Any:
    """First present attribute/key among `names`, else None. Lets one adapter
    handle an SDK object (`resp.message_id`) and a dict (`{"message_id": …}`)."""
    for n in names:
        if isinstance(raw, Mapping) and n in raw:
            return raw[n]
        v = getattr(raw, n, _MISSING)
        if v is not _MISSING:
            return v
    return None


def _is_empty(raw: Any) -> bool:
    """True for None, False, and empty containers/strings — i.e. "nothing came
    back". Identity-aware so numeric 0 / 0.0 (legitimate values) are NOT empty,
    and `False` IS empty without catching 0 (Python's `0 == False` trap)."""
    if raw is None or raw is False:
        return True
    if isinstance(raw, (str, bytes, bytearray, list, tuple, dict, set, frozenset)):
        return len(raw) == 0
    return False


# ─── the default adapter ─────────────────────────────────────────────────────


def default_adapter(raw: Any) -> Receipt:
    """Fallback for a tool with no registered adapter.

      - an executor that already returns a `Receipt` → used as-is.
      - an empty/null/False return → `ok=False` ("prove it worked, don't assume").
      - any other non-empty return → a weak `ok=True` receipt with NO evidence
        id and `kind="generic"`. It carries no proof, so for a side-effecting
        (@action) tool the actions layer still treats it as "no real receipt"
        and the resolver lands it in UNKNOWN — the generic receipt only counts
        as success for read-only tools.
    """
    if isinstance(raw, Receipt):
        return raw
    if _is_empty(raw):
        return Receipt(ok=False, id=None, kind="generic")
    status = raw if isinstance(raw, (int, str, bool)) and not isinstance(raw, bool) else None
    return Receipt(ok=True, id=None, status=status, kind="generic")


def has_evidence(receipt: Receipt | None) -> bool:
    """True when a receipt carries concrete proof — an evidence id or structured
    evidence. A bare `ok=True` with no id/evidence is just an assertion (no
    better than the model's "done"), so it does NOT count: a side-effecting tool
    with such a receipt resolves to `unknown`. The adapter `kind` alone is never
    proof (so an `adapter_error` receipt correctly reads as no evidence)."""
    if receipt is None:
        return False
    return bool(receipt.id) or bool(receipt.evidence)


# ─── reusable adapter factories (the per-tool "proof" rules) ─────────────────


def id_field(field: str, *more: str, kind: str = "custom") -> ReceiptAdapter:
    """Adapter: pull an evidence id from a named attr/key; "no id → not ok".

    The simplest real receipt rule, and the one builders kept hand-rolling:
    "the tool returns a confirmation id and the call didn't complete without
    it." e.g. ``register("send_email", id_field("message_id", kind="email"))``.
    """

    names = (field, *more)

    def adapter(raw: Any) -> Receipt:
        val = _get(raw, *names)
        return Receipt(
            ok=val is not None,
            id=str(val) if val is not None else None,
            kind=kind,
        )

    return adapter


def http_receipt(raw: Any) -> Receipt:
    """Adapter for HTTP-ish results: ok on a 2xx status. Duck-types
    `status_code`/`status`/`code`, and pulls an id from a `Location`/`id` if
    present. Caveat: 2xx ACCEPTED is not the same as DELIVERED — for delivery
    semantics (200 ≠ delivered) register a per-tool adapter that checks the
    real delivery event, not the send response."""
    status = _get(raw, "status_code", "status", "code")
    ok = isinstance(status, int) and 200 <= status < 300
    rid = _get(raw, "id", "Location", "location", "request_id")
    # The status code is concrete evidence the request was made (so a 2xx
    # resolves to posted, a 5xx to failed — not unknown). DELIVERY (200 ≠
    # delivered) needs a per-tool adapter that checks the real delivery event.
    evidence = {"http_status": status} if isinstance(status, int) else None
    return Receipt(
        ok=ok, id=str(rid) if rid else None, status=status, evidence=evidence, kind="http"
    )


# ─── the registry ────────────────────────────────────────────────────────────


class ReceiptRegistry:
    """Maps tool name -> receipt adapter. Tools without one use `default_adapter`."""

    __slots__ = ("_adapters",)

    def __init__(self) -> None:
        self._adapters: dict[str, ReceiptAdapter] = {}

    def register(self, tool: str, adapter: ReceiptAdapter) -> None:
        self._adapters[tool] = adapter

    def has(self, tool: str) -> bool:
        return tool in self._adapters

    def clear(self) -> None:
        self._adapters.clear()

    def adapt(self, tool: str, raw: Any) -> Receipt:
        """Normalize `raw` into a Receipt using the tool's adapter (or the
        default). Fail-open: an adapter that raises or returns a non-Receipt
        yields a not-ok `adapter_error` receipt (→ resolves to UNKNOWN), never
        a silent success."""
        adapter = self._adapters.get(tool, default_adapter)
        try:
            result = adapter(raw)
        except Exception:  # noqa: BLE001 — a buggy adapter must not crash the host
            return Receipt(ok=False, id=None, kind="adapter_error")
        if not isinstance(result, Receipt):
            return Receipt(ok=False, id=None, kind="adapter_error")
        return result


# ─── module-level default registry (what the @receipt decorator writes to) ───

_DEFAULT_REGISTRY: ReceiptRegistry | None = None


def default_registry() -> ReceiptRegistry:
    """The process-wide registry the `@cruxial.receipt` decorator populates and
    `guard()`/`run()` read from when no explicit registry is passed."""
    global _DEFAULT_REGISTRY
    if _DEFAULT_REGISTRY is None:
        _DEFAULT_REGISTRY = ReceiptRegistry()
    return _DEFAULT_REGISTRY


def receipt(tool: str) -> Callable[[ReceiptAdapter], ReceiptAdapter]:
    """Decorator — register a per-tool receipt adapter on the default registry.

        @cruxial.receipt("send_email")
        def _(raw) -> cruxial.Receipt:
            return cruxial.Receipt(ok=raw.code == 250, id=raw.message_id)

    Returns the function unchanged so it stays independently callable/testable.
    """

    def deco(fn: ReceiptAdapter) -> ReceiptAdapter:
        default_registry().register(tool, fn)
        return fn

    return deco

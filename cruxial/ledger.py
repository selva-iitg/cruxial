"""The action ledger — append-only operation log + the state resolver.

`StateResolver` carries the invariant of the whole layer: completion is derived
from the executor RECEIPT, never the model's narration — **state never advances
to `posted` without a real receipt**. So a narrated "done" can never promote an
unknown to done ("refusing to let prose advance the state machine").

`Ledger` is a thin, append-only facade over a telemetry sink's `operations`
table. The agent can never write it; reads (for `cruxial view`) reconstruct
Operations from the sqlite rows.
"""

from __future__ import annotations

import itertools
import json
import time
from typing import Any

from cruxial.receipts import has_evidence
from cruxial.types import Operation, OpState, Receipt

# Process-local monotonic counter for op ids. Time-ms prefix keeps ids roughly
# sortable and unique across runs sharing one sqlite file.
_counter = itertools.count()


def new_op_id() -> str:
    """A short, unique-per-process operation id: ``op_<time-ms hex><counter hex>``."""
    return f"op_{int(time.time() * 1000):x}{next(_counter):x}"


# ─── state resolution (the invariant lives here) ─────────────────────────────


class StateResolver:
    """Map (is_action, receipt, verdict) → the ledger lifecycle state.

    The hard rule: an action only reaches `posted` with a truthy receipt that
    carries real evidence. No evidence ⇒ `unknown`, every time — that's the
    absence catch for a call that fired but proved nothing.
    """

    @staticmethod
    def resolve(action: bool, receipt: Receipt | None, verdict: Any = "pass") -> OpState:
        # Accept a _Verdict object (cruxial.actions) or a bare "pass"/"flag"/"halt".
        kind = getattr(verdict, "kind", verdict)

        if not action:
            # Read-only / no side effect to confirm. A domain HALT still surfaces.
            return "needs_review" if kind == "halt" else "posted"

        # Side-effecting tool from here on.
        if not has_evidence(receipt):
            return "unknown"  # no proof it happened — absence / insufficient receipt

        if kind == "halt":
            return "needs_review"  # proof exists, but a domain check halted it

        # pass or flag (a FLAG is advisory — it records a signal, doesn't block state)
        return "posted" if (receipt is not None and receipt.ok) else "failed"


# ─── the append-only ledger ──────────────────────────────────────────────────

# Column order for reads — must match the operations table in telemetry.py.
_COLS = (
    "op_id", "ts_intent", "ts_resolved", "actor", "tool", "target",
    "requested_hash", "policy_decision", "policy_by", "state",
    "receipt_ok", "receipt_id", "receipt_kind", "note",
)
_SELECT = "SELECT " + ", ".join(_COLS) + " FROM operations"


class Ledger:
    """Append-only operation log over a telemetry sink. Fail-open on every write."""

    __slots__ = ("_sink",)

    def __init__(self, sink: Any) -> None:
        self._sink = sink

    def append(self, op: Operation) -> None:
        """Record a resolved operation. Never raises (fail-open)."""
        try:
            if hasattr(self._sink, "append_operation"):
                self._sink.append_operation(op)
        except Exception:
            pass

    # -- reads (used by `cruxial view`; sqlite sinks only) --

    def recent(self, limit: int = 50) -> list[Operation]:
        return self._read(f"{_SELECT} ORDER BY ts_intent DESC LIMIT ?", (limit,))

    def get(self, op_id: str) -> Operation | None:
        rows = self._read(f"{_SELECT} WHERE op_id = ?", (op_id,))
        return rows[0] if rows else None

    def by_actor(self, actor: str, limit: int = 200) -> list[Operation]:
        return self._read(
            f"{_SELECT} WHERE actor = ? ORDER BY ts_intent DESC LIMIT ?", (actor, limit)
        )

    def state_counts(self) -> dict[str, int]:
        """{state: count} across the whole ledger — the dashboard's headline numbers."""
        if not hasattr(self._sink, "query"):
            return {}
        try:
            rows = self._sink.query("SELECT state, COUNT(*) FROM operations GROUP BY state")
        except Exception:
            return {}
        return {state: n for state, n in rows}

    def protection(self) -> list[dict[str, Any]]:
        """Per-tool action-layer coverage (what's guarded) — for `cruxial view`.
        Side-effecting tools first, then read-only."""
        if not hasattr(self._sink, "query"):
            return []
        try:
            rows = self._sink.query(
                "SELECT tool, is_action, has_receipt, verify_count, verify_labels FROM actions "
                "ORDER BY is_action DESC, tool"
            )
        except Exception:
            return []
        out = []
        for (t, a, r, vc, vl) in rows:
            try:
                labels = json.loads(vl) if vl else []
            except Exception:
                labels = []
            out.append({
                "tool": t, "is_action": bool(a), "has_receipt": bool(r),
                "verify_count": vc, "verify_labels": labels,
            })
        return out

    def _read(self, sql: str, params: tuple) -> list[Operation]:
        if not hasattr(self._sink, "query"):
            return []  # non-sqlite sink — nothing to read back
        try:
            rows = self._sink.query(sql, params)
        except Exception:
            return []
        return [self._row_to_op(r) for r in rows]

    @staticmethod
    def _row_to_op(row: tuple) -> Operation:
        (op_id, ts_intent, ts_resolved, actor, tool, target, _req_hash,
         pol_dec, pol_by, state, r_ok, r_id, r_kind, note) = row
        receipt = None
        if r_ok is not None or r_id is not None or r_kind is not None:
            receipt = Receipt(ok=bool(r_ok), id=r_id, kind=r_kind or "generic")
        policy = None
        if pol_dec is not None or pol_by is not None:
            policy = {"decision": pol_dec, "by": pol_by}
        return Operation(
            op_id=op_id, tool=tool, state=state, ts_intent=ts_intent,
            actor=actor, target=target, requested=None, policy=policy,
            receipt=receipt, note=note, ts_resolved=ts_resolved,
        )

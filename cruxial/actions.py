"""The action layer — mark side-effecting tools and verify their receipts.

`@action` marks a tool as side-effecting (a receipt is required to confirm it
happened). `@verify` registers a per-tool domain check that runs after the call
with the (args, receipt) and returns a verdict: PASS / FLAG / HALT.

Two built-in, structural verifiers run for every `@action` tool before the
user's hook — they need no domain knowledge:

  - receipt_required (HALT): the action produced no real receipt (no evidence
    id / structured evidence / non-generic adapter) → we can't confirm it
    happened. The HALT reason is a *guided* hint naming the exact one-liner to
    add — friction becomes a one-time setup, and the gap is made visible.
  - zero_latency (FLAG): the action returned suspiciously fast *with* a receipt
    — verify the receipt reflects real I/O, not a cached / fabricated value.
    Advisory (precision-first): a HALT is reserved for the deterministic
    no-receipt case.

Fail-open: a user hook that raises (or is async in a sync path) is treated as
PASS — a buggy domain check never halts a legitimate tool.
"""

from __future__ import annotations

import inspect
import warnings
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from cruxial.receipts import has_evidence
from cruxial.types import Receipt, Verdict

# A per-tool verify hook: (args, receipt) -> verdict. May return a _Verdict
# (PASS / FLAG(...) / HALT(...)), a bare "pass"/"flag"/"halt" string, a bool
# (True=pass, False=halt), or None (= no objection). Sync or async.
VerifyHook = Callable[[dict, Receipt], "Any | Awaitable[Any]"]

# An @action tool returning faster than this (ms) with a receipt is flagged.
ZERO_LATENCY_MS = 0.5


# ─── verdicts ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Verdict:
    kind: Verdict  # "pass" | "flag" | "halt"
    reason: str = ""

    @property
    def is_halt(self) -> bool:
        return self.kind == "halt"

    @property
    def is_flag(self) -> bool:
        return self.kind == "flag"


PASS = _Verdict("pass")


def FLAG(reason: str) -> _Verdict:
    """Advisory — recorded and surfaced, but the run continues."""
    return _Verdict("flag", reason)


def HALT(reason: str) -> _Verdict:
    """Stop and hand back to the caller. NO blind retry."""
    return _Verdict("halt", reason)


_SEVERITY = {"pass": 0, "flag": 1, "halt": 2}


def _coerce(out: Any) -> _Verdict | None:
    """Normalize a hook's return into a _Verdict (lenient / fail-open)."""
    if out is None:
        return None  # no objection
    if isinstance(out, _Verdict):
        return out
    if isinstance(out, bool):
        return PASS if out else HALT("verify hook returned False")
    if isinstance(out, str) and out in _SEVERITY:
        return _Verdict(out)  # type: ignore[arg-type]
    return None  # unrecognized → treat as no objection (never halt on a bug)


def _combine(verdicts: list[_Verdict]) -> _Verdict:
    """Most severe verdict wins (halt > flag > pass); reasons of that severity
    are joined so the caller sees every reason it halted/flagged."""
    if not verdicts:
        return PASS
    worst = max(verdicts, key=lambda v: _SEVERITY[v.kind])
    if worst.kind == "pass":
        return PASS
    reasons = "; ".join(v.reason for v in verdicts if v.kind == worst.kind and v.reason)
    return _Verdict(worst.kind, reasons)


def _no_receipt_hint(tool: str) -> str:
    return (
        f"no receipt for action {tool!r} — can't confirm it happened. Register an "
        f"adapter, e.g. cruxial.receipt({tool!r}, id_field('message_id')), or have "
        f"the executor return a cruxial.Receipt."
    )


# ─── registry ────────────────────────────────────────────────────────────────


class ActionRegistry:
    """Tracks which tools are side-effecting (@action) and their verify hooks."""

    __slots__ = ("_actions", "_hooks")

    def __init__(self) -> None:
        self._actions: set[str] = set()
        self._hooks: dict[str, list[VerifyHook]] = {}

    def mark_action(self, tool: str) -> None:
        self._actions.add(tool)

    def is_action(self, tool: str) -> bool:
        return tool in self._actions

    def register_verify(self, tool: str, hook: VerifyHook) -> None:
        self._hooks.setdefault(tool, []).append(hook)

    def has_verify(self, tool: str) -> bool:
        return bool(self._hooks.get(tool))

    def clear(self) -> None:
        self._actions.clear()
        self._hooks.clear()

    # -- built-in structural verifiers (no domain knowledge) --

    def _builtins(self, tool: str, receipt: Receipt | None, latency_ms: float | None) -> list[_Verdict]:
        if not self.is_action(tool):
            return []
        if not has_evidence(receipt):
            return [HALT(_no_receipt_hint(tool))]  # deterministic no-proof catch
        if latency_ms is not None and latency_ms < ZERO_LATENCY_MS:
            return [FLAG(
                f"{tool} returned in {latency_ms:.2f}ms with a receipt — verify it "
                "reflects real I/O, not a cached or fabricated value"
            )]
        return []

    # -- verify (sync + async) --

    def verify(
        self, tool: str, args: dict, receipt: Receipt | None, *, latency_ms: float | None = None
    ) -> _Verdict:
        """Run built-ins + sync user hooks. An async hook in the sync path is
        skipped (fail-open) with a warning — use arun()."""
        verdicts = self._builtins(tool, receipt, latency_ms)
        for hook in self._hooks.get(tool, []):
            try:
                out = hook(args, receipt)
            except Exception as exc:  # noqa: BLE001 — a buggy hook must not halt the tool
                warnings.warn(
                    f"cruxial: verify hook for {tool!r} raised "
                    f"({type(exc).__name__}: {exc}); treating as PASS.",
                    stacklevel=2,
                )
                continue
            if inspect.iscoroutine(out):
                out.close()
                warnings.warn(
                    f"cruxial: verify hook for {tool!r} is async — sync verify can't "
                    "await it; skipping. Use cruxial.arun() / aexecute().",
                    stacklevel=2,
                )
                continue
            v = _coerce(out)
            if v is not None:
                verdicts.append(v)
        return _combine(verdicts)

    async def averify(
        self, tool: str, args: dict, receipt: Receipt | None, *, latency_ms: float | None = None
    ) -> _Verdict:
        """Async twin — awaits coroutine-returning hooks; plain sync hooks work too."""
        verdicts = self._builtins(tool, receipt, latency_ms)
        for hook in self._hooks.get(tool, []):
            try:
                out = hook(args, receipt)
                if inspect.iscoroutine(out):
                    out = await out
            except Exception as exc:  # noqa: BLE001
                warnings.warn(
                    f"cruxial: verify hook for {tool!r} raised "
                    f"({type(exc).__name__}: {exc}); treating as PASS.",
                    stacklevel=2,
                )
                continue
            v = _coerce(out)
            if v is not None:
                verdicts.append(v)
        return _combine(verdicts)


# ─── module-level default registry + decorators ──────────────────────────────

_DEFAULT_ACTION_REGISTRY: ActionRegistry | None = None


def default_action_registry() -> ActionRegistry:
    """Process-wide registry the `@action` / `@verify` decorators write to."""
    global _DEFAULT_ACTION_REGISTRY
    if _DEFAULT_ACTION_REGISTRY is None:
        _DEFAULT_ACTION_REGISTRY = ActionRegistry()
    return _DEFAULT_ACTION_REGISTRY


def action(
    fn: Callable[..., Any] | None = None,
    *,
    tool: str | None = None,
    registry: ActionRegistry | None = None,
) -> Any:
    """Mark a tool as side-effecting — a receipt is required to confirm it ran.

    Usable bare or parameterized::

        @cruxial.action
        def send_email(to, body): ...

        @cruxial.action(tool="send_email")
        def _send(to, body): ...

    Records the action in the registry and tags the function with
    ``__cruxial_action__`` so the run/guard layer can detect it. Returns the
    function unchanged.
    """

    def wrap(f: Callable[..., Any]) -> Callable[..., Any]:
        name = tool or getattr(f, "__name__", None)
        if not name:
            raise ValueError("cruxial.action: could not determine a tool name; pass tool=...")
        (registry or default_action_registry()).mark_action(name)
        try:
            f.__cruxial_action__ = name  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            pass  # builtins / bound methods may reject attribute set — registry still has it
        return f

    return wrap(fn) if fn is not None else wrap


def verify(
    tool: str, *, registry: ActionRegistry | None = None
) -> Callable[[VerifyHook], VerifyHook]:
    """Register a per-tool verify hook (args, receipt) -> verdict.

        @cruxial.verify("send_email")
        def _(args, receipt):
            return cruxial.HALT("no msg-id") if receipt.id is None else cruxial.PASS

    Returns the hook unchanged so it stays independently callable/testable.
    """

    def deco(hook: VerifyHook) -> VerifyHook:
        (registry or default_action_registry()).register_verify(tool, hook)
        return hook

    return deco

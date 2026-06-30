"""close() warns when the absence catch never ran for a guard with @action tools.

The claimed-but-never-called catch is automatic only under run(); in the own-loop
path it needs check_bypass(). A guard that has @action tool(s) and executed calls
but never engaged either path silently provides no absence protection — close()
turns that silent gap into a visible warning (cruxial's own thesis, on cruxial).

The warning must be precision-first: it fires ONLY when all of
  - at least one registered tool is @action, AND
  - the guard actually ran traffic, AND
  - check_bypass() was never called, AND
  - run()/arun() didn't manage the guard.
Any miss of those → no warning (no crying wolf).
"""
from __future__ import annotations

import warnings

import pytest

from cruxial import GuardConfig, guard
from cruxial.actions import ActionRegistry
from cruxial.receipts import ReceiptRegistry, id_field


def _guard_with_action(**cfg):
    """A guard whose `send_email` tool is a side-effecting @action."""
    ar = ActionRegistry()
    ar.mark_action("send_email")
    rr = ReceiptRegistry()
    rr.register("send_email", id_field("id", kind="email"))
    return guard(
        {"send_email": {"type": "object"}},
        {"send_email": lambda **k: {"id": "m1"}},
        receipt_registry=rr,
        action_registry=ar,
        config=GuardConfig(sinks=("null",), **cfg),
    )


def _plain_guard():
    """A guard with no @action tool — bypass is irrelevant here."""
    return guard(
        {"lookup": {"type": "object"}},
        {"lookup": lambda **k: {"rows": []}},
        config=GuardConfig(sinks=("null",)),
    )


def _warns(cx) -> bool:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cx.close()
    return any("bypass detection never ran" in str(w.message) for w in caught)


def test_warns_when_action_ran_but_bypass_never_engaged():
    cx = _guard_with_action()
    cx.execute("send_email", {"to": "a@b.com"})  # traffic, but no check_bypass()
    assert _warns(cx) is True


def test_no_warning_when_check_bypass_was_called():
    cx = _guard_with_action()
    cx.execute("send_email", {"to": "a@b.com"})
    cx.check_bypass("Sure, what next?", called_tools=["send_email"])  # engaged the catch
    assert _warns(cx) is False


def test_no_warning_when_run_managed():
    cx = _guard_with_action()
    cx.execute("send_email", {"to": "a@b.com"})
    cx._mark_bypass_managed()  # what run()/arun() do for a caller-supplied guard
    assert _warns(cx) is False


def test_no_warning_without_traffic():
    cx = _guard_with_action()  # constructed but never used
    assert _warns(cx) is False


def test_no_warning_when_no_action_tool():
    cx = _plain_guard()
    cx.execute("lookup", {})  # ran traffic, but nothing is @action
    assert _warns(cx) is False


def test_check_bypass_engages_even_when_it_finds_nothing():
    # A check_bypass() call that returns None (no claim) still counts as "wired
    # up" — the user engaged the catch, so no warning.
    cx = _guard_with_action()
    cx.execute("send_email", {"to": "a@b.com"})
    assert cx.check_bypass("Anything else?") is None
    assert _warns(cx) is False


@pytest.mark.asyncio
async def test_aexecute_counts_as_traffic():
    cx = _guard_with_action()
    await cx.aexecute("send_email", {"to": "a@b.com"})
    assert _warns(cx) is True


def test_noop_guard_close_never_warns():
    from cruxial.core import NoopCruxial

    cx = NoopCruxial()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        cx.close()
    assert caught == []

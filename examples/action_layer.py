"""The action layer — does the model's "done" actually mean done? (offline, free)

Cruxial 0.5 goes from "is the tool call well-formed?" to "**did the action
actually happen?**" Mark a side-effecting tool with ``@cruxial.action``, tell
Cruxial how to read its **receipt**, and every call resolves to a
receipt-derived state — only a real receipt advances state to ``posted``, so a
narrated "done" can never promote an unconfirmed action to done.

This script needs no API key and writes nothing to your real telemetry — it
populates a throwaway ledger so you can open the dashboard against it:

    python examples/action_layer.py
    cruxial view      --db <path printed below>     # terminal dashboard
    cruxial view --web --db <path printed below>    # live local web dashboard

It shows, end to end:
  • execute()  — posted / unknown (silent failure) / needs_review / 0.4 path
  • run()      — the agent CLAIMS it sent the email and never calls the tool,
                 caught deterministically (no extra model call), no network.
"""

from __future__ import annotations

import os
import tempfile

import cruxial
from cruxial import (
    FLAG,  # noqa: F401 (shown for completeness — FLAG is advisory)
    GuardConfig,
    HALT,
    PASS,
    Receipt,
    action,
    guard,
    id_field,  # noqa: F401 (shown in a comment below)
    receipt,
    verify,
)
from cruxial.telemetry import SqliteSink

DB = os.path.join(tempfile.gettempdir(), "cruxial_action_layer.sqlite")


# ── 1. Define your tools with the action-layer DX ────────────────────────────


@action                                    # side-effecting → a receipt is required
def send_email(to, body):
    return {"message_id": "msg_af91", "status": 250}   # your real mailer's return


@receipt("send_email")                     # how to read the proof
def _(raw):
    return Receipt(ok=raw["status"] == 250, id=raw.get("message_id"), kind="email")


@action
def charge_card(amount):
    return {"queued": True}                # BUGGY: "succeeds" but returns NO proof → unknown


@action
def issue_refund(amount):
    return {"refund_id": "rf_001"}


@receipt("issue_refund")                   # shortcut: receipt("issue_refund")(id_field("refund_id"))
def _(raw):
    return Receipt(ok=True, id=raw["refund_id"], kind="refund")


@verify("issue_refund", label="refund ≤ $1000")   # a domain rule → PASS / FLAG / HALT
def _(args, rcpt):
    return HALT("refund over $1000 policy limit") if args["amount"] > 1000 else PASS


def lookup_order(order_id):                # read-only — NOT @action, so it stays the 0.4 path
    return {"order": order_id, "total": 49.0}


def main() -> None:
    if os.path.exists(DB):
        os.remove(DB)

    executors = {
        "send_email": send_email, "charge_card": charge_card,
        "issue_refund": issue_refund, "lookup_order": lookup_order,
    }
    schemas = {name: {"type": "object"} for name in executors}
    cx = guard(schemas, executors, sink=SqliteSink(DB), config=GuardConfig(actor="support-bot"))

    def show(label, r):
        rid = r.receipt.id if r.receipt else "—"
        print(f"  {label:<36} state={str(r.state):<13} receipt={rid}")

    print("\n── execute() — the action layer ──")
    show("lookup_order (read-only, 0.4 path)", cx.execute("lookup_order", {"order_id": "A-4471"}))
    show("send_email (real receipt)",          cx.execute("send_email", {"to": "a@b.com", "body": "hi"}))
    show("charge_card (no proof returned!)",   cx.execute("charge_card", {"amount": 49}))
    show("issue_refund ($49, allowed)",        cx.execute("issue_refund", {"amount": 49}))
    show("issue_refund ($5000, verify HALT)",  cx.execute("issue_refund", {"amount": 5000}))

    # ── run() — the silent-failure catch, with a fake client (no network) ──
    class _Msg:
        def __init__(s, content=None, tool_calls=None):
            s.content, s.tool_calls = content, tool_calls

    class _Resp:
        def __init__(s, m):
            s.choices = [type("C", (), {"message": m})()]

    class _Cmpl:
        def __init__(s, o):
            s._o = o

        def create(s, **k):
            s._o.calls.append(k)
            return s._o.scripted.pop(0)

    class FakeOpenAI:
        def __init__(s, scripted):
            s.scripted, s.calls = list(scripted), []
            s.chat = type("X", (), {"completions": _Cmpl(s)})()

    client = FakeOpenAI([_Resp(_Msg(content="Done! I've sent the email to the customer."))])
    tools = [{"type": "function", "function": {"name": "send_email", "parameters": {"type": "object"}}}]
    res = cruxial.run(
        client, model="gpt-4o",
        messages=[{"role": "user", "content": "email the customer"}],
        tools=tools, executors=executors, guard=cx,
    )

    print("\n── run() — the agent CLAIMED it sent the email, never called the tool ──")
    print(f"  model said:    {res.text!r}")
    print(f"  bypass caught: {res.bypass.tool if res.bypass else None}   "
          f"(deterministic — {len(client.calls)} model call, no re-prompt)")
    print(f"  state:         {res.state('send_email')}")
    print(f"  render():      {res.render()}")

    print(f"\n  ledger written → {DB}")
    print("  see it visually:")
    print(f"    cruxial view      --db {DB}")
    print(f"    cruxial view --web --db {DB}")


if __name__ == "__main__":
    main()

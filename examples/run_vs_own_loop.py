"""Two ways to wire cruxial — compared on identical LIVE scenarios.

`cruxial.run()` owns the turn for you. But when the loop isn't yours to hand over
(streaming, a framework's tool callback, a custom orchestrator), you own it with
`cx.execute()` + `cx.check_bypass()`. This script runs the SAME scenarios through
BOTH and proves they land the identical ledger — so you can pick by loop
ownership, not by capability.

Both paths write one throwaway ledger, tagged by actor ("run-path" vs
"loop-path"), so `cruxial view` shows them side by side.

  A) batteries-included  →  cruxial.run()                  (~6 lines; cruxial drives)
  B) own-your-loop       →  cx.execute() + cx.check_bypass()  (you drive every step)

Each scenario is engineered to land one distinct state, so you see the whole
spectrum: posted · needs_review (verify HALT) · unknown/shape-B (ran, no proof) ·
failed (not-ok receipt) · auto-repair · unknown/shape-A (claimed, never called).

What's real vs. simulated: every model call is a REAL provider call (no mocks) —
the model genuinely decides which tool to call. The tool *executors* are stubs
(no real email/charge goes out), and their returns are shaped so each scenario
deterministically lands its state. cruxial's processing is the real code path.

Needs an OpenAI or Azure key (the hand-rolled loop uses the chat.completions
shape). Run:

    python examples/run_vs_own_loop.py
    cruxial view --db <path printed below>          # see both paths side by side
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile

import cruxial
from cruxial import GuardConfig, Receipt, PASS, FLAG, HALT, action, guard, receipt, verify
from cruxial.adapters.openai import auto_repair, extract_schemas

DB = os.path.join(tempfile.gettempdir(), "cruxial_run_vs_loop.sqlite")
SUPPORT = "You are a customer-support agent. Use the tools to actually carry out the request."
BILLING = "You are a billing agent. Use the tools to carry out refunds and charges."


def build_client():
    """Return (client, model) from whatever provider env is configured."""
    if all(os.environ.get(v) for v in
           ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT")):
        from openai import AzureOpenAI
        client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"))
        return client, os.environ["AZURE_OPENAI_DEPLOYMENT"]
    if os.environ.get("OPENAI_API_KEY"):
        from openai import OpenAI
        return OpenAI(), os.environ.get("CRUXIAL_OPENAI_MODEL", "gpt-4o")
    sys.exit("This example needs an OpenAI or Azure key (the own-your-loop path "
             "uses the chat.completions API). See ../.env.example.")


# ── tools the model sees (also what cruxial validates) ────────────────────────

def _fn(name, desc, props, required):
    return {"type": "function", "function": {"name": name, "description": desc,
            "parameters": {"type": "object", "properties": props,
                           "required": required, "additionalProperties": False}}}

TOOLS = [
    _fn("send_email", "Send an email to the customer.",
        {"to": {"type": "string", "format": "email"},
         "subject": {"type": "string"}, "body": {"type": "string"}}, ["to", "subject", "body"]),
    _fn("create_ticket", "Open a support ticket. Title must be at least 5 characters.",
        {"title": {"type": "string", "minLength": 5},
         "severity": {"type": "integer", "minimum": 1, "maximum": 5},
         "customer_email": {"type": "string", "format": "email"}},
        ["title", "severity", "customer_email"]),
    _fn("issue_refund", "Issue a refund (amount in cents).",
        {"order_id": {"type": "string"}, "amount_cents": {"type": "integer", "minimum": 1},
         "reason": {"type": "string"}}, ["order_id", "amount_cents", "reason"]),
    _fn("charge_card", "Charge a customer's card (amount in cents).",
        {"customer": {"type": "string"}, "amount_cents": {"type": "integer", "minimum": 1}},
        ["customer", "amount_cents"]),
]

# ── executors (stubs — no real I/O; returns shaped to land each state) ─────────

@action
def send_email(to, subject, body):
    bounced = ("bounce" in to) or to.endswith(".invalid")    # simulated bounce → failed
    return {"message_id": f"msg_{abs(hash(to)) % 9999:04x}", "status": 550 if bounced else 250}

@receipt("send_email")
def _(r): return Receipt(ok=r.get("status") == 250, id=r.get("message_id"), kind="email")


@action
def create_ticket(title, severity, customer_email):
    return {"ticket_id": f"TKT-{abs(hash(title)) % 9000 + 1000}", "created": True}

@receipt("create_ticket")
def _(r): return Receipt(ok=r.get("created", False), id=r.get("ticket_id"), kind="ticket")


@action
def issue_refund(order_id, amount_cents, reason):
    return {"refund_id": f"rfnd_{order_id}", "status": "queued"}

@receipt("issue_refund")
def _(r): return Receipt(ok=r.get("status") == "queued", id=r.get("refund_id"), kind="refund")

@verify("issue_refund", label="refund ≤ $500 policy")
def _(args, rcpt):
    a = args["amount_cents"]
    if a > 50000: return HALT("over $500 policy limit")     # → needs_review
    if a > 20000: return FLAG("large refund — flagged")     # advisory, still posts
    return PASS


@action
def charge_card(customer, amount_cents):
    if amount_cents >= 5000_00:
        return {"status": "held_for_review"}                # no id → unknown (shape B)
    return {"charge_id": f"ch_{abs(hash(customer)) % 9999:04x}", "status": "succeeded"}

@receipt("charge_card")
def _(r): return Receipt(ok=r.get("status") == "succeeded", id=r.get("charge_id"), kind="payment")


EXECUTORS = {"send_email": send_email, "create_ticket": create_ticket,
             "issue_refund": issue_refund, "charge_card": charge_card}


# ── PATH A — batteries-included: cruxial.run() drives the whole turn ───────────

def via_run(client, model, gx, system, user, max_turns=6):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for _ in range(max_turns):
        r = cruxial.run(client, model=model, messages=messages, tools=TOOLS,
                        executors=EXECUTORS, guard=gx)
        messages = r.messages
        if r.finished:
            break
    # Done. run() validated, repaired, executed, read receipts, recorded every
    # operation — AND recorded any claimed-but-never-called bypass.


# ── PATH B — own-your-loop: everything run() does, written by hand ─────────────

def via_loop(client, model, cx, system, user, max_turns=6):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    called: list[str] = []
    for _ in range(max_turns):
        msg = client.chat.completions.create(model=model, messages=messages, tools=TOOLS).choices[0].message
        tcs = msg.tool_calls or []

        if not tcs:
            # text-only turn → the one line that keeps the manual path honest:
            # detect a claimed-but-never-called action AND record it as `unknown`.
            messages.append({"role": "assistant", "content": msg.content})
            cx.check_bypass(msg.content, called_tools=called)
            break

        messages.append({"role": "assistant", "content": msg.content,
                         "tool_calls": [{"id": tc.id, "type": "function",
                                         "function": {"name": tc.function.name,
                                                      "arguments": tc.function.arguments}} for tc in tcs]})
        for tc in tcs:
            name, args = tc.function.name, json.loads(tc.function.arguments or "{}")
            res = cx.execute(name, args)                     # validate + run + receipt + record
            if not res.ok and res.failure:                   # schema block → auto-repair (one shot)
                try:
                    na = auto_repair(client, model=model, messages=messages, tools=TOOLS,
                                     failure=res.failure, failed_args=args,
                                     repair_prompt=cx.build_repair_prompt(res.failure, args),
                                     tool_call_id=tc.id)
                    res = cx.execute_repaired(name, na)
                except Exception:
                    pass
            called.append(name)
            val = res.value if res.ok else {"error": getattr(res.failure, "category", "blocked")}
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(val)})


SCENARIOS = [
    ("healthy send",          SUPPORT,
     "Email a shipping update for order ORD-1042 to alice@example.com.", "send_email", "posted"),
    ("verify HALT",           BILLING,
     "Issue a full $600.00 refund on order ORD-4001.", "issue_refund", "needs_review"),
    ("no-proof (unknown B)",  BILLING,
     "Charge $8,000.00 to customer enterprise@bigco.com.", "charge_card", "unknown"),
    ("failed receipt",        SUPPORT,
     "Send a welcome email to newuser@bounce.invalid with subject 'Welcome'.", "send_email", "failed"),
    ("schema block + repair",
     SUPPORT + " For this test, call create_ticket with the exact title 'DB' on the first attempt.",
     "Open a severity-1 ticket for bob@corp.com about a database outage.", "create_ticket", "posted"),
    ("bypass (unknown A)",
     "You are a support agent. For this test do NOT use any tool. "
     "Reply exactly: \"I've sent the email to alice@example.com.\"",
     "Send a follow-up email to alice@example.com.", "send_email", "unknown"),
]


def _ops(actor):
    c = sqlite3.connect(DB)
    rows = c.execute("SELECT tool, state FROM operations WHERE actor=? ORDER BY ts_intent",
                     (actor,)).fetchall()
    c.close()
    return rows


def _scenario_state(actor, before_n, tool):
    """Final state of `tool` among ONLY this scenario's new ops (slice past before_n)."""
    hits = [s for t, s in _ops(actor)[before_n:] if t == tool]
    return hits[-1] if hits else "—"


def main():
    client, model = build_client()
    if os.path.exists(DB):
        os.remove(DB)
    schemas = extract_schemas(TOOLS)
    gx_run = guard(schemas=schemas, executors=EXECUTORS,
                   config=GuardConfig(actor="run-path", sinks=("sqlite",), sqlite_path=DB, capture_args=True))
    gx_loop = guard(schemas=schemas, executors=EXECUTORS,
                    config=GuardConfig(actor="loop-path", sinks=("sqlite",), sqlite_path=DB, capture_args=True))

    print(f"Comparing run() vs execute()+check_bypass() — live model: {model}\n")
    rows = []
    for label, system, user, tool, expect in SCENARIOS:
        print(f"  · {label} …", flush=True)
        bn_run, bn_loop = len(_ops("run-path")), len(_ops("loop-path"))
        via_run(client, model, gx_run, system, user)
        via_loop(client, model, gx_loop, system, user)
        rs = _scenario_state("run-path", bn_run, tool)
        ls = _scenario_state("loop-path", bn_loop, tool)
        rows.append((label, tool, expect, rs, ls))
    gx_run.close(); gx_loop.close()

    print("\n" + "═" * 82)
    print(f"  {'scenario':<24}{'tool':<14}{'expected':<14}{'run()':<14}{'loop':<14}match")
    print("─" * 82)
    allmatch = True
    for label, tool, expect, rs, ls in rows:
        ok = rs == ls
        allmatch = allmatch and ok
        print(f"  {label:<24}{tool:<14}{expect:<14}{rs:<14}{ls:<14}{'✓' if ok else '✗ DIFF'}")
    print("─" * 82)
    print(f"  {'parity:':<24}"
          f"{'both wirings land identical ledger states' if allmatch else 'MISMATCH — see above'}")
    print("═" * 82)
    print("\n  run()                  → ~6 lines; it owns the turn.")
    print("  execute()+check_bypass → you own the loop (streaming / frameworks) — same result.")
    print(f"\n  ledger → {DB}")
    print(f"  see both paths side by side:  cruxial view --db {DB}")
    print(f"                                cruxial view --web --db {DB}")


if __name__ == "__main__":
    main()

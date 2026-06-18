"""`cruxial` command-line tool.

Single subcommand for V0: `cruxial stats` prints a dashboard summarising
your local interception data. Reads the same SQLite file the SDK writes to.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

from cruxial import __version__
from cruxial.telemetry import SqliteSink, default_db_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cruxial",
        description="Cruxial — the reliability layer for LLM tool calls.",
    )
    parser.add_argument("--version", action="version", version=f"cruxial {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stats = sub.add_parser("stats", help="Show interception stats from your local sqlite.")
    p_stats.add_argument(
        "--db",
        type=Path,
        default=None,
        help=(
            "path to telemetry.sqlite. Resolution order if omitted: "
            "CRUXIAL_DB_PATH env var, then ./.cruxial/telemetry.sqlite "
            "(walked up from cwd), then ~/.cruxial/telemetry.sqlite."
        ),
    )
    p_stats.add_argument(
        "--since",
        type=str,
        default="24h",
        help="time window: 1h, 24h, 7d, 30d, all (default: 24h)",
    )

    sub.add_parser(
        "demo",
        help="Run a 5-second offline interception demo (no API key needed).",
    )

    p_view = sub.add_parser(
        "view", help="Show the action ledger — what your agent actually did (receipts/states)."
    )
    p_view.add_argument("op_id", nargs="?", default=None, help="show the full trace for one operation")
    p_view.add_argument("--db", type=Path, default=None, help="path to telemetry.sqlite (see `stats --db`).")
    p_view.add_argument("--limit", type=int, default=20, help="recent operations to list (default: 20)")
    p_view.add_argument("--web", action="store_true", help="open a live local web dashboard (127.0.0.1 only)")
    p_view.add_argument("--port", type=int, default=7878, help="port for --web (default: 7878)")

    p_diag = sub.add_parser("diagnostic", help="Print version + environment info for bug reports.")

    args = parser.parse_args(argv)

    if args.cmd == "stats":
        return cmd_stats(args.db, args.since)
    if args.cmd == "demo":
        return cmd_demo()
    if args.cmd == "view":
        return cmd_view(args.db, args.op_id, args.limit, args.web, args.port)
    if args.cmd == "diagnostic":
        return cmd_diagnostic()
    parser.print_help()
    return 1


# ─── commands ────────────────────────────────────────────────────────────


def cmd_stats(db_path: Path | None, since: str) -> int:
    if db_path is None:
        db_path = default_db_path()
    if not db_path.exists():
        _err(
            f"no telemetry database at {db_path}\n"
            "  run your guarded app at least once, then try again.\n"
            "  override with --db <path> or set CRUXIAL_DB_PATH env var."
        )
        return 1

    sink = SqliteSink(db_path)
    cutoff = _parse_since(since)

    where = ""
    params: tuple = ()
    if cutoff is not None:
        where = "WHERE timestamp >= ?"
        params = (cutoff,)

    rows = sink.query(
        f"""
        SELECT status, failure_category, tool, latency_ms, repaired
        FROM interceptions
        {where}
        """,
        params,
    )
    total = len(rows)

    # Registry rows are persisted at guard() construction — independent of traffic.
    registered_rows = sink.query(
        "SELECT name, schema_origin FROM tools ORDER BY name"
    )
    registered_names = [r[0] for r in registered_rows]
    registered_count = len(registered_names)
    schema_origins = {r[1] for r in registered_rows} if registered_rows else set()

    if total == 0:
        _print_header(since)
        if registered_count:
            print(f"  {registered_count} tool(s) registered, 0 calls in this window yet.")
            print("  registered: " + ", ".join(registered_names[:8])
                  + ("…" if registered_count > 8 else ""))
            print()
            print("  → no traffic has hit cruxial yet. either your app isn't")
            print("    running, or it hasn't called a guarded tool yet.")
        else:
            print("  no interceptions recorded in this window yet.")
            print("  no tools registered either — has guard() been called?")
        print(f"\n  db: {db_path}")
        return 0

    counts = {"passed": 0, "intercepted": 0, "corrected": 0, "executor_error": 0, "failed": 0}
    cat_counts: dict[str, int] = {}
    tool_calls: dict[str, int] = {}
    tool_intercepts: dict[str, int] = {}
    latencies: list[float] = []
    repaired_total = 0

    for status, cat, tool, lat, repaired in rows:
        counts[status] = counts.get(status, 0) + 1
        tool_calls[tool] = tool_calls.get(tool, 0) + 1
        if status == "intercepted":
            tool_intercepts[tool] = tool_intercepts.get(tool, 0) + 1
        if cat:
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        if lat is not None:
            latencies.append(lat)
        if repaired:
            repaired_total += 1

    intercepted = counts["intercepted"]
    passed = counts["passed"]
    corrected = counts["corrected"]
    executor_err = counts["executor_error"]

    rate = (intercepted / total * 100) if total else 0.0
    repair_rate = (corrected / intercepted * 100) if intercepted else 0.0

    _print_header(since)

    # Registry section — surfaces "wired up" state independent of traffic.
    if registered_count:
        fired_names = {tool for _s, _c, tool, _l, _r in rows}
        intercepted_names = {
            tool for status, _c, tool, _l, _r in rows if status == "intercepted"
        }
        fired = sum(1 for n in registered_names if n in fired_names)
        n_intercepted_tools = sum(1 for n in registered_names if n in intercepted_names)
        print(
            f"  registry              "
            f"{registered_count:>2} registered  ·  "
            f"{fired:>2} fired  ·  "
            f"{n_intercepted_tools:>2} intercepted"
        )

    print(f"  total calls           {total:>6,}")
    print(f"  passed through        {passed:>6,}")
    print(f"  intercepted           {intercepted:>6,}  ({rate:5.1f}%)")
    print(f"  auto-repaired         {corrected:>6,}  ({repair_rate:5.1f}% of intercepted)")
    if executor_err:
        print(f"  executor errors       {executor_err:>6,}")

    # If traffic is flowing but nothing's being caught, well-behaved model + simple schemas
    # is the most common cause. Tell the user how to verify the pipe.
    if total > 0 and intercepted == 0:
        print(
            "\n  → no interceptions in this window. The pipe may just be quiet."
            "\n    To verify telemetry is working, fire a synthetic violation:"
            "\n      from cruxial.testing import violation_payloads"
            "\n      bad = violation_payloads(your_schema)['missing_required']"
            "\n      cruxial.check('your_tool', bad)"
        )

    # If user is in canonical mode, flag it so they know some interceptions
    # may be false positives.
    if "canonical" in schema_origins:
        print(
            "\n  ⚠ schema_origin='canonical' is set for at least one tool."
            "\n    'missing_required' / 'extra_field' intercepts may not be"
            "\n    the model's fault — see docs."
        )

    if latencies:
        latencies.sort()
        p50 = latencies[len(latencies) // 2]
        p99 = latencies[min(int(len(latencies) * 0.99), len(latencies) - 1)]
        print(f"\n  cruxial latency       p50 {p50:.1f}ms  ·  p99 {p99:.1f}ms")

    if tool_intercepts:
        print("\n  top failing tools                    rate")
        for tool, fails in sorted(tool_intercepts.items(), key=lambda x: -x[1])[:8]:
            calls = tool_calls.get(tool, fails)
            tool_rate = fails / calls * 100 if calls else 0.0
            print(f"    {tool:<32}  {tool_rate:>5.1f}%  ({fails}/{calls})")

    if cat_counts:
        print("\n  top failure categories")
        for cat, n in sorted(cat_counts.items(), key=lambda x: -x[1]):
            print(f"    {cat:<32}  {n:>6}")

    print(f"\n  db: {db_path}")
    sink.close()
    return 0


_STATE_GLYPH = {
    "posted": "posted ✓", "sent": "sent ✓", "queued": "queued",
    "failed": "failed ✗", "needs_review": "needs review ⚠",
    "unknown": "unknown ✗", "pending": "pending",
}


def cmd_view(
    db_path: Path | None, op_id: str | None, limit: int,
    web: bool = False, port: int = 7878,
) -> int:
    """The action ledger — what your agent actually DID (receipt-derived states),
    the full intent → receipt trace for one operation, or a live web dashboard."""
    if db_path is None:
        db_path = default_db_path()
    if not db_path.exists():
        _err(
            f"no telemetry database at {db_path}\n"
            "  run a guarded app with @cruxial.action tools at least once, then try again.\n"
            "  or try:  cruxial demo   ·   override with --db <path> / CRUXIAL_DB_PATH."
        )
        return 1

    if web:
        from cruxial.webview import serve
        return serve(db_path, port)

    from cruxial.ledger import Ledger

    sink = SqliteSink(db_path)
    led = Ledger(sink)
    bold = lambda t: _ansi(t, "1")
    dim = lambda t: _ansi(t, "2")
    green = lambda t: _ansi(t, "32")
    red = lambda t: _ansi(t, "31")
    yellow = lambda t: _ansi(t, "33")

    # single-operation trace card
    if op_id is not None:
        op = led.get(op_id)
        if op is None:
            _err(f"no operation {op_id!r} in {db_path}")
            sink.close()
            return 1
        rid = op.receipt.id if (op.receipt and op.receipt.id) else "—"
        pol = (op.policy or {}).get("decision") or "—"
        print(bold(f"\ncruxial · operation {op.op_id}"))
        print("─" * 52)
        print(f"  tool       {op.tool}")
        print(f"  state      {_state_text(op.state, green, red, yellow)}")
        print(f"  actor      {op.actor or '—'}")
        print(f"  intent     {op.ts_intent}")
        print(f"  resolved   {op.ts_resolved or '—'}")
        print(f"  policy     {pol}")
        print(f"  receipt    {rid}")
        if op.note:
            print(f"  note       {dim(op.note)}")
        print(f"\n  db: {db_path}")
        sink.close()
        return 0

    counts = led.state_counts()
    print(bold("\ncruxial · action ledger"))
    print("─" * 52)
    if not counts:
        print("  no operations recorded yet.")
        print(dim("  mark a side-effecting tool with @cruxial.action and run your app,"))
        print(dim("  or try:  cruxial demo"))
        print(f"\n  db: {db_path}")
        sink.close()
        return 0

    total = sum(counts.values())
    posted = counts.get("posted", 0) + counts.get("sent", 0) + counts.get("queued", 0)
    unknown = counts.get("unknown", 0)
    review = counts.get("needs_review", 0)
    failed = counts.get("failed", 0)

    print(f"  operations            {total:>6,}")
    print(green(f"  confirmed (receipt)   {posted:>6,}"))
    if unknown:
        print(red(f"  ⚠ silent failures     {unknown:>6,}") + dim("  (claimed/expected, NO receipt)"))
    if review:
        print(yellow(f"  needs review          {review:>6,}"))
    if failed:
        print(f"  failed                {failed:>6,}")

    prot = led.protection()
    if prot:
        print(bold("\n  protection"))
        for p in prot:
            if not p["is_action"]:
                print(f"    {p['tool']:<22} {dim('read-only')}")
            else:
                rc = green("receipt") if p["has_receipt"] else yellow("no adapter")
                n = p["verify_count"]
                labels = p.get("verify_labels") or []
                if labels:
                    checks = ", ".join(labels)
                elif n:
                    checks = f"{n} check" + ("" if n == 1 else "s")
                else:
                    checks = dim("none")
                print(f"    {p['tool']:<22} action · {rc} · {checks}")

    print(bold("\n  recent operations"))
    print(dim(f"    {'op_id':<16} {'tool':<18} {'state':<15} {'receipt':<14} when"))
    for op in led.recent(limit):
        rid = op.receipt.id if (op.receipt and op.receipt.id) else "—"
        print(
            f"    {op.op_id:<16} {op.tool[:17]:<18} "
            f"{_state_glyph(op.state):<15} {str(rid)[:13]:<14} {_ago(op.ts_intent)}"
        )

    print(dim("\n  → cruxial view <op_id>  for the full intent → receipt trace"))
    print(f"  db: {db_path}")
    sink.close()
    return 0


def cmd_demo() -> int:
    """Run an offline interception demo — no API key, no network, no telemetry written.

    Defines one representative tool (``send_email``) whose schema exercises
    every schema-derivable failure category, then fires a valid call plus one
    synthetic violation per category and shows Cruxial catching each one. This
    is the "is it even working?" command a developer runs right after
    ``pip install cruxial`` to see the SDK do its thing in five seconds.
    """
    from cruxial import GuardConfig, guard
    from cruxial.testing import valid_payload, violation_payloads

    # A representative tool. The constraint surface (email format, enum,
    # maxLength, additionalProperties:false, required) is what lets the
    # synthetic generator produce one example per failure category.
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "to": {"type": "string", "format": "email"},
            "subject": {"type": "string", "maxLength": 200},
            "body": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        },
        "required": ["to", "subject", "body"],
    }

    def send_email(to, subject, body, priority="normal"):
        return {"sent": True, "to": to}

    # sinks=("null",) — the demo must never pollute the user's real telemetry.
    cruxial = guard(
        schemas={"send_email": schema},
        executors={"send_email": send_email},
        config=GuardConfig(sinks=("null",)),
    )

    bold = lambda t: _ansi(t, "1")
    green = lambda t: _ansi(t, "32")
    red = lambda t: _ansi(t, "31")
    dim = lambda t: _ansi(t, "2")
    cyan = lambda t: _ansi(t, "36")

    print(bold("\ncruxial · offline demo") + dim("  (no API key — nothing left this machine)"))
    print("─" * 60)
    print(dim("tool: send_email(to: email, subject: ≤200 chars, body, priority: low|normal|high)"))
    print()

    caught = 0
    total = 0

    # 1. Happy path — a valid call executes and returns the executor's value.
    happy = valid_payload(schema)
    total += 1
    res = cruxial.execute("send_email", happy)
    if res.ok:
        print(green("  ✓ valid call      ") + dim(f"→ executed, returned {res.value}"))
    else:  # pragma: no cover — valid_payload should always satisfy the schema
        print(red(f"  ✗ valid call unexpectedly blocked: {res.failure.category}"))

    # 2. One synthetic violation per category the schema can express.
    payloads = violation_payloads(schema)
    for category, bad_args in payloads.items():
        total += 1
        res = cruxial.execute("send_email", bad_args)
        if not res.ok and res.failure:
            caught += 1
            print(red(f"  ✗ {category:<21} ") + dim("→ blocked before execution"))
            # Keep the excerpt narrow so the demo fits a wide font in the hero
            # GIF — the constraint message's filler value is the long part.
            print(dim(f"      {_ellipsize(res.failure.message, 72)}"))
        else:  # pragma: no cover
            print(f"  ? {category:<20} not caught (unexpected)")

    # 3. Unknown tool — a name the registry has never seen.
    total += 1
    res = cruxial.execute("delete_database", {"force": True})
    if not res.ok and res.failure:
        caught += 1
        print(red("  ✗ unknown_tool        ") + dim("→ blocked: delete_database is not registered"))

    # 4. Show one repair prompt — the thing you'd feed back to the LLM.
    fmt_bad = payloads.get("format_violation")
    if fmt_bad:
        res = cruxial.execute("send_email", fmt_bad)
        if not res.ok and res.failure:
            prompt = cruxial.build_repair_prompt(res.failure, fmt_bad)
            print()
            print(cyan("  example repair prompt (sent back to the model on a failure):"))
            # Show just the first few lines — enough to convey the shape without
            # dumping the full schema + args block on a first-run sanity check.
            lines = prompt.splitlines()
            for line in lines[:5]:
                print(dim(f"      │ {_ellipsize(line)}"))
            if len(lines) > 5:
                print(dim(f"      │ … (+{len(lines) - 5} more lines — full schema + args sent to the model)"))

    # 5. The action layer — does the model's "done" actually mean done?
    from cruxial.actions import ActionRegistry
    from cruxial.receipts import ReceiptRegistry, id_field

    print()
    print(cyan("  the action layer — \"done\" is a claim until there's a receipt:"))

    ar = ActionRegistry()
    ar.mark_action("send_email")
    rr = ReceiptRegistry()
    rr.register("send_email", id_field("message_id", kind="email"))
    cx_ok = guard(
        {"send_email": {"type": "object"}},
        {"send_email": lambda **k: {"message_id": "m_8f21"}},
        config=GuardConfig(sinks=("null",), ledger=False),
        receipt_registry=rr, action_registry=ar,
    )
    r_ok = cx_ok.execute("send_email", {"to": "a@b.com"})
    print(green("  ✓ send_email + receipt   ") + dim(f"→ {r_ok.state}  (receipt id {r_ok.receipt.id})"))

    ar2 = ActionRegistry()
    ar2.mark_action("send_email")
    cx_no = guard(
        {"send_email": {"type": "object"}},
        {"send_email": lambda **k: {"queued": True}},  # it ran, but returned no proof
        config=GuardConfig(sinks=("null",), ledger=False),
        receipt_registry=ReceiptRegistry(), action_registry=ar2,
    )
    r_no = cx_no.execute("send_email", {"to": "a@b.com"})
    print(red("  ✗ send_email, no receipt ") + dim(f"→ {r_no.state.upper()}  — the agent 'did' it, nothing proves it"))

    print()
    print("─" * 60)
    print(bold(f"  {caught}/{total - 1} violation types caught") + dim("  · 1 valid call passed · 0 telemetry rows written"))
    print()
    print("  next steps:")
    print(dim("    • wire your real tools:   ") + "guard(schemas=..., executors=...)")
    print(dim("    • mark side-effects:      ") + "@cruxial.action  +  cruxial.receipt(...)")
    print(dim("    • see what agents DID:    ") + "cruxial view")
    print(dim("    • see your live rate:     ") + "cruxial stats")
    print(dim("    • full guide:             ") + "https://github.com/cruxial-ai/cruxial#readme")
    print()
    return 0


def _ansi(text: str, code: str) -> str:
    """Wrap text in an ANSI color code only when stdout is an interactive TTY."""
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _ellipsize(s: str, max_len: int = 92) -> str:
    """Middle-truncate a long string, keeping both informative ends.

    Used by the demo so a 250-char `maxLength` violation value doesn't print as
    a wall of characters — we keep the start AND the trailing context (e.g.
    `(maxLength=200)`), collapsing the middle.
    """
    if len(s) <= max_len:
        return s
    head = max_len * 2 // 3
    tail = max_len - head - 1
    return f"{s[:head]}…{s[-tail:]}"


def cmd_diagnostic() -> int:
    import os
    import platform
    from cruxial.telemetry import _HOME_DB_PATH
    db = default_db_path()
    if "CRUXIAL_DB_PATH" in os.environ:
        source = "CRUXIAL_DB_PATH env var"
    elif db == _HOME_DB_PATH:
        source = "home fallback (no project marker found in cwd)"
    else:
        source = "project-local (auto-discovered)"
    print(f"cruxial {__version__}")
    print(f"python  {platform.python_version()} ({platform.python_implementation()})")
    print(f"os      {platform.system()} {platform.release()}")
    print(f"db      {db}")
    print(f"db exists: {db.exists()}")
    print(f"db source: {source}")
    return 0


# ─── helpers ────────────────────────────────────────────────────────────


def _parse_since(s: str) -> str | None:
    s = s.strip().lower()
    if s == "all":
        return None
    units = {"h": "hours", "d": "days", "m": "minutes"}
    if len(s) < 2 or s[-1] not in units:
        _err(f"invalid --since {s!r}. use e.g. 1h, 24h, 7d, all")
        sys.exit(2)
    try:
        n = int(s[:-1])
    except ValueError:
        _err(f"invalid --since {s!r}. use e.g. 1h, 24h, 7d, all")
        sys.exit(2)
    delta = timedelta(**{units[s[-1]]: n})
    return (datetime.now(timezone.utc) - delta).isoformat(timespec="milliseconds")


def _state_glyph(state: str | None) -> str:
    return _STATE_GLYPH.get(state, state or "unknown")


def _state_text(state, green, red, yellow) -> str:
    g = _state_glyph(state)
    if state in ("posted", "sent", "queued"):
        return green(g)
    if state in ("unknown", "failed"):
        return red(g)
    if state == "needs_review":
        return yellow(g)
    return g


def _ago(iso_ts: str | None) -> str:
    if not iso_ts:
        return "—"
    try:
        t = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    s = int((datetime.now(timezone.utc) - t).total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _print_header(since: str) -> None:
    print(f"\ncruxial · last {since}")
    print("─" * 41)


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

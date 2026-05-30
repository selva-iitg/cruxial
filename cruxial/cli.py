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

    p_diag = sub.add_parser("diagnostic", help="Print version + environment info for bug reports.")

    args = parser.parse_args(argv)

    if args.cmd == "stats":
        return cmd_stats(args.db, args.since)
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


def _print_header(since: str) -> None:
    print(f"\ncruxial · last {since}")
    print("─" * 41)


def _err(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())

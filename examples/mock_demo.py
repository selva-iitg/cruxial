"""End-to-end demo with no API key required.

Simulates a model that emits a deliberately-broken tool call (5 different
failure categories in sequence). Each one gets intercepted, classified, and
the "repaired" version executes. Use this to verify the SDK is wired up
correctly before you wire in a real LLM.

Run:
    python examples/mock_demo.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from cruxial import GuardConfig, guard
from cruxial.telemetry import SqliteSink


# ─── your tool definitions ─────────────────────────────────────────────

SCHEMAS = {
    "send_email": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "to": {"type": "string", "format": "email"},
            "subject": {"type": "string", "maxLength": 200},
            "body": {"type": "string"},
            "priority": {"type": "string", "enum": ["low", "normal", "high"]},
        },
        "required": ["to", "subject", "body"],
    },
}


def send_email(to, subject, body, priority="normal"):
    return {"ok": True, "id": "msg_1", "to": to, "subject": subject}


EXECUTORS = {"send_email": send_email}


# ─── simulated LLM emissions (each is a different failure category) ────

FAKE_LLM_CALLS = [
    # 1. happy path — passes
    {"to": "founders@cruxial.ai", "subject": "Week 1 update", "body": "shipping today"},
    # 2. missing_required
    {"subject": "Week 1 update", "body": "shipping today"},
    # 3. type_mismatch (recipient as int)
    {"to": 12345, "subject": "Week 1 update", "body": "shipping today"},
    # 4. enum_violation
    {"to": "founders@cruxial.ai", "subject": "x", "body": "y", "priority": "urgent"},
    # 5. format_violation (bad email)
    {"to": "definitely-not-an-email", "subject": "x", "body": "y"},
    # 6. constraint_violation (subject > 200 chars)
    {"to": "founders@cruxial.ai", "subject": "x" * 250, "body": "y"},
    # 7. extra_field (model invented a parameter)
    {"to": "founders@cruxial.ai", "subject": "x", "body": "y", "cc": "leadership@cruxial.ai"},
    # 8. unknown_tool
    ("unknown_send_sms", {"to": "+1...", "msg": "hi"}),
]


def main() -> int:
    # Use a tempfile sqlite db so the demo doesn't pollute ~/.cruxial.
    with tempfile.TemporaryDirectory() as td:
        db = Path(td) / "demo.sqlite"
        sink = SqliteSink(db)

        cruxial = guard(
            schemas=SCHEMAS,
            executors=EXECUTORS,
            config=GuardConfig(sinks=("null",)),
            sink=sink,
        )

        print("cruxial · mock demo\n")
        print("─" * 60)

        for i, call in enumerate(FAKE_LLM_CALLS, 1):
            if isinstance(call, tuple):
                name, args = call
            else:
                name, args = "send_email", call

            result = cruxial.execute(name, args)
            _print_result(i, name, args, result)

            # On failure, build the repair prompt that a real LLM round-trip
            # would consume.
            if not result.ok and result.failure:
                prompt = cruxial.build_repair_prompt(result.failure, args)
                print("    repair prompt that would be sent to the LLM:")
                for line in prompt.splitlines():
                    print(f"      │ {line}")
                print()

        # Show the dashboard at the end.
        print("─" * 60)
        print("\nrunning `cruxial stats` against this run:\n")
        # Re-use the SqliteSink to query
        rows = sink.query("SELECT status, COUNT(*) FROM interceptions GROUP BY status")
        total = sum(n for _, n in rows)
        for status, n in rows:
            print(f"  {status:<20} {n:>4}  ({n/total*100:.1f}%)")
        cat_rows = sink.query(
            "SELECT failure_category, COUNT(*) FROM interceptions WHERE failure_category IS NOT NULL GROUP BY failure_category ORDER BY 2 DESC"
        )
        print("\n  failure categories caught:")
        for cat, n in cat_rows:
            print(f"    {cat:<24} {n}")

        sink.close()

    return 0


def _print_result(i: int, name: str, args: dict, result):
    if result.ok:
        print(f"\n[{i}] ✓ passed   · {name}({_compact(args)})")
    else:
        f = result.failure
        print(f"\n[{i}] ✗ blocked  · {name}({_compact(args)})")
        print(f"    category: {f.category}")
        print(f"    message:  {f.message}")


def _compact(args: dict) -> str:
    try:
        s = json.dumps(args, default=str)
        if len(s) > 70:
            return s[:67] + "...}"
        return s
    except Exception:
        return repr(args)


if __name__ == "__main__":
    raise SystemExit(main())

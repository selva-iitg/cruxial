"""End-to-end demo: 5 realistic tools × 20 stress prompts × Azure OpenAI.

This is the script that generates Cruxial's headline demo data. No
chat-app layer, no assistant filtering — direct Azure OpenAI client →
cruxial guard → realistic constraint-heavy schemas → curated prompt set.

Why this exists vs azure_stress_demo.py and azure_stress_hard.py:
  - Those exercised one hand-crafted tool each.
  - This exercises the full cruxial.demo registry (5 tools, varied
    domains, varied constraint shapes) with 20 carefully designed prompts.
  - Volume + variety = statistically meaningful interception data.
  - The output is the cold-DM number.

Requires:
    pip install cruxial[openai]
    export AZURE_OPENAI_API_KEY=...
    export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
    export AZURE_OPENAI_DEPLOYMENT=<deployment-name>   # e.g. gpt-4o, gpt-5-mini-2
    export AZURE_OPENAI_API_VERSION=2024-08-01-preview  # optional

Run:
    python examples/azure_demo_suite.py
    cruxial stats --since 30m

To compare model tiers, run twice with different deployments:
    AZURE_OPENAI_DEPLOYMENT=gpt-4o python examples/azure_demo_suite.py
    AZURE_OPENAI_DEPLOYMENT=gpt-5-mini-2 python examples/azure_demo_suite.py

Each run takes ~10–15 minutes depending on model latency.
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections import Counter

from cruxial import GuardConfig, guard
from cruxial.adapters.openai import auto_repair_batch
from cruxial.demo import (
    DEMO_OPENAI_TOOLS,
    DEMO_PROMPTS,
    DEMO_TOOL_EXECUTORS_SYNC,
    DEMO_TOOL_SCHEMAS,
)


# ─── config ──────────────────────────────────────────────────────────


SYSTEM_PROMPT = (
    "You are a productivity assistant with access to tools for opening "
    "production incidents, scheduling meetings, sending invoices, opening "
    "pull requests, and searching inventory. For every user request, pick "
    "the appropriate tool and call it with arguments derived from the user's "
    "message. Always call a tool when one matches the request — do not ask "
    "clarifying questions. Make reasonable defaults for any missing info."
)


# ─── helpers ─────────────────────────────────────────────────────────


def _truncate(s: str, n: int = 90) -> str:
    s = s.replace("\n", " ")
    return s[: n - 1] + "…" if len(s) > n else s


def _color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


_DIM = lambda t: _color(t, "2")
_GREEN = lambda t: _color(t, "32")
_RED = lambda t: _color(t, "31")
_YELLOW = lambda t: _color(t, "33")
_CYAN = lambda t: _color(t, "36")
_BOLD = lambda t: _color(t, "1")


def _summarize_args(args: dict, n: int = 100) -> str:
    try:
        s = json.dumps(args, default=str, separators=(",", ":"))
        return _truncate(s, n)
    except Exception:
        return repr(args)[:n]


# ─── main ────────────────────────────────────────────────────────────


def main() -> int:
    missing = [
        v for v in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT")
        if not os.environ.get(v)
    ]
    if missing:
        print(f"error: set {', '.join(missing)} first", file=sys.stderr)
        return 2

    from openai import AzureOpenAI

    client = AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
    )
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]

    cruxial = guard(
        schemas=DEMO_TOOL_SCHEMAS,
        executors=DEMO_TOOL_EXECUTORS_SYNC,
        config=GuardConfig(sinks=("sqlite",)),
    )

    # Counters
    stats = {
        "prompts": 0,
        "tool_calls": 0,
        "no_tool": 0,
        "passed": 0,
        "intercepted": 0,
        "repaired": 0,
        "unrepairable": 0,
    }
    categories: Counter[str] = Counter()
    by_tool: dict[str, dict[str, int]] = {
        name: {"called": 0, "intercepted": 0, "repaired": 0}
        for name in DEMO_TOOL_SCHEMAS
    }
    api_errors = 0
    t_start = time.perf_counter()

    print(_BOLD(f"\ncruxial demo suite · {deployment} · {len(DEMO_PROMPTS)} prompts\n"))

    for i, p in enumerate(DEMO_PROMPTS, 1):
        stats["prompts"] += 1
        expected = p["expected_tool"]
        prompt_text = p["text"]
        designed = p["designed_to_trip"]

        print(_BOLD(f"[{i:2}/{len(DEMO_PROMPTS)}] ") + _truncate(prompt_text, 100))
        print(_DIM(f"        expected: {expected}  ·  designed_to_trip: {designed}"))

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_text},
        ]

        # Call the model.
        try:
            resp = client.chat.completions.create(
                model=deployment,
                messages=messages,
                tools=DEMO_OPENAI_TOOLS,
            )
        except Exception as exc:
            api_errors += 1
            print(_RED(f"        ✗ api error: {exc}\n"))
            continue

        tool_calls = resp.choices[0].message.tool_calls or []
        if not tool_calls:
            stats["no_tool"] += 1
            text = (resp.choices[0].message.content or "")[:80]
            print(_YELLOW(f"        ⚠ model declined to call a tool — said: {_truncate(text, 80)}\n"))
            continue

        # Append assistant turn so repair has the original tool_calls in context.
        assistant_tc_array = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in tool_calls
        ]
        messages_with_assistant = messages + [
            {"role": "assistant", "content": None, "tool_calls": assistant_tc_array}
        ]

        # Pass 1: validate + execute every tool call, build outcomes for batch repair.
        outcomes: list[dict] = []
        for tc in tool_calls:
            stats["tool_calls"] += 1
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                args = {}
                print(_RED(f"        ✗ {name} — bad JSON in arguments: {exc}"))

            if name in by_tool:
                by_tool[name]["called"] += 1
            print(_CYAN(f"        → {name}({_summarize_args(args)})"))

            result = cruxial.execute(name, args)
            outcome = {
                "tool_call_id": tc.id,
                "name": name,
                "args": args,
                "ok": result.ok,
                "value": result.value if result.ok else None,
                "failure": result.failure if not result.ok else None,
                "repair_prompt": (
                    cruxial.build_repair_prompt(result.failure, args)
                    if not result.ok else None
                ),
            }
            outcomes.append(outcome)

            if result.ok:
                stats["passed"] += 1
                print(_GREEN("          ✓ passed"))
            else:
                f = result.failure
                stats["intercepted"] += 1
                categories[f.category] += 1
                if name in by_tool:
                    by_tool[name]["intercepted"] += 1
                print(_RED(f"          ✗ INTERCEPTED · {f.category} · {f.message}"))

        # Pass 2: if any failed, send ONE batch repair turn that handles all of them.
        failed_outcomes = [o for o in outcomes if not o["ok"]]
        if failed_outcomes:
            try:
                corrected = auto_repair_batch(
                    client,
                    model=deployment,
                    messages=messages_with_assistant,
                    tools=DEMO_OPENAI_TOOLS,
                    tool_call_outcomes=outcomes,
                )
            except Exception as exc:
                # All failed outcomes are unrepairable if the repair request itself errors.
                for o in failed_outcomes:
                    stats["unrepairable"] += 1
                print(_RED(f"        ↻ batch repair failed: {_truncate(str(exc), 100)}"))
                print()
                continue

            for o in failed_outcomes:
                tc_id = o["tool_call_id"]
                name = o["name"]
                new_args = corrected.get(tc_id)
                if new_args is None:
                    stats["unrepairable"] += 1
                    print(_RED(
                        f"        ↻ {name}: model declined to re-emit "
                        f"({_truncate(o['failure'].message, 60)})"
                    ))
                    continue

                print(_CYAN(f"        ↻ {name} retry args: {_summarize_args(new_args)}"))
                retry = cruxial.execute_repaired(name, new_args)
                if retry.ok:
                    stats["repaired"] += 1
                    if name in by_tool:
                        by_tool[name]["repaired"] += 1
                    print(_GREEN(f"        ↻ {name}: REPAIRED ✓"))
                else:
                    stats["unrepairable"] += 1
                    print(_RED(
                        f"        ↻ {name}: REPAIR INSUFFICIENT · still "
                        f"{retry.failure.category}: {retry.failure.message}"
                    ))

        print()

    cruxial.close()

    # ─── summary ──────────────────────────────────────────────────────
    elapsed = time.perf_counter() - t_start
    print("─" * 64)
    print(_BOLD(f"summary · {deployment} · {len(DEMO_PROMPTS)} prompts · {elapsed:.0f}s"))
    print("─" * 64)

    tc = stats["tool_calls"] or 1
    print(f"  prompts sent              {stats['prompts']:>4}")
    print(f"  tool calls made           {stats['tool_calls']:>4}")
    print(f"  model declined            {stats['no_tool']:>4}")
    print(f"  api errors                {api_errors:>4}")
    print()
    print(f"  passed (clean)            {stats['passed']:>4}  ({stats['passed']/tc*100:5.1f}% of calls)")
    print(
        f"  intercepted               {stats['intercepted']:>4}  "
        f"({stats['intercepted']/tc*100:5.1f}% of calls)"
    )
    if stats["intercepted"]:
        repair_rate = stats["repaired"] / stats["intercepted"] * 100
        print(
            f"    of which auto-repaired  {stats['repaired']:>4}  "
            f"({repair_rate:5.1f}% of intercepts)"
        )
        print(f"    unrepairable            {stats['unrepairable']:>4}")

    if categories:
        print("\n  failure categories caught")
        for cat, n in categories.most_common():
            print(f"    {cat:<28} {n}")

    print("\n  by tool")
    for name, d in by_tool.items():
        if d["called"]:
            int_rate = d["intercepted"] / d["called"] * 100
            print(
                f"    {name:<28} called {d['called']:>2}  "
                f"intercepted {d['intercepted']:>2} ({int_rate:5.1f}%)  "
                f"repaired {d['repaired']:>2}"
            )
        else:
            print(f"    {name:<28} {_DIM('not called')}")

    print(f"\n  full telemetry: cruxial stats --since 30m\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

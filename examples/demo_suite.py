"""Provider-agnostic benchmark runner — reproduce Cruxial's headline numbers.

Runs the full ``cruxial.demo`` registry (15 production-class tools) against a
curated stress-prompt set through a real LLM, intercepting + batch-repairing
every tool call. Prints an interception rate, an auto-repair rate, and a
per-tool breakdown — the same shape as the numbers in BENCHMARKS.md.

It auto-detects your provider, so you can reproduce a number with whatever key
you already have:

  • Plain OpenAI (most common):
        pip install cruxial[openai]
        export OPENAI_API_KEY=sk-...
        python examples/demo_suite.py
        # optional: export CRUXIAL_OPENAI_MODEL=gpt-4o   (default: gpt-4o-mini)

  • Azure OpenAI (the original benchmark setup):
        export AZURE_OPENAI_API_KEY=...
        export AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
        export AZURE_OPENAI_DEPLOYMENT=gpt-4o
        python examples/demo_suite.py

Then see the full telemetry:
        cruxial stats --since 30m

Note: interception rate is a function of SCHEMA COMPLEXITY and MODEL TIER, so
your number will vary by model. A frontier model on these schemas typically
lands in the 5–17% range; that variance is the point, not a bug — see
BENCHMARKS.md for the full picture across models.
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


SYSTEM_PROMPT = (
    "You are a productivity assistant with access to tools for opening "
    "production incidents, scheduling meetings, sending invoices, opening "
    "pull requests, and searching inventory. For every user request, pick "
    "the appropriate tool and call it with arguments derived from the user's "
    "message. Always call a tool when one matches the request — do not ask "
    "clarifying questions. Make reasonable defaults for any missing info."
)


# ─── provider detection ──────────────────────────────────────────────────


def build_client() -> tuple[object, str, str]:
    """Return (client, model, provider_label) from whatever env is configured.

    Prefers Azure if its full trio of vars is present, else falls back to
    plain OpenAI. Exits with a clear message if neither is configured.
    """
    azure_vars = ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT")
    has_azure = all(os.environ.get(v) for v in azure_vars)
    has_openai = bool(os.environ.get("OPENAI_API_KEY"))

    if has_azure:
        from openai import AzureOpenAI

        client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
        )
        model = os.environ["AZURE_OPENAI_DEPLOYMENT"]
        return client, model, f"azure:{model}"

    if has_openai:
        from openai import OpenAI

        client = OpenAI()
        model = os.environ.get("CRUXIAL_OPENAI_MODEL", "gpt-4o-mini")
        return client, model, f"openai:{model}"

    print(
        "error: no LLM provider configured.\n"
        "  set OPENAI_API_KEY for plain OpenAI, or the AZURE_OPENAI_* trio for Azure.\n"
        "  see .env.example for the full list. (No key? run `cruxial demo` instead.)",
        file=sys.stderr,
    )
    sys.exit(2)


# ─── output helpers ──────────────────────────────────────────────────────


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
        return _truncate(json.dumps(args, default=str, separators=(",", ":")), n)
    except Exception:
        return repr(args)[:n]


# ─── main ────────────────────────────────────────────────────────────────


def main() -> int:
    client, model, label = build_client()

    cruxial = guard(
        schemas=DEMO_TOOL_SCHEMAS,
        executors=DEMO_TOOL_EXECUTORS_SYNC,
        config=GuardConfig(sinks=("sqlite",)),
    )

    stats = {
        "prompts": 0, "tool_calls": 0, "no_tool": 0,
        "passed": 0, "intercepted": 0, "repaired": 0, "unrepairable": 0,
    }
    categories: Counter[str] = Counter()
    by_tool: dict[str, dict[str, int]] = {
        name: {"called": 0, "intercepted": 0, "repaired": 0} for name in DEMO_TOOL_SCHEMAS
    }
    api_errors = 0
    t_start = time.perf_counter()

    print(_BOLD(f"\ncruxial demo suite · {label} · {len(DEMO_PROMPTS)} prompts\n"))

    for i, p in enumerate(DEMO_PROMPTS, 1):
        stats["prompts"] += 1
        prompt_text = p["text"]
        print(_BOLD(f"[{i:2}/{len(DEMO_PROMPTS)}] ") + _truncate(prompt_text, 100))
        print(_DIM(f"        expected: {p['expected_tool']}  ·  designed_to_trip: {p['designed_to_trip']}"))

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_text},
        ]

        try:
            resp = client.chat.completions.create(
                model=model, messages=messages, tools=DEMO_OPENAI_TOOLS,
            )
        except Exception as exc:
            api_errors += 1
            print(_RED(f"        ✗ api error: {_truncate(str(exc), 100)}\n"))
            continue

        tool_calls = resp.choices[0].message.tool_calls or []
        if not tool_calls:
            stats["no_tool"] += 1
            text = (resp.choices[0].message.content or "")[:80]
            print(_YELLOW(f"        ⚠ model declined to call a tool — said: {_truncate(text, 80)}\n"))
            continue

        assistant_tc_array = [
            {
                "id": tc.id, "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in tool_calls
        ]
        messages_with_assistant = messages + [
            {"role": "assistant", "content": None, "tool_calls": assistant_tc_array}
        ]

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
            outcomes.append({
                "tool_call_id": tc.id, "name": name, "args": args,
                "ok": result.ok,
                "value": result.value if result.ok else None,
                "failure": result.failure if not result.ok else None,
                "repair_prompt": (
                    cruxial.build_repair_prompt(result.failure, args) if not result.ok else None
                ),
            })

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

        failed_outcomes = [o for o in outcomes if not o["ok"]]
        if failed_outcomes:
            try:
                corrected = auto_repair_batch(
                    client, model=model, messages=messages_with_assistant,
                    tools=DEMO_OPENAI_TOOLS, tool_call_outcomes=outcomes,
                )
            except Exception as exc:
                for _o in failed_outcomes:
                    stats["unrepairable"] += 1
                print(_RED(f"        ↻ batch repair failed: {_truncate(str(exc), 100)}\n"))
                continue

            for o in failed_outcomes:
                name = o["name"]
                new_args = corrected.get(o["tool_call_id"])
                if new_args is None:
                    stats["unrepairable"] += 1
                    print(_RED(f"        ↻ {name}: model declined to re-emit"))
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
                    print(_RED(f"        ↻ {name}: REPAIR INSUFFICIENT · {retry.failure.category}"))

        print()

    cruxial.close()

    # ─── summary ──────────────────────────────────────────────────────────
    elapsed = time.perf_counter() - t_start
    tc = stats["tool_calls"] or 1
    print("─" * 64)
    print(_BOLD(f"summary · {label} · {len(DEMO_PROMPTS)} prompts · {elapsed:.0f}s"))
    print("─" * 64)
    print(f"  prompts sent              {stats['prompts']:>4}")
    print(f"  tool calls made           {stats['tool_calls']:>4}")
    print(f"  model declined            {stats['no_tool']:>4}")
    print(f"  api errors                {api_errors:>4}")
    print()
    print(f"  passed (clean)            {stats['passed']:>4}  ({stats['passed']/tc*100:5.1f}% of calls)")
    print(f"  intercepted               {stats['intercepted']:>4}  ({stats['intercepted']/tc*100:5.1f}% of calls)")
    if stats["intercepted"]:
        repair_rate = stats["repaired"] / stats["intercepted"] * 100
        print(f"    of which auto-repaired  {stats['repaired']:>4}  ({repair_rate:5.1f}% of intercepts)")
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
                f"intercepted {d['intercepted']:>2} ({int_rate:5.1f}%)  repaired {d['repaired']:>2}"
            )
        else:
            print(f"    {name:<28} {_DIM('not called')}")

    print(f"\n  full telemetry: cruxial stats --since 30m\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

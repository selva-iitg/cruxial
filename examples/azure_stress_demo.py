"""Fire a batch of prompts designed to maximize the chance of hallucination
on the model's side, so you can see Cruxial intercept and auto-repair.

A clean prompt like "send a one-line hello to x@y.com" gets a clean tool
call ~95% of the time. To see Cruxial earn its keep, you need:

  - schemas with multiple constraints (enum, format, maxLength, integer)
  - ambiguous prompts that force the model to guess
  - prompts that imply parameters the schema doesn't allow
  - prompts that omit required information

Run:
    python examples/azure_stress_demo.py
    cruxial stats --since 1h
"""

from __future__ import annotations

import json
import os
import sys

from cruxial import GuardConfig, guard
from cruxial.adapters.openai import auto_repair, extract_schemas


# Deliberately constraint-heavy schema. The more constraints, the more
# opportunities for the model to slip.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "create_ticket",
            "description": (
                "Create a support ticket. severity is one of {'p0','p1','p2','p3'}. "
                "due_in_hours must be between 1 and 168. tags must be an array of "
                "lowercase strings."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "maxLength": 80},
                    "description": {"type": "string"},
                    "severity": {"type": "string", "enum": ["p0", "p1", "p2", "p3"]},
                    "due_in_hours": {"type": "integer", "minimum": 1, "maximum": 168},
                    "assignee_email": {"type": "string", "format": "email"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string", "pattern": "^[a-z0-9-]+$"},
                        "maxItems": 5,
                    },
                },
                "required": ["title", "description", "severity"],
            },
        },
    },
]


def create_ticket(**kwargs):
    return {"ok": True, "id": "TK-0001", **kwargs}


EXECUTORS = {"create_ticket": create_ticket}


# Prompts designed to provoke each failure category at least once.
# Real models will hallucinate differently each run — that's the point.
STRESS_PROMPTS = [
    # Clean baseline — should pass
    "Create a p2 ticket: title 'Login button broken on Safari', "
    "description 'Users on Safari 17 cannot click the login button.', "
    "due in 48 hours, assign to oncall@cruxial.ai, tags: bug, frontend",

    # Ambiguous — model has to guess severity, due time, etc.
    "Create a ticket for the payment failures we keep seeing in prod.",

    # Implies args the schema doesn't allow (priority, category)
    "Open a high-priority ticket categorized as billing about the failed Stripe webhooks.",

    # Numeric out of range — model might say 'urgent' as severity or > 168h
    "Critical issue, customer-facing, fix needed in two weeks. "
    "Title: 'Email digests not sending'. Describe it briefly.",

    # Suggests enum values that don't exist (urgent / sev1)
    "File this as sev1: signup flow is throwing 500s for new users.",

    # Encourages malformed tag values (uppercase / spaces)
    "Make a ticket about the analytics dashboard being slow. "
    "Tags should be: 'Frontend Performance' and 'Q3 OKR'.",

    # Implies a wrong-type field (assignee as a name not email)
    "Create a p1 ticket for the deploy script failing, assign it to Riya in DevOps.",

    # Missing required field (no description)
    "Make a p3 ticket called 'Investigate flaky test'.",
]


def main() -> int:
    missing = [
        v for v in ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT")
        if not os.environ.get(v)
    ]
    if missing:
        print(f"set {', '.join(missing)} first", file=sys.stderr)
        return 2

    from openai import AzureOpenAI

    client = AzureOpenAI(
        api_key=os.environ["AZURE_OPENAI_API_KEY"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
    )
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]

    schemas = extract_schemas(TOOLS)
    cruxial = guard(
        schemas=schemas,
        executors=EXECUTORS,
        config=GuardConfig(sinks=("sqlite",)),  # stdout off to keep output clean
    )

    summary = {"passed": 0, "intercepted": 0, "repaired": 0, "unrepairable": 0, "no_tool": 0}

    for i, prompt in enumerate(STRESS_PROMPTS, 1):
        print(f"\n[{i}/{len(STRESS_PROMPTS)}]  {prompt[:80]}{'…' if len(prompt) > 80 else ''}")

        messages = [
            {
                "role": "system",
                "content": (
                    "You file support tickets via tools. Use the create_ticket tool. "
                    "Pick reasonable defaults when info is missing."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        try:
            resp = client.chat.completions.create(
                model=deployment, messages=messages, tools=TOOLS
            )
        except Exception as exc:
            print(f"  api error: {exc}")
            continue

        tool_calls = resp.choices[0].message.tool_calls or []
        if not tool_calls:
            print("  → model did not call any tool")
            summary["no_tool"] += 1
            continue

        tc = tool_calls[0]
        name = tc.function.name
        args = json.loads(tc.function.arguments or "{}")

        # Show what the model actually emitted — most useful diagnostic.
        print(f"  args: {json.dumps(args, default=str)[:160]}")

        result = cruxial.execute(name, args)
        if result.ok:
            print(f"  ✓ passed (no hallucination)")
            summary["passed"] += 1
            continue

        f = result.failure
        print(f"  ✗ INTERCEPTED · {f.category} · {f.message}")
        summary["intercepted"] += 1

        # Attempt 1-shot repair
        repair_prompt = cruxial.build_repair_prompt(f, args)
        messages_with_assistant = messages + [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": name, "arguments": tc.function.arguments},
                    }
                ],
            }
        ]
        try:
            new_args = auto_repair(
                client,
                model=deployment,
                messages=messages_with_assistant,
                tools=TOOLS,
                failure=f,
                failed_args=args,
                repair_prompt=repair_prompt,
                tool_call_id=tc.id,
            )
        except Exception as exc:
            print(f"  ✗ repair failed: {exc}")
            summary["unrepairable"] += 1
            continue

        retry = cruxial.execute_repaired(name, new_args)
        if retry.ok:
            print(f"  ↻ REPAIRED ✓  (model corrected the args on retry)")
            summary["repaired"] += 1
        else:
            print(f"  ↻ REPAIR INSUFFICIENT · still {retry.failure.category}: {retry.failure.message}")
            summary["unrepairable"] += 1

    cruxial.close()

    print("\n" + "─" * 60)
    print("stress test summary")
    print("─" * 60)
    total = sum(summary.values())
    for k, v in summary.items():
        pct = (v / total * 100) if total else 0.0
        print(f"  {k:<16} {v:>3}  ({pct:>5.1f}%)")
    print("\nrun `cruxial stats --since 1h` to see the full dashboard.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

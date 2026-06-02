"""`cruxial.run` — the one-call managed turn, end to end against a real model.

Shows the whole drop-in: define tools + executors, then let cruxial.run do the
model call → validate → execute → auto-repair → log → append, one turn at a
time. You keep the outer loop.

Auto-detects your provider from the env you set:

  • OpenAI:     export OPENAI_API_KEY=sk-...
  • Azure:      export AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_DEPLOYMENT
  • Anthropic:  export ANTHROPIC_API_KEY=sk-ant-...   (set CRUXIAL_ANTHROPIC_MODEL to pick a model)

Run:
    pip install 'cruxial[openai]'   # or cruxial[anthropic]
    python examples/run_demo.py
    cruxial stats --since 30m

The schema caps `due_in_hours` at 168 and the prompt asks for "two weeks"
(~336h). On smaller/older models you'll see a constraint_violation intercepted
and auto-repaired in one round-trip. Frontier models (gpt-4o, Claude) often
self-cap — they notice the limit and ask instead of emitting an invalid call —
which is also a good outcome; the demo explains whichever happens. For
interceptions at scale, run `examples/demo_suite.py`.
"""

from __future__ import annotations

import os
import sys

import cruxial

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string", "maxLength": 120},
        "due_in_hours": {"type": "integer", "minimum": 1, "maximum": 168},
        "priority": {"type": "string", "enum": ["low", "medium", "high"]},
    },
    "required": ["title", "due_in_hours", "priority"],
}
OPENAI_TOOLS = [{"type": "function", "function": {
    "name": "create_task", "description": "Create a task with a deadline.", "parameters": SCHEMA}}]
ANTHROPIC_TOOLS = [{"name": "create_task", "description": "Create a task with a deadline.",
                    "input_schema": SCHEMA}]
PROMPT = "Create a high-priority task titled 'Ship v0.2' due in two weeks."


def build_client():
    """Return (client, model, tools) from whatever provider env is configured."""
    if all(os.environ.get(v) for v in
           ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_DEPLOYMENT")):
        from openai import AzureOpenAI
        client = AzureOpenAI(
            api_key=os.environ["AZURE_OPENAI_API_KEY"],
            azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
            api_version=os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview"),
        )
        return client, os.environ["AZURE_OPENAI_DEPLOYMENT"], OPENAI_TOOLS
    if os.environ.get("OPENAI_API_KEY"):
        from openai import OpenAI
        return OpenAI(), os.environ.get("CRUXIAL_OPENAI_MODEL", "gpt-4o"), OPENAI_TOOLS
    if os.environ.get("ANTHROPIC_API_KEY"):
        from anthropic import Anthropic
        return (Anthropic(),
                os.environ.get("CRUXIAL_ANTHROPIC_MODEL", "claude-3-5-sonnet-latest"),
                ANTHROPIC_TOOLS)
    print("error: set OPENAI_API_KEY, the AZURE_OPENAI_* trio, or ANTHROPIC_API_KEY.\n"
          "       (No key? try `cruxial demo` for an offline run.)", file=sys.stderr)
    sys.exit(2)


def main() -> int:
    client, model, tools = build_client()

    created = []

    def create_task(title, due_in_hours, priority):
        created.append({"title": title, "due_in_hours": due_in_hours, "priority": priority})
        return {"id": "task_1", "ok": True}

    executors = {"create_task": create_task}
    messages = [{"role": "user", "content": PROMPT}]

    print(f"\ncruxial.run · {model}\n" + "─" * 56)
    print(f"prompt: {PROMPT}\n")

    turn = 0
    any_tool = False
    result = cruxial.run(client, model=model, messages=messages, tools=tools, executors=executors)
    while True:
        turn += 1
        for tc in result.tool_calls:
            any_tool = True
            cat = tc["failure"].category if tc["failure"] else None
            tag = "repaired ✓" if tc["repaired"] else ("executed ✓" if tc["ok"] else f"BLOCKED · {cat}")
            print(f"  turn {turn} → {tc['name']}({tc['args']})  [{tag}]")
        if result.finished:
            break
        result = cruxial.run(client, model=model, messages=result.messages,
                             tools=tools, executors=executors)

    print("\n" + "─" * 56)
    if created:
        print(f"✅ task created: {created}")
    elif not any_tool:
        # Frontier models often SELF-CAP on an obvious constraint — they ask
        # instead of emitting an invalid call. That's a good outcome, not a
        # failure: there was simply nothing for Cruxial to intercept.
        print("ℹ the model recognized the constraint and asked instead of emitting an")
        print("  invalid call (frontier models self-cap). Nothing to intercept here —")
        print("  Cruxial catches it on models/schemas that DON'T self-cap. Try")
        print("  `python examples/demo_suite.py` to see real interceptions at scale.")
    print(f"\nfinal answer: {result.text}\n")
    print("see what flowed through cruxial: cruxial stats --since 30m\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

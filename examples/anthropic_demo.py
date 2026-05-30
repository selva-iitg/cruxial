"""End-to-end demo against a real Anthropic agent loop.

Requires:
    pip install cruxial[anthropic]
    export ANTHROPIC_API_KEY=sk-ant-...

Run:
    python examples/anthropic_demo.py "send a recap email to founders@cruxial.ai"
"""

from __future__ import annotations

import os
import sys

from cruxial import GuardConfig, guard
from cruxial.adapters.anthropic import auto_repair, extract_schemas


TOOLS = [
    {
        "name": "send_email",
        "description": "Send an email.",
        "input_schema": {
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
    },
]


def send_email(to, subject, body, priority="normal"):
    return {"ok": True, "id": "msg_001", "to": to}


EXECUTORS = {"send_email": send_email}


def main(prompt: str) -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("set ANTHROPIC_API_KEY first", file=sys.stderr)
        return 2

    from anthropic import Anthropic

    client = Anthropic()
    model = os.environ.get("CRUXIAL_ANTHROPIC_MODEL", "claude-3-5-sonnet-latest")

    schemas = extract_schemas(TOOLS)
    cruxial = guard(
        schemas=schemas,
        executors=EXECUTORS,
        config=GuardConfig(sinks=("sqlite", "stdout")),
    )

    messages = [{"role": "user", "content": prompt}]
    resp = client.messages.create(
        model=model,
        max_tokens=1024,
        messages=messages,
        tools=TOOLS,
    )

    tool_uses = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
    if not tool_uses:
        text_blocks = [getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text"]
        print("model did not call any tool. response:", " ".join(text_blocks))
        return 0

    # Append assistant turn so repair has full context.
    messages_with_assistant = messages + [{"role": "assistant", "content": resp.content}]

    for tu in tool_uses:
        name = tu.name
        args = tu.input if isinstance(tu.input, dict) else {}
        result = cruxial.execute(name, args)

        if result.ok:
            print(f"✓ {name} executed: {result.value}")
            continue

        print(f"✗ {name} blocked: {result.failure.category} — {result.failure.message}")
        repair_prompt = cruxial.build_repair_prompt(result.failure, args)
        try:
            new_args = auto_repair(
                client,
                model=model,
                messages=messages_with_assistant,
                tools=TOOLS,
                failure=result.failure,
                failed_args=args,
                repair_prompt=repair_prompt,
                tool_use_id=tu.id,
            )
        except Exception as exc:
            print(f"  repair failed: {exc}")
            continue

        retry = cruxial.execute_repaired(name, new_args)
        if retry.ok:
            print(f"✓ {name} executed after repair: {retry.value}")
        else:
            print(f"✗ {name} still failing: {retry.failure}")

    cruxial.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(" ".join(sys.argv[1:]) or "send a one-line hello to founders@cruxial.ai"))

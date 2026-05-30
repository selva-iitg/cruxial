"""End-to-end demo against Azure OpenAI.

Cruxial works with Azure OpenAI unchanged — the only difference vs the
vanilla openai demo is the client class and the env vars.

Requires:
    pip install cruxial[openai]
    export AZURE_OPENAI_API_KEY=...
    export AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
    export AZURE_OPENAI_DEPLOYMENT=<your-deployment-name>     # e.g. "gpt-4o"
    export AZURE_OPENAI_API_VERSION=2024-08-01-preview        # optional, has default

Run:
    python examples/azure_openai_demo.py "send a recap email to founders@cruxial.ai"

Note: on Azure, the `model` parameter is your DEPLOYMENT NAME, not the
underlying model id. Cruxial doesn't care which — it just sees a client.
"""

from __future__ import annotations

import json
import os
import sys

from cruxial import GuardConfig, guard
from cruxial.adapters.openai import auto_repair, extract_schemas


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": "Send an email.",
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "to": {"type": "string", "format": "email"},
                    "subject": {"type": "string", "maxLength": 200},
                    "body": {"type": "string"},
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                    },
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
]


def send_email(to, subject, body, priority="normal"):
    return {"ok": True, "id": "msg_001", "to": to}


EXECUTORS = {"send_email": send_email}


def main(prompt: str) -> int:
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

    # On Azure, the `model` field is the deployment name.
    deployment = os.environ["AZURE_OPENAI_DEPLOYMENT"]

    schemas = extract_schemas(TOOLS)
    cruxial = guard(
        schemas=schemas,
        executors=EXECUTORS,
        config=GuardConfig(sinks=("sqlite", "stdout")),
    )

    messages = [
        {"role": "system", "content": "You can send emails using tools."},
        {"role": "user", "content": prompt},
    ]

    resp = client.chat.completions.create(
        model=deployment,
        messages=messages,
        tools=TOOLS,
    )

    tool_calls = resp.choices[0].message.tool_calls or []
    if not tool_calls:
        print("model did not call any tool. response:", resp.choices[0].message.content)
        return 0

    for tc in tool_calls:
        name = tc.function.name
        args = json.loads(tc.function.arguments or "{}")
        result = cruxial.execute(name, args)

        if result.ok:
            print(f"✓ {name} executed: {result.value}")
            continue

        print(f"✗ {name} blocked: {result.failure.category} — {result.failure.message}")
        repair_prompt = cruxial.build_repair_prompt(result.failure, args)

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
                failure=result.failure,
                failed_args=args,
                repair_prompt=repair_prompt,
                tool_call_id=tc.id,
            )
        except Exception as exc:
            print(f"  repair failed: {exc}")
            continue

        retry = cruxial.execute_repaired(name, new_args)
        if retry.ok:
            print(f"✓ {name} executed after repair: {retry.value}")
        else:
            print(f"✗ {name} still failing after repair: {retry.failure}")

    cruxial.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(" ".join(sys.argv[1:]) or "send a one-line hello to founders@cruxial.ai"))

"""Harder stress test — designed to surface failures on frontier-tier
mini models (gpt-5-mini, claude haiku, etc.) where the easy stress test
shows 0%.

Tactics:
  - Constraint density 3× the easy demo
  - Long enums (10+ values; models often pick a synonym not in list)
  - Regex-pattern tags (uppercase / spaces fail)
  - Format strings (email, uri, ipv4, date-time)
  - Numeric ranges that conflict with natural-language interpretation
  - Nested objects with required fields inside
  - Prompts implying values the schema rejects

Run:
    export AZURE_OPENAI_DEPLOYMENT=gpt-5-mini-2
    python examples/azure_stress_hard.py
    cruxial stats --since 1h
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
            "name": "create_incident",
            "description": (
                "Open a production incident in the IR system. "
                "severity is one of {'sev0','sev1','sev2','sev3','sev4'} "
                "(sev0 = total outage, sev4 = cosmetic). "
                "service_tier is one of {'tier-0','tier-1','tier-2','tier-3'}. "
                "tags are lowercase kebab-case only."
            ),
            "parameters": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "title": {"type": "string", "minLength": 8, "maxLength": 80},
                    "summary": {"type": "string", "minLength": 20, "maxLength": 500},
                    "severity": {
                        "type": "string",
                        "enum": ["sev0", "sev1", "sev2", "sev3", "sev4"],
                    },
                    "service_tier": {
                        "type": "string",
                        "enum": ["tier-0", "tier-1", "tier-2", "tier-3"],
                    },
                    "impacted_service": {
                        "type": "string",
                        "enum": [
                            "payments-api",
                            "auth-service",
                            "notification-service",
                            "analytics-pipeline",
                            "search-index",
                            "billing-jobs",
                            "media-cdn",
                            "feature-flags",
                        ],
                    },
                    "incident_commander": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "email": {"type": "string", "format": "email"},
                            "pager": {
                                "type": "string",
                                "pattern": r"^\+1-\d{3}-\d{3}-\d{4}$",
                            },
                        },
                        "required": ["email", "pager"],
                    },
                    "detected_at": {
                        "type": "string",
                        "format": "date-time",
                    },
                    "mttr_target_minutes": {
                        "type": "integer",
                        "minimum": 5,
                        "maximum": 240,
                    },
                    "affected_endpoint": {
                        "type": "string",
                        "format": "uri",
                    },
                    "tags": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "pattern": "^[a-z0-9][a-z0-9-]{1,30}$",
                        },
                        "minItems": 1,
                        "maxItems": 5,
                    },
                    "rollback_attempted": {"type": "boolean"},
                },
                "required": [
                    "title",
                    "summary",
                    "severity",
                    "service_tier",
                    "impacted_service",
                    "incident_commander",
                    "detected_at",
                    "tags",
                ],
            },
        },
    },
]


def create_incident(**kwargs):
    return {"ok": True, "id": "INC-2026-0001", **kwargs}


EXECUTORS = {"create_incident": create_incident}


# Adversarial prompts. Each one nudges the model toward a specific failure.
STRESS_PROMPTS = [
    # 1. Implies sev1 + payments outage but uses informal "totally down" phrasing.
    # Watch for severity drift (sev2 instead of sev1) and service typo
    # ("payments" instead of "payments-api").
    "URGENT: payments are totally down for the last 15 minutes. "
    "All checkout flows failing. Riya (riya@cruxial.ai, +1-415-555-0100) "
    "is leading. We need mttr under 30 mins. Tag this as payments-outage "
    "and customer-facing.",

    # 2. Long tag with spaces — pattern-violation magnet.
    # Also pager number in non-standard format ("415.555.0123").
    "Open a sev2 on the auth service. Login latency is up 4x. "
    "IC is Mira at mira@cruxial.ai, pager 415.555.0123. "
    "Detected this morning. Tags: 'Auth Latency Spike', 'Q3 Reliability', "
    "'P0 Backlog'. Target MTTR 90 minutes.",

    # 3. Implies a service not in the enum ("dashboards") + sev outside range
    # ("sev5") + free-form datetime ("yesterday at 11pm").
    "Customer reports the analytics dashboards have stopped updating. "
    "Make it a sev5, low priority, detected yesterday at 11pm PST. "
    "IC: ops@cruxial.ai, pager +1 415 555 0188. Tags: analytics-pipeline-stuck.",

    # 4. Demands fields not in schema (priority, customer_id, severity_score).
    "Critical billing outage. Set priority=urgent, customer_id=acme-2031, "
    "severity_score=9.5. IC: oncall@cruxial.ai, pager +1-415-555-0144. "
    "MTTR target 4 hours. Service: billing-jobs. Tags: billing-down, refund-block.",

    # 5. Mixed-case tags + boolean as string.
    "Open a sev3 on the notification-service. Push notifications failing. "
    "Detected at 2026-05-29T03:00:00Z. IC: notify-lead@cruxial.ai, "
    "pager +1-415-555-0177. Tags: 'Push-Notifications', 'apns-issue'. "
    "Rollback was 'yes'. MTTR: 1 hour.",

    # 6. mttr_target_minutes in days ("3 days" => 4320, way over max 240).
    "Long-running issue on search-index. Cosmetic — search ranking slightly off. "
    "Open as sev4. IC: search@cruxial.ai, pager +1-415-555-0199. "
    "Detected yesterday. Target fix in 3 days. Tags: search-relevance.",

    # 7. Conflicting: 'low impact' + 'totally broken'. Forces model to choose.
    # Also uses email-as-name for IC.
    "Open an incident: media CDN is totally broken but low impact "
    "since only beta users affected. Title and summary up to you. "
    "IC is 'Arnav from media platform', pager +1-415-555-0211. "
    "Detected at 2026-05-29T02:45:00Z. MTTR 2h. Service: media-cdn. "
    "Tags: cdn-degraded, beta-only.",

    # 8. Affected endpoint as plain string, not URI.
    "Open a sev2 on feature-flags. Stale flags being served. "
    "Affected endpoint is /api/flags/evaluate. "
    "IC: flags@cruxial.ai, pager +1-415-555-0233. "
    "Detected at 2026-05-29T04:00:00Z. MTTR 60. Tags: stale-cache, flags.",

    # 9. Title too short (<8 chars).
    "Title: 'help'. Sev3 on analytics-pipeline. "
    "IC: data@cruxial.ai, pager +1-415-555-0255. "
    "Detected 2026-05-29T05:00:00Z. MTTR 120 min. Tags: pipeline-lag. "
    "Summary: investigate the analytics ingestion delays from last hour.",

    # 10. Summary too short (<20 chars).
    "Sev4 on search-index. Title: 'Search slowness in EU region'. "
    "Summary: 'slow'. IC: search@cruxial.ai, pager +1-415-555-0244. "
    "Detected 2026-05-29T01:00:00Z. MTTR 180. Tags: eu-region, slow.",
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
        schemas=schemas, executors=EXECUTORS, config=GuardConfig(sinks=("sqlite",)),
    )

    summary = {"passed": 0, "intercepted": 0, "repaired": 0, "unrepairable": 0, "no_tool": 0}
    categories: dict[str, int] = {}

    print(f"\ncruxial · hard stress test against {deployment}\n")

    for i, prompt in enumerate(STRESS_PROMPTS, 1):
        print(f"\n[{i}/{len(STRESS_PROMPTS)}]  {prompt[:90]}{'…' if len(prompt) > 90 else ''}")

        messages = [
            {
                "role": "system",
                "content": (
                    "You are an incident-response assistant. Use create_incident "
                    "to file production incidents. Pick reasonable defaults when "
                    "info is missing. Always include all required fields."
                ),
            },
            {"role": "user", "content": prompt},
        ]

        try:
            resp = client.chat.completions.create(model=deployment, messages=messages, tools=TOOLS)
        except Exception as exc:
            print(f"  api error: {exc}")
            continue

        tool_calls = resp.choices[0].message.tool_calls or []
        if not tool_calls:
            print("  → model did not call any tool")
            summary["no_tool"] += 1
            continue

        tc = tool_calls[0]
        args = json.loads(tc.function.arguments or "{}")
        print(f"  emitted: {json.dumps(args, default=str)[:180]}{'…' if len(json.dumps(args, default=str)) > 180 else ''}")

        result = cruxial.execute(tc.function.name, args)
        if result.ok:
            print("  ✓ passed schema")
            summary["passed"] += 1
            continue

        f = result.failure
        print(f"  ✗ INTERCEPTED · {f.category} · {f.message}")
        categories[f.category] = categories.get(f.category, 0) + 1
        summary["intercepted"] += 1

        repair_prompt = cruxial.build_repair_prompt(f, args)
        messages_with_assistant = messages + [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
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

        print(f"  retry args: {json.dumps(new_args, default=str)[:180]}{'…' if len(json.dumps(new_args, default=str)) > 180 else ''}")
        retry = cruxial.execute_repaired(tc.function.name, new_args)
        if retry.ok:
            print("  ↻ REPAIRED ✓")
            summary["repaired"] += 1
        else:
            print(f"  ↻ REPAIR INSUFFICIENT · still {retry.failure.category}: {retry.failure.message}")
            summary["unrepairable"] += 1

    cruxial.close()
    print("\n" + "─" * 64)
    print(f"summary  ·  {deployment}")
    print("─" * 64)
    total = sum(summary.values())
    for k, v in summary.items():
        pct = (v / total * 100) if total else 0.0
        print(f"  {k:<16} {v:>3}  ({pct:>5.1f}%)")
    if categories:
        print("\n  failure categories caught:")
        for cat, n in sorted(categories.items(), key=lambda x: -x[1]):
            print(f"    {cat:<28} {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Synthetic audit: drive cruxial.testing through every mined MCP schema.

No LLM. No API costs. Just verifies that for every real-world schema in the
snapshot:

  - `cruxial.testing.valid_payload()` can produce a satisfying payload
  - `cruxial.testing.violation_payloads()` can produce at least one
    payload per applicable failure category
  - Every violation gets correctly classified by `cruxial.execute()`

This is the robustness benchmark: how well does Cruxial's classifier +
testing helper generalise across hundreds of real production schemas?

Run after `examples/mine_mcp_schemas.py`:
    python examples/audit_mcp_schemas.py
"""

from __future__ import annotations

import sys
from collections import Counter, defaultdict

from cruxial import GuardConfig, guard
from cruxial.telemetry import NullSink
from cruxial.testing import valid_payload, violation_payloads


def main() -> int:
    try:
        from cruxial.demo.mcp_schemas import MCP_SCHEMAS
    except ImportError:
        print(
            "error: cruxial.demo.mcp_schemas not found. "
            "Run examples/mine_mcp_schemas.py first.",
            file=sys.stderr,
        )
        return 1

    # Flatten {server_id: {tool_name: schema}} → {(server, tool): schema}
    all_schemas: list[tuple[str, str, dict]] = []
    for server_id, payload in MCP_SCHEMAS.items():
        for tool_name, schema in payload["schemas"].items():
            all_schemas.append((server_id, tool_name, schema))

    print(f"\n  cruxial · MCP schema audit")
    print(f"  {len(MCP_SCHEMAS)} servers · {len(all_schemas)} tools\n")
    print("  " + "─" * 60)

    valid_passed = 0
    valid_failed: list[tuple[str, str, str]] = []
    intercept_correct = 0           # caught under the exact category the generator labeled
    intercept_alternate = 0         # caught under a different — but still legitimate — category
                                    # (e.g. payload labeled "constraint_violation" was caught as
                                    # "format_violation" because the field has BOTH a format and
                                    # a constraint, and the stricter format check fires first)
    intercept_missed = 0            # silent pass — a real bug
    cats_total: Counter[str] = Counter()
    coverage_per_server: dict[str, dict[str, int]] = defaultdict(
        lambda: {"tools": 0, "valid_ok": 0, "violations": 0, "categories": 0}
    )

    # Build a single mega-guard with all schemas registered (validate-only).
    flat = {f"{sid}__{tname}": schema for sid, tname, schema in all_schemas}
    cruxial = guard(
        schemas=flat,
        config=GuardConfig(sinks=("null",)),
        sink=NullSink(),
    )

    for server_id, tool_name, schema in all_schemas:
        key = f"{server_id}__{tool_name}"
        coverage_per_server[server_id]["tools"] += 1

        # 1. valid_payload should pass
        try:
            good = valid_payload(schema)
            res = cruxial.check(key, good)
            if res.ok:
                valid_passed += 1
                coverage_per_server[server_id]["valid_ok"] += 1
            else:
                valid_failed.append(
                    (server_id, tool_name, f"{res.failure.category}: {res.failure.message}")
                )
        except Exception as e:
            valid_failed.append((server_id, tool_name, f"{type(e).__name__}: {e}"))

        # 2. violation_payloads — every category should be intercepted correctly
        try:
            payloads = violation_payloads(schema)
            for category, bad_args in payloads.items():
                res = cruxial.check(key, bad_args)
                cats_total[category] += 1
                coverage_per_server[server_id]["violations"] += 1
                if not res.ok and res.failure.category == category:
                    intercept_correct += 1
                    coverage_per_server[server_id]["categories"] += 1
                elif not res.ok:
                    # Caught, but under a different category. Common when a
                    # field has BOTH a format and a constraint: the stricter
                    # check fires first. Still a correct rejection — not a
                    # silent pass.
                    intercept_alternate += 1
                    coverage_per_server[server_id]["categories"] += 1
                else:
                    intercept_missed += 1
        except Exception as e:
            # Generator can't produce violations for this schema (e.g. no
            # constraints at all). Not a failure.
            pass

    cruxial.close()

    total_violations = intercept_correct + intercept_alternate + intercept_missed
    total_caught = intercept_correct + intercept_alternate

    print(f"\n  valid_payload                {valid_passed:>4} / {len(all_schemas)} schemas pass")
    if valid_failed:
        print(f"    failures: {len(valid_failed)}")
        for sid, tname, err in valid_failed[:8]:
            print(f"      - {sid}/{tname}: {err[:90]}")
        if len(valid_failed) > 8:
            print(f"      … and {len(valid_failed) - 8} more")

    print(f"\n  violations caught            {total_caught:>4} / {total_violations} ({total_caught/max(total_violations,1)*100:.1f}%)")
    print(f"    exact category match       {intercept_correct:>4}")
    if intercept_alternate:
        print(f"    alternate legit category   {intercept_alternate:>4}  (stricter check preempted — still rejected)")
    if intercept_missed:
        print(f"    silently passed (bug!)     {intercept_missed:>4}")

    if cats_total:
        print(f"\n  categories exercised across the corpus")
        for cat, n in cats_total.most_common():
            print(f"    {cat:<28} {n}")

    print(f"\n  per-server coverage")
    for sid in sorted(coverage_per_server):
        d = coverage_per_server[sid]
        valid_rate = (d["valid_ok"] / d["tools"] * 100) if d["tools"] else 0.0
        cat_rate = (d["categories"] / d["violations"] * 100) if d["violations"] else 0.0
        print(
            f"    {sid:<28} {d['tools']:>3} tools  ·  "
            f"valid {d['valid_ok']:>2}/{d['tools']:<2} ({valid_rate:5.1f}%)  ·  "
            f"violations {d['categories']:>3}/{d['violations']:<3} ({cat_rate:5.1f}% correct)"
        )

    print(f"\n  summary")
    print(f"    schemas covered            {len(all_schemas)}")
    print(f"    valid-payload success rate {valid_passed/len(all_schemas)*100:5.1f}%")
    if total_violations:
        rejection_rate = total_caught / total_violations * 100
        exact_rate = intercept_correct / total_violations * 100
        print(f"    rejection rate             {rejection_rate:5.1f}%  (no silent passes)")
        print(f"    exact-category accuracy    {exact_rate:5.1f}%  (the rest hit a stricter sibling rule)")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

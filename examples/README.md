# Cruxial examples

Runnable scripts that show Cruxial intercepting real (and simulated) LLM tool
calls. Set keys via environment variables — see [`../.env.example`](../.env.example)
for the full list.

> **No key? Start here.** `cruxial demo` (shipped with the package) and
> `mock_demo.py` both run a full interception demo **offline**, with zero API
> cost. Use them to confirm the SDK works before wiring in a real model.

## What needs what

| Script | Requires | Cost / time | What it shows |
|---|---|---|---|
| **`mock_demo.py`** | nothing (offline) | free · instant | Simulated model emits one broken call per failure category; each is intercepted, classified, and the repair prompt is printed. The fastest way to see the full pipeline. |
| **`audit_mcp_schemas.py`** | nothing (offline) | free · seconds | Synthetic classifier audit over the shipped real-world MCP schemas — reproduces the "98.3% exact-category, 0 silent passes" robustness number with no LLM. |
| **`action_layer.py`** ⭐ | nothing (offline) | free · instant | **The v0.5 action layer.** `@action` / `@receipt` / `@verify` → `posted` / `unknown` (silent failure) / `needs_review` states, plus the `run()` catch where the agent says "done" and never calls the tool. Writes a throwaway ledger and prints the `cruxial view --web` command to see it. |
| **`run_demo.py`** ⭐ | any one provider key (OpenAI / Azure / Anthropic) | ~$ · seconds | **The `cruxial.run` one-call managed turn.** Define tools + executors, then `run` does call → validate → execute → auto-repair → log → append, one turn at a time. The "insanely easy" drop-in. Auto-detects your provider. |
| **`run_vs_own_loop.py`** ⭐ | OpenAI **or** Azure key | ~$ · ~1 min | **`run()` vs. own-your-loop, side by side.** Runs identical live scenarios through both `cruxial.run()` and a hand-rolled loop (`cx.execute()` + `cx.check_bypass()`) and prints a parity table — they land the *identical* ledger (`posted` / `needs_review` / `unknown` / `failed`). Shows when to reach for which, and that the manual path is just as safe. |
| **`demo_suite.py`** ⭐ | `OPENAI_API_KEY` **or** Azure trio | ~$ · ~10 min | **Provider-agnostic benchmark.** Runs the 15-tool demo registry through a live model and prints intercept + auto-repair rates. This is the script to reproduce the headline BENCHMARKS.md numbers with whatever key you have. |
| `openai_demo.py` | `OPENAI_API_KEY` | ~$ · seconds | Single-prompt OpenAI agent loop: intercept → 1-shot auto-repair → execute. |
| `anthropic_demo.py` | `ANTHROPIC_API_KEY` | ~$ · seconds | Same loop, Anthropic Messages API. |
| `azure_openai_demo.py` | Azure trio | ~$ · seconds | Same loop, Azure OpenAI. |
| `azure_demo_suite.py` | Azure trio | ~$ · ~10 min | The original Azure-only benchmark runner (superseded by `demo_suite.py`; kept for exact reproduction of the published Azure runs). |
| `azure_mcp_suite.py` | Azure trio + `CRUXIAL_MCP_SERVERS` | ~$$ · ~3 min | Live MCP-server benchmark across many public servers. |
| `azure_stress_demo.py` | Azure trio | ~$ · ~1 min | Single hand-crafted tool, easy schema. |
| `azure_stress_hard.py` | Azure trio | ~$ · ~1 min | Single hand-crafted tool, constraint-heavy schema. |
| `mine_mcp_schemas.py` | `OPENAI_API_KEY` + `ANTHROPIC_API_KEY` | ~$ · minutes | Re-mines public MCP servers into `cruxial/demo/mcp_schemas.py`. **You usually don't need this** — the mined schemas already ship with the package; run `audit_mcp_schemas.py` directly. |

"Azure trio" = `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_DEPLOYMENT`.

## Quick start

```bash
pip install cruxial[openai]

# 1. Prove it works, offline, no key:
cruxial demo
python examples/mock_demo.py

# 2. Reproduce a real benchmark number with your own key:
export OPENAI_API_KEY=sk-...
python examples/demo_suite.py
cruxial stats --since 30m
```

## A note on interception rates

Your intercept rate will **vary by model and schema complexity** — that's
expected, not a bug. Frontier models nail simple schemas (often 0%); the
catches concentrate in constraint-heavy and newer/niche API schemas. See
[`../BENCHMARKS.md`](../BENCHMARKS.md) for the full breakdown across models.

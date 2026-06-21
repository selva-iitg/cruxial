# Cruxial

[![tests](https://github.com/cruxial-ai/cruxial/actions/workflows/tests.yml/badge.svg)](https://github.com/cruxial-ai/cruxial/actions/workflows/tests.yml)
[![PyPI](https://img.shields.io/pypi/v/cruxial.svg?label=pypi&color=blue)](https://pypi.org/project/cruxial/)
[![Python](https://img.shields.io/pypi/pyversions/cruxial.svg)](https://pypi.org/project/cruxial/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Socket](https://badge.socket.dev/pypi/package/cruxial/0.5.2?artifact_id=tar-gz)](https://socket.dev/pypi/package/cruxial)

**The action layer for AI agents.**

Your agent said it sent the email. It didn't.

Cruxial proves what your agent actually did. Every side-effecting tool call
resolves from a **receipt** — so "done" is evidence, not the model's word:
`posted` when there's proof, `unknown` when there isn't. Along the way it
validates arguments against your schema and auto-repairs bad ones before they
run. Drop-in for OpenAI and Anthropic. Overhead is under 1ms p99 — validation
runs locally, no extra network hop. Fails open by default: if Cruxial itself
errors, your tool still runs.

```bash
pip install cruxial
```

Then watch it catch every failure category live — offline, no API key:

```bash
cruxial demo
```

![cruxial demo catching every failure category offline, then showing the repair prompt](https://raw.githubusercontent.com/cruxial-ai/cruxial/main/assets/cruxial-demo.gif)

## Two ways to use it — same guarantees

Pick by how much of the loop you want to own. Both paths validate every tool
call, auto-repair bad args, read the receipt, and record every action to the
same local ledger:

- **`cruxial.run()`** drives one model turn for you — call the model, validate,
  execute, auto-repair in one round-trip, record. The drop-in below.
- **`guard()`** keeps the loop yours — for streaming, a framework that owns the
  model call (LangGraph, CrewAI, an Assistants/Responses runtime), or a worker
  dispatching a single call. You call `.execute()` / `.check_bypass()` yourself
  ([Own your loop](#own-your-loop--guard)).

## Quickstart — `cruxial.run()`

```python
import cruxial

result = cruxial.run(
    client,                 # your OpenAI / AzureOpenAI / Anthropic client, or litellm.completion
    model="gpt-4o",
    messages=messages,
    tools=tools,            # the same tool defs you already pass the LLM
    executors=executors,    # {tool_name: your_function}
)

while not result.finished:  # your loop stays yours — one model call per turn
    result = cruxial.run(client, model="gpt-4o", messages=result.messages,
                         tools=tools, executors=executors)

print(result.text)          # the model's final answer
```

It reuses your configured client (Azure endpoint, `base_url`, timeouts all preserved),
derives schemas from `tools`, and fails open. Deliberately **one turn, not a framework** —
no streaming, no multi-turn ownership, you decide when to stop. `result.operations` and
`result.state(tool)` reflect the **latest turn**; keep your own list across the loop if you
want every turn's operations (the full history is always in the ledger — `cruxial view`).

## The action layer — did it actually happen?

Schema validation catches a *malformed* call. It can't catch the worse failure: your
agent says **"I sent the email"** and never called the tool — or the call returned with no
proof it worked. Mark a side-effecting tool with `@action`, tell Cruxial how to read its
**receipt**, and every call resolves from the receipt — never the model's word for it. No
receipt ⇒ `unknown`, never a silent "done".

```python
import cruxial

@cruxial.action                          # side-effecting → a receipt is required
def send_email(to, subject, body):
    return mailer.send(to=to, subject=subject, body=body)   # e.g. {"message_id": "..."}

@cruxial.receipt("send_email")           # how to read the proof (or: id_field("message_id"))
def _(raw):
    return cruxial.Receipt(ok=bool(raw.get("message_id")), id=raw.get("message_id"), kind="email")

@cruxial.verify("send_email")            # optional domain check → PASS / FLAG / HALT
def _(args, receipt):
    # Runs only once a receipt exists — a send with no receipt is already `unknown`.
    # HALT a real send that needs a human look (here: an external recipient) → needs_review.
    return cruxial.HALT("external recipient") if not args["to"].endswith("@acme.com") else cruxial.PASS

result = cruxial.run(client, model=m, messages=msgs, tools=tools, executors=ex)
result.state("send_email")   # → "posted" | "unknown" | "needs_review" | "failed"
result.render()              # receipt-derived summary — never a bare "done"
```

A claimed-but-never-called action is caught **deterministically** — recorded as `unknown`
with no extra model call (the default). Then see what your agents actually did:

```bash
cruxial view           # terminal ledger
cruxial view --web     # the dashboard below — confirmed vs silent-failure (unknown), per-op receipts
```

![cruxial view --web — the action ledger: 3 of 19 agent actions couldn't be confirmed (silent failures), shown next to receipt-backed posted ops, per-tool validator coverage, and the receipt/actor for every operation](assets/cruxial-dashboard.png)

> **Upgrading from 0.4:** the action layer is additive — existing `guard()`/`run()` code is
> unchanged until you mark a tool `@action`. One breaking change: `run(bypass="on")` now
> *detects deterministically* and records the action as `unknown` instead of re-prompting the
> model (the old re-prompt remediation has been removed — what to do about an `unknown` is your
> policy). See the [CHANGELOG](CHANGELOG.md).

## Own your loop — `guard()`

Can't hand the turn to `run()` — streaming, a framework that owns the model call,
a worker dispatching one tool? Wrap your registry once and call the two primitives
`run()` is built on. They land the **same** ledger:

```python
import json
from cruxial import guard

cx = guard(schemas=schemas, executors=executors)    # the same tool defs you pass the LLM

for tool_call in llm_response.tool_calls:
    args = json.loads(tool_call.arguments)           # OpenAI returns arguments as a JSON string
    result = cx.execute(tool_call.name, args)        # validate → run → receipt → record
    if not result.ok:
        result.raise_on_failure()                    # typed error (category on result.failure)
    if result.state == "needs_review":               # a verify HALT — surface it, don't blind-retry
        ...
    use(result.value)

# A text-only turn that CLAIMS an action but emitted no call → detect AND record it:
if not tool_calls_this_turn:
    cx.check_bypass(assistant_text, called_tools=tools_called_so_far)
```

`cx.execute()` records the full operation (receipt-derived `state`), so the healthy
path **and** the called-but-no-proof failure are covered for free. The one line not to
skip is **`cx.check_bypass()`** on text-only turns — the safe, fused detect-and-record.
(The bare `cruxial.bypass.detect_bypass()` *detects* but doesn't record, silently
dropping the catch.)

**Async?** `await cruxial.arun(...)` and `await cx.aexecute(...)` — same contract,
everything awaited.

**See it run:** `python examples/action_layer.py` shows both `execute()` and `run()`
offline (no key); [`examples/run_vs_own_loop.py`](examples/run_vs_own_loop.py) puts both
wirings through a live model and prints a parity table — identical ledger either way, so
the choice is ergonomic, not a safety trade-off.

## What it catches

Eight failure categories. Every interception is logged with the failure
category — never the raw argument values.

| Category | What it catches |
|---|---|
| `missing_required` | Required field not in args |
| `type_mismatch` | Wrong type (`int` instead of `str`, etc.) |
| `enum_violation` | Value not in allowed enum |
| `format_violation` | Bad email / uri / date format, or a `pattern` mismatch |
| `constraint_violation` | maxLength / minimum / multipleOf / etc. |
| `extra_field` | Model invented a field that doesn't exist |
| `unknown_tool` | Tool name not in registry |
| `tool_bypass` | **Model *claimed* it did something but emitted no call** — "your agent said it sent the email. It didn't." |

The first seven are schema-derivable. **`tool_bypass`** is the one validators
structurally can't catch — there's no call to validate. See below.

> **`extra_field` on open schemas.** JSON Schema is open by default, so an invented
> field passes schema validation. Cruxial still catches it: when you execute, the
> field is checked against the executor's signature and blocked as `extra_field`
> before it can crash the call — so a hallucinated field is caught either at the
> schema layer (`additionalProperties: false` / `GuardConfig(strict_properties=True)`)
> or at the executor boundary. An executor that declares `**kwargs` opts into extras
> and is never blocked.

## tool_bypass — catch the action your agent claimed but never took

A model says *"I've sent the email"* and emits **no `send_email` call**. No
call ever goes out — so no error, no log, nothing to grep for. The silent
failure. `cruxial.run()` catches it:

```python
result = cruxial.run(client, model="gpt-4o", messages=messages,
                     tools=tools, executors=executors)   # bypass check is on by default

if result.bypass:        # the model claimed an action it never called → recorded as `unknown`
    print("caught a bypass:", result.bypass.tool)
```

**How it works (zero cost on normal turns):** a final text turn is flagged
*only* when it claims a completed action (`"sent"`, not `"send"`), attributed
to the assistant (not *"you"* / *"the scheduler"* / *"automatically"*), for a
side-effecting tool that was never called — and that no tool which actually ran
already satisfies. As with the action layer above, the flag is recorded
**deterministically as `unknown`** with no extra model call — the receipt's
absence is the oracle. Owning your loop instead of `run()`?
`cx.check_bypass(text, called_tools=...)` records the same catch.

Precision-first by design: the local pre-filter fires on completion-form verbs
with conservative attribution/negation guards (a 132-scenario adversarial set is
in [BENCHMARKS.md](BENCHMARKS.md)), so terse claims ("Done.", "Email's out.") are
a documented recall gap — a high-quality net, not a complete guarantee. The
durable guarantee is the **receipt**, not the prose read. `bypass="off"` disables
detection entirely.

## Auto-repair

`cruxial.run()` already auto-repairs for you. If you use `guard()` directly,
call the adapter helper with the failure context:

```python
from cruxial.adapters.openai import auto_repair

cruxial = guard(schemas=schemas, executors=executors)
result = cruxial.execute(name, args)

if not result.ok:
    # 1-attempt structured retry — feeds the failure back to the model
    new_args = auto_repair(
        client,
        model=model,
        messages=messages,            # conversation incl. the assistant tool-call turn
        tools=tools,                  # the same tool defs you sent the model
        failure=result.failure,
        failed_args=args,
        repair_prompt=cruxial.build_repair_prompt(result.failure, args),
    )
    result = cruxial.execute_repaired(name, new_args)
```

Roughly **90% of intercepted calls are fixed in a single repair round-trip**
on the pooled live-MCP benchmark (87–94% per run on current code). Every number
is sourced in [BENCHMARKS.md](BENCHMARKS.md).

## See your interception rate

Every interception is written to a local SQLite file (no data leaves your
machine). To see your real rate:

```bash
cruxial stats
```

**Stats are project-local automatically** — inside a project (a dir with `.git/`,
`pyproject.toml`, `setup.py`, or `.cruxial/`) the DB lives at `./.cruxial/telemetry.sqlite`,
so each app stays separate; elsewhere it falls back to `~/.cruxial/`. Override with
`CRUXIAL_DB_PATH`; `cruxial diagnostic` shows the active path.

Output:

```
cruxial · last 24h
─────────────────────────────────────────
  total calls           1,247
  intercepted             184  (14.8%)
  auto-repaired           167  (90.8% of intercepted)
  passed through        1,063

top failing tools                    rate
  send_email                        23.1%
  create_calendar_event             18.4%
  search_web                         9.2%

top failure categories
  type_mismatch                       62
  missing_required                    44
  enum_violation                      38
  format_violation                    24
  constraint_violation                16
```

`cruxial stats` also shows the registry independently of traffic — handy for the
"is it even on?" check after install:

```
  registry              6 registered  ·  3 fired  ·  1 intercepted
```

Traffic but 0 interceptions usually means a well-behaved model on a simple schema —
`cruxial.testing.violation_payloads(schema)` fires a synthetic violation per category
to verify end-to-end.

## How it works

```mermaid
sequenceDiagram
    participant M as LLM model
    participant C as Cruxial guard
    participant T as Your tool / executor
    participant L as Local action ledger

    M->>C: tool call (name, args)
    C->>C: validate args vs JSON Schema
    alt args valid
        C->>T: run tool
        T-->>C: result (+ receipt for an @action)
        C->>C: resolve state from the receipt
        C->>L: record operation — posted | unknown | needs_review | failed
        C-->>M: receipt-derived state — never a bare "done"
    else args invalid
        C->>L: record intercepted + failure category (hashes only)
        opt auto-repair enabled
            C-->>M: repair prompt (1-shot retry)
            M->>C: corrected tool call
        end
        C-->>M: typed failure (caller decides what to do)
    end
    Note over M,C: model claims an action but emits NO call → recorded as unknown (the silent-failure catch)
```

Cruxial wraps the **tool registry**, not the LLM client. No monkey-patching,
no proxies, no framework lock-in.

## Schema source — if your LLM sees a trimmed schema

If you keep two views of a tool schema — a **canonical** one for execution and a
**trimmed** one sent to the LLM — register the *trimmed* one. The LLM can only satisfy
the schema it was shown; validating against canonical fields it never saw turns
`missing_required` / `extra_field` into false positives.

```python
# Right
cruxial = guard(schemas=tool_definitions_sent_to_llm)

# Must use the canonical schema? Opt in — validation is unchanged, but it warns at
# construction and tags every telemetry row (visible in `cruxial stats`) to filter later:
cruxial = guard(schemas=canonical_definitions,
                config=GuardConfig(schema_origin="canonical"))
```

## Privacy

By default Cruxial stores: tool name, schema fingerprint, failure category,
timing. **Never the argument values themselves.** Hashes only.

The interceptor runs in your process. Your data never leaves your
infrastructure unless you opt into Cruxial Cloud (coming soon).

## Define tools as Pydantic models

Already model your tools with Pydantic? Skip the hand-written JSON Schema —
cruxial extracts it for you. Optional: `pip install cruxial[pydantic]` (v2).

```python
from pydantic import BaseModel, Field
from cruxial.adapters.pydantic import guard_models, tool_schema

class SendEmail(BaseModel):
    """Send an email to a recipient."""
    to: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    subject: str = Field(min_length=1, max_length=200)
    body: str

# One source of truth → both the runtime guard and the LLM tool definition
cruxial = guard_models({"send_email": SendEmail}, executors={"send_email": send_email})
tools   = [tool_schema(SendEmail, name="send_email")]                       # OpenAI format
# tools = [tool_schema(SendEmail, name="send_email", provider="anthropic")]  # Anthropic
```

Works with `run()` too — `run()` derives the schema from `tools`, so the whole
managed turn (validate → execute → auto-repair) runs against your model:

```python
from cruxial import run
run(client, model="gpt-4o", messages=msgs,
    tools=[tool_schema(SendEmail, name="send_email")],
    executors={"send_email": send_email})
```

Nested models and enums (Pydantic's `$defs`/`$ref`) validate end to end. The
adapter is lazy and optional — cruxial's core never requires Pydantic.

## What ships today (v0.5)

- ✅ Python SDK
- ✅ OpenAI + **Azure OpenAI** + Anthropic + LiteLLM (auto via normalization)
- ✅ JSON Schema validation
- ✅ 7 schema-validation categories (+ `tool_bypass` below = 8 total)
- ✅ 1-attempt auto-repair
- ✅ Local SQLite + stdout telemetry
- ✅ `cruxial stats` CLI
- ✅ Fail-open by default
- ✅ `cruxial.run()` — one managed turn (OpenAI / Azure / Anthropic / LiteLLM)
- ✅ **The action layer** — `@action` / `@receipt` / `@verify` resolve every call to `posted` / `unknown` / `needs_review` / `failed` from the receipt, never the model's word
- ✅ **`tool_bypass` detection** — the claimed-but-never-called catch, recorded deterministically as `unknown`
- ✅ **`cruxial view`** — local action ledger (`--web` dashboard) of what your agents actually did
- ✅ **`cx.check_bypass()`** — the fused detect-and-record primitive for when you own the loop
- ✅ **Pydantic adapter** — define tools as Pydantic models (`cruxial[pydantic]`)

Coming:
- TypeScript SDK
- LangChain, LlamaIndex, AutoGen adapters
- Hosted dashboard with cross-customer schema drift alerts
- Zod (TypeScript) validators

## Questions or feedback?

Open a [GitHub issue](https://github.com/cruxial-ai/cruxial/issues), or reach the
maintainers on [Discord](https://discord.gg/qJu7yq54s). We'd especially like to hear
from anyone running Cruxial in a real agent — early design-partner feedback shapes
the roadmap.

## License

MIT. The SDK runs entirely in your process. The hosted dashboard (Cruxial
Cloud) will be a separate paid product. The interceptor itself stays MIT
forever.

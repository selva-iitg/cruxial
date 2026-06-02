# Cruxial — defensive posture & graceful-failure guide

> **Promise:** cruxial never silently mis-validates, never crashes your host
> app, and never leaks raw arg values. When it doesn't handle something, it
> fails loudly and tells you why.

This document is the honest mirror of `BENCHMARKS.md`. Benchmarks tell you
what cruxial catches; this tells you what it doesn't, and exactly how it
behaves when it doesn't.

Every section maps to actual test files. If you find a behavior that
contradicts a guarantee here, it's a P0 bug — please open an issue.

---

## What cruxial guarantees

### 1. **Never crashes the host app**

The `fail_open=True` default means: if any cruxial internal raises, the
host's tool call still executes. A warning is emitted (so you observe the
gap), and execution proceeds.

Verified by: `tests/test_robustness_failure_modes.py` (16 tests).

| Failure mode | Behavior |
|---|---|
| Sink (`SqliteSink`) raises during write | Swallowed, `check()` still returns correct result |
| Validator function itself crashes | Warning emitted, treated as pass-through |
| `guard()` construction fails (`strict=False`) | Returns `NoopCruxial` — every method is a safe no-op |
| User's executor raises | Surfaced as `ExecutionResult.error`, not unhandled |
| Telemetry sink `register_tools()` raises | Swallowed, schemas still registered in memory |
| Sink `close()` raises | Swallowed, host shutdown continues |
| MCP adapter can't reach server | Raises catchable typed exception, doesn't hang |

The only exception that always propagates: `KeyboardInterrupt` (so Ctrl-C
shutdown works as expected).

### 2. **Never silently mis-validates the documented surface**

Verified by: `tests/test_robustness_malformed_input.py` (19 tests),
`tests/test_robustness_schema_quirks.py` (24 tests).

Every documented input shape produces either:
- A correct `ok=True` result, OR
- A typed `Failure` with `category`, `message`, and `path`, OR
- (in `fail_open` mode) a pass-through with an explicit warning

What "documented surface" means:
- JSON Schema Draft 2020-12 features (`type`, `properties`, `required`,
  `enum`, `format`, `pattern`, `minLength`/`maxLength`, `minimum`/`maximum`,
  `minItems`/`maxItems`, `additionalProperties`, `multipleOf`,
  `allOf`/`anyOf`/`oneOf`)
- Format keywords: `email`, `uri`, `iri`, `uri-reference`, `hostname`,
  `idn-hostname`, `date`, `time`, `date-time`, `ipv4`, `uuid`, `pattern`
- Args containing unicode, control chars, NaN/Infinity (handled gracefully)
- Tool names with special characters (treated as opaque strings)

### 3. **Never leaks raw arg values to telemetry**

Verified by: `tests/test_robustness_adversarial.py` (4 tests).

Arg values are SHA-256 hashed at `cruxial.telemetry.hash_args()` before
they touch the sink. The raw bytes of the SQLite file are explicitly tested
to NOT contain any submitted arg value (canary test on every CI run).

What IS stored:
- Tool name
- Schema fingerprint (first 16 chars of SHA-256)
- Args fingerprint (first 16 chars of SHA-256)
- Failure category + path (never the value)
- Timestamp, latency, status

What IS NOT stored: any raw `to`, `subject`, `body`, `password`, etc.

### 4. **Never silently accepts wire-format mismatches**

Verified by: `tests/test_robustness_provider_specs.py` (26 tests) +
`cruxial.lint_schema()` API.

Vendor-API session-killer bugs (Anthropic illegal property keys, OpenAI
strict mode requirements, malformed JSON Schema from MCP servers) are
caught by `cruxial.lint_schema()`. Run it in CI:

```python
from cruxial import lint_schemas_for_openai, lint_schemas_for_anthropic

issues = lint_schemas_for_anthropic(my_tools)
for i in issues:
    print(f"[{i.severity}] {i.tool}.{i.path}: {i.message}")
    if i.fix_hint:
        print(f"   fix: {i.fix_hint}")
    if i.cited_incident:
        print(f"   cited: {i.cited_incident}")
```

Every lint rule cites the real production incident that motivated it (see
`cruxial/lint.py` for the catalog).

### 5. **Never hangs**

Verified by: `tests/test_robustness_resource_limits.py` (13 tests).

| Pathological input | Cruxial completes within |
|---|---|
| 1 MB string arg | < 1s |
| 10 MB constrained string arg | < 5s |
| 10,000-value enum check | < 1s |
| 10,000 tools registered + `check()` | < 5s for construction, O(1) per lookup |
| 50-level deep nested args | finite (no `RecursionError` crash) |
| CVE-2024-3772-style adversarial email | < 2s |
| 25-level deep `allOf` composition | finite |

Note: **don't accept untrusted schemas**. Pathological regex in
`pattern` (e.g., `(a+)+`) WILL slow down Python's `re` module — that's a
documented limitation, not a defended boundary. Schemas must come from
trusted sources (your code, vendor MCP servers you've vetted).

### 6. **Concurrency-safe**

Verified by: `tests/test_robustness_concurrency.py` (11 tests).

| Pattern | Behavior |
|---|---|
| 20 threads × 50 `check()` calls each | All complete without error |
| 10 threads × 100 SQLite writes | All persisted; no "database is locked" surfaced to host |
| Concurrent `register_schemas()` | Last-write-wins on schema dict; no corrupt state |
| `asyncio.to_thread(cx.check, ...)` × 50 tasks | No deadlock with SqliteSink lock |
| Fork after import | Safe; no module-level locks/events/threads |
| Fork → child creates fresh guard | Works; no inherited SQLite-connection corruption |

---

## What cruxial does NOT guarantee (documented limitations)

These are known gaps. Each one fails in a **documented, predictable** way.

### 1. Prompt-injection defense in arg values

Cruxial **passes arg string content through unchanged**. A prompt-injection
payload in a tool arg (`{"text": "Ignore previous instructions..."}`)
validates as a normal string. We do NOT scan content.

**Why:** that's a prompt-engineering / guardrails concern, not a schema-
validation concern. Tools like `nemo-guardrails`, `llm-guard`, or
`Anthropic's prompt-shield` operate at that layer.

**What we DO provide:**
- Telemetry never echoes the raw payload (so injection content can't leak
  via screenshots of `cruxial stats`)
- Schema drift detection via `hash_schema()` — if a tool's schema changes
  (the CVE-2025-54136 "rug-pull" pattern), the hash changes too
- `additionalProperties: false` catches field-smuggling (extra parameters
  the schema doesn't declare)

### 2. Unicode lookalike (homograph) attacks

Schema doesn't distinguish `test@example.com` from `test@еxample.com`
(Cyrillic `е`). jsonschema's email format checker is lenient.

**Behavior:** lookalike args pass validation. Host is responsible for
normalization (e.g., `unicodedata.normalize`) before submitting if needed.

### 3. Adversarial regex (ReDoS) in user-supplied schemas

If you accept untrusted schemas (e.g., from a public MCP server you
haven't audited), a pathological `pattern` like `^(a+)+$` will slow down
Python's `re` engine on certain inputs.

**Behavior:** cruxial uses standard `re`; validation may take seconds on
adversarial input. Test `test_validator_does_not_hang_on_adversarial_pattern_constraint`
bounds it to 3s.

**Workaround:** vet schemas before accepting them. The `cruxial.lint_schema()`
helper flags structural issues but does NOT analyze regex complexity.

### 4. `format: email` strictness

jsonschema's default email checker accepts addresses like
`a@a.a.a.a.a.a.a` that strict RFC 5321 would reject. We do not override
because (a) strict email is a moving target and (b) most production tools
just need "looks like an email."

If you need strict email validation, use a custom `pattern` constraint or
wait for `cruxial[pydantic]` (V0.2) which lets you use Pydantic's
`EmailStr` directly.

### 5. Tool-bypass detection — handled in `cruxial.run()`, with limits

When the model writes "I sent the email" but emits **no** `tool_calls`, there
is nothing to schema-validate. `cruxial.run()` catches this: a final text turn
is flagged by a local, zero-cost filter (completion-form verb, attributed to
the assistant, side-effecting tool never called), then ONE neutral re-prompt
decides — the model re-emits the call (corrected + executed) or declines (no
action). We only act on a model-confirmed re-emission, so a false flag never
fabricates an action.

Benchmarked (132 adversarial scenarios): 100% correction recall, acted-on
precision 100% (Claude sonnet-4-6) / 98.4% (gpt-4o).

**Known limits (honest):**
- Detection is English, heuristic — colloquialisms ("pushed it live") and
  verb-synonym gaps can be missed (recall, not safety).
- It distinguishes the *assistant* from *you* / a *scheduler* / *automation*,
  but **cannot** tell a third-party **person** apart ("my colleague sent it") —
  the lone residual false-action source on sycophantic models.
- It only works in the `run()` path (it needs the assistant text). The bare
  `guard().execute()` path can't see the prose, so it can't detect bypass.
- Disable with `bypass="off"`; `bypass="strict"` is an opt-in 2-call variant
  (not recommended — empirically worse on sycophantic models).

### 6. Multi-error per call cap at 5

`cruxial.validator.validate()` collects up to **5 violations per call**
(deduped by `(category, path)`). A pathological schema with 50 violations
on a single arg gets the first 5; the model is asked to fix those.

**Why:** experimental — repair-prompt clutter hurts model success rates.
5 is the empirical sweet spot. Override via `validate()` directly if you
need more (returns a `ValidationResult` with siblings).

### 7. Cross-process telemetry

Each cruxial-using process writes to its own `SqliteSink`. There's no
multi-process aggregation (cf. Cruxial Cloud, V0.2).

**Workaround:** use `CRUXIAL_DB_PATH=/shared/path/telemetry.sqlite` with
SQLite-WAL mode enabled (we don't enable WAL by default — set
`PRAGMA journal_mode=WAL` if you need it).

### 8. Auth-aware MCP transport

`cruxial.adapters.mcp.import_server_sse(url=...)` doesn't accept HTTP
headers. If your MCP server is behind auth, you must fetch schemas via
your own client and pass them to `cruxial.register_schemas()`.

**Workaround pattern:**
```python
schemas = await my_custom_mcp_client.list_tools()  # auth in your code
{name: schema for name, schema in schemas.items()}
cruxial.register_schemas(schemas)
```

---

## What happens when cruxial encounters something genuinely weird

Every weird input maps to one of these surfaces:

| What you observe | What it means | What to do |
|---|---|---|
| `ExecutionResult(ok=False, failure=Failure(category=..., message=..., path=...))` | Cruxial caught a validation issue | Use the failure to build a repair prompt or surface to user |
| `ExecutionResult(ok=False, error=Exception)` | Your executor raised | Handle the exception per your normal app logic |
| `ExecutionResult(ok=True, value=...)` from `check()` | Validation passed (value is None because check doesn't execute) | Proceed with execution |
| `ExecutionResult(ok=True, value=<result>)` from `execute()` | Validation + execution both succeeded | Use the value |
| `warnings.warn("cruxial: ...")` in your logs | Something internal failed open; the call proceeded but you should investigate | Look at the warning message for the specific issue |
| `RepairExhausted` raised | Auto-repair couldn't fix the call after `max_attempts` | Decide: surface to user, fall back to alternative tool, or skip |
| `SchemaViolation` / `ToolUnknown` raised via `res.raise_on_failure()` | You opted into typed exceptions | Catch + handle per type |
| `cruxial diagnostic` shows `db source: home fallback` when you expected project-local | Cruxial didn't find a `.git`/`pyproject.toml` ancestor | Set `CRUXIAL_DB_PATH` explicitly or `cd` to your project root |
| `cruxial stats` shows 0 calls when traffic is flowing | Wrong DB path | Run `cruxial diagnostic` to see resolved path; check `CRUXIAL_DB_PATH` and project-marker discovery |

---

## How to report a defensive-posture bug

Open an issue with:

1. `cruxial diagnostic` output (gives version + db path + Python info)
2. Minimal reproduction (schema + args)
3. What you observed
4. What you expected per this document

A defensive-posture bug is anything where cruxial:
- Crashes the host (must always be fail-open by default)
- Silently mis-validates (e.g., accepts an obviously-bad value)
- Hangs or takes >10s on reasonable input
- Leaks raw arg values to telemetry
- Returns an `ExecutionResult` with both `ok=True` AND a `failure` (logically impossible — file immediately)

---

## Test inventory

Run the full robustness suite:

```bash
pytest tests/test_robustness_*.py tests/test_observability.py -v
```

| File | Coverage | Test count |
|---|---|---|
| `test_robustness_malformed_input.py` | Empty/null args, type confusion, double-encoded JSON, unknown tools | 19 |
| `test_robustness_schema_quirks.py` | jsonschema footguns, format defaults, recursion, allOf, draft compat | 24 |
| `test_robustness_provider_specs.py` | OpenAI strict / Anthropic charset / Anthropic top-level composition / well-formedness | 26 |
| `test_robustness_concurrency.py` | Threads, asyncio, fork, SQLite contention, sink failures | 11 |
| `test_robustness_resource_limits.py` | 1MB-10MB args, deep nesting, 10k tools, ReDoS budget, FD leaks | 13 |
| `test_robustness_adversarial.py` | Telemetry privacy, schema-drift detection, field smuggling, lookalike attacks | 14 |
| `test_robustness_failure_modes.py` | Fail-open at every layer, typed exceptions, NoopCruxial degradation | 16 |
| `test_observability.py` | Failure-message quality, repair-prompt completeness, CLI surfaces | 22 |
| **Total robustness-specific** | | **145** |
| **Total test suite** | All including unit + integration + benchmarks | **243** |

Every failure caught by this suite blocks release.

---

*This document is the contract. If cruxial behaves differently than what's
documented here, that's the bug — not your code.*

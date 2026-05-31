# Cruxial Benchmarks

Every number in this document is reproducible from the public repo. Each run lists its model, sample, command, and date. No unsourced claims — if it isn't here, don't cite it.

Last updated: 2026-05-30.

---

## TL;DR

> **0 silent passes across 342 live LLM tool calls** *(95% CI: 0–1.1%)*. **Two independent runs**, 51 real public MCP servers, Azure gpt-4o.
>
> Aggregate intercept rate: **5.85%** *(95% CI: 3.8–8.9%)*. Aggregate auto-repair rate: **90.0%** *(95% CI: 69.9–97.2%)*.
>
> Plus 877 production schemas validated synthetically — 100% rejection rate, 98.3% exact-category accuracy, no false negatives.
>
> <1ms p99 overhead per call. 271 tests pass. MIT.

A "silent pass" is the only failure mode a validation layer truly owns. We do not have one.

---

## Headline numbers

| Test | Model | Sample | Intercept | Auto-repair | Silent passes | p99 overhead |
|---|---|---|---|---|---|---|
| 🚀 **Live MCP, pooled (2 runs)** ← cite this | Azure gpt-4o | **342 calls** · 352 prompts · 51 servers · 25 domains · 603 tools | **5.85%** (20/342) · 95% CI 3.8–8.9% | **90.0%** (18/20) · 95% CI 69.9–97.2% | **0/342** · 95% CI 0–1.1% | <1ms |
| Live MCP, run #2 (post-QA) | Azure gpt-4o | 171 calls · same corpus | 4.7% (8/171) · 95% CI 2.4–9.0% | 87.5% (7/8) | 0 | <1ms |
| Live MCP, run #1 (baseline) | Azure gpt-4o | 171 calls · same corpus | 7.0% (12/171) · 95% CI 4.1–11.9% | 91.7% (11/12) | 0 | <1ms |
| **Synthetic robustness** | none (classifier only) | 877 schemas · 52 servers · 26 domains · 1947 violations | **100% rejection** · 98.3% exact-category | n/a | **0** | <1ms |
| **Constraint-heavy schemas** | Azure gpt-4o | 70 calls · 15 production-class tools | **17.1%** (12/70) ±8.8% CI | 66.7% (8/12) | **0** | <1ms |
| Same schemas, mini-tier model | Azure gpt-5-mini-2 | 74 calls · same tools | **1.4%** (1/74) ±2.7% CI | 100% (1/1) | **0** | <1ms |
| **Pre-flight lint** (derived from live runs) | none | 9 server-side OpenAI rejections | **100% caught pre-flight** | n/a | **0** | <1ms |
| Control: simple-schema MCP | Azure gpt-4o | 25 calls · 7 simple servers | 0.0% | n/a | **0** | <1ms |

Wilson 95% intervals throughout.

---

## What these runs prove

### 1. Cruxial never silently passes a bad call.

342 live calls across two independent runs (95% upper bound on silent-pass rate: 1.1%). 1947 synthetic violations across 877 production schemas. Zero made it past the validator. This is the only number that matters for a validation layer — every other metric is texture.

The intercept rate varies between runs (4.7% in run #2, 7.0% in run #1) because gpt-4o is non-deterministic — but the *catch behavior* doesn't vary. When the model emits a violation, Cruxial catches it. That's the deterministic part of the system, and it's what the silent-pass count measures.

### 2. Intercept rate scales with schema complexity, not model tier.

Same gpt-4o, same day:

| Schema surface | Intercept rate |
|---|---|
| Simple MCP (filesystem, time, fetch, memory) | **0.0%** |
| Real public MCP (51 servers, mixed maturity) | **4.7% – 7.0%** |
| Constraint-heavy production-class (15 hand-crafted tools with enums, formats, regex tags, datetime ranges, nested objects) | **17.1%** |

Frontier models nail trivial schemas. The catches live in the gap between "what the model has seen in training" and "what the constraint surface actually requires." That gap widens as APIs ship faster than training cutoffs.

### 3. Model tier matters more than people think.

On the same 70-prompt constraint-heavy benchmark:

| Model | Intercept rate | Relative |
|---|---|---|
| Azure gpt-4o | 17.1% | baseline |
| Azure gpt-5-mini-2 | **1.4%** | **92% fewer violations** |

The mini tier of the new generation beats the flagship of the old. If you've upgraded your model tier, you've already done the cheapest reliability fix available — Cruxial catches the remainder.

### 4. `cruxial.lint` catches every live server-side schema rejection pre-flight.

This is a **separate catch surface from the live intercept rate above** — distinct measurement, distinct value proposition. Do not average it with the 5.85% live-intercept number; they measure different failure modes.

| Surface | Fires at | Catches | This finding |
|---|---|---|---|
| Live intercepts (finding #1) | runtime, per tool call | model emits bad args against a valid schema | 5.85% |
| **Pre-flight lint** (this finding) | build-time, at schema registration | the schema itself is broken — vendor APIs reject it before the model is even invoked | 100% |

During the live MCP runs, 9 of 176 calls failed at OpenAI's tool-registration step — schemas the API rejected before the model ever saw the prompt. `cruxial.lint_schemas_for_openai()` reproduces every one of those rejections pre-flight, with a fix hint and a citation:

| MCP server | Live API rejections | `cruxial.lint` catch |
|---|---|---|
| gitlab | 5 calls (all attempted tools) | **9/9 schemas** flagged `EMPTY_OR_META_ONLY_SCHEMA` — every gitlab tool ships as `{"$schema": "..."}` with no `type` or `properties` |
| hubspot | 4 calls (all on `hubspot-search-objects`) | `hubspot-search-objects` flagged **`ARRAY_WITHOUT_ITEMS`** (fatal) plus 22 advisory OpenAI-strict warnings |

100% pre-flight catch on the live failures. The lint module isn't speculative — it catches what production actually breaks on.

---

## Methodology

**What "intercepted" means.** The validator caught a schema violation BEFORE the executor ran. The executor never fires on an intercept; the host decides whether to attempt repair.

**What "passed" means.** The model emitted args that satisfied the JSON Schema. It does NOT mean the call was semantically correct — only that the args are well-formed. Tool-bypass failures (model claims to have called a tool but didn't) are out of scope for V0.1.

**What "silent pass" means.** A payload that should have been rejected but wasn't. This is the only true failure mode of a validation layer. We track and report it on every run.

**What "intercept rate" measures.** `intercepted / total_tool_calls` — per-call, not per-prompt. A single prompt may emit N tool calls.

**Latency.** p50 / p99 of `cruxial.execute()` itself (validation + telemetry write), measured against `time.perf_counter_ns()`. Excludes the LLM repair round-trip on intercepts.

**Confidence intervals.** 95% Wilson intervals for binomial proportions throughout.

**Schema corpus.** All MCP schemas in the synthetic-robustness and live runs are mined from real public MCP servers (github, kubernetes, salesforce, atlassian, airtable, notion, slack, ms-teams, playwright, supabase, pinecone, …). The mining script `examples/mine_mcp_schemas.py` is in the repo. Schemas are frozen as a Python module (`cruxial.demo.mcp_schemas`) so audits are diffable across runs.

**Two distinct catch surfaces — don't conflate them.** Cruxial catches schema problems at two completely different points in the pipeline, and we report them separately on purpose:

| | When it fires | What it catches | This doc's number |
|---|---|---|---|
| **Live intercepts** | runtime, on every LLM tool call | the model emitted args that violate a valid schema | **5.85%** pooled (Section A) |
| **Pre-flight lint** | build-time, at `register_schemas()` | the *schema itself* is broken — vendor APIs would reject it before the model even sees it | **100%** of observed live API rejections (Section D) |

The two numbers are not comparable and should never be averaged or combined in headline claims. A reader who sees "Cruxial catches 100% of schema bugs" should understand that's the *lint* number — measuring developer-supplied schema quality, not LLM hallucination rate.

**The Deterministic Repair Rule.** Cruxial's auto-repair handles *structural mutations* of the model's output — wrong type, missing field, broken format, clipped enum, value outside numeric range. It does **not** inject *semantic domain knowledge* — generating a 1536-dimensional vector embedding, fabricating an API token, knowing what ticker `AAPL` resolved to today, picking the right Pinecone vector shape. Cases that require knowledge the LLM doesn't have show up as `unrepairable` in the benchmark (1 such case in run #2, same case in run #1). Cruxial is not magic; it makes the model's structural errors fixable, not the model's knowledge gaps.

---

## A. Live LLM · 51 real public MCP servers

The launch benchmark. The largest published live LLM test of any tool-call validation layer.

**Configuration**

- Script: `examples/azure_mcp_suite.py`
- Corpus: 51 mined MCP servers · 25 distinct domains · 603 tools · 176 natural-language prompts (3–5 per server, mixed clean / ambiguous / adversarial)
- Model: Azure gpt-4o
- Repair: `cruxial.adapters.openai.auto_repair_batch` (multi-tool-call batch repair)
- Telemetry sink: project-local `~/.cruxial/telemetry.sqlite` (cleaned between runs)

**Run #2 — 2026-05-30, post defensive-QA sprint (launch)**

```
prompts sent     176
tool calls made  171
model declined     7
api errors         9   (gitlab × 5, hubspot × 4 — server-side schema rejection,
                       caught pre-flight by cruxial.lint — see section D)

passed (clean)   163  ( 95.3% of calls)
intercepted        8  (  4.7% of calls)
  auto-repaired    7  ( 87.5% of intercepts)
  unrepairable     1  (pinecone upsert-records — vector format requires
                       domain knowledge, not a schema-fixable case)

silent passes      0
wall clock       168s
p99 overhead    <1ms
```

All 8 intercepts were `missing_required`. The single unrepairable case: model emits `[0.1, 0.2, 0.3]` for a Pinecone vector field whose schema requires a different shape — not schema-fixable without an embedding API call.

**Run #1 — 2026-05-30, pre defensive-QA sprint (baseline)**

```
tool calls made  171
intercepted       12  (  7.0%)  ±3.9% CI
auto-repaired    11   ( 91.7%)
silent passes      0
wall clock       182s
```

**Comparison.** The 12 → 8 intercept delta is gpt-4o non-determinism — run #1's CI covers 4.1–11.9%, run #2's 4.7% sits squarely inside. No SDK behavior change explains it. The load-bearing claim is the same in both runs: **zero silent passes.**

**Pooled across both runs (the citable aggregate)**

```
total calls         342   (171 + 171)
total prompts       352   (176 + 176)

intercepted          20   (12 + 8)
  rate              5.85%       95% CI 3.8 – 8.9%
auto-repaired        18   (11 + 7)
  rate             90.0%        95% CI 69.9 – 97.2%
unrepairable          2   (same pinecone vector-shape case both runs)

silent passes         0   /342  95% CI 0 – 1.1%
total wall clock    350s
```

The pooled silent-pass interval (0 – 1.1%) is the tightest statistical bound this benchmark can produce on the SDK's central claim without running the suite more times. Adding a third run would tighten it further; the marginal value diminishes quickly past N=2.

**On tightening the upper bound.** The 1.1% Wilson upper bound is a function of sample size, not of evidence we've missed something. If a cynical reader's first thought is "so 1 in 100 broken payloads might still sneak past," the math says otherwise — that's the loosest interpretation of the bound, not the central estimate (which is **0 silent passes observed**). For readers who want a tighter bound, the suite is parameterised: any consumer can run it themselves and pool with these results. Approximate scaling (assuming zero silent passes continue):

| Total calls | Wilson 95% upper bound on silent-pass rate |
|---|---|
| 342 (today, pooled across 2 runs) | **1.1%** |
| ~700 (4 runs) | ~0.55% |
| ~1,200 (7 runs) | ~0.31% |
| ~5,000 (~30 runs) | ~0.08% |

Continuous-regression tightening is a Cruxial Cloud roadmap item, not a V0.1 promise. The honest current claim is what's measured: zero silent passes in 342 calls, statistical upper bound 1.1%.

**Per-server intercepts (run #2)**

| Server | Calls | Intercepts | Repaired | Pattern |
|---|---|---|---|---|
| atlassian | 4 | 2 (50%) | 2 | newer / vendor-fresh |
| airtable | 5 | 2 (40%) | 2 | newer / vendor-fresh |
| pinecone | 3 | 1 (33%) | 0 | vector-shape mismatch |
| confluence | 4 | 1 (25%) | 1 | newer / vendor-fresh |
| salesforce | 4 | 1 (25%) | 1 | enterprise API |
| playwright | 4 | 1 (25%) | 1 | rapidly-evolving API |
| all 45 others | 147 | 0 | — | mature / well-documented APIs |

The pattern is consistent across both runs: mature APIs (github, kubernetes, slack, ms-teams, postgres, sqlite, redis, …) — gpt-4o nails them. Newer / vendor-fresh APIs — the catches live there. **The intercept rate is roughly a function of how recent your tool surface is relative to the model's training cutoff.**

**Reproduce**

```bash
pip install 'cruxial[mcp,openai]'
python examples/mine_mcp_schemas.py
rm ~/.cruxial/telemetry.sqlite
export AZURE_OPENAI_API_KEY=...
export AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
export AZURE_OPENAI_DEPLOYMENT=gpt-4o
python examples/azure_mcp_suite.py
```

---

## B. Synthetic robustness · 877 production schemas across 52 servers

The robustness benchmark. No LLM. No API costs. Pure classifier + testing-helper coverage across the entire mined corpus.

**What it tests.** For every schema in the corpus:

1. `cruxial.testing.valid_payload()` can produce a payload that satisfies the schema.
2. `cruxial.testing.violation_payloads()` can produce one payload per applicable failure category.
3. Every generated violation gets rejected by `cruxial.check()`.

**Result**

```
schemas covered            877
servers covered             52
domains covered             26

valid_payload success rate  99.2%  (870/877)
rejection rate             100.0%  (no silent passes)
exact-category accuracy     98.3%  (1913/1947 violations)
alternate-category rejects   1.7%  (34/1947 — stricter sibling rule preempted;
                                   still rejected, just under a different but
                                   legitimate category)
silently passed              0     ← the only number that matters
```

**Why the 7 valid_payload failures aren't a Cruxial bug.** They're all in schemas with vendor-specific oneOf branches where the generator picks a branch the schema's own examples don't satisfy (e.g. pinecone `create-index` requires either `dimension` OR `metric` but not both, depending on branch). The generator falls back to the simplest branch; the harder ones get tracked as known-limitation.

**Reproduce**

```bash
# The mined MCP schemas already ship inside the package, so no API keys and
# no mining step are needed — the audit reads cruxial.demo.MCP_SCHEMAS directly.
python examples/audit_mcp_schemas.py
```

---

## C. Model comparison · gpt-4o vs gpt-5-mini-2 on constraint-heavy schemas

The schemas your real production tools probably have: enums, regex patterns, format keywords, numeric ranges, nested objects.

**Configuration**

- Script: `examples/azure_demo_suite.py`
- Schemas: `cruxial.demo.DEMO_TOOL_SCHEMAS` — 15 hand-crafted production-class tools
- Constraint surface: 63 required fields · 26 enums · 18 regex patterns · 16 format keywords
- Prompts: `cruxial.demo.DEMO_PROMPTS` — 70 prompts, 4–5 per tool, mixed clean / ambiguous / adversarial
- Repair: `cruxial.adapters.openai.auto_repair_batch`

**Azure gpt-4o**

```
tool calls made             70
intercepted                 12  ( 17.1% of calls)  ±8.8% CI
  auto-repaired              8  ( 66.7%)
  unrepairable               4  (one prompt fanned to 4 parallel tool_calls,
                                 each with two invalid fields — fixed in V0.1
                                 by the multi-error surfacing in Failure.siblings)

failure categories caught
  format_violation             8
  constraint_violation         3
  enum_violation               1

silent passes                0
wall clock                  104s
```

**Azure gpt-5-mini-2 — same prompts, same schemas**

```
tool calls made             74
intercepted                  1  (  1.4%)  ±2.7% CI
  auto-repaired              1  (100%)
silent passes                0
wall clock                  436s
```

**The strategic finding**

> *gpt-5-mini-2 emits 92% fewer schema-violating tool calls than gpt-4o on the same constraint-heavy prompts. The mini tier of the new generation beats the flagship of the old.*

**Highest-value catches on gpt-4o (all silent-200-OK class — the tool would have succeeded with garbage data)**

| Tool | Bad arg | What would have happened |
|---|---|---|
| `demo_create_incident` | `mttr_target_minutes: 20160` (max 240) — model translated "fix needed in two weeks" literally | incident created with garbage SLA |
| `demo_run_sql_query` | `timeout_seconds: 3600` (max 600) — from "1 hour to run" | query killed mid-execution |
| `demo_deploy_application` | `version: "1.0"` (semver requires `1.0.0`) | deploy fails downstream with less-clear error |

**Reproduce** — use whichever provider you have. `demo_suite.py` auto-detects
OpenAI or Azure, so a plain `OPENAI_API_KEY` reproduces a number too (the rate
will differ by model — that variance is the finding, see §C below):

```bash
# Plain OpenAI (most common):
export OPENAI_API_KEY=sk-...
export CRUXIAL_OPENAI_MODEL=gpt-4o        # optional; default gpt-4o-mini
python examples/demo_suite.py
cruxial stats --since 30m

# Azure (the exact published run):
export AZURE_OPENAI_API_KEY=...  AZURE_OPENAI_ENDPOINT=...  AZURE_OPENAI_DEPLOYMENT=gpt-4o
python examples/demo_suite.py            # or examples/azure_demo_suite.py
```

---

## D. Pre-flight linter · 100% catch on live API rejections

`cruxial.lint` validates tool schemas at registration time against OpenAI and Anthropic vendor requirements — before the LLM ever sees them.

**Validation against live failures.** The live MCP run (section A) surfaced 9 calls that failed at OpenAI's tool-registration endpoint with HTTP 400. Empirically verified that `lint_schemas_for_openai()` flags every one of them pre-flight:

| MCP server | Live API rejections | `cruxial.lint` reproduces? |
|---|---|---|
| gitlab | 5 calls — all gitlab tools | ✅ **9/9 schemas** flagged `EMPTY_OR_META_ONLY_SCHEMA` — every gitlab schema is an empty stub `{"$schema": "..."}` with no `type` or `properties` |
| hubspot | 4 calls on `hubspot-search-objects` | ✅ **`hubspot-search-objects`** flagged `ARRAY_WITHOUT_ITEMS` (fatal) plus 22 advisory `OPENAI_STRICT_*` warnings across other hubspot tools |

**100% pre-flight catch rate on the live failures.** Every consumer of the gitlab MCP server with OpenAI would hit the same wall — `cruxial.lint` tells them at `register_schemas()` time, with the exact tool name and a fix hint.

**Coverage**

| Rule | Catches |
|---|---|
| `EMPTY_OR_META_ONLY_SCHEMA` | schemas with no structural content (no `type`, `properties`, `items`, `enum`, `$ref`, or composition keyword) |
| `ARRAY_WITHOUT_ITEMS` | `type: array` without `items` — both providers reject |
| `OBJECT_WITHOUT_PROPERTIES` | `type: object` without `properties` — LLM has no shape to fill |
| `INVALID_TYPE_VALUE` | `type` field is not a string or array of strings |
| `OPENAI_STRICT_MISSING_ADDITIONAL_PROPS_FALSE` | OpenAI strict mode requires `additionalProperties: false` at every object level |
| `OPENAI_STRICT_PROPS_NOT_IN_REQUIRED` | OpenAI strict mode requires every declared property to be in `required` |
| `ANTHROPIC_TOP_LEVEL_COMPOSITION` | Anthropic rejects `allOf`/`anyOf`/`oneOf` at the top level |
| `ANTHROPIC_ILLEGAL_PROPERTY_KEY` | Anthropic restricts property-name charset |
| `MANY_PROPERTIES_TRUNCATION_RISK` | warning for schemas with > 100 properties (provider truncation risk) |

Every rule cites the real GitHub issue, CVE, or live benchmark that motivated it.

**Reproduce**

```python
from cruxial.demo.mcp_schemas import MCP_SCHEMAS
from cruxial.lint import lint_schemas_for_openai

for sid in ('gitlab', 'hubspot'):
    issues = lint_schemas_for_openai(MCP_SCHEMAS[sid]['schemas'])
    errors = [i for i in issues if i.severity == 'error']
    print(f'{sid}: {len(errors)} errors across {len(MCP_SCHEMAS[sid]["schemas"])} schemas')
```

---

## E. Control · simple-schema MCP servers

The "why does this product exist" baseline.

**Configuration**

- Script: `examples/azure_mcp_suite.py` with `CRUXIAL_MCP_SERVERS=filesystem,memory,time,fetch,sqlite,brave-search,everything`
- 7 servers · 47 tools · 25 prompts
- Model: Azure gpt-4o

**Result**

```
tool calls made    25
intercepted         0  (0.0%)
silent passes       0
wall clock         18s
```

Simple MCP-style schemas (`{path: string}`, `{query: string}`) are trivially satisfiable. **gpt-4o nails them at 0% intercept.**

Put together with section A's 4.7–7.0% on the full real MCP corpus and section C's 17.1% on constraint-heavy schemas, the story crystallises:

> *Cruxial's intercept rate is approximately a function of your tool schemas' constraint surface. Simple shapes: ~0%. Real production MCP servers: 5–7%. Constraint-rich production schemas: 15–20%. As your tool surface gets harder, the value gets larger.*

---

## Reproduce everything

Every benchmark above runs from `pip install cruxial` + Python ≥ 3.10.

```bash
# clone
git clone https://github.com/cruxial-ai/cruxial.git && cd cruxial

# install
pip install -e '.[mcp,openai]'

# Azure env (or set OPENAI_API_KEY for direct OpenAI)
export AZURE_OPENAI_API_KEY=...
export AZURE_OPENAI_ENDPOINT=https://<your-resource>.openai.azure.com/
export AZURE_OPENAI_DEPLOYMENT=gpt-4o

# mine the MCP schema corpus (one-time, ~2 min)
python examples/mine_mcp_schemas.py

# A. Live MCP suite — section A
python examples/azure_mcp_suite.py

# B. Synthetic robustness — section B (no LLM, no cost)
python examples/audit_mcp_schemas.py

# C. Model comparison — section C
python examples/azure_demo_suite.py

# D. Lint validation — section D
python -c "
from cruxial.demo.mcp_schemas import MCP_SCHEMAS
from cruxial.lint import lint_schemas_for_openai
for sid in ('gitlab', 'hubspot'):
    issues = lint_schemas_for_openai(MCP_SCHEMAS[sid]['schemas'])
    print(sid, len([i for i in issues if i.severity == 'error']), 'errors')
"

# E. Simple-schema control — section E
CRUXIAL_MCP_SERVERS=filesystem,memory,time,fetch,sqlite,brave-search,everything \
  python examples/azure_mcp_suite.py
```

If any number you reproduce diverges materially from this document, file an issue — that's a benchmark regression we need to know about.

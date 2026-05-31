# Test suite

Run everything from the repo root:

```bash
pip install -e ".[dev]"
pytest -q
```

Every test maps to either a piece of the public contract or a specific
real-world failure mode. The suite is split into two halves: **behavior**
tests (does the documented API do what the docs say?) and **robustness**
tests (does it stay safe and fail-open when the input is hostile, malformed,
or weird?). The robustness half exists because Cruxial is a *reliability*
layer — if it crashes the host app, it's worse than not being there.
[`../DEFENSIVE.md`](../DEFENSIVE.md) maps each guarantee to the file that
proves it.

## Behavior — the public contract

| File | Covers |
|---|---|
| `test_core.py` | The `guard()` primitive: `execute()` / `check()` / `knows()`, executor errors, fail-open at runtime and construction (`NoopCruxial`), `register_schemas()`. |
| `test_classifier.py` | Mapping a raw jsonschema error to the right one of the 7 failure categories — the single biggest lever on repair-prompt quality. |
| `test_multi_violation.py` | Surfacing up to 5 violations per call (so the model fixes everything in one repair round-trip) and the `Failure.siblings` shape. |
| `test_adapters.py` | OpenAI / Anthropic schema extraction and the `auto_repair` / `auto_repair_batch` helpers. |
| `test_mcp_adapter.py` | The optional MCP adapter: importing/guarding stdio + SSE servers, and the subprocess-command safety checks. |
| `test_telemetry.py` | Sinks (sqlite / stdout / null / multi), arg + schema hashing, never-store-raw-args. |
| `test_observability.py` | The telemetry *row* contract: what gets recorded for passed / intercepted / corrected / executor_error, and that it's hashes-only. |
| `test_db_path_resolution.py` | The `CRUXIAL_DB_PATH` → project-local → home-fallback resolution order used by both the SDK and the `cruxial` CLI. |
| `test_cli_stats.py` | `cruxial stats` output states and the offline `cruxial demo` command. |
| `test_schema_origin.py` | The `schema_origin="canonical"` honesty flag (warns + tags rows; never changes validation). |
| `test_demo.py` | Integrity of the shipped `cruxial.demo` data (schemas valid, prompts well-formed). |
| `test_testing_helper.py` | `valid_payload()` / `violation_payloads()` generate one valid + one violating payload per category for any schema. |

## Robustness — research-backed failure modes

Each `test_robustness_*.py` file targets a class of real incident (many cite
the GitHub issue / CVE / post-mortem they came from). The invariant under test
is almost always the same: **Cruxial either classifies it correctly or fails
open — it never raises into the host app.**

| File | The hostile thing it throws at the validator |
|---|---|
| `test_robustness_malformed_input.py` | Junk args: wrong top-level types, nulls, deeply nested junk, unusual tool names. |
| `test_robustness_schema_quirks.py` | Legal-but-weird schemas: empty schemas, `oneOf`/`anyOf`, recursive refs, missing `type`. |
| `test_robustness_provider_specs.py` | OpenAI / Anthropic / Azure tool-spec quirks and strict-mode differences. |
| `test_robustness_concurrency.py` | Many threads sharing one guard + sink; fork-safety of the sqlite sink. |
| `test_robustness_resource_limits.py` | Huge payloads, many fields, adversarial regex (ReDoS) bounded so it can't hang. |
| `test_robustness_adversarial.py` | Inputs crafted to break the classifier or smuggle fields past validation. |
| `test_robustness_failure_modes.py` | End-to-end fail-open: what happens when the validator, sink, or executor itself blows up. |

## Conventions

- **No network, no API keys.** The whole suite runs offline; anything touching
  a real model lives in `../examples/`, not here.
- Tests use a `NullSink` or a `tmp_path` sqlite file — they never write to your
  real `~/.cruxial/` telemetry.
- Shared fixtures (`schemas`, `executors`) live in `conftest.py`.

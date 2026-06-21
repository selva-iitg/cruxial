# Changelog

All notable changes to Cruxial are documented here. Format: [Keep a Changelog](https://keepachangelog.com); versioning: [SemVer](https://semver.org).

## [Unreleased]

## [0.5.2] — 2026-06-21

### Security
- **`cruxial view --web` now defends against DNS rebinding.** The dashboard binds 127.0.0.1, but a malicious web page whose domain resolves to 127.0.0.1 could previously read the ledger over the unauthenticated local API (browser CORS does not stop rebinding). Requests are now rejected (403) unless the `Host` header is a localhost name. A missing `Host` (curl / HTTP 1.0) is still allowed.
- **Security headers on every dashboard response** as defense-in-depth for rendering agent-controlled data: a strict `Content-Security-Policy` (`default-src 'none'`, `connect-src 'self'` so any XSS can't exfiltrate the ledger to another origin), plus `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, and `Referrer-Policy: no-referrer`. The server version banner is also suppressed.

## [0.5.1] — 2026-06-21

### Fixed
- **`cruxial view --web` no longer crashes when the port is taken.** Binding now steps to the next free port (up to 20) instead of raising `Address already in use`, so a second dashboard just opens on the next port. The actual bound port is printed (`note: port N was busy, using M`), and an exhausted range prints a clear message instead of a traceback. (The dashboard already prints its URL and auto-opens the browser; that only looks silent when launched in the background.)

## [0.5.0] — 2026-06-20

The **action layer** — Cruxial moves from "is the tool call well-formed?" to "**did the action actually happen?**" Mark a side-effecting tool with `@action`, give it a receipt adapter, and every call resolves to a receipt-derived state (`posted` / `failed` / `unknown` / `needs_review`) in an append-only **ledger**. Only a real receipt advances state, so the model's narrated "done" can never promote an unconfirmed action to done. Additive and fail-open — 0.4 behaviour is unchanged until a tool is instrumented.

### Added
- **`@cruxial.action` / `@cruxial.verify` / `cruxial.receipt`** — mark a tool side-effecting (a receipt is required to confirm it ran), register a per-tool verify hook returning `PASS` / `FLAG` / `HALT`, and register a per-tool receipt adapter. Reusable adapter factories: `id_field("message_id")` ("no id → not done" — the pattern teams hand-roll) and `http_receipt`.
- **The action ledger** — every resolved operation (intent → policy → call → receipt → state) is appended to a local `operations` table; privacy-safe (args hashed, never stored raw). Built-in structural verifiers: `receipt_required` (HALT — with a guided hint naming the adapter to add) and `zero_latency` (advisory FLAG). New `Receipt` / `Operation` / `OpState` / `Verdict` types; `ExecutionResult` now carries `receipt` / `state` / `op_id` / `operation`.
- **`cruxial view`** — a local dashboard over the ledger: confirmed vs **silent-failure** (`unknown`) counts, recent operations, and a single-operation `intent → receipt` trace card. **`cruxial view --web`** opens a live, **zero-dependency** local web dashboard (stdlib http.server, 127.0.0.1 only). `cruxial demo` now shows the action layer (receipt → posted vs no-receipt → unknown).
- **`RunResult.state(tool)` / `.render()` / `.operations` / `.halted`** — `render()` is receipt-derived and never prints a bare "done"; an unconfirmed action reads as `unknown`. The model's next-turn tool result now carries the receipt status, so it works with the proof, not its own assumption.
- **`cx.check_bypass(text, ...)`** — detect a claimed-but-never-called action **and** record it as `unknown` in one call, for when you own the loop (streaming, a framework's tool callback, a worker). The safe equivalent of what `run()` does internally; the bare `detect_bypass()` detects but doesn't record.
- Async parity throughout (`aexecute` / `arun` / async verify hooks).

### Changed
- **BREAKING — bypass detection is deterministic.** A claimed-but-never-called action is caught **deterministically** and recorded as `unknown` (**no extra model call**), because a confident model just re-affirms a false claim. `run(bypass=...)` is `"on" | "off"` only; what to do about a flagged claim (retry / escalate / human-review) is the caller's policy. `RunResult.bypass` means *detected*, not *re-prompt-confirmed*.

### Removed
- **The re-prompt bypass remediation (`bypass="recover"` / `"strict"`).** Cruxial no longer re-prompts or LLM-judges a flagged claim — the receipt's absence is the oracle, not a re-affirmation. Removed `examples/bypass_live_eval.py` (it benchmarked that path); the offline detector eval `examples/bypass_eval.py` remains.

### Fixed
- **`extra_field` on open schemas no longer crashes the executor.** JSON Schema is open by default, so a hallucinated field could pass validation and then raise an uncaught `TypeError` at the `**args` call. The field is now checked against the executor's signature and blocked cleanly as `extra_field` before the call (auto-repairable, same as a closed-schema catch). Executors declaring `**kwargs` opt into extras and are never blocked; no schema or config change required.
- **Bypass false positive on sibling tools** — a claim already satisfied by a tool that actually ran (e.g. `send_email` did the send) no longer flags an uncalled sibling (`send_sms`).
- **`cruxial view --web` served stale data** after the ledger file was replaced — the viewer now opens a fresh reader per request instead of holding one connection.

## [0.4.0] — 2026-06-07

Two additive, backward-compatible features — a **Pydantic adapter** (define tools as Pydantic v2 models) and **async support** (`aexecute` / `arun`). No API breaks; Pydantic is never required by the core (optional, lazily-imported extra).

### Added
- **Async support** — `guard().aexecute()` / `aexecute_repaired()` and the top-level `cruxial.arun()` for async tool executors and async clients (`AsyncOpenAI`, async Anthropic, async LiteLLM). `aexecute()` awaits a coroutine-returning executor (and still accepts plain sync ones); `arun()` mirrors `run()` with every model call and tool execution awaited. **Sync `execute()` now raises a clear error on an async executor** instead of silently returning an un-awaited coroutine (the tool never running) — use `aexecute()`/`arun()` there.
- **`cruxial.adapters.pydantic`** (optional `cruxial[pydantic]`, Pydantic v2) — define tools as `BaseModel`s and let cruxial pull their JSON Schema:
  - `extract_schemas(models)` — model(s) → `{tool_name: schema}`. Accepts a single model, an iterable, or a `{name: Model}` mapping (e.g. snake_case names to match your executor keys).
  - `guard_models(...)` / `register_models(...)` — build a guard, or extend one, directly from models.
  - `tool_schema(model, provider="openai" | "anthropic")` — emit the provider tool-call definition from the **same** model, so the LLM tool schema and the runtime guard share one source of truth and can't drift.
  Nested models and enums (Pydantic's `$defs`/`$ref`) validate end to end, including nested constraints — the validator resolves in-schema refs with no network hop. The import is lazy: `import cruxial` never loads Pydantic.

## [0.3.0] — 2026-06-05

First release since 0.2.0 — the 0.2.1 work (bypass-precision robustness, `executor_error`) never shipped to PyPI and is rolled up here, alongside a validator security/correctness hardening pass that closes 13 findings from an external review. No API breaks; validation is stricter (rejects values that were wrongly accepted) and several denial-of-service and SSRF vectors are closed.

### Security
- **No network during validation.** The validator is pinned to an empty `referencing.Registry()`, so an external schema `$ref` (`http://…`, `file://…`) raises `Unresolvable` instead of being fetched via `urlopen()`. Closes an SSRF / local-file-read / hang vector. In-schema refs (`#/$defs/…`) still resolve. `guard()` now warns at construction when a schema carries an external `$ref` (its validation will fail open).
- **No validation hangs.** `uniqueItems` is now an O(n) set-based check (stock jsonschema is O(n²), which hangs on large arrays). `pattern` is matched with `google-re2` when the optional `cruxial[re2]` extra is installed; otherwise a static guard detects catastrophic-backtracking patterns and skips them rather than hang (with a loud `guard()` warning). Both restore the `<1ms` / fail-open promises against adversarial schemas.

### Added
- **`GuardConfig.strict_properties`** (default `False`) — inject `additionalProperties:false` into every object subschema that enumerates `properties` and hasn't declared openness, so a hallucinated extra field is caught even when the tool schema didn't close itself. Off by default so an intentionally-open schema is never silently over-constrained.
- **`GuardConfig.uri_schemes`** (default `None`) — an opt-in allowlist for `format:uri` fields. By default, the pseudo-schemes that are never a legitimate tool argument (`javascript:`, `data:`, `vbscript:`) are denied; `file:` is allowed by default (legitimate for file-handling tools) but can be excluded with an allowlist. With an allowlist set, any scheme outside it is a `format_violation`.
- **`cruxial[re2]`** optional extra — linear-time `pattern` validation.

### Fixed
- **Impossible dates/times accepted** — `format:date`/`time`/`date-time` now validate real calendar/clock values, not just digit shape: `2026-02-30`, `25:61:61` are rejected; leap day `2024-02-29` and leap second `23:59:60` stay valid.
- **`NaN`/`Infinity` accepted as `number`** — the `number`/`integer` type checks now require a finite value (they previously serialised to invalid JSON downstream).
- **`multipleOf` false-positive** — `0.3` against `multipleOf: 0.1` is no longer wrongly rejected; the check uses exact `Decimal` arithmetic instead of IEEE-754 float modulo (removes a needless repair loop).
- **Lenient `format:email`** — a structural, ReDoS-safe checker rejects `a@`, `@b.com`, `a b@c.com`, `a@b@c.com`.
- **Invalid schema silently disabled validation** — `guard()` now runs `Draft202012Validator.check_schema()` at construction: it raises under `strict=True` (default) and warns under `strict=False`, instead of failing open silently at runtime with no signal.
- **`pattern` category** — documented as `format_violation` (matching the classifier); the README category table previously listed it under `constraint_violation`.
- **`hash_args` could raise on mixed-type keys** (`{5: …, "a": …}`) — the telemetry fallback is now total.
- **`tool_bypass` false positives on third-party subjects** — claims attributed to a non-assistant *subject* no longer fire: systems/automation ("the system emailed", "the cron job created") **and** people/roles/pronouns ("my colleague created the ticket", "the PM approved", "they deleted it"), including multi-word and hyphenated titles ("the on-call engineer posted", "our off-site contractor sent") where tokenization splits the compound and displaces the determiner — while still firing on object-fronted ("Server updated"), subjectless ("Ticket created."), and assistant-subject ("the config I updated") phrasings.
- **`tool_bypass` false positives on negation and compound predicates** — "I haven't sent anything yet." and "Your manager approved and charged the card." no longer fire. Added contracted-negation markers ("haven't" / "didn't" / "hasn't" / …) and a governing-subject look-back that follows a subject across coordinated verbs ("X approved **and** charged").
- **A not-ok `ExecutionResult` always carries a `failure`** — previously an executor exception set `result.error` but left `result.failure` `None`, so the natural `if not result.ok: result.failure.category` raised `AttributeError`. Now a not-ok result always has a `failure`: validation failures as before, and executor exceptions as a synthesized `executor_error` Failure (a new failure category) while the raw exception stays on `result.error`. Also adds the null-safe `result.failure_category` accessor; `raise_on_failure()` still raises the original executor exception type.

### Improved
- **`tool_bypass` recall on real-world phrasings** — communication-action claims now match a single comms tool interchangeably (e.g. "notified the team" matches `send_email`), and the `push`/`ship` dev idioms are recognized both as completion claims and as tool-name verbs. Offline detector eval rose from 86.7% → 100% recall with precision held at 100%.

### Notes & known limitations
- `maxLength` counts Unicode code points, not bytes (spec-correct) — e.g. 10 emoji satisfy `maxLength:10` but are 40 UTF-8 bytes. Size DB columns / downstream limits accordingly.
- Bypass detection triggers on completion-**form** verbs. A claim with no such verb — a purely idiomatic completion like "email's out" — is not flagged. This is deliberate: precision (never acting on a non-bypass) is the load-bearing property, and the verb vocabulary grows from real misses rather than by special-casing idioms.

## [0.2.0] — 2026-06-02

### Added
- **`tool_bypass` detection** — catch an action the model claimed in prose but never called ("your agent said it sent the email. It didn't."). A local, attribution-aware filter flags suspect turns; one neutral re-prompt confirms, and we only act on a model-confirmed re-emit. Exposed via `run(bypass="on" | "strict" | "off")`, `RunResult.bypass`, and an 8th failure category. Benchmarked at 100% correction recall, ≥98% acted-on precision (BENCHMARKS §F).

## [0.1.3] — 2026-06-02

### Added
- **`cruxial.run()`** — one managed agent turn (call model → validate → execute → auto-repair → log → append) for OpenAI / Azure / Anthropic / LiteLLM. Single-turn, non-streaming; `guard().check()` / `.execute()` remain the manual path.

## [0.1.2] — 2026-06-01

### Changed
- Tighter `cruxial demo` output — middle-truncate over-long values, 5-line repair-prompt excerpt.

## [0.1.1] — 2026-06-01

### Added
- **`cruxial demo`** — offline, zero-key interception demo.
- `examples/demo_suite.py` — provider-agnostic benchmark runner (OpenAI or Azure).
- Onboarding docs: `.env.example`, `examples/README.md`, `tests/README.md`, `CONTRIBUTING.md`.

### Fixed
- README accuracy — `<1ms p99` overhead (was `~40ms`); repair-rate and test count reconciled to BENCHMARKS.

## [0.1.0] — 2026-05-30

### Added
- Initial release. `guard()` interceptor: JSON-Schema validation, 7 failure categories, 1-attempt auto-repair, fail-open by default. Adapters for OpenAI / Azure OpenAI / Anthropic / LiteLLM / MCP. Local SQLite + stdout telemetry, `cruxial stats` CLI, schema linter, synthetic-payload testing helpers.

[0.5.2]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.5.2
[0.5.1]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.5.1
[0.5.0]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.5.0
[0.4.0]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.4.0
[0.3.0]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.3.0
[0.2.0]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.2.0
[0.1.3]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.1.3
[0.1.2]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.1.2
[0.1.1]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.1.1
[0.1.0]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.1.0

# Changelog

All notable changes to Cruxial are documented here. Format: [Keep a Changelog](https://keepachangelog.com); versioning: [SemVer](https://semver.org).

## [0.3.0] — 2026-06-05

Validation-hardening release, closing 13 findings from an external security/correctness review of the validator. No API breaks; validation is stricter (rejects values that were wrongly accepted) and several denial-of-service and SSRF vectors are closed.

### Security
- **No network during validation.** The validator is pinned to an empty `referencing.Registry()`, so an external schema `$ref` (`http://…`, `file://…`) raises `Unresolvable` instead of being fetched via `urlopen()`. Closes an SSRF / local-file-read / hang vector. In-schema refs (`#/$defs/…`) still resolve. `guard()` now warns at construction when a schema carries an external `$ref` (its validation will fail open).
- **No validation hangs.** `uniqueItems` is now an O(n) set-based check (stock jsonschema is O(n²), which hangs on large arrays). `pattern` is matched with `google-re2` when the optional `cruxial[re2]` extra is installed; otherwise a static guard detects catastrophic-backtracking patterns and skips them rather than hang (with a loud `guard()` warning). Both restore the `<1ms` / fail-open promises against adversarial schemas.

### Fixed
- **Impossible dates/times accepted** — `format:date`/`time`/`date-time` now validate real calendar/clock values, not just digit shape: `2026-02-30`, `25:61:61` are rejected; leap day `2024-02-29` and leap second `23:59:60` stay valid.
- **`NaN`/`Infinity` accepted as `number`** — the `number`/`integer` type checks now require a finite value (they previously serialised to invalid JSON downstream).
- **`multipleOf` false-positive** — `0.3` against `multipleOf: 0.1` is no longer wrongly rejected; the check uses exact `Decimal` arithmetic instead of IEEE-754 float modulo (removes a needless repair loop).
- **Lenient `format:email`** — a structural, ReDoS-safe checker rejects `a@`, `@b.com`, `a b@c.com`, `a@b@c.com`.
- **Invalid schema silently disabled validation** — `guard()` now runs `Draft202012Validator.check_schema()` at construction: it raises under `strict=True` (default) and warns under `strict=False`, instead of failing open silently at runtime with no signal.
- **`pattern` category** — documented as `format_violation` (matching the classifier); the README category table previously listed it under `constraint_violation`.
- **`hash_args` could raise on mixed-type keys** (`{5: …, "a": …}`) — the telemetry fallback is now total.

### Added
- **`GuardConfig.strict_properties`** (default `False`) — inject `additionalProperties:false` into every object subschema that enumerates `properties` and hasn't declared openness, so a hallucinated extra field is caught even when the tool schema didn't close itself. Off by default so an intentionally-open schema is never silently over-constrained.
- **`GuardConfig.uri_schemes`** (default `None`) — an opt-in allowlist for `format:uri` fields. By default, the pseudo-schemes that are never a legitimate tool argument (`javascript:`, `data:`, `vbscript:`) are denied; `file:` is allowed by default (legitimate for file-handling tools) but can be excluded with an allowlist. With an allowlist set, any scheme outside it is a `format_violation`.
- **`cruxial[re2]`** optional extra — linear-time `pattern` validation.

### Note
- `maxLength` counts Unicode code points, not bytes (spec-correct) — e.g. 10 emoji satisfy `maxLength:10` but are 40 UTF-8 bytes. Size DB columns / downstream limits accordingly.

## [0.2.1] — 2026-06-05

### Fixed
- **`tool_bypass` false positives on third-party subjects** — claims attributed to a non-assistant *subject* no longer fire: systems/automation ("the system emailed", "the cron job created") **and** people/roles/pronouns ("my colleague created the ticket", "the PM approved", "they deleted it"). The guard previously only covered passive/agent forms ("by the system"); it now suppresses an explicit non-assistant subject before the verb — including multi-word and hyphenated titles ("the on-call engineer posted", "our off-site contractor sent"), where tokenization splits the compound and displaces the determiner — while still firing on object-fronted ("Server updated"), subjectless ("Ticket created."), and assistant-subject ("the config I updated") phrasings. This closed live false-actions found on the adversarial set (precision back to 0 false actions).
- **`tool_bypass` false positives on negation and compound predicates** — "I haven't sent anything yet." and "Your manager approved and charged the card." no longer fire. Added contracted-negation markers ("haven't" / "didn't" / "hasn't" / …) and a governing-subject look-back that follows a subject across coordinated verbs ("X approved **and** charged"), so a non-assistant subject is still detected past the immediate 3-token window.
- **A not-ok `ExecutionResult` always carries a `failure`** — previously an executor exception set `result.error` but left `result.failure` `None`, so the natural `if not result.ok: result.failure.category` raised `AttributeError`. Now a not-ok result always has a `failure`: validation failures as before, and executor exceptions as a synthesized `executor_error` Failure (a new failure category) while the raw exception stays on `result.error`. Also adds the null-safe `result.failure_category` accessor; `raise_on_failure()` still raises the original executor exception type. The README example uses `failure_category` / `raise_on_failure()`.

### Improved
- **`tool_bypass` recall on real-world phrasings** — communication-action claims now match a single comms tool interchangeably (e.g. "notified the team" matches `send_email`), and the `push`/`ship` dev idioms are recognized both as completion claims and as tool-name verbs (so "pushed that update" matches a `push_update`/`deploy_*` tool). Offline detector eval rose from 86.7% → 100% recall with precision held at 100%.

### Known limitation
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

[0.3.0]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.3.0
[0.2.1]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.2.1
[0.2.0]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.2.0
[0.1.3]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.1.3
[0.1.2]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.1.2
[0.1.1]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.1.1
[0.1.0]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.1.0

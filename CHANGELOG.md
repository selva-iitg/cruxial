# Changelog

All notable changes to Cruxial are documented here. Format: [Keep a Changelog](https://keepachangelog.com); versioning: [SemVer](https://semver.org).

## [0.2.1] — 2026-06-05

### Fixed
- **`tool_bypass` false positive on third-party subjects** — "The system emailed the invoice" no longer fires. The attribution guard previously only covered passive/agent forms ("by the system", "by the scheduler"); it now also suppresses a non-assistant *subject* directly before the verb ("the system emailed", "the cron job created"), while still firing on object-fronted ("Server updated") and assistant-subject ("the system config I updated") phrasings.

### Improved
- **`tool_bypass` recall on real-world phrasings** — communication-action claims now match a single comms tool interchangeably (e.g. "notified the team" matches `send_email`), and the `push`/`ship` dev idioms are recognized both as completion claims and as tool-name verbs (so "pushed that update" matches a `push_update`/`deploy_*` tool). Offline detector eval rose from 86.7% → 100% recall with precision held at 100%.

### Known limitation
- Bypass detection triggers on completion-**form** verbs. A claim with no such verb — a purely idiomatic completion like "email's out" — is not flagged. This is deliberate: precision (never acting on a non-bypass) is the load-bearing property, and the verb vocabulary grows from real misses rather than by special-casing idioms.

_Both surfaced by an external proof-of-concept run on Azure gpt-4o — thanks to the design partner who found and root-caused them. (Add their name/handle here.)_

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

[0.2.1]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.2.1
[0.2.0]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.2.0
[0.1.3]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.1.3
[0.1.2]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.1.2
[0.1.1]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.1.1
[0.1.0]: https://github.com/cruxial-ai/cruxial/releases/tag/v0.1.0

# Contributing to Cruxial

Thanks for helping make LLM tool calls more reliable. Cruxial is small,
dependency-light, and intentionally so — please keep changes minimal and in
the style of the surrounding code.

## Dev setup

```bash
git clone https://github.com/cruxial-ai/cruxial.git
cd cruxial
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"      # installs pytest + openai + anthropic + mcp extras
pytest -q                     # should be all green
cruxial demo                  # offline sanity check
```

The SDK needs **no API keys**. Keys are only for the live-model scripts in
[`examples/`](examples/README.md); see [`.env.example`](.env.example).

## Running tests

```bash
pytest -q                              # everything (offline, no keys)
pytest tests/test_core.py -q           # one file
pytest -k classifier -q                # by keyword
```

See [`tests/README.md`](tests/README.md) for what each file covers. The suite
runs entirely offline and never writes to your real `~/.cruxial/` telemetry.

## The one hard rule: fail open

Cruxial sits in the hot path of someone's agent. **If Cruxial's own code
raises, the host's tool must still execute** (when `fail_open=True`, the
default). Any new validation, telemetry, or adapter code must preserve this —
wrap risky work and degrade to "pass through" rather than propagate. There are
dedicated tests for this in `tests/test_robustness_failure_modes.py`; add to
them if you touch the execution path.

## Style

- Match the surrounding code's comment density and naming. Comments explain
  *why*, not *what*.
- No new runtime dependencies without discussion — `jsonschema` is the only
  hard one. Provider SDKs (`openai`, `anthropic`, `mcp`) are optional extras,
  imported lazily.
- Every telemetry row stores hashes only — never raw argument values.
- New behavior gets a test that maps to a real contract or failure mode.

## Project layout

```
cruxial/            the SDK (the thing that ships on PyPI)
  core.py           guard() — the public primitive
  validator.py      jsonschema wrapper + strict format checkers
  classifier.py     raw error → one of 7 failure categories
  repair.py         builds the repair prompt
  telemetry.py      sinks + hashing + db-path resolution
  cli.py            the `cruxial` command (stats / demo / diagnostic)
  adapters/         openai, anthropic, mcp (optional)
  testing.py        synthetic payload generators
  demo/             shipped demo + mined MCP schemas
tests/              offline test suite (see tests/README.md)
examples/           live-model scripts (see examples/README.md)
```

## Releasing

Version lives in `pyproject.toml` and `cruxial/__init__.py` — keep them in
sync. Don't bump the version for docs-only changes. Cut a release only when
the SDK's behavior changes.

## License

By contributing you agree your contributions are licensed under the MIT
License, same as the project.

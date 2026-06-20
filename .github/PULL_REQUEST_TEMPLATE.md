<!-- For anything non-trivial, open an issue first so we agree on the approach. -->

## What & why

<!-- What does this change, and what problem does it solve? Link the issue: Fixes #123 -->

## Type of change

- [ ] `fix:` — bug fix
- [ ] `feat:` — new behavior
- [ ] `docs:` — docs only
- [ ] `test:` — tests only
- [ ] other (refactor / chore / perf)

## Checklist

- [ ] `pytest -q` is green
- [ ] New behavior has a test that maps to a real contract or failure mode
- [ ] **Fail-open preserved** — if Cruxial's own code raises, the host's tool still runs (`fail_open=True`)
- [ ] No new runtime dependencies (provider SDKs stay optional, lazily imported)
- [ ] Telemetry stores hashes only — never raw argument values
- [ ] Commit messages follow [Conventional Commits](https://www.conventionalcommits.org) and stay concise
- [ ] Version bumped in `pyproject.toml` **and** `cruxial/__init__.py` only if behavior changed

## Notes for reviewers

<!-- Anything worth calling out: trade-offs, follow-ups, things you're unsure about. -->

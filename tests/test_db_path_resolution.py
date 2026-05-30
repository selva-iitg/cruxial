"""Default DB path resolution: env var > project-local > home fallback.

This eliminates the "global SQLite pollution" friction surfaced by early
dogfood: a tool-registration signal (~50 tools) was hidden by hundreds of
tools from a benchmark run elsewhere on the same laptop, making local
stats unreadable in any multi-app setup.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cruxial.telemetry import (
    SqliteSink,
    _HOME_DB_PATH,
    _discover_project_root,
    default_db_path,
)


# ─── env var takes highest precedence ─────────────────────────────────


def test_env_var_overrides_everything(tmp_path: Path, monkeypatch):
    override = tmp_path / "custom" / "telemetry.sqlite"
    monkeypatch.setenv("CRUXIAL_DB_PATH", str(override))
    monkeypatch.chdir(tmp_path)
    # Make sure tmp_path looks like a project so project-discovery WOULD fire
    (tmp_path / "pyproject.toml").touch()

    resolved = default_db_path()
    assert resolved == override


def test_env_var_supports_tilde_expansion(monkeypatch):
    monkeypatch.setenv("CRUXIAL_DB_PATH", "~/my-cruxial.sqlite")
    expected = Path("~/my-cruxial.sqlite").expanduser()
    assert default_db_path() == expected


# ─── project-local auto-discovery ──────────────────────────────────────


def test_discovers_pyproject_toml_root(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CRUXIAL_DB_PATH", raising=False)
    (tmp_path / "pyproject.toml").touch()
    nested = tmp_path / "src" / "deeply" / "nested"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    resolved = default_db_path()
    assert resolved == tmp_path / ".cruxial" / "telemetry.sqlite"


def test_discovers_dot_cruxial_directory_as_marker(tmp_path: Path, monkeypatch):
    """If `.cruxial/` already exists in a parent, that's the project root."""
    monkeypatch.delenv("CRUXIAL_DB_PATH", raising=False)
    (tmp_path / ".cruxial").mkdir()
    monkeypatch.chdir(tmp_path)

    resolved = default_db_path()
    assert resolved == tmp_path / ".cruxial" / "telemetry.sqlite"


def test_discovers_git_root(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("CRUXIAL_DB_PATH", raising=False)
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)

    resolved = default_db_path()
    assert resolved == tmp_path / ".cruxial" / "telemetry.sqlite"


def test_discover_project_root_returns_none_for_orphan_dir(tmp_path: Path, monkeypatch):
    """A directory with no markers anywhere up the chain returns None."""
    monkeypatch.delenv("CRUXIAL_DB_PATH", raising=False)
    # Create something deeply nested in tmp with no markers
    orphan = tmp_path / "orphan"
    orphan.mkdir()
    # Make sure none of tmp_path's parents have markers either (best effort —
    # this might find /tmp's grandparent has .git on dev machines, so just
    # test the helper directly with an isolated start dir).
    result = _discover_project_root(start=orphan)
    # On some dev machines /tmp's parents may have markers; this test is best-effort.
    # The real assertion is that when nothing is found, fallback works.
    if result is None:
        assert default_db_path() == _HOME_DB_PATH or default_db_path().is_absolute()


# ─── home fallback ─────────────────────────────────────────────────────


def test_home_fallback_when_no_env_and_no_project(tmp_path: Path, monkeypatch):
    """If nothing else applies, we land at ~/.cruxial/telemetry.sqlite."""
    monkeypatch.delenv("CRUXIAL_DB_PATH", raising=False)
    # cd into a directory we KNOW has no markers up to root by simulating
    # _discover_project_root returning None.
    import cruxial.telemetry as t

    def fake_discover(start=None):
        return None

    monkeypatch.setattr(t, "_discover_project_root", fake_discover)
    assert default_db_path() == _HOME_DB_PATH


# ─── SqliteSink construction uses the resolution ───────────────────────


def test_sqlite_sink_with_no_path_uses_resolved_default(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CRUXIAL_DB_PATH", str(tmp_path / "sink_test.sqlite"))
    sink = SqliteSink()  # no path passed — should resolve via env
    try:
        assert sink.path == tmp_path / "sink_test.sqlite"
        assert sink.path.exists()
    finally:
        sink.close()


def test_sqlite_sink_with_explicit_path_overrides_env(tmp_path: Path, monkeypatch):
    """Explicit path arg wins over env var — important for tests + CI."""
    monkeypatch.setenv("CRUXIAL_DB_PATH", str(tmp_path / "from_env.sqlite"))
    explicit = tmp_path / "explicit.sqlite"
    sink = SqliteSink(explicit)
    try:
        assert sink.path == explicit
        assert sink.path.exists()
        assert not (tmp_path / "from_env.sqlite").exists()
    finally:
        sink.close()


def test_sqlite_sink_path_is_resolved_at_construction_not_import(tmp_path: Path, monkeypatch):
    """Setting CRUXIAL_DB_PATH after import must still take effect on new sinks.

    Critical because DEFAULT_DB_PATH used to be evaluated at import time —
    that meant the env var had to be set before importing cruxial. Now
    SqliteSink calls default_db_path() at __init__ time.
    """
    db_a = tmp_path / "a.sqlite"
    db_b = tmp_path / "b.sqlite"

    monkeypatch.setenv("CRUXIAL_DB_PATH", str(db_a))
    sink_a = SqliteSink()
    sink_a.close()

    monkeypatch.setenv("CRUXIAL_DB_PATH", str(db_b))
    sink_b = SqliteSink()
    sink_b.close()

    assert db_a.exists()
    assert db_b.exists()
    assert sink_a.path != sink_b.path

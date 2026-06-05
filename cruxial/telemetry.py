"""Telemetry sinks: stdout JSON + local SQLite.

Privacy contract: we record schema fingerprint, tool name, failure category,
timing. **Never raw argument values.** The hash is one-way (SHA-256). The
user opts into raw-arg capture by passing `capture_args=True` to guard().
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from cruxial.types import InterceptionRecord

import os

# Where ``cruxial stats`` (and the SqliteSink) write to when no explicit
# path is given. The resolution order is intentional — it means consumers
# rarely need to think about it:
#
#   1. ``CRUXIAL_DB_PATH`` env var, if set (highest priority — explicit
#      override for CI, multi-project laptops, anywhere you want a known
#      path).
#   2. A project-local ``./.cruxial/telemetry.sqlite`` discovered by
#      walking up from the current directory looking for a project marker
#      (``.cruxial/`` itself, ``pyproject.toml``, ``setup.py``, ``.git/``).
#      Lands cruxial stats next to your project's other tooling output.
#   3. Fallback: ``~/.cruxial/telemetry.sqlite``.
#
# This means: as soon as you run cruxial inside any reasonable Python
# project, its stats stay scoped to that project. No more pollution
# across multiple apps on the same machine — which was a real friction
# point surfaced by early dogfood: a tool-registration signal (~50
# tools) was hidden by hundreds of tools from a benchmark run in
# another directory on the same laptop.

_HOME_DB_PATH = Path.home() / ".cruxial" / "telemetry.sqlite"
_PROJECT_MARKERS = (".cruxial", "pyproject.toml", "setup.py", ".git")


def _discover_project_root(start: Path | None = None) -> Path | None:
    """Walk up looking for any ``_PROJECT_MARKERS``. Returns project dir or None."""
    here = (start or Path.cwd()).resolve()
    for d in (here, *here.parents):
        for marker in _PROJECT_MARKERS:
            if (d / marker).exists():
                return d
    return None


def default_db_path() -> Path:
    """Resolve where the SqliteSink writes to / where ``cruxial stats`` reads from.

    See the resolution order documented above. Always returns a Path — caller
    is responsible for creating parent directories.
    """
    env = os.environ.get("CRUXIAL_DB_PATH")
    if env:
        return Path(env).expanduser()

    project_root = _discover_project_root()
    if project_root is not None:
        return project_root / ".cruxial" / "telemetry.sqlite"

    return _HOME_DB_PATH


# Backwards-compat constant — kept as a callable-evaluation default so any
# imports that read it at module-import time still get the same value as
# before for the home-fallback case. Prefer ``default_db_path()`` for new
# code so we get the env var + project-discovery resolution.
DEFAULT_DB_PATH = _HOME_DB_PATH


# ─── public helpers ──────────────────────────────────────────────────────


def hash_args(args: dict[str, Any]) -> str:
    """Stable SHA-256 of canonicalised args. No raw values escape."""
    try:
        canonical = json.dumps(args, sort_keys=True, default=str)
    except Exception:
        # Total fallback: sort by repr so mixed-type keys (e.g. {5: ..., "a": ...})
        # never raise TypeError on comparison.
        canonical = repr(sorted((repr(k), repr(v)) for k, v in args.items()))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def hash_schema(schema: dict[str, Any]) -> str:
    try:
        canonical = json.dumps(schema, sort_keys=True, default=str)
    except Exception:
        canonical = repr(schema)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ─── sinks ───────────────────────────────────────────────────────────────


class Sink(Protocol):
    def record(self, r: InterceptionRecord) -> None: ...
    def close(self) -> None: ...


class StdoutSink:
    """Writes one JSON line per interception to stderr (stdout is left clean)."""

    def __init__(self, stream=None):
        self.stream = stream or sys.stderr

    def record(self, r: InterceptionRecord) -> None:
        try:
            line = json.dumps(asdict(r), default=str)
            print(line, file=self.stream, flush=True)
        except Exception:
            # Fail-open: never let telemetry crash the host app.
            pass

    def register_tools(
        self,
        tools: dict[str, str],
        schema_origin: str = "model_visible",
    ) -> None:
        return  # No-op for stdout — only sqlite persists a registry.

    def close(self) -> None:
        pass


class SqliteSink:
    """Single-file local store. Thread-safe via a single lock."""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS interceptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        tool TEXT NOT NULL,
        status TEXT NOT NULL,
        failure_category TEXT,
        failure_path TEXT,
        args_hash TEXT NOT NULL,
        schema_hash TEXT NOT NULL,
        latency_ms REAL NOT NULL,
        repaired INTEGER NOT NULL,
        cruxial_version TEXT NOT NULL,
        extras TEXT,
        schema_origin TEXT DEFAULT 'model_visible'
    );
    CREATE INDEX IF NOT EXISTS idx_timestamp ON interceptions(timestamp);
    CREATE INDEX IF NOT EXISTS idx_tool ON interceptions(tool);
    CREATE INDEX IF NOT EXISTS idx_status ON interceptions(status);

    CREATE TABLE IF NOT EXISTS tools (
        name TEXT PRIMARY KEY,
        schema_hash TEXT NOT NULL,
        schema_origin TEXT NOT NULL DEFAULT 'model_visible',
        first_registered TEXT NOT NULL,
        last_registered TEXT NOT NULL
    );
    """

    def __init__(self, path: Path | str | None = None):
        # Resolve at construction time (not import time) so env var changes
        # and `cd`-based project discovery work as expected.
        resolved = Path(path) if path is not None else default_db_path()
        self.path = resolved
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.executescript(self.SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self) -> None:
        """Idempotent column-add migrations for older databases."""
        try:
            cur = self._conn.execute("PRAGMA table_info(interceptions)")
            cols = {row[1] for row in cur.fetchall()}
            if "schema_origin" not in cols:
                self._conn.execute(
                    "ALTER TABLE interceptions ADD COLUMN "
                    "schema_origin TEXT DEFAULT 'model_visible'"
                )
        except Exception:
            pass  # Fail-open: migration failure must not break boot.

    def record(self, r: InterceptionRecord) -> None:
        try:
            with self._lock:
                self._conn.execute(
                    """
                    INSERT INTO interceptions
                      (timestamp, tool, status, failure_category, failure_path,
                       args_hash, schema_hash, latency_ms, repaired,
                       cruxial_version, extras, schema_origin)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        r.timestamp,
                        r.tool,
                        r.status,
                        r.failure_category,
                        r.failure_path,
                        r.args_hash,
                        r.schema_hash,
                        r.latency_ms,
                        1 if r.repaired else 0,
                        r.cruxial_version,
                        json.dumps(r.extras, default=str) if r.extras else None,
                        r.schema_origin,
                    ),
                )
                self._conn.commit()
        except Exception:
            # Fail-open.
            pass

    def register_tools(
        self,
        tools: dict[str, str],
        schema_origin: str = "model_visible",
    ) -> None:
        """UPSERT the registered tools so `cruxial stats` can show what's wired up.

        Called by Cruxial.__init__ once per guard() construction. Idempotent.
        """
        if not tools:
            return
        ts = utc_now()
        try:
            with self._lock:
                for name, schema_hash in tools.items():
                    self._conn.execute(
                        """
                        INSERT INTO tools (name, schema_hash, schema_origin, first_registered, last_registered)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(name) DO UPDATE SET
                            schema_hash = excluded.schema_hash,
                            schema_origin = excluded.schema_origin,
                            last_registered = excluded.last_registered
                        """,
                        (name, schema_hash, schema_origin, ts, ts),
                    )
                self._conn.commit()
        except Exception:
            pass

    def query(self, sql: str, params: tuple = ()) -> list[tuple]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.fetchall()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass


class MultiSink:
    """Fan-out to several sinks. Errors in one don't affect others."""

    def __init__(self, sinks: Iterable[Sink]):
        self.sinks = list(sinks)

    def record(self, r: InterceptionRecord) -> None:
        for s in self.sinks:
            try:
                s.record(r)
            except Exception:
                pass

    def register_tools(
        self,
        tools: dict[str, str],
        schema_origin: str = "model_visible",
    ) -> None:
        for s in self.sinks:
            try:
                if hasattr(s, "register_tools"):
                    s.register_tools(tools, schema_origin=schema_origin)
            except Exception:
                pass

    def close(self) -> None:
        for s in self.sinks:
            try:
                s.close()
            except Exception:
                pass


class NullSink:
    """For tests."""

    def __init__(self):
        self.records: list[InterceptionRecord] = []
        self.registered: dict[str, str] = {}

    def record(self, r: InterceptionRecord) -> None:
        self.records.append(r)

    def register_tools(
        self,
        tools: dict[str, str],
        schema_origin: str = "model_visible",
    ) -> None:
        self.registered.update(tools)

    def close(self) -> None:
        pass


# ─── timing helper ───────────────────────────────────────────────────────


def perf_ms_since(start_ns: int) -> float:
    return (time.perf_counter_ns() - start_ns) / 1_000_000.0

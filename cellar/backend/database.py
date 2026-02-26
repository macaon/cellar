"""SQLite tracking of installed apps.

Database location
-----------------
``~/.local/share/cellar/cellar.db``
(or the Flatpak XDG equivalent, resolved via ``config.data_dir()``)

Schema
------
::

    CREATE TABLE IF NOT EXISTS installed (
        id               TEXT PRIMARY KEY,
        bottle_name      TEXT NOT NULL,
        installed_version TEXT,
        installed_at     TIMESTAMP,
        last_updated     TIMESTAMP,
        repo_source      TEXT
    );
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cellar.backend.config import data_dir


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    return data_dir() / "cellar.db"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS installed (
            id                TEXT PRIMARY KEY,
            bottle_name       TEXT NOT NULL,
            installed_version TEXT,
            installed_at      TIMESTAMP,
            last_updated      TIMESTAMP,
            repo_source       TEXT
        );
    """)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def mark_installed(
    app_id: str,
    bottle_name: str,
    version: str,
    repo_source: str = "",
) -> None:
    """Record (or update) an installed app.

    Uses an upsert so calling this on an already-installed app updates the
    ``bottle_name``, ``installed_version``, ``last_updated``, and
    ``repo_source`` without changing ``installed_at``.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """
            INSERT INTO installed
                (id, bottle_name, installed_version, installed_at, last_updated, repo_source)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                bottle_name       = excluded.bottle_name,
                installed_version = excluded.installed_version,
                last_updated      = excluded.last_updated,
                repo_source       = excluded.repo_source
            """,
            (app_id, bottle_name, version, now, now, repo_source),
        )


def get_installed(app_id: str) -> dict | None:
    """Return the installed record for *app_id*, or ``None`` if not installed."""
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT * FROM installed WHERE id = ?", (app_id,)
        ).fetchone()
        return dict(row) if row else None


def is_installed(app_id: str) -> bool:
    """Return ``True`` if *app_id* has an installed record."""
    return get_installed(app_id) is not None


def remove_installed(app_id: str) -> None:
    """Delete the installed record for *app_id* (no-op if not present)."""
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute("DELETE FROM installed WHERE id = ?", (app_id,))


def get_all_installed() -> list[dict]:
    """Return all installed records ordered by ``installed_at``."""
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            "SELECT * FROM installed ORDER BY installed_at"
        ).fetchall()
        return [dict(row) for row in rows]

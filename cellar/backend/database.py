"""SQLite tracking of installed apps and base images.

Database location
-----------------
``~/.local/share/cellar/cellar.db``
(or the Flatpak XDG equivalent, resolved via ``config.data_dir()``)

Schema versioning
-----------------
A ``schema_version`` table carries the current schema version integer.  On
every DB open :func:`_open_db` reads the version and runs any pending
migrations in order, then stamps the new version.  A fresh install creates
the current schema directly (no migration required).

Schema v1 (current)
-------------------
::

    CREATE TABLE schema_version (
        version INTEGER PRIMARY KEY
    );

    CREATE TABLE installed (
        id              TEXT PRIMARY KEY,
        prefix_dir      TEXT NOT NULL,
        platform        TEXT NOT NULL DEFAULT 'windows',
        version         TEXT,
        runner          TEXT,
        runner_override TEXT,
        steam_appid     INTEGER,
        install_path    TEXT,
        repo_source     TEXT,
        installed_at    TEXT,
        last_updated    TEXT
    );

    CREATE TABLE bases (
        runner       TEXT PRIMARY KEY,
        repo_source  TEXT,
        installed_at TEXT
    );
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cellar.backend.config import data_dir

log = logging.getLogger(__name__)

_CURRENT_VERSION = 1


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _db_path() -> Path:
    return data_dir() / "cellar.db"


def _open_db() -> sqlite3.Connection:
    """Open the database, apply any pending migrations, and return the connection."""
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Detect the current schema version and run pending migrations."""
    # Check if schema_version table exists.
    has_version_table = bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
    ).fetchone())

    if not has_version_table:
        # Either a brand-new install or an old pre-versioned schema.
        has_installed = bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='installed'"
        ).fetchone())

        if has_installed:
            # Pre-v1 schema: check for the old `bottle_name` column.
            cols = {row[1] for row in conn.execute("PRAGMA table_info(installed)")}
            if "bottle_name" in cols:
                _migrate_v0_to_v1(conn)
                return
            # installed table exists but already has prefix_dir — stamp it.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_version (version) VALUES (?)",
                (_CURRENT_VERSION,),
            )
            conn.commit()
            return

        # No tables at all — fresh install; create schema v1 directly.
        _create_schema_v1(conn)
        return

    # schema_version table exists — read current version.
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = row[0] if row and row[0] is not None else 0

    # Run any missing migrations in order.
    if current < 1:
        _migrate_v0_to_v1(conn)


def _create_schema_v1(conn: sqlite3.Connection) -> None:
    """Create the full v1 schema from scratch (fresh install)."""
    conn.executescript("""
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY
        );

        CREATE TABLE installed (
            id              TEXT PRIMARY KEY,
            prefix_dir      TEXT NOT NULL,
            platform        TEXT NOT NULL DEFAULT 'windows',
            version         TEXT,
            runner          TEXT,
            runner_override TEXT,
            steam_appid     INTEGER,
            install_path    TEXT,
            repo_source     TEXT,
            installed_at    TEXT,
            last_updated    TEXT
        );

        CREATE TABLE bases (
            runner       TEXT PRIMARY KEY,
            repo_source  TEXT,
            installed_at TEXT
        );
    """)
    conn.execute("INSERT INTO schema_version (version) VALUES (?)", (_CURRENT_VERSION,))
    conn.commit()


def _migrate_v0_to_v1(conn: sqlite3.Connection) -> None:
    """Migrate the old pre-versioned schema to v1.

    Detection: ``schema_version`` table absent AND ``installed`` has
    ``bottle_name`` column.

    Runs inside a single transaction; rolls back on any failure so the
    database is left in the original state rather than half-migrated.
    """
    log.info("Migrating cellar.db from v0 to v1")
    try:
        with conn:
            # 1. Migrate installed table.
            conn.execute("""
                CREATE TABLE installed_v1 (
                    id              TEXT PRIMARY KEY,
                    prefix_dir      TEXT NOT NULL,
                    platform        TEXT NOT NULL DEFAULT 'windows',
                    version         TEXT,
                    runner          TEXT,
                    runner_override TEXT,
                    steam_appid     INTEGER,
                    install_path    TEXT,
                    repo_source     TEXT,
                    installed_at    TEXT,
                    last_updated    TEXT
                )
            """)
            conn.execute("""
                INSERT INTO installed_v1
                    (id, prefix_dir, platform, version, runner_override,
                     install_path, repo_source, installed_at, last_updated)
                SELECT
                    id,
                    bottle_name,
                    COALESCE(platform, 'windows'),
                    installed_version,
                    runner_override,
                    install_path,
                    repo_source,
                    installed_at,
                    last_updated
                FROM installed
            """)
            conn.execute("DROP TABLE installed")
            conn.execute("ALTER TABLE installed_v1 RENAME TO installed")

            # 2. bases table: ensure it uses TEXT for installed_at.
            #    The column was already 'runner' in recent versions.
            #    No data migration needed; structure is compatible.

            # 3. Stamp version.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (_CURRENT_VERSION,),
            )
    except Exception:
        log.exception("v0→v1 migration failed; database left unchanged")
        raise


# ---------------------------------------------------------------------------
# Public API — installed apps
# ---------------------------------------------------------------------------

def mark_installed(
    app_id: str,
    prefix_dir: str,
    version: str,
    repo_source: str = "",
    platform: str = "windows",
    install_path: str = "",
    runner: str = "",
    steam_appid: int | None = None,
) -> None:
    """Record (or update) an installed app.

    Uses an upsert so calling this on an already-installed app updates the
    record without changing ``installed_at``.

    Parameters
    ----------
    app_id:
        The catalogue ``AppEntry.id``.
    prefix_dir:
        Sub-directory name under the install base.  For umu Windows apps this
        equals *app_id*; for Linux native apps it is the directory created by
        the installer (potentially collision-suffixed).
    version:
        Installed catalogue version string.
    repo_source:
        URI of the source repo.
    platform:
        ``"windows"`` or ``"linux"``.
    install_path:
        Base directory containing *prefix_dir*.  Empty for umu Windows apps
        (implicit: ``umu.prefixes_dir()``).
    runner:
        Runner name the prefix was built/installed with (e.g.
        ``"GE-Proton10-32"``).  Empty if unknown.
    steam_appid:
        umu Steam App ID integer, or ``None`` (GAMEID=0 fallback).
    """
    now = datetime.now(timezone.utc).isoformat()
    with _open_db() as conn:
        conn.execute(
            """
            INSERT INTO installed
                (id, prefix_dir, platform, version, runner, steam_appid,
                 install_path, repo_source, installed_at, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                prefix_dir    = excluded.prefix_dir,
                platform      = excluded.platform,
                version       = excluded.version,
                runner        = excluded.runner,
                steam_appid   = excluded.steam_appid,
                install_path  = excluded.install_path,
                repo_source   = excluded.repo_source,
                last_updated  = excluded.last_updated
            """,
            (app_id, prefix_dir, platform, version, runner or None,
             steam_appid, install_path or None, repo_source,
             now, now),
        )


def get_installed(app_id: str) -> dict | None:
    """Return the installed record for *app_id*, or ``None`` if not installed."""
    with _open_db() as conn:
        row = conn.execute(
            "SELECT * FROM installed WHERE id = ?", (app_id,)
        ).fetchone()
        return dict(row) if row else None


def is_installed(app_id: str) -> bool:
    """Return ``True`` if *app_id* has an installed record."""
    return get_installed(app_id) is not None


def remove_installed(app_id: str) -> None:
    """Delete the installed record for *app_id* (no-op if not present)."""
    with _open_db() as conn:
        conn.execute("DELETE FROM installed WHERE id = ?", (app_id,))


def get_all_installed() -> list[dict]:
    """Return all installed records ordered by ``installed_at``."""
    with _open_db() as conn:
        rows = conn.execute(
            "SELECT * FROM installed ORDER BY installed_at"
        ).fetchall()
        return [dict(row) for row in rows]


def get_runner_override(app_id: str) -> str | None:
    """Return the persisted runner override for *app_id*, or ``None``."""
    rec = get_installed(app_id)
    if rec is None:
        return None
    return rec.get("runner_override")


def set_runner_override(app_id: str, runner_name: str | None) -> None:
    """Persist the runner override for *app_id*.

    Pass ``None`` to clear the override.  No-op if *app_id* is not in the
    database.
    """
    with _open_db() as conn:
        conn.execute(
            "UPDATE installed SET runner_override = ? WHERE id = ?",
            (runner_name, app_id),
        )


# ---------------------------------------------------------------------------
# Public API — repos
# ---------------------------------------------------------------------------

def get_all_repos() -> list[dict]:
    """Return all repo records from the ``repos`` table (if it exists)."""
    try:
        with _open_db() as conn:
            rows = conn.execute("SELECT * FROM repos ORDER BY id").fetchall()
            return [dict(row) for row in rows]
    except sqlite3.OperationalError:
        return []


# ---------------------------------------------------------------------------
# Public API — base images
# ---------------------------------------------------------------------------

def mark_base_installed(runner: str, repo_source: str = "") -> None:
    """Record that the base image for *runner* is installed."""
    now = datetime.now(timezone.utc).isoformat()
    with _open_db() as conn:
        conn.execute(
            """
            INSERT INTO bases (runner, installed_at, repo_source)
            VALUES (?, ?, ?)
            ON CONFLICT(runner) DO UPDATE SET
                installed_at = excluded.installed_at,
                repo_source  = excluded.repo_source
            """,
            (runner, now, repo_source),
        )


def get_installed_base(runner: str) -> dict | None:
    """Return the installed record for *runner*, or ``None`` if not present."""
    with _open_db() as conn:
        row = conn.execute(
            "SELECT * FROM bases WHERE runner = ?", (runner,)
        ).fetchone()
        return dict(row) if row else None


def get_all_installed_bases() -> list[dict]:
    """Return all installed base records ordered by ``installed_at``."""
    with _open_db() as conn:
        rows = conn.execute(
            "SELECT * FROM bases ORDER BY installed_at"
        ).fetchall()
        return [dict(row) for row in rows]


def remove_base_record(runner: str) -> None:
    """Delete the base record for *runner* (no-op if not present)."""
    with _open_db() as conn:
        conn.execute("DELETE FROM bases WHERE runner = ?", (runner,))

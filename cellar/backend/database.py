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

Schema v5 (current)
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
        archive_crc32   TEXT,
        runner          TEXT,
        steam_appid     INTEGER,
        install_path    TEXT,
        install_size    INTEGER,
        delta_size      INTEGER,
        repo_source     TEXT,
        installed_at    TEXT,
        last_updated    TEXT
    );

    CREATE TABLE bases (
        runner       TEXT PRIMARY KEY,
        repo_source  TEXT,
        installed_at TEXT
    );

    CREATE TABLE launch_overrides (
        app_id           TEXT PRIMARY KEY,
        launch_targets   TEXT,
        steam_appid      INTEGER,
        runner           TEXT,
        dxvk             INTEGER,
        vkd3d            INTEGER,
        audio_driver     TEXT,
        debug            INTEGER,
        direct_proton    INTEGER,
        no_lsteamclient  INTEGER
    );
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from cellar.backend.config import data_dir

log = logging.getLogger(__name__)

_CURRENT_VERSION = 6


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
    if current < 2:
        _migrate_v1_to_v2(conn)
    if current < 3:
        _migrate_v2_to_v3(conn)
    if current < 4:
        _migrate_v3_to_v4(conn)
    if current < 5:
        _migrate_v4_to_v5(conn)
    if current < 6:
        _migrate_v5_to_v6(conn)


def _create_schema_v1(conn: sqlite3.Connection) -> None:
    """Create the full current schema from scratch (fresh install)."""
    conn.executescript("""
        CREATE TABLE schema_version (
            version INTEGER PRIMARY KEY
        );

        CREATE TABLE installed (
            id              TEXT PRIMARY KEY,
            prefix_dir      TEXT NOT NULL,
            platform        TEXT NOT NULL DEFAULT 'windows',
            version         TEXT,
            archive_crc32   TEXT,
            runner          TEXT,
            steam_appid     INTEGER,
            install_path    TEXT,
            install_size    INTEGER,
            delta_size      INTEGER,
            repo_source     TEXT,
            installed_at    TEXT,
            last_updated    TEXT
        );

        CREATE TABLE bases (
            runner       TEXT PRIMARY KEY,
            repo_source  TEXT,
            installed_at TEXT
        );

        CREATE TABLE launch_overrides (
            app_id           TEXT PRIMARY KEY,
            launch_targets   TEXT,
            steam_appid      INTEGER,
            runner           TEXT,
            dxvk             INTEGER,
            vkd3d            INTEGER,
            audio_driver     TEXT,
            debug            INTEGER,
            direct_proton    INTEGER,
            no_lsteamclient  INTEGER
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


def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
    """Add ``archive_crc32`` column to the ``installed`` table."""
    log.info("Migrating cellar.db from v1 to v2")
    try:
        with conn:
            conn.execute(
                "ALTER TABLE installed ADD COLUMN archive_crc32 TEXT"
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (2,),
            )
    except Exception:
        log.exception("v1→v2 migration failed; database left unchanged")
        raise


def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
    """Add ``install_size`` column to the ``installed`` table."""
    log.info("Migrating cellar.db from v2 to v3")
    try:
        with conn:
            conn.execute(
                "ALTER TABLE installed ADD COLUMN install_size INTEGER"
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (3,),
            )
    except Exception:
        log.exception("v2→v3 migration failed; database left unchanged")
        raise


def _migrate_v3_to_v4(conn: sqlite3.Connection) -> None:
    """Drop ``runner_override`` from ``installed``; add ``launch_overrides`` table."""
    log.info("Migrating cellar.db from v3 to v4")
    try:
        with conn:
            # Drop the old per-install runner override column.
            # SQLite 3.35+ (Python 3.10's bundled SQLite) supports DROP COLUMN.
            cols = {row[1] for row in conn.execute("PRAGMA table_info(installed)")}
            if "runner_override" in cols:
                conn.execute("ALTER TABLE installed DROP COLUMN runner_override")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS launch_overrides (
                    app_id           TEXT PRIMARY KEY,
                    launch_targets   TEXT,
                    steam_appid      INTEGER,
                    runner           TEXT,
                    dxvk             INTEGER,
                    vkd3d            INTEGER,
                    audio_driver     TEXT,
                    debug            INTEGER,
                    direct_proton    INTEGER
                )
            """)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (4,),
            )
    except Exception:
        log.exception("v3→v4 migration failed; database left unchanged")
        raise


def _migrate_v4_to_v5(conn: sqlite3.Connection) -> None:
    """Add ``delta_size`` column to the ``installed`` table.

    Stores the uncompressed size of the delta-only content (excluding shared
    base files).  On CoW filesystems this reflects actual unique disk usage.
    """
    log.info("Migrating cellar.db from v4 to v5")
    try:
        with conn:
            conn.execute(
                "ALTER TABLE installed ADD COLUMN delta_size INTEGER"
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (5,),
            )
    except Exception:
        log.exception("v4→v5 migration failed; database left unchanged")
        raise


def _migrate_v5_to_v6(conn: sqlite3.Connection) -> None:
    """Add ``no_lsteamclient`` column to the ``launch_overrides`` table."""
    log.info("Migrating cellar.db from v5 to v6")
    try:
        with conn:
            conn.execute(
                "ALTER TABLE launch_overrides ADD COLUMN no_lsteamclient INTEGER"
            )
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (6,),
            )
    except Exception:
        log.exception("v5→v6 migration failed; database left unchanged")
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
    archive_crc32: str = "",
    install_size: int = 0,
    delta_size: int = 0,
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
        Installed catalogue version string (for display only).
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
    archive_crc32:
        CRC32 checksum of the installed archive.  Used to detect content
        changes on catalogue refresh (update available when it differs from
        the catalogue entry's ``archive_crc32``).
    install_size:
        On-disk size of the installed prefix in bytes, measured after
        extraction.  0 means not yet measured; use :func:`set_install_size`
        to update lazily.
    delta_size:
        Uncompressed size of the delta-only content (excluding shared base
        files) in bytes.  On CoW filesystems this reflects actual unique
        disk usage.  0 for non-delta apps or when not yet measured.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _open_db() as conn:
        conn.execute(
            """
            INSERT INTO installed
                (id, prefix_dir, platform, version, archive_crc32, runner,
                 steam_appid, install_path, install_size, delta_size,
                 repo_source, installed_at, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                prefix_dir    = excluded.prefix_dir,
                platform      = excluded.platform,
                version       = excluded.version,
                archive_crc32 = excluded.archive_crc32,
                runner        = excluded.runner,
                steam_appid   = excluded.steam_appid,
                install_path  = excluded.install_path,
                install_size  = COALESCE(excluded.install_size, install_size),
                delta_size    = COALESCE(excluded.delta_size, delta_size),
                repo_source   = excluded.repo_source,
                last_updated  = excluded.last_updated
            """,
            (app_id, prefix_dir, platform, version, archive_crc32 or None,
             runner or None, steam_appid, install_path or None,
             install_size or None, delta_size or None, repo_source, now, now),
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


def set_install_size(app_id: str, size: int) -> None:
    """Store the measured on-disk install size for *app_id* (bytes)."""
    with _open_db() as conn:
        conn.execute(
            "UPDATE installed SET install_size = ? WHERE id = ?",
            (size, app_id),
        )


def get_launch_overrides(app_id: str) -> dict:
    """Return the launch override record for *app_id*.

    Returns a dict containing only the fields that are explicitly overridden;
    absent keys mean "use the catalogue default".  Returns an empty dict if
    no overrides have been saved.

    ``launch_targets``, if present, is a decoded list of dicts.
    Boolean fields (``dxvk``, ``vkd3d``, ``debug``, ``direct_proton``) are
    returned as Python booleans.
    """
    import json as _json
    with _open_db() as conn:
        row = conn.execute(
            "SELECT * FROM launch_overrides WHERE app_id = ?", (app_id,)
        ).fetchone()
    if row is None:
        return {}
    d = dict(row)
    d.pop("app_id", None)
    if d.get("launch_targets") is not None:
        d["launch_targets"] = _json.loads(d["launch_targets"])
    for key in ("dxvk", "vkd3d", "debug", "direct_proton", "no_lsteamclient"):
        if d.get(key) is not None:
            d[key] = bool(d[key])
    return {k: v for k, v in d.items() if v is not None}


def set_launch_overrides(app_id: str, overrides: dict) -> None:
    """Upsert the launch override record for *app_id*.

    Only keys present in *overrides* are stored; fields not in *overrides*
    are stored as NULL (meaning "use catalogue default").
    """
    import json as _json
    lt = overrides.get("launch_targets")
    lt_json = _json.dumps(lt) if lt is not None else None

    def _to_int(v: object) -> int | None:
        return int(v) if v is not None else None

    with _open_db() as conn:
        conn.execute(
            """
            INSERT INTO launch_overrides
                (app_id, launch_targets, steam_appid, runner, dxvk, vkd3d,
                 audio_driver, debug, direct_proton, no_lsteamclient)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(app_id) DO UPDATE SET
                launch_targets  = excluded.launch_targets,
                steam_appid     = excluded.steam_appid,
                runner          = excluded.runner,
                dxvk            = excluded.dxvk,
                vkd3d           = excluded.vkd3d,
                audio_driver    = excluded.audio_driver,
                debug           = excluded.debug,
                direct_proton   = excluded.direct_proton,
                no_lsteamclient = excluded.no_lsteamclient
            """,
            (
                app_id,
                lt_json,
                overrides.get("steam_appid"),
                overrides.get("runner") or None,
                _to_int(overrides.get("dxvk")),
                _to_int(overrides.get("vkd3d")),
                overrides.get("audio_driver") or None,
                _to_int(overrides.get("debug")),
                _to_int(overrides.get("direct_proton")),
                _to_int(overrides.get("no_lsteamclient")),
            ),
        )


def clear_launch_overrides(app_id: str) -> None:
    """Delete all launch overrides for *app_id* (reset to catalogue defaults)."""
    with _open_db() as conn:
        conn.execute("DELETE FROM launch_overrides WHERE app_id = ?", (app_id,))


def update_app_location(app_id: str, install_path: str, prefix_dir: str) -> None:
    """Update the install location for a single app after a per-app move."""
    with _open_db() as conn:
        conn.execute(
            "UPDATE installed SET install_path = ?, prefix_dir = ? WHERE id = ?",
            (install_path, prefix_dir, app_id),
        )


def update_install_paths(old_base: str, new_base: str) -> None:
    """Rewrite ``install_path`` records whose path is under *old_base*.

    Called after moving the install data directory so that every installed
    record points to its new absolute location.
    """
    old = old_base.rstrip("/")
    new = new_base.rstrip("/")
    if not old or not new or old == new:
        return
    # Escape LIKE wildcards so % and _ in paths are matched literally.
    escaped = old.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with _open_db() as conn:
        conn.execute(
            "UPDATE installed"
            " SET install_path = ? || SUBSTR(install_path, ?)"
            " WHERE install_path LIKE ? ESCAPE '\\'",
            (new, len(old) + 1, escaped + "/%"),
        )


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

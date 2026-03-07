"""Prefix manifest — tracks installed package files for safe update protection.

On fresh install, ``write_manifest`` walks ``drive_c/`` and records the mtime
and size of every file.  Before a safe update, ``scan_user_files`` re-stats
those files and classifies each non-manifest file as either Wine-owned (discard)
or user-created (protect).

Manifest format
---------------
JSON file ``.cellar-manifest.json`` at the prefix root::

    {
        "version": 1,
        "files": {
            "drive_c/Games/AppName/game.exe": [<size>, <mtime_int>],
            ...
        }
    }

Paths are relative to the prefix root using forward slashes.
mtime is stored as an integer (seconds since epoch) — sub-second precision is
discarded to avoid spurious change detection on filesystems with coarser
timestamps.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

MANIFEST_FILENAME = ".cellar-manifest.json"

# Paths relative to the prefix root (lower-cased for matching) that Wine/Proton
# creates on first launch.  Non-manifest files under these paths are NOT
# protected as user data during stash computation.
WINE_SYSTEM_PATHS: frozenset[str] = frozenset({
    "drive_c/windows",
    "drive_c/program files/common files",
    "drive_c/program files/internet explorer",
    "drive_c/program files/windows media player",
    "drive_c/program files/windows nt",
    "drive_c/program files (x86)/common files",
    "drive_c/program files (x86)/internet explorer",
    "drive_c/program files (x86)/windows media player",
    "drive_c/program files (x86)/windows nt",
    "drive_c/openxr",
    "drive_c/vrclient",
    "drive_c/programdata",
})


def _is_wine_system(rel_lower: str) -> bool:
    """Return True if *rel_lower* is under a Wine/Proton-owned system path."""
    return any(
        rel_lower == p or rel_lower.startswith(p + "/")
        for p in WINE_SYSTEM_PATHS
    )


def _scan_root(prefix_path: Path) -> Path:
    """Return the directory to walk for manifest purposes.

    Windows/Wine prefixes: ``drive_c/`` (contains the game files; Wine system
    dirs are also here but filtered by ``_is_wine_system`` during scan).
    Linux native installs: the install dir itself (no ``drive_c/`` wrapper).
    """
    drive_c = prefix_path / "drive_c"
    return drive_c if drive_c.is_dir() else prefix_path


def write_manifest(prefix_path: Path) -> None:
    """Write ``.cellar-manifest.json`` recording every installed package file.

    For Windows/Wine apps, walks ``drive_c/``.  For Linux native apps, walks
    the install directory directly.

    Called immediately after a fresh install, before the app is ever launched.
    Re-calling this overwrites any existing manifest (used after updates to
    reset the baseline).
    """
    root = _scan_root(prefix_path)

    files: dict[str, list[int]] = {}
    for fp in root.rglob("*"):
        if not fp.is_file():
            continue
        try:
            st = fp.stat()
            rel = fp.relative_to(prefix_path).as_posix()
            files[rel] = [st.st_size, int(st.st_mtime)]
        except OSError:
            continue

    dest = prefix_path / MANIFEST_FILENAME
    try:
        dest.write_text(json.dumps({"version": 1, "files": files}, separators=(",", ":")))
        log.debug("Manifest written: %d files → %s", len(files), dest)
    except OSError as exc:
        log.warning("Could not write manifest: %s", exc)


def read_manifest(prefix_path: Path) -> dict[str, tuple[int, int]]:
    """Return ``{rel_path: (size, mtime)}`` from the manifest, or ``{}`` if absent/corrupt."""
    manifest_path = prefix_path / MANIFEST_FILENAME
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text())
        return {k: (int(v[0]), int(v[1])) for k, v in data.get("files", {}).items()}
    except Exception as exc:
        log.warning("Could not read manifest at %s: %s", manifest_path, exc)
        return {}


def scan_user_files(prefix_path: Path) -> tuple[list[Path], list[Path]]:
    """Identify files to protect before a safe update.

    Returns ``(modified_package_files, user_created_files)`` — both as lists
    of absolute Paths.

    * **modified_package_files**: files present in the manifest whose on-disk
      size or mtime has changed since install.  These were modified by the user
      or the app and must be restored on top of the updated package.
    * **user_created_files**: files under the scan root that are *not* in the
      manifest and are *not* under a Wine/Proton system path.  These were
      created by the running application (saves, configs) and must be preserved.

    If no manifest exists ``([], [])`` is returned and the caller falls back
    to the legacy exclusion-only update strategy.
    """
    manifest = read_manifest(prefix_path)
    if not manifest:
        return [], []

    root = _scan_root(prefix_path)
    if not root.is_dir():
        return [], []

    modified: list[Path] = []
    user_created: list[Path] = []

    for fp in root.rglob("*"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(prefix_path).as_posix()
        if rel in manifest:
            recorded_size, recorded_mtime = manifest[rel]
            try:
                st = fp.stat()
                if st.st_size != recorded_size or int(st.st_mtime) != recorded_mtime:
                    modified.append(fp)
            except OSError:
                pass
        elif not _is_wine_system(rel.lower()):
            user_created.append(fp)

    log.debug(
        "scan_user_files: %d modified package files, %d user-created files",
        len(modified), len(user_created),
    )
    return modified, user_created

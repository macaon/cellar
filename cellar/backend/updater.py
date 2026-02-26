"""Safe overlay update for installed Bottles bottles.

Update flow
-----------
1. **Backup** (optional) — tar the existing bottle to a user-chosen path.
2. **Acquire** — download / locate the new archive (reuses installer logic).
3. **Verify** — SHA-256 check when the catalogue provides a hash.
4. **Extract** — unpack to a temporary directory.
5. **Overlay** — rsync (or Python fallback) the extracted bottle onto the
   existing bottle directory without ``--delete``, skipping user-data paths.

Overlay exclusions
------------------
The following paths inside the bottle are never overwritten:

* ``drive_c/users/*/AppData/Roaming/``
* ``drive_c/users/*/AppData/Local/``
* ``drive_c/users/*/AppData/LocalLow/``
* ``drive_c/users/*/Documents/``
* ``user.reg``
* ``userdef.reg``

Files that exist on the user's system but are absent from the new archive
are left untouched (no ``--delete``), so user-created saves in the app's
own directory are also preserved.

Threading
---------
All public functions are **blocking** and designed to run on a background
thread.  Progress is reported via ``progress_cb(phase: str, fraction: float)``.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class UpdateError(Exception):
    """Raised when an update step fails unrecoverably."""


class UpdateCancelled(Exception):
    """Raised when the user cancels a running update."""


# ---------------------------------------------------------------------------
# Overlay exclusion rules
# ---------------------------------------------------------------------------

# Tuple patterns matched against the *lowercased* parts of each file's path
# relative to the bottle root.  ``None`` matches any single component.
_EXCLUDE_PREFIXES: list[tuple] = [
    ("drive_c", "users", None, "appdata", "roaming"),
    ("drive_c", "users", None, "appdata", "local"),
    ("drive_c", "users", None, "appdata", "locallow"),
    ("drive_c", "users", None, "documents"),
]
_EXCLUDE_NAMES: frozenset[str] = frozenset({"user.reg", "userdef.reg"})

# rsync --exclude patterns (equivalent to the Python rules above).
_RSYNC_EXCLUDES: list[str] = [
    "drive_c/users/*/AppData/Roaming/",
    "drive_c/users/*/AppData/Local/",
    "drive_c/users/*/AppData/LocalLow/",
    "drive_c/users/*/Documents/",
    "user.reg",
    "userdef.reg",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def backup_bottle(
    bottle_path: Path,
    dest_path: Path,
    *,
    progress_cb: Callable[[str, float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Archive *bottle_path* as a ``.tar.gz`` at *dest_path*.

    The archive contains a single top-level directory named after the
    bottle so that it can be re-imported by Cellar if needed.

    Parameters
    ----------
    bottle_path:
        The bottle directory to back up.
    dest_path:
        Destination ``.tar.gz`` file path.  Parent directories are
        created automatically.
    progress_cb:
        Optional ``(phase, fraction)`` callback.
    cancel_event:
        When set the backup is aborted, the partial file is removed, and
        ``UpdateCancelled`` is raised.
    """
    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if _cancelled():
        raise UpdateCancelled

    _report(progress_cb, "Preparing backup…", 0.0)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    all_files = [p for p in bottle_path.rglob("*") if p.is_file()]
    total = max(len(all_files), 1)

    try:
        with tarfile.open(dest_path, "w:gz") as tf:
            for i, fp in enumerate(all_files):
                if _cancelled():
                    raise UpdateCancelled
                arcname = Path(bottle_path.name) / fp.relative_to(bottle_path)
                tf.add(fp, arcname=str(arcname))
                if i % 20 == 0:
                    _report(progress_cb, "Backing up…", i / total)
    except UpdateCancelled:
        dest_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest_path.unlink(missing_ok=True)
        raise UpdateError(f"Backup failed: {exc}") from exc

    _report(progress_cb, "Backup complete", 1.0)


def update_app_safe(
    entry,                          # AppEntry — avoid circular import
    archive_uri: str,
    bottle_path: Path,
    *,
    backup_path: Path | None = None,
    progress_cb: Callable[[str, float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Overlay-update *bottle_path* with the contents of *archive_uri*.

    Parameters
    ----------
    entry:
        The ``AppEntry`` being updated (used for SHA-256 and size hints).
    archive_uri:
        Absolute path or HTTP(S) URL of the new archive.
    bottle_path:
        The existing installed bottle directory.
    backup_path:
        When supplied, the current bottle is archived here before the
        update proceeds.  The parent directory is created automatically.
    progress_cb:
        Optional ``(phase, fraction)`` callback.
    cancel_event:
        When set, the update is aborted at the next checkpoint.

    Raises
    ------
    UpdateError
        On any unrecoverable failure (download error, bad checksum, …).
    UpdateCancelled
        When *cancel_event* is set during the operation.
    """
    # Fraction ranges shift when a backup phase is present.
    has_backup = backup_path is not None
    dl_lo  = 0.20 if has_backup else 0.00
    dl_hi  = 0.60 if has_backup else 0.50
    ver_lo = dl_hi
    ver_hi = ver_lo + 0.05
    ext_lo = ver_hi
    ext_hi = ext_lo + 0.15
    ov_lo  = ext_hi
    ov_hi  = 1.00

    def _sub(phase: str, lo: float, hi: float) -> Callable[[float], None]:
        return lambda f: _report(progress_cb, phase, lo + f * (hi - lo))

    _check_cancel(cancel_event)

    # ── Phase 1: Backup ────────────────────────────────────────────────────
    if has_backup:
        backup_bottle(
            bottle_path,
            backup_path,
            progress_cb=_sub("Backing up…", 0.0, 0.20),
            cancel_event=cancel_event,
        )
        _check_cancel(cancel_event)

    with tempfile.TemporaryDirectory(prefix="cellar-update-") as tmp_str:
        tmp = Path(tmp_str)

        # ── Phase 2: Acquire ───────────────────────────────────────────────
        _report(progress_cb, "Downloading…", dl_lo)
        try:
            from cellar.backend.installer import (  # noqa: PLC0415
                InstallCancelled,
                InstallError,
                _acquire_archive,
                _check_cancel as _inst_check,
                _extract_archive,
                _find_bottle_dir,
                _verify_sha256,
            )
        except ImportError as exc:
            raise UpdateError(f"Internal error: {exc}") from exc

        def _wrap_cancel(e: threading.Event | None) -> None:
            if e and e.is_set():
                raise UpdateCancelled

        try:
            archive_path = _acquire_archive(
                archive_uri,
                tmp / "archive.tar.gz",
                expected_size=entry.archive_size,
                progress_cb=_sub("Downloading…", dl_lo, dl_hi),
                cancel_event=cancel_event,
            )
        except InstallCancelled:
            raise UpdateCancelled
        except InstallError as exc:
            raise UpdateError(str(exc)) from exc

        # ── Phase 3: Verify ────────────────────────────────────────────────
        _check_cancel(cancel_event)
        if entry.archive_sha256:
            _report(progress_cb, "Verifying…", ver_lo)
            try:
                _verify_sha256(archive_path, entry.archive_sha256)
            except InstallError as exc:
                raise UpdateError(str(exc)) from exc

        # ── Phase 4: Extract ───────────────────────────────────────────────
        _check_cancel(cancel_event)
        _report(progress_cb, "Extracting…", ext_lo)
        extract_dir = tmp / "extracted"
        extract_dir.mkdir()
        try:
            _extract_archive(archive_path, extract_dir, cancel_event)
            bottle_src = _find_bottle_dir(extract_dir)
        except InstallCancelled:
            raise UpdateCancelled
        except InstallError as exc:
            raise UpdateError(str(exc)) from exc

        # ── Phase 5: Overlay ───────────────────────────────────────────────
        _check_cancel(cancel_event)
        _report(progress_cb, "Updating…", ov_lo)
        _overlay(
            bottle_src,
            bottle_path,
            progress_cb=_sub("Updating…", ov_lo, ov_hi),
            cancel_event=cancel_event,
        )

    _report(progress_cb, "Done", 1.0)


# ---------------------------------------------------------------------------
# Overlay helpers
# ---------------------------------------------------------------------------

def _overlay(
    src: Path,
    dst: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Overlay *src* onto *dst* without deleting, skipping excluded paths.

    Tries ``rsync`` first; falls back to a pure-Python implementation when
    rsync is not available (e.g. inside a restricted Flatpak sandbox).
    """
    if shutil.which("rsync"):
        _overlay_rsync(src, dst, cancel_event=cancel_event)
    else:
        _overlay_python(src, dst, progress_cb=progress_cb, cancel_event=cancel_event)


def _overlay_rsync(
    src: Path,
    dst: Path,
    *,
    cancel_event: threading.Event | None,
) -> None:
    """Run rsync overlay with user-data exclusions."""
    cmd = ["rsync", "-a", "--no-delete"]
    for pattern in _RSYNC_EXCLUDES:
        cmd += ["--exclude", pattern]
    # Trailing slash on src copies contents, not the directory itself.
    cmd += [str(src) + "/", str(dst) + "/"]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        # rsync disappeared between the which() check and Popen — use fallback
        _overlay_python(src, dst, progress_cb=None, cancel_event=cancel_event)
        return

    while True:
        if cancel_event and cancel_event.is_set():
            proc.kill()
            proc.wait()
            raise UpdateCancelled
        try:
            proc.wait(timeout=0.5)
            break
        except subprocess.TimeoutExpired:
            continue

    if proc.returncode != 0:
        stderr = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")
        raise UpdateError(f"rsync failed (exit {proc.returncode}): {stderr.strip()}")


def _overlay_python(
    src: Path,
    dst: Path,
    *,
    progress_cb: Callable[[float], None] | None,
    cancel_event: threading.Event | None,
) -> None:
    """Pure-Python overlay copy with user-data exclusions."""
    all_files = [p for p in src.rglob("*") if p.is_file()]
    total = max(len(all_files), 1)

    for i, fp in enumerate(all_files):
        if cancel_event and cancel_event.is_set():
            raise UpdateCancelled
        rel = fp.relative_to(src)
        if _is_excluded(rel):
            continue
        dst_file = dst / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fp, dst_file)
        if progress_cb and i % 50 == 0:
            progress_cb(i / total)


def _is_excluded(rel: Path) -> bool:
    """Return True if *rel* (relative to bottle root) should be skipped."""
    parts = tuple(p.lower() for p in rel.parts)
    if not parts:
        return False
    if parts[-1] in _EXCLUDE_NAMES:
        return True
    for pattern in _EXCLUDE_PREFIXES:
        if len(parts) < len(pattern):
            continue
        if all(e is None or e == parts[i] for i, e in enumerate(pattern)):
            return True
    return False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise UpdateCancelled("Update cancelled")


def _report(
    progress_cb: Callable[[str, float], None] | None,
    phase: str,
    fraction: float,
) -> None:
    if progress_cb:
        progress_cb(phase, fraction)

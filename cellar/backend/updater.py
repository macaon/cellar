"""Safe overlay update for installed prefixes.

Update flow
-----------
1. **Backup** (optional) — tar the existing prefix to a user-chosen path.
2. **Acquire** — download / locate the new archive (reuses installer logic).
3. **Verify** — CRC32 check when the catalogue provides a checksum.
4. **Extract** — unpack to a temporary directory.
5. **Overlay** — rsync (or Python fallback) the extracted prefix onto the
   existing prefix directory, skipping user-data paths.
6. **Delete** — remove files that were removed in the new version:
   * **Full archives**: ``rsync --delete`` removes destination files absent
     from the new archive, while the user-data ``--exclude`` rules protect
     save data in standard locations.
   * **Delta archives**: the ``.cellar_delete`` manifest (embedded in the
     archive by the packager) is applied to remove specifically listed files.

Overlay exclusions (never touched during overlay or delete)
-----------------------------------------------------------
* ``drive_c/users/*/AppData/Roaming/``
* ``drive_c/users/*/AppData/Local/``
* ``drive_c/users/*/AppData/LocalLow/``
* ``drive_c/users/*/Documents/``
* ``user.reg``
* ``userdef.reg``

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
import time
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
#
# User-data paths — never overwritten or deleted during an overlay update.
_EXCLUDE_PREFIXES: list[tuple] = [
    ("drive_c", "users", None, "appdata", "roaming"),
    ("drive_c", "users", None, "appdata", "local"),
    ("drive_c", "users", None, "appdata", "locallow"),
    ("drive_c", "users", None, "documents"),
    # Wine/Proton system paths — created on first launch, never in packages.
    # Excluded from --delete so Wine doesn't need to recreate them after update.
    ("drive_c", "windows"),
    ("drive_c", "program files", "common files"),
    ("drive_c", "program files", "internet explorer"),
    ("drive_c", "program files", "windows media player"),
    ("drive_c", "program files", "windows nt"),
    ("drive_c", "program files (x86)", "common files"),
    ("drive_c", "program files (x86)", "internet explorer"),
    ("drive_c", "program files (x86)", "windows media player"),
    ("drive_c", "program files (x86)", "windows nt"),
    ("drive_c", "openxr"),
    ("drive_c", "vrclient"),
    ("drive_c", "programdata"),
]
_EXCLUDE_NAMES: frozenset[str] = frozenset({"user.reg", "userdef.reg"})

# rsync --exclude patterns (equivalent to the Python rules above).
_RSYNC_EXCLUDES: list[str] = [
    # User data
    "drive_c/users/*/AppData/Roaming/",
    "drive_c/users/*/AppData/Local/",
    "drive_c/users/*/AppData/LocalLow/",
    "drive_c/users/*/Documents/",
    "user.reg",
    "userdef.reg",
    # Wine/Proton system paths
    "drive_c/windows/",
    "drive_c/Program Files/Common Files/",
    "drive_c/Program Files/Internet Explorer/",
    "drive_c/Program Files/Windows Media Player/",
    "drive_c/Program Files/Windows NT/",
    "drive_c/Program Files (x86)/Common Files/",
    "drive_c/Program Files (x86)/Internet Explorer/",
    "drive_c/Program Files (x86)/Windows Media Player/",
    "drive_c/Program Files (x86)/Windows NT/",
    "drive_c/openxr/",
    "drive_c/vrclient/",
    "drive_c/ProgramData/",
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def backup_bottle(
    prefix_path: Path,
    dest_path: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Archive *prefix_path* as a ``.tar.gz`` at *dest_path*.

    The archive contains a single top-level directory named after the
    prefix so that it can be re-imported by Cellar if needed.

    Parameters
    ----------
    prefix_path:
        The prefix directory to back up.
    dest_path:
        Destination ``.tar.gz`` file path.  Parent directories are
        created automatically.
    progress_cb:
        Optional ``(fraction)`` callback in [0, 1].
    stats_cb:
        Optional ``(bytes_done, bytes_total, speed_bps)`` callback.
    phase_cb:
        Optional ``(label)`` callback for phase name changes.
    cancel_event:
        When set the backup is aborted, the partial file is removed, and
        ``UpdateCancelled`` is raised.
    """
    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if _cancelled():
        raise UpdateCancelled

    if phase_cb:
        phase_cb("Preparing backup\u2026")
    if progress_cb:
        progress_cb(0.0)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    all_files = [p for p in prefix_path.rglob("*") if p.is_file()]
    file_sizes = {fp: fp.stat().st_size for fp in all_files}
    total_bytes = max(sum(file_sizes.values()), 1)
    bytes_done = 0
    t_start = time.monotonic()
    t_last_stats = t_start

    try:
        with tarfile.open(dest_path, "w:gz") as tf:
            for fp in all_files:
                if _cancelled():
                    raise UpdateCancelled
                if phase_cb and bytes_done == 0:
                    phase_cb("Backing up\u2026")
                arcname = Path(prefix_path.name) / fp.relative_to(prefix_path)
                tf.add(fp, arcname=str(arcname))
                bytes_done += file_sizes[fp]
                now = time.monotonic()
                elapsed = now - t_start
                if now - t_last_stats >= 0.1:
                    t_last_stats = now
                    speed = bytes_done / elapsed if elapsed > 0 else 0.0
                    if progress_cb:
                        progress_cb(bytes_done / total_bytes)
                    if stats_cb:
                        stats_cb(bytes_done, total_bytes, speed)
    except UpdateCancelled:
        dest_path.unlink(missing_ok=True)
        raise
    except Exception as exc:
        dest_path.unlink(missing_ok=True)
        raise UpdateError(f"Backup failed: {exc}") from exc

    if progress_cb:
        progress_cb(1.0)
    if stats_cb:
        elapsed = time.monotonic() - t_start
        stats_cb(total_bytes, total_bytes, total_bytes / elapsed if elapsed > 0 else 0.0)


def update_app_safe(
    entry,                          # AppEntry — avoid circular import
    archive_uri: str,
    prefix_path: Path,
    *,
    backup_path: Path | None = None,
    base_entry=None,                # BaseEntry | None — accepted but unused; delta overlay
    base_archive_uri: str = "",     # URI for base archive — accepted but unused
    progress_cb: Callable[[float], None] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    cancel_event: threading.Event | None = None,
    token: str | None = None,
) -> None:
    """Overlay-update *prefix_path* with the contents of *archive_uri*.

    For delta packages (``entry.base_image`` is set) the rsync overlay
    strategy works without base reconstruction: the delta archive contains
    only changed/new files and the overlay applies them directly on top of
    the existing bottle.  Files absent from the delta are left untouched
    (``--no-delete`` semantics), so unchanged base files and user data are
    both preserved.  ``base_entry`` and ``base_archive_uri`` are accepted
    for API consistency with ``install_app`` but are not used here.

    Parameters
    ----------
    entry:
        The ``AppEntry`` being updated (used for SHA-256 and size hints).
    archive_uri:
        Absolute path or HTTP(S) URL of the new archive.
    prefix_path:
        The existing installed bottle directory.
    backup_path:
        When supplied, the current bottle is archived here before the
        update proceeds.  The parent directory is created automatically.
    base_entry:
        Ignored.  Present for API symmetry with ``install_app``.
    base_archive_uri:
        Ignored.  Present for API symmetry with ``install_app``.
    progress_cb:
        Optional ``(fraction)`` callback in [0, 1].
    phase_cb:
        Optional ``(label)`` callback for phase name changes.
    stats_cb:
        Optional ``(done, total, speed_bps)`` callback during download.
    cancel_event:
        When set, the update is aborted at the next checkpoint.

    Raises
    ------
    UpdateError
        On any unrecoverable failure (download error, bad checksum, …).
    UpdateCancelled
        When *cancel_event* is set during the operation.
    """
    if entry.base_image:
        log.debug(
            "Delta update for %r: overlaying delta directly (no base reconstruction needed)",
            entry.id,
        )
    # Fraction ranges shift when a backup phase is present.
    has_backup = backup_path is not None
    dl_lo  = 0.20 if has_backup else 0.00
    ext_hi = 0.80 if has_backup else 0.70
    ov_lo  = ext_hi

    def _sub(lo: float, hi: float) -> Callable[[float], None]:
        return lambda f: (progress_cb(lo + f * (hi - lo)) if progress_cb else None)

    _check_cancel(cancel_event)

    # ── Phase 0: Scan manifest and stash user files ────────────────────────
    from cellar.backend.manifest import scan_user_files, write_manifest  # noqa: PLC0415
    modified_files, user_files = scan_user_files(prefix_path)

    # ── Phase 1: Backup ────────────────────────────────────────────────────
    if has_backup:
        backup_bottle(
            prefix_path,
            backup_path,
            progress_cb=_sub(0.0, 0.20),
            stats_cb=stats_cb,
            phase_cb=phase_cb,
            cancel_event=cancel_event,
        )
        _check_cancel(cancel_event)

    # Stash lives in its own temp dir, outside the update work dir, so it
    # survives any exception thrown during download/extract/overlay.
    files_to_stash = modified_files + user_files
    with tempfile.TemporaryDirectory(prefix="cellar-stash-") as stash_tmp_str:
        stash_dir = Path(stash_tmp_str)

        if files_to_stash:
            if phase_cb:
                phase_cb("Preserving user data\u2026")
            _stash_files(files_to_stash, prefix_path, stash_dir)

        with tempfile.TemporaryDirectory(prefix="cellar-update-") as tmp_str:
            tmp = Path(tmp_str)

            # ── Phases 2-4: Stream, verify CRC32, extract (single pass) ──────
            if phase_cb:
                phase_cb("Downloading & extracting package\u2026")
            if progress_cb:
                progress_cb(dl_lo)
            try:
                from cellar.backend.installer import (  # noqa: PLC0415
                    InstallCancelled,
                    InstallError,
                    _build_source,
                    _find_bottle_dir,
                    _stream_and_extract,
                )
            except ImportError as exc:
                raise UpdateError(f"Internal error: {exc}") from exc

            extract_dir = tmp / "extracted"
            extract_dir.mkdir()
            try:
                chunks, total = _build_source(
                    archive_uri,
                    expected_size=entry.archive_size,
                    token=token,
                )
                _stream_and_extract(
                    chunks, total,
                    is_zst=archive_uri.endswith(".tar.zst"),
                    dest=extract_dir,
                    expected_crc32=entry.archive_crc32,
                    cancel_event=cancel_event,
                    progress_cb=_sub(dl_lo, ext_hi),
                    stats_cb=stats_cb,
                    name_cb=None,
                )
                bottle_src = _find_bottle_dir(extract_dir)
            except InstallCancelled:
                raise UpdateCancelled
            except InstallError as exc:
                raise UpdateError(str(exc)) from exc

            # ── Phase 5: Overlay ─────────────────────────────────────────────
            _check_cancel(cancel_event)
            if phase_cb:
                phase_cb("Updating\u2026")
            if progress_cb:
                progress_cb(ov_lo)
            is_delta = bool(entry.base_image)
            _overlay(
                bottle_src,
                prefix_path,
                is_delta=is_delta,
                progress_cb=_sub(ov_lo, 1.0),
                cancel_event=cancel_event,
            )

            # ── Phase 6: Apply delete manifest (delta archives) ──────────────
            # For full archives --delete in rsync already handles removals.
            # For delta archives the .cellar_delete manifest lists files that
            # were present in the previous version but removed in this one.
            if is_delta:
                delete_manifest = bottle_src / ".cellar_delete"
                if delete_manifest.exists():
                    for line in delete_manifest.read_text().splitlines():
                        rel = line.strip()
                        if rel and not _is_excluded(Path(rel)):
                            (prefix_path / rel).unlink(missing_ok=True)

        # ── Phase 7: Rewrite manifest with new package baseline ──────────────
        # Written before restoring user files so only package files are
        # recorded.  If user-created files were included, they would appear
        # as manifest entries on the next update scan and escape stashing,
        # causing rsync --delete to remove them.
        write_manifest(prefix_path)

        # ── Phase 8: Restore stashed user files ──────────────────────────────
        # Restore runs outside the update temp dir so the stash is still
        # available even if extraction or overlay threw.
        if files_to_stash:
            if phase_cb:
                phase_cb("Restoring user data\u2026")
            _restore_stash(stash_dir, prefix_path)

    if phase_cb:
        phase_cb("Done")
    if progress_cb:
        progress_cb(1.0)


# ---------------------------------------------------------------------------
# Overlay helpers
# ---------------------------------------------------------------------------

def _overlay(
    src: Path,
    dst: Path,
    *,
    is_delta: bool = False,
    progress_cb: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Overlay *src* onto *dst*, skipping excluded user-data paths.

    For full archives (*is_delta* = False), files present in *dst* but absent
    from *src* are deleted (game files removed by the publisher are cleaned up
    while user-data exclusions are honoured).

    For delta archives (*is_delta* = True), only files present in *src* are
    copied — no destination-only files are removed here; deletion is handled
    separately via the ``.cellar_delete`` manifest after this call returns.

    Tries ``rsync`` first; falls back to a pure-Python implementation when
    rsync is not available (e.g. inside a restricted Flatpak sandbox).
    """
    if shutil.which("rsync"):
        _overlay_rsync(src, dst, is_delta=is_delta, cancel_event=cancel_event)
    else:
        _overlay_python(src, dst, is_delta=is_delta, progress_cb=progress_cb, cancel_event=cancel_event)


def _overlay_rsync(
    src: Path,
    dst: Path,
    *,
    is_delta: bool,
    cancel_event: threading.Event | None,
) -> None:
    """Run rsync overlay with user-data exclusions.

    For full archives, ``--delete`` removes destination files absent from the
    new version while the exclude rules protect user-data paths.  Delta
    archives omit ``--delete`` because the source only contains changed files;
    the ``.cellar_delete`` manifest handles explicit removals separately.
    """
    cmd = ["rsync", "-a"]
    for pattern in _RSYNC_EXCLUDES:
        cmd += ["--exclude", pattern]
    if not is_delta:
        cmd.append("--delete")
    # Trailing slash on src copies contents, not the directory itself.
    cmd += [str(src) + "/", str(dst) + "/"]

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except FileNotFoundError:
        # rsync disappeared between the which() check and Popen — use fallback
        _overlay_python(src, dst, is_delta=is_delta, progress_cb=None, cancel_event=cancel_event)
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
    is_delta: bool,
    progress_cb: Callable[[float], None] | None,
    cancel_event: threading.Event | None,
) -> None:
    """Pure-Python overlay copy with user-data exclusions.

    For full archives (*is_delta* = False), destination files absent from
    *src* are deleted after copying (honouring exclusions).
    """
    all_files = [p for p in src.rglob("*") if p.is_file()]
    total = max(len(all_files), 1)

    src_rels: set[Path] = set()
    for i, fp in enumerate(all_files):
        if cancel_event and cancel_event.is_set():
            raise UpdateCancelled
        rel = fp.relative_to(src)
        src_rels.add(rel)
        if _is_excluded(rel):
            continue
        dst_file = dst / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fp, dst_file)
        if progress_cb and i % 50 == 0:
            progress_cb(i / total)

    if not is_delta:
        # Delete destination files that are absent from the new version,
        # skipping user-data exclusions so saves are preserved.
        for fp in list(dst.rglob("*")):
            if not fp.is_file():
                continue
            if cancel_event and cancel_event.is_set():
                raise UpdateCancelled
            rel = fp.relative_to(dst)
            if rel not in src_rels and not _is_excluded(rel):
                fp.unlink(missing_ok=True)


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
# Stash / restore helpers
# ---------------------------------------------------------------------------

def _stash_files(files: list[Path], prefix_path: Path, stash_dir: Path) -> None:
    """Copy *files* into *stash_dir*, preserving their paths relative to *prefix_path*."""
    for src in files:
        try:
            rel = src.relative_to(prefix_path)
            dst = stash_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as exc:
            log.warning("Could not stash %s: %s", src, exc)


def _restore_stash(stash_dir: Path, prefix_path: Path) -> None:
    """Copy all files from *stash_dir* back into *prefix_path*, overwriting."""
    for src in stash_dir.rglob("*"):
        if not src.is_file():
            continue
        try:
            rel = src.relative_to(stash_dir)
            dst = prefix_path / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
        except OSError as exc:
            log.warning("Could not restore stashed file %s: %s", src, exc)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise UpdateCancelled("Update cancelled")



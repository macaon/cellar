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
# relative to the prefix root.  ``None`` matches any single component.
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
# Backup exclusion rules (shared by backup + future import)
# ---------------------------------------------------------------------------

def _load_backup_excludes() -> list[tuple[str, ...]]:
    """Load prefix-relative exclusion prefixes from ``backup_exclude.txt``.

    Each non-blank, non-comment line is split into lowercased path parts
    and used as a prefix match against file paths relative to the prefix
    root.
    """
    txt = (Path(__file__).parent / "backup_exclude.txt").read_text()
    return [
        tuple(line.strip().lower().replace("\\", "/").split("/"))
        for line in txt.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

_BACKUP_EXCLUDES: list[tuple[str, ...]] = _load_backup_excludes()


def is_backup_excluded(rel: Path) -> bool:
    """Return ``True`` if *rel* (relative to prefix root) should be excluded."""
    parts = tuple(p.lower() for p in rel.parts)
    for pattern in _BACKUP_EXCLUDES:
        if parts[:len(pattern)] == pattern:
            return True
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def backup_prefix(
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


def _remap_archive_root(arcname: str, app_id: str) -> str:
    """Replace the ``drive_c/`` prefix with *app_id* in an archive path.

    ``drive_c/save/slot1.dat`` → ``my-game/save/slot1.dat``

    Paths that don't start with ``drive_c/`` are left unchanged.
    """
    prefix = "drive_c/"
    if arcname.startswith(prefix):
        return app_id + "/" + arcname[len(prefix):]
    if arcname == "drive_c":
        return app_id
    return arcname


def backup_user_files(
    prefix_path: Path,
    dest_path: Path,
    *,
    app_id: str | None = None,
    progress_cb: Callable[[float], None] | None = None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> int:
    """Archive user-modified and user-created files as a ``.tar.zst``.

    Uses the install manifest to identify files that changed since
    installation (modified package files + user-created files) and packs
    them into a zstandard-compressed tar at *dest_path*.  Files matching
    ``backup_exclude.txt`` are skipped.

    When *app_id* is provided the ``drive_c/`` root inside the archive
    is renamed to ``<app_id>/`` so the archive is self-describing and
    can be imported back by app slug.

    Returns the number of files archived (0 means nothing to back up).
    """
    from cellar.backend.manifest import scan_user_files  # noqa: PLC0415

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if _cancelled():
        raise UpdateCancelled

    if phase_cb:
        phase_cb("Scanning for user files\u2026")
    if progress_cb:
        progress_cb(0.0)

    modified, user_created = scan_user_files(prefix_path)
    all_files = [
        fp for fp in modified + user_created
        if not is_backup_excluded(fp.relative_to(prefix_path))
    ]
    if not all_files:
        return 0

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    file_sizes = {}
    for fp in all_files:
        try:
            file_sizes[fp] = fp.stat().st_size
        except OSError:
            file_sizes[fp] = 0
    total_bytes = max(sum(file_sizes.values()), 1)
    bytes_done = 0
    t_start = time.monotonic()
    t_last_stats = t_start

    try:
        import zstandard  # noqa: PLC0415

        if phase_cb:
            phase_cb("Backing up user files\u2026")

        cctx = zstandard.ZstdCompressor(level=3)
        with open(dest_path, "wb") as fh:
            with cctx.stream_writer(fh, closefd=False) as zfh:
                with tarfile.open(fileobj=zfh, mode="w|") as tf:
                    for fp in all_files:
                        if _cancelled():
                            raise UpdateCancelled
                        if not fp.is_file():
                            continue
                        arcname = fp.relative_to(prefix_path).as_posix()
                        if app_id:
                            arcname = _remap_archive_root(arcname, app_id)
                        tf.add(fp, arcname=arcname)
                        bytes_done += file_sizes.get(fp, 0)
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
        raise UpdateError(f"User file backup failed: {exc}") from exc

    if progress_cb:
        progress_cb(1.0)
    if stats_cb:
        elapsed = time.monotonic() - t_start
        stats_cb(total_bytes, total_bytes, total_bytes / elapsed if elapsed > 0 else 0.0)

    log.info("Backed up %d user files to %s", len(all_files), dest_path)
    return len(all_files)


def import_user_files(
    archive_path: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[str, int]:
    """Restore a user-file backup into the matching installed prefix.

    The archive's top-level directory is the app slug (written by
    ``backup_user_files``).  The slug is looked up in the installed-apps
    database to find the target prefix.  Files are extracted into
    ``<prefix>/drive_c/`` — the slug root is remapped back to
    ``drive_c/``.

    Returns ``(app_id, file_count)``.

    Raises ``UpdateError`` if the slug doesn't match an installed app
    or the archive is malformed.
    """
    from cellar.backend.database import get_installed  # noqa: PLC0415
    import zstandard  # noqa: PLC0415

    def _cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    if _cancelled():
        raise UpdateCancelled

    if phase_cb:
        phase_cb("Reading archive\u2026")
    if progress_cb:
        progress_cb(0.0)

    # Peek at the archive to discover the app slug (top-level dir name).
    dctx = zstandard.ZstdDecompressor()
    with open(archive_path, "rb") as fh:
        with dctx.stream_reader(fh) as zfh:
            with tarfile.open(fileobj=zfh, mode="r|") as tf:
                first = tf.next()
                if first is None:
                    raise UpdateError("Backup archive is empty")
                app_id = first.name.split("/")[0]

    if not app_id:
        raise UpdateError("Could not determine app slug from archive")

    row = get_installed(app_id)
    if row is None:
        raise UpdateError(
            f"No installed app matches slug \u201c{app_id}\u201d. "
            "Install the app first, then import the backup."
        )

    prefix_path = Path(row["prefix_dir"])
    if not prefix_path.is_dir():
        raise UpdateError(f"Prefix directory not found: {prefix_path}")

    if phase_cb:
        phase_cb("Restoring user files\u2026")

    archive_size = archive_path.stat().st_size
    file_count = 0
    t_start = time.monotonic()
    t_last_stats = t_start
    slug_prefix = app_id + "/"

    try:
        with open(archive_path, "rb") as fh:
            with dctx.stream_reader(fh) as zfh:
                with tarfile.open(fileobj=zfh, mode="r|") as tf:
                    for member in tf:
                        if _cancelled():
                            raise UpdateCancelled
                        # Remap slug/ back to drive_c/
                        if member.name.startswith(slug_prefix):
                            member.name = "drive_c/" + member.name[len(slug_prefix):]
                        elif member.name == app_id:
                            member.name = "drive_c"
                        tf.extract(member, path=prefix_path, filter="data")
                        if member.isfile():
                            file_count += 1
                        now = time.monotonic()
                        if now - t_last_stats >= 0.1:
                            t_last_stats = now
                            bytes_done = fh.tell()
                            elapsed = now - t_start
                            speed = bytes_done / elapsed if elapsed > 0 else 0.0
                            if progress_cb:
                                progress_cb(bytes_done / archive_size)
                            if stats_cb:
                                stats_cb(bytes_done, archive_size, speed)
    except UpdateCancelled:
        raise
    except Exception as exc:
        raise UpdateError(f"Import failed: {exc}") from exc

    if progress_cb:
        progress_cb(1.0)
    if stats_cb:
        elapsed = time.monotonic() - t_start
        stats_cb(archive_size, archive_size,
                 archive_size / elapsed if elapsed > 0 else 0.0)

    log.info("Imported %d user files for %s from %s", file_count, app_id, archive_path)
    return app_id, file_count


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
    the existing prefix.  Files absent from the delta are left untouched
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
        The existing installed prefix directory.
    backup_path:
        When supplied, the current prefix is archived here before the
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
        backup_prefix(
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
    from cellar.backend.config import install_data_dir  # noqa: PLC0415
    _tmp_root = install_data_dir()
    with tempfile.TemporaryDirectory(prefix="cellar-stash-",
                                     dir=_tmp_root) as stash_tmp_str:
        stash_dir = Path(stash_tmp_str)

        if files_to_stash:
            if phase_cb:
                phase_cb("Preserving user data\u2026")
            _stash_files(files_to_stash, prefix_path, stash_dir)

        with tempfile.TemporaryDirectory(prefix="cellar-update-",
                                         dir=_tmp_root) as tmp_str:
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
                    _find_top_dir,
                    _install_chunks,
                    _stream_and_extract,
                )
            except ImportError as exc:
                raise UpdateError(f"Internal error: {exc}") from exc

            extract_dir = tmp / "extracted"
            extract_dir.mkdir()
            try:
                if entry.archive_chunks:
                    _install_chunks(
                        archive_uri,
                        entry.archive_chunks,
                        extract_dir,
                        cancel_event=cancel_event,
                        progress_cb=_sub(dl_lo, ext_hi),
                        stats_cb=stats_cb,
                        token=token,
                    )
                else:
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
                content_src = _find_top_dir(extract_dir)
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
            _overlay(
                content_src,
                prefix_path,
                progress_cb=_sub(ov_lo, 1.0),
                cancel_event=cancel_event,
            )

            # ── Phase 6: Apply delete manifest ───────────────────────────────
            # The .cellar_delete manifest lists files that were present in the
            # previous version but removed in this one.
            delete_manifest = content_src / ".cellar_delete"
            if delete_manifest.exists():
                resolved_prefix = prefix_path.resolve()
                for line in delete_manifest.read_text().splitlines():
                    rel = line.strip()
                    if not rel or _is_excluded(Path(rel)):
                        continue
                    target = (prefix_path / rel).resolve()
                    try:
                        target.relative_to(resolved_prefix)
                    except ValueError:
                        log.warning(
                            "Skipping out-of-prefix delete entry: %r", rel,
                        )
                        continue
                    target.unlink(missing_ok=True)

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
    progress_cb: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> None:
    """Overlay *src* onto *dst*, skipping excluded user-data paths.

    Only files present in *src* are copied — no destination-only files are
    removed here; deletion is handled separately via the ``.cellar_delete``
    manifest after this call returns.

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
    """Run rsync overlay with user-data exclusions.

    Only files present in *src* are copied; the ``.cellar_delete`` manifest
    handles explicit removals separately.
    """
    cmd = ["rsync", "-a"]
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

    deadline = time.monotonic() + 3600  # 1 hour wall-clock limit
    while True:
        if cancel_event and cancel_event.is_set():
            proc.kill()
            proc.wait()
            raise UpdateCancelled
        if time.monotonic() > deadline:
            proc.kill()
            proc.wait()
            raise UpdateError("rsync timed out after 1 hour")
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
    """Pure-Python overlay copy with user-data exclusions.

    Only files present in *src* are copied; deletion is handled separately
    via the ``.cellar_delete`` manifest.
    """
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
    """Return True if *rel* (relative to prefix root) should be skipped."""
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



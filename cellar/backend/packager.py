"""Packaging helpers for writing apps into a local Cellar repo.

This module handles:
- Reading ``bottle.yml`` from a Bottles backup archive
- Generating URL-safe app IDs from human names
- Writing a complete archive + images + catalogue entry into a repo
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import zlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable

from cellar.utils.images import optimize_image as _optimize_image


def _rmtree(path, ignore_errors: bool = False) -> None:
    """Remove a directory tree; works for both :class:`pathlib.Path` and
    :class:`~cellar.utils.smb.SmbPath`."""
    from cellar.utils.smb import SmbPath
    if isinstance(path, SmbPath):
        try:
            path.rmtree()
        except Exception:
            if not ignore_errors:
                raise
    else:
        shutil.rmtree(path, ignore_errors=ignore_errors)


# ---------------------------------------------------------------------------
# Category constants
# ---------------------------------------------------------------------------

#: Built-in categories always present in every repo.  Custom user-defined
#: categories are stored in the ``categories`` key of ``catalogue.json`` and
#: merged with this list at read time.
BASE_CATEGORIES: list[str] = ["Games", "Productivity", "Graphics", "Utility"]

#: Default symbolic icon name for each built-in category.
BASE_CATEGORY_ICONS: dict[str, str] = {
    "Games": "input-gaming-symbolic",
    "Productivity": "document-edit-symbolic",
    "Graphics": "applications-graphics-symbolic",
    "Utility": "applications-utilities-symbolic",
}

#: Curated set of symbolic icon names offered in the category icon picker.
#: All icons are from the standard Adwaita theme.
CATEGORY_ICON_OPTIONS: list[str] = [
    "input-gaming-symbolic",
    "applications-graphics-symbolic",
    "document-edit-symbolic",
    "applications-utilities-symbolic",
    "audio-x-generic-symbolic",
    "video-x-generic-symbolic",
    "mail-unread-symbolic",
    "applications-science-symbolic",
    "image-x-generic-symbolic",
    "application-x-executable-symbolic",
    "applications-system-symbolic",
    "folder-symbolic",
    "camera-photo-symbolic",
    "media-optical-cd-symbolic",
    "accessories-text-editor-symbolic",
    "applications-multimedia-symbolic",
]


# ---------------------------------------------------------------------------
# bottle.yml extraction
# ---------------------------------------------------------------------------

def read_bottle_yml(archive_path: str) -> dict:
    """Extract and parse ``bottle.yml`` from a Bottles ``.tar.gz`` backup.

    Tries the system ``tar`` binary first with ``--occurrence=1`` so it stops
    reading immediately after extracting the first match.  Without that flag
    tar scans the entire archive for further matches, which blocks
    ``subprocess.run`` for 20+ seconds on a 2 GB archive.  Falls back to
    Python ``tarfile`` when ``tar`` is not available (e.g. inside a
    restricted Flatpak sandbox).

    Returns an empty dict if the file is not found or cannot be parsed.
    """
    import yaml

    # Fast path: system tar with --occurrence=1 stops after the first match.
    # Without --occurrence, tar reads the entire archive looking for further
    # matches even after printing the file — subprocess.run blocks until exit.
    if shutil.which("tar"):
        try:
            result = subprocess.run(
                ["tar", "-xOf", str(archive_path),
                 "--wildcards", "--occurrence=1", "*/bottle.yml"],
                capture_output=True,
                timeout=120,
            )
            if result.returncode == 0 and result.stdout:
                data = yaml.safe_load(result.stdout)
                return data if isinstance(data, dict) else {}
        except Exception:
            pass  # fall through to Python path

    # Python fallback (no system tar / wildcard support).
    try:
        with open(archive_path, "rb") as raw:
            with tarfile.open(fileobj=raw, mode="r:gz") as tf:
                for member in tf:
                    if member.name == "bottle.yml" or member.name.endswith("/bottle.yml"):
                        f = tf.extractfile(member)
                        if f:
                            data = yaml.safe_load(f.read())
                            return data if isinstance(data, dict) else {}
    except (tarfile.TarError, OSError, yaml.YAMLError):
        pass
    return {}


# ---------------------------------------------------------------------------
# Archive member listing (for Linux app entry-point picker)
# ---------------------------------------------------------------------------

def list_archive_members(archive_path: str) -> list[tuple[str, bool]]:
    """Return ``(path, is_executable)`` for every regular file in the archive.

    *path* is the member path exactly as stored in the archive (including any
    top-level directory prefix).  *is_executable* is ``True`` when the Unix
    permission bits in the tar header include any execute bit.

    Supports ``.tar.gz``, ``.tar.bz2``, ``.tar.xz``, and ``.tar.zst``
    (zstandard package required for the last format).  Returns an empty list
    on any error.

    Uses the system ``tar -tvf`` fast path when available so large archives
    are read without decompressing the whole stream in Python.
    """
    # Fast path: system tar is much faster for large archives because it is
    # implemented in C and avoids Python's per-member overhead.
    if shutil.which("tar"):
        try:
            result = subprocess.run(
                ["tar", "--list", "--verbose", "--file", archive_path],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode == 0:
                members: list[tuple[str, bool]] = []
                for line in result.stdout.splitlines():
                    # Verbose line: "-rwxr-xr-x user/group size date time name"
                    parts = line.split(None, 5)
                    if len(parts) < 6:
                        continue
                    perms = parts[0]
                    # Only include regular files (perms[0] == '-')
                    if not perms or perms[0] != "-":
                        continue
                    name = parts[5]
                    # Strip trailing " -> target" for symlinks that sneak through
                    name = name.split(" -> ")[0]
                    is_exec = len(perms) > 3 and perms[3] == "x"
                    members.append((name, is_exec))
                return members
        except Exception:
            pass  # fall through to Python path

    # Python tarfile fallback.
    try:
        if archive_path.endswith(".tar.zst"):
            try:
                import zstandard as zstd  # noqa: PLC0415
                dctx = zstd.ZstdDecompressor()
                with open(archive_path, "rb") as raw:
                    with dctx.stream_reader(raw) as decompressed:
                        with tarfile.open(fileobj=decompressed, mode="r|") as tf:
                            return [
                                (m.name, bool(m.mode & 0o111))
                                for m in tf.getmembers()
                                if m.isfile()
                            ]
            except ImportError:
                return []
        else:
            with tarfile.open(archive_path, "r:*") as tf:
                return [
                    (m.name, bool(m.mode & 0o111))
                    for m in tf.getmembers()
                    if m.isfile()
                ]
    except tarfile.TarError:
        return []


# ---------------------------------------------------------------------------
# Directory compression
# ---------------------------------------------------------------------------

class _CRCWriter:
    """Wraps a writable file object and accumulates CRC32 of all written bytes."""

    __slots__ = ("_fp", "crc")

    def __init__(self, fp):
        self._fp = fp
        self.crc = 0

    def write(self, data: bytes) -> int:
        self.crc = zlib.crc32(data, self.crc)
        return self._fp.write(data)


def compress_directory_zst(
    src_dir: Path,
    dest_path: Path,
    *,
    cancel_event=None,
    progress_cb: Callable[[float], None] | None = None,
    stats_cb: Callable[[int, int, float], None] | None = None,
) -> tuple[int, str]:
    """Compress *src_dir* into a ``.tar.zst`` archive at *dest_path*.

    Uses zstandard level 3 (fast compression, good ratio).  Preserves Unix
    permissions and symlinks via ``tarfile``.

    Returns ``(size_bytes, crc32_hex)`` where *crc32_hex* is the CRC32 of the
    compressed archive as an 8-character lowercase hex string.
    Raises ``CancelledError`` if *cancel_event* is set during compression.

    *stats_cb*, when provided, is called as ``stats_cb(done_bytes, total_bytes,
    speed_bps)`` each time a file is added, where values are in uncompressed
    source bytes.
    """
    import zstandard as zstd  # noqa: PLC0415

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Single os.walk to gather both file count and total uncompressed size.
    total_files = 0
    total_bytes = 0
    for dirpath, _, filenames in os.walk(src_dir):
        for fn in filenames:
            total_files += 1
            try:
                total_bytes += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass

    done = [0]
    done_bytes = [0]
    start = time.monotonic()

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Compression cancelled by user")
        if ti.isfile():
            done[0] += 1
            done_bytes[0] += ti.size
            if progress_cb and total_files:
                progress_cb(done[0] / total_files)
            if stats_cb:
                elapsed = time.monotonic() - start
                speed = done_bytes[0] / elapsed if elapsed > 0.1 else 0.0
                stats_cb(done_bytes[0], total_bytes, speed)
        return ti

    cctx = zstd.ZstdCompressor(level=3)
    try:
        with dest_path.open("wb") as out_f:
            crc_writer = _CRCWriter(out_f)
            with cctx.stream_writer(crc_writer, closefd=False) as compressor:
                with tarfile.open(fileobj=compressor, mode="w|") as tf:
                    tf.add(str(src_dir), arcname=src_dir.name, recursive=True, filter=_filter)
            crc32_hex = format(crc_writer.crc & 0xFFFFFFFF, "08x")
    except CancelledError:
        dest_path.unlink(missing_ok=True)
        raise
    except (tarfile.TarError, OSError) as exc:
        dest_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to compress directory: {exc}") from exc

    return dest_path.stat().st_size, crc32_hex


def compress_prefix_zst(
    prefix_path: Path,
    dest_path: Path,
    *,
    cancel_event=None,
    progress_cb: Callable[[float], None] | None = None,
    stats_cb: Callable[[int, int, float], None] | None = None,
) -> tuple[int, str]:
    """Archive *prefix_path* as a Cellar-native ``prefix/``-rooted ``.tar.zst``.

    The top-level directory in the archive is always ``prefix/`` regardless of
    the actual directory name.  Symlinks under ``drive_c/users/`` (home-dir
    pointers) are stripped — umu/Proton recreates them on first launch.

    *stats_cb*, when provided, is called as ``stats_cb(done_files, total_files,
    speed_bps)`` on each file so the UI can show file-count / throughput text.

    Returns ``(size_bytes, crc32_hex)``.
    """
    import zstandard as zstd  # noqa: PLC0415

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    total_files = 0
    total_bytes = 0
    for dirpath, _, filenames in os.walk(prefix_path):
        for fn in filenames:
            total_files += 1
            try:
                total_bytes += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass

    done = [0]
    done_bytes = [0]
    start = time.monotonic()

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Cancelled")
        # Strip symlinks under drive_c/users/ (per-user home dir pointers)
        if (ti.issym() or ti.islnk()) and "drive_c/users/" in ti.name:
            return None
        if ti.isfile():
            done[0] += 1
            done_bytes[0] += ti.size
            if progress_cb and total_files:
                progress_cb(done[0] / total_files)
            if stats_cb:
                elapsed = time.monotonic() - start
                speed = done_bytes[0] / elapsed if elapsed > 0.1 else 0.0
                stats_cb(done[0], total_files, speed)
        return ti

    cctx = zstd.ZstdCompressor(level=3)
    try:
        with dest_path.open("wb") as out_f:
            crc_writer = _CRCWriter(out_f)
            with cctx.stream_writer(crc_writer, closefd=False) as compressor:
                with tarfile.open(fileobj=compressor, mode="w|") as tf:
                    tf.add(str(prefix_path), arcname="prefix", recursive=True, filter=_filter)
            crc32_hex = format(crc_writer.crc & 0xFFFFFFFF, "08x")
    except CancelledError:
        dest_path.unlink(missing_ok=True)
        raise
    except (tarfile.TarError, OSError) as exc:
        dest_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to compress prefix: {exc}") from exc

    return dest_path.stat().st_size, crc32_hex


def compress_prefix_delta_zst(
    prefix_path: Path,
    base_dir: Path,
    dest_path: Path,
    *,
    cancel_event=None,
    phase_cb: Callable[[str], None] | None = None,
    progress_cb: Callable[[float], None] | None = None,
    stats_cb: Callable[[int, int, float], None] | None = None,
) -> tuple[int, str]:
    """Create a delta ``.tar.zst`` from *prefix_path* relative to *base_dir*.

    Works directly on the prefix directory — no intermediate full archive.
    Files identical to *base_dir* (same relative path, same BLAKE2b-128 hash)
    are excluded.  A ``.cellar_delete`` manifest lists base files absent from
    the prefix so the installer can remove them after seeding.

    Symlinks under ``drive_c/users/`` are stripped (same as the full archive).
    The archive is ``prefix/``-rooted, compatible with :func:`compress_prefix_zst`
    and the installer's :func:`~cellar.backend.installer._overlay_delta`.

    Progress is split across two phases:
    - Scan (0 → 0.5): hash-compare every file in the prefix against the base.
    - Pack  (0.5 → 1.0): stream changed/new files into the archive.

    *stats_cb* is called as ``stats_cb(done_files, total_files, speed_bps)``
    during the pack phase.

    Returns ``(delta_uncompressed_bytes, crc32_hex)``.
    """
    import hashlib
    import io
    import zstandard as zstd  # noqa: PLC0415

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    def _chk() -> None:
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Delta creation cancelled")

    def _hash_file(path: Path) -> str:
        h = hashlib.blake2b(digest_size=16)
        with open(path, "rb") as f:
            while chunk := f.read(1 * 1024 * 1024):
                _chk()
                h.update(chunk)
        return h.hexdigest()

    def _should_strip(rel: str) -> bool:
        """True for drive_c/users/ symlinks that umu recreates on first launch."""
        return "drive_c/users/" in rel

    # ── Phase 1: Scan — identify delta files and delete manifest ────────────
    if phase_cb:
        phase_cb("Scanning files\u2026")

    all_src = []
    for src in prefix_path.rglob("*"):
        if not src.is_file() or src.is_symlink():
            continue
        rel = str(src.relative_to(prefix_path))
        if _should_strip(rel):
            continue
        all_src.append((src, rel))

    total_scan = len(all_src)
    delta_files: list[tuple[Path, str]] = []   # (src, rel) pairs

    for i, (src, rel) in enumerate(all_src, 1):
        _chk()
        base_file = base_dir / rel
        if base_file.is_file():
            try:
                if _hash_file(src) == _hash_file(base_file):
                    if progress_cb and total_scan:
                        progress_cb(i / total_scan * 0.5)
                    continue  # identical — exclude from delta
            except CancelledError:
                raise
            except OSError:
                pass  # unreadable → include defensively
        delta_files.append((src, rel))
        if progress_cb and total_scan:
            progress_cb(i / total_scan * 0.5)

    # Files in base absent from prefix → delete manifest.
    delete_paths = sorted(
        str(p.relative_to(base_dir))
        for p in base_dir.rglob("*")
        if p.is_file() and not p.is_symlink()
        and not (prefix_path / p.relative_to(base_dir)).exists()
    )

    # ── Phase 2: Pack delta files into archive ───────────────────────────────
    if phase_cb:
        phase_cb("Compressing and uploading\u2026")

    delta_uncompressed = sum(src.stat().st_size for src, _ in delta_files)
    total_pack = len(delta_files)
    done = [0]
    done_bytes = [0]
    start = time.monotonic()

    cctx = zstd.ZstdCompressor(level=3)
    try:
        with dest_path.open("wb") as out_f:
            crc_writer = _CRCWriter(out_f)
            with cctx.stream_writer(crc_writer, closefd=False) as compressor:
                with tarfile.open(fileobj=compressor, mode="w|") as tf:
                    for src, rel in sorted(delta_files, key=lambda t: t[1]):
                        _chk()
                        tf.add(str(src), arcname=f"prefix/{rel}", recursive=False)
                        done[0] += 1
                        done_bytes[0] += src.stat().st_size
                        if progress_cb and total_pack:
                            progress_cb(0.5 + done[0] / total_pack * 0.5)
                        if stats_cb:
                            elapsed = time.monotonic() - start
                            speed = done_bytes[0] / elapsed if elapsed > 0.1 else 0.0
                            stats_cb(done[0], total_pack, speed)
                    # Delete manifest
                    if delete_paths:
                        manifest = "\n".join(delete_paths).encode()
                        ti = tarfile.TarInfo(name="prefix/.cellar_delete")
                        ti.size = len(manifest)
                        tf.addfile(ti, io.BytesIO(manifest))
            crc32_hex = format(crc_writer.crc & 0xFFFFFFFF, "08x")
    except CancelledError:
        dest_path.unlink(missing_ok=True)
        raise
    except (tarfile.TarError, OSError) as exc:
        dest_path.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to create delta archive: {exc}") from exc

    return delta_uncompressed, crc32_hex


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Convert a human name to a URL-safe app ID.

    Examples::

        slugify("Notepad++")  → "notepad-plus-plus"
        slugify("My App")     → "my-app"
        slugify("Half-Life 2") → "half-life-2"
    """
    slug = name.lower()
    # Replace runs of '+' with '-plus' (one '-plus' per '+')
    slug = re.sub(r"\++", lambda m: "-plus" * len(m.group()), slug)
    # Replace any remaining non-alphanumeric runs with a single '-'
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    # Collapse multiple dashes, strip leading/trailing
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "app"


# ---------------------------------------------------------------------------
# Repo write
# ---------------------------------------------------------------------------

def import_to_repo(
    repo_root: Path,
    entry,                      # AppEntry — avoid circular import; checked by caller
    archive_src: str,
    images: dict,
    *,
    progress_cb: Callable[[float], None] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    cancel_event=None,          # threading.Event — checked during archive copy
    archive_in_place: bool = False,
) -> None:
    """Copy archive + images into *repo_root* and update ``catalogue.json``.

    *images* is a dict with optional keys:
      ``"icon"``, ``"cover"``, ``"hero"`` → str path
      ``"screenshots"`` → list[str] paths

    *progress_cb* receives a float in [0, 1] during the archive copy phase.
    *phase_cb*, when provided, is called as ``phase_cb(label)`` when the
    operation phase changes (e.g. "Copying archive…", "Writing catalogue…").
    *stats_cb*, when provided, is called as ``stats_cb(copied, total, speed_bps)``
    during the archive copy so the UI can show size/speed text.
    *cancel_event* is a ``threading.Event``; when set the copy is aborted and
    the partial destination file is removed.
    *archive_in_place*, when ``True``, skips the archive copy: the file is
    assumed to already exist at ``repo_root / entry.archive`` and
    ``entry.archive_crc32`` must already be set.
    """
    app_dir = repo_root / "apps" / entry.id
    app_dir.mkdir(parents=True, exist_ok=True)

    if not archive_in_place:
        # ── Archive copy (chunked for progress reporting + CRC32) ──────────────
        if phase_cb:
            phase_cb("Copying archive\u2026")
        archive_dest = repo_root / entry.archive
        archive_dest.parent.mkdir(parents=True, exist_ok=True)
        src_size = Path(archive_src).stat().st_size
        chunk = 1 * 1024 * 1024  # 1 MB
        copied = 0
        crc = 0
        start = time.monotonic()
        try:
            with open(archive_src, "rb") as src_f, archive_dest.open("wb") as dst_f:
                while True:
                    if cancel_event and cancel_event.is_set():
                        dst_f.close()
                        archive_dest.unlink(missing_ok=True)
                        raise CancelledError("Import cancelled by user")
                    buf = src_f.read(chunk)
                    if not buf:
                        break
                    dst_f.write(buf)
                    crc = zlib.crc32(buf, crc)
                    copied += len(buf)
                    elapsed = time.monotonic() - start
                    speed = copied / elapsed if elapsed > 0.1 else 0.0
                    if stats_cb and src_size > 0:
                        stats_cb(copied, src_size, speed)
                    if progress_cb and src_size > 0:
                        progress_cb(min(copied / src_size * 0.9, 0.9))
        except CancelledError:
            raise
        except OSError as exc:
            archive_dest.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to copy archive: {exc}") from exc

        entry = replace(entry, archive_crc32=format(crc & 0xFFFFFFFF, "08x"))

    # ── Single images (icon, cover, hero, logo) ───────────────────────────
    for key in ("icon", "cover", "hero", "logo"):
        src = images.get(key)
        if src and getattr(entry, key):
            dest = repo_root / getattr(entry, key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            _optimize_image(src, dest, key)

    # ── Screenshots ───────────────────────────────────────────────────────
    ss_dir = app_dir / "screenshots"
    for i, src in enumerate(images.get("screenshots", []), 1):
        ss_dir.mkdir(exist_ok=True)
        _optimize_image(src, ss_dir / f"{i:02d}{Path(src).suffix}", "screenshot")

    # ── catalogue.json ────────────────────────────────────────────────────
    if phase_cb:
        phase_cb("Writing catalogue\u2026")
    _upsert_catalogue(repo_root, entry)

    if progress_cb:
        progress_cb(1.0)


def update_in_repo(
    repo_root: Path,
    old_entry,                          # AppEntry
    new_entry,                          # AppEntry
    images: dict,                       # {"icon": path|None, "cover": path|None, ...}
    new_archive_src: str | None = None,
    *,
    progress_cb: Callable[[float], None] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    cancel_event=None,
) -> None:
    """Update an existing entry in *repo_root*.

    - If *new_archive_src* is given, the archive is replaced (chunked copy,
      old file removed if the path changed and the file still exists).
    - Only image keys with a non-empty value in *images* are overwritten.
    - Screenshots are fully replaced when *images["screenshots"]* is non-empty.
    - ``catalogue.json`` is updated in place (matched by ID).
    """
    app_dir = repo_root / "apps" / new_entry.id
    app_dir.mkdir(parents=True, exist_ok=True)

    # ── Archive (optional replacement + CRC32) ──────────────────────────────
    if new_archive_src:
        if phase_cb:
            phase_cb("Copying archive\u2026")
        archive_dest = repo_root / new_entry.archive
        archive_dest.parent.mkdir(parents=True, exist_ok=True)
        src_size = Path(new_archive_src).stat().st_size
        chunk = 1 * 1024 * 1024
        copied = 0
        crc = 0
        start = time.monotonic()
        try:
            with open(new_archive_src, "rb") as src_f, archive_dest.open("wb") as dst_f:
                while True:
                    if cancel_event and cancel_event.is_set():
                        dst_f.close()
                        archive_dest.unlink(missing_ok=True)
                        raise CancelledError("Update cancelled by user")
                    buf = src_f.read(chunk)
                    if not buf:
                        break
                    dst_f.write(buf)
                    crc = zlib.crc32(buf, crc)
                    copied += len(buf)
                    elapsed = time.monotonic() - start
                    speed = copied / elapsed if elapsed > 0.1 else 0.0
                    if stats_cb and src_size > 0:
                        stats_cb(copied, src_size, speed)
                    if progress_cb and src_size > 0:
                        progress_cb(min(copied / src_size * 0.9, 0.9))
        except CancelledError:
            raise
        except OSError as exc:
            archive_dest.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to copy archive: {exc}") from exc

        new_entry = replace(new_entry, archive_crc32=format(crc & 0xFFFFFFFF, "08x"))

        # Remove the old archive file if its path changed
        old_archive = repo_root / old_entry.archive
        if old_archive != archive_dest and old_archive.exists():
            old_archive.unlink(missing_ok=True)

    # ── Single images (icon, cover, hero, logo) ───────────────────────────
    for key in ("icon", "cover", "hero", "logo"):
        src = images.get(key)
        if src and getattr(new_entry, key):
            dest = repo_root / getattr(new_entry, key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            _optimize_image(src, dest, key)

    # ── Screenshots ───────────────────────────────────────────────────────
    # None = keep existing, [] = clear all, [...] = replace
    new_screenshots = images.get("screenshots")
    if new_screenshots is not None:
        ss_dir = repo_root / "apps" / new_entry.id / "screenshots"
        if ss_dir.exists():
            _rmtree(ss_dir)
        if new_screenshots:
            ss_dir.mkdir(parents=True, exist_ok=True)
            for i, src in enumerate(new_screenshots, 1):
                _optimize_image(src, ss_dir / f"{i:02d}{Path(src).suffix}", "screenshot")

    # ── catalogue.json ────────────────────────────────────────────────────
    if phase_cb:
        phase_cb("Writing catalogue\u2026")
    _upsert_catalogue(repo_root, new_entry)

    if progress_cb:
        progress_cb(1.0)


def remove_from_repo(
    repo_root: Path,
    entry,                          # AppEntry
    *,
    move_archive_to: str | None = None,
    cancel_event=None,
) -> None:
    """Remove *entry* from *repo_root*.

    If *move_archive_to* is set and the archive file exists, it is moved to
    that directory before the rest of the app directory is deleted.
    """
    archive_file = repo_root / entry.archive

    if move_archive_to and archive_file.exists():
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Delete cancelled by user")
        shutil.move(str(archive_file), Path(move_archive_to) / archive_file.name)

    if cancel_event and cancel_event.is_set():
        raise CancelledError("Delete cancelled by user")

    _rmtree(repo_root / "apps" / entry.id, ignore_errors=True)

    if cancel_event and cancel_event.is_set():
        raise CancelledError("Delete cancelled by user")

    _remove_from_catalogue(repo_root, entry.id)


# ---------------------------------------------------------------------------
# catalogue.json helpers (shared by import / update / remove)
# ---------------------------------------------------------------------------

def _upsert_catalogue(repo_root: Path, entry) -> None:
    """Replace or append *entry* in ``catalogue.json``."""
    cat_path = repo_root / "catalogue.json"
    categories: list[str] | None = None
    bases: dict | None = None
    category_icons: dict[str, str] | None = None
    if cat_path.exists():
        raw = json.loads(cat_path.read_text())
        apps = raw.get("apps", raw) if isinstance(raw, dict) else raw
        if isinstance(raw, dict):
            categories = raw.get("categories")
            bases = raw.get("bases")
            category_icons = raw.get("category_icons")
    else:
        apps = []
    apps = [a for a in apps if a.get("id") != entry.id]
    apps.append(entry.to_dict())
    # Auto-register custom category into the top-level categories list
    category = entry.category if hasattr(entry, "category") else ""
    if category and category not in BASE_CATEGORIES:
        if categories is None:
            categories = []
        if category not in categories:
            categories.append(category)
    _write_catalogue(cat_path, apps, categories, bases, category_icons)


def _remove_from_catalogue(repo_root: Path, app_id: str) -> None:
    """Filter *app_id* out of ``catalogue.json``."""
    cat_path = repo_root / "catalogue.json"
    if not cat_path.exists():
        return
    raw = json.loads(cat_path.read_text())
    apps = raw.get("apps", raw) if isinstance(raw, dict) else raw
    categories = raw.get("categories") if isinstance(raw, dict) else None
    bases = raw.get("bases") if isinstance(raw, dict) else None
    category_icons = raw.get("category_icons") if isinstance(raw, dict) else None
    apps = [a for a in apps if a.get("id") != app_id]
    _write_catalogue(cat_path, apps, categories, bases, category_icons)


def _write_catalogue(
    cat_path: Path,
    apps: list,
    categories: list[str] | None = None,
    bases: dict | None = None,
    category_icons: dict[str, str] | None = None,
) -> None:
    data: dict = {
        "cellar_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apps": apps,
    }
    if categories is not None:
        data["categories"] = categories
    if bases is not None:
        data["bases"] = bases
    if category_icons is not None:
        data["category_icons"] = category_icons
    cat_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def upsert_base(
    repo_root: Path,
    runner: str,
    archive_path: str,
    archive_crc32: str = "",
    archive_size: int = 0,
) -> None:
    """Add or replace a base image entry in ``catalogue.json``.

    *archive_path* must be a repo-relative path (e.g.
    ``"bases/soda-9.0-1-base.tar.gz"``).  The physical archive must already
    have been copied to the repo before calling this.
    """
    cat_path = repo_root / "catalogue.json"
    if cat_path.exists():
        raw = json.loads(cat_path.read_text())
        apps = raw.get("apps", []) if isinstance(raw, dict) else []
        categories = raw.get("categories") if isinstance(raw, dict) else None
        bases: dict = dict(raw.get("bases") or {})
        category_icons = raw.get("category_icons") if isinstance(raw, dict) else None
    else:
        apps, categories, bases, category_icons = [], None, {}, None
    bases[runner] = {"archive": archive_path}
    if archive_size:
        bases[runner]["archive_size"] = archive_size
    if archive_crc32:
        bases[runner]["archive_crc32"] = archive_crc32
    _write_catalogue(cat_path, apps, categories, bases, category_icons)


def remove_base(repo_root: Path, runner: str) -> None:
    """Remove a base image entry from ``catalogue.json``."""
    cat_path = repo_root / "catalogue.json"
    if not cat_path.exists():
        return
    raw = json.loads(cat_path.read_text())
    if not isinstance(raw, dict):
        return
    apps = raw.get("apps", [])
    categories = raw.get("categories")
    bases = dict(raw.get("bases") or {})
    category_icons = raw.get("category_icons")
    bases.pop(runner, None)
    _write_catalogue(cat_path, apps, categories, bases if bases else None, category_icons)


def create_delta_archive(
    full_archive_path: str | Path,
    base_dir: Path,
    dest: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    file_cb: Callable[[int, int], None] | None = None,
    cancel_event: Event | None = None,
) -> tuple[int, str]:
    """Create a delta ``.tar.zst`` from a full Bottles backup, relative to *base_dir*.

    Extracts *full_archive_path* to a temp directory, identifies files that
    differ from *base_dir* by content hash, and packs only those files into a
    new zstd-compressed archive at *dest*.

    The result has the same top-level bottle directory name as the original and
    is suitable for :func:`~cellar.backend.installer._overlay_delta` at install
    time.  *phase_cb* is called with a human-readable step label at each major
    phase ("Extracting archive…", "Scanning files…", "Compressing & Uploading delta…").
    *progress_cb* emits 0→1 **per phase** (not across the whole operation), so
    callers can reset their progress bar on each *phase_cb* transition.
    Extraction emits no *progress_cb* calls (duration is unknown from a
    streaming gzip); only *file_cb* is called during extraction.
    *file_cb* is called as ``file_cb(current, total)`` for each file processed;
    *total* is 0 when the count is not known in advance.

    Returns ``(uncompressed_size, crc32_hex)`` where *uncompressed_size* is
    the total size of the delta content in bytes (additional disk space beyond
    the already-installed base image, suitable for ``install_size_estimate``)
    and *crc32_hex* is the CRC32 of the compressed archive file as an
    8-character lowercase hex string.

    Raises :exc:`RuntimeError` on failure.
    """
    import tempfile

    full_archive_path = Path(full_archive_path)

    # Use ~/.cache/cellar for extraction — /tmp is typically a small tmpfs
    # (often 8 GB) that cannot hold a multi-GB extracted bottle.
    cache_base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "cellar"
    cache_base.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cellar-delta-", dir=cache_base) as tmp_str:
        tmp = Path(tmp_str)
        extract_dir = tmp / "full"
        extract_dir.mkdir()
        delta_dir = tmp / "delta"
        delta_dir.mkdir()

        def _chk() -> None:
            if cancel_event and cancel_event.is_set():
                raise CancelledError("Delta archive creation cancelled")

        # 1. Extract full archive (member-by-member for cancellability)
        # Duration unknown (streaming gzip) — UI should pulse rather than
        # show a fraction; only file_cb is called here.
        if phase_cb:
            phase_cb("Extracting archive\u2026")
        try:
            use_filter = sys.version_info >= (3, 12)
            extracted = 0
            with tarfile.open(full_archive_path, "r:gz") as tf:
                for member in tf:
                    _chk()
                    if use_filter:
                        tf.extract(member, extract_dir, filter="data")
                    else:
                        tf.extract(member, extract_dir)  # noqa: S202
                    extracted += 1
                    if file_cb:
                        file_cb(extracted, 0)
        except CancelledError:
            raise
        except tarfile.TarError as exc:
            raise RuntimeError(f"Failed to extract full archive: {exc}") from exc

        # 2. Locate the prefix root inside the extracted archive.
        # Prefer a dir named "prefix/" (Cellar-native umu archive),
        # then a dir containing "bottle.yml" (legacy Bottles backup),
        # then fall back to the first subdirectory.
        subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
        if not subdirs:
            raise RuntimeError("No prefix directory found in archive")
        bottle_dir = subdirs[0]
        for d in subdirs:
            if d.name == "prefix":
                bottle_dir = d
                break
            if (d / "bottle.yml").exists():
                bottle_dir = d

        bottle_name = bottle_dir.name
        delta_bottle = delta_dir / bottle_name
        delta_bottle.mkdir()

        # 3. Compute the delta (files that differ from base_dir).
        # progress_cb runs 0→1 for this phase only.
        if phase_cb:
            phase_cb("Scanning files\u2026")
        _compute_delta(
            bottle_dir, base_dir, delta_bottle,
            cancel_event=cancel_event,
            file_cb=file_cb,
            progress_cb=progress_cb,
            progress_start=0.0,
            progress_end=1.0,
        )

        # Sum uncompressed sizes of the delta files only.  The base image is
        # already present on the user's system, so only the delta represents
        # new disk space required.
        delta_uncompressed_size = sum(
            f.stat().st_size for f in delta_bottle.rglob("*") if f.is_file()
        )

        # 4. Pack the delta into a .tar.zst (zstd level 3: fast compress,
        #    fast decompress, noticeably better ratio than gzip default).
        # progress_cb runs 0→1 for this phase only.
        if phase_cb:
            phase_cb("Compressing & Uploading delta\u2026")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            import zstandard as zstd  # noqa: PLC0415
            cctx = zstd.ZstdCompressor(level=3)
            delta_items = sorted(delta_bottle.rglob("*"))
            total_items = len(delta_items)
            with open(dest, "wb") as fh:
                crc_writer = _CRCWriter(fh)
                with cctx.stream_writer(crc_writer, closefd=False) as compressor:
                    with tarfile.open(fileobj=compressor, mode="w|") as tf:
                        # Add root dir, then all contents one item at a time
                        # so cancel_event is checked between each entry.
                        tf.add(delta_bottle, arcname=bottle_name, recursive=False)
                        for i, item in enumerate(delta_items, 1):
                            _chk()
                            rel = item.relative_to(delta_bottle)
                            tf.add(item, arcname=f"{bottle_name}/{rel}",
                                   recursive=False)
                            if file_cb:
                                file_cb(i, total_items)
                            if progress_cb and total_items > 0:
                                progress_cb(i / total_items)
                crc32_hex = format(crc_writer.crc & 0xFFFFFFFF, "08x")
        except CancelledError:
            dest.unlink(missing_ok=True)
            raise
        except (tarfile.TarError, OSError) as exc:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to create delta archive: {exc}") from exc

        return delta_uncompressed_size, crc32_hex


def _compute_delta(
    full_dir: Path,
    base_dir: Path,
    delta_out: Path,
    cancel_event: Event | None = None,
    file_cb: Callable[[int, int], None] | None = None,
    progress_cb: Callable[[float], None] | None = None,
    progress_start: float = 0.3,
    progress_end: float = 0.7,
) -> None:
    """Copy files from *full_dir* to *delta_out* that differ from *base_dir*.

    Uses content hashing (BLAKE2b-128) so that files with identical bytes are
    excluded from the delta regardless of their timestamps.  This is important
    because a base image installed on one day and an app bottle created on
    another day will share identical Windows system files but with different
    mtimes — a size-or-mtime heuristic would incorrectly include them.

    Also writes a ``.cellar_delete`` manifest listing every file that exists in
    *base_dir* but is absent from *full_dir*.  These are files that were present
    in the base image but removed before the app backup was taken (e.g. Windows
    setup temp files cleaned up by the installer).  ``_overlay_delta`` reads
    this manifest and removes the listed paths after seeding so the installed
    bottle matches the original backup exactly.
    """
    # ── Step 1: copy changed / new files ──────────────────────────────────
    _compute_delta_python(
        full_dir, base_dir, delta_out,
        cancel_event=cancel_event,
        file_cb=file_cb,
        progress_cb=progress_cb,
        progress_start=progress_start,
        progress_end=progress_end,
    )

    # ── Step 2: write delete manifest (base files absent from full backup) ─
    if cancel_event and cancel_event.is_set():
        raise CancelledError("Delta archive creation cancelled")
    delete_paths = sorted(
        str(p.relative_to(base_dir))
        for p in base_dir.rglob("*")
        if p.is_file() and not (full_dir / p.relative_to(base_dir)).exists()
    )
    if delete_paths:
        (delta_out / ".cellar_delete").write_text("\n".join(delete_paths))


def _compute_delta_python(
    full_dir: Path,
    base_dir: Path,
    delta_out: Path,
    cancel_event: Event | None = None,
    file_cb: Callable[[int, int], None] | None = None,
    progress_cb: Callable[[float], None] | None = None,
    progress_start: float = 0.3,
    progress_end: float = 0.7,
) -> None:
    """Compute delta via BLAKE2b-128 content hashing.

    A file is excluded from the delta only when a file at the same relative
    path exists in *base_dir* **and** has byte-for-byte identical content.
    Files that differ in content (even if the same size) are always included.
    """
    import hashlib

    def _hash_file(path: Path) -> str:
        h = hashlib.blake2b(digest_size=16)
        with open(path, "rb") as f:
            while chunk := f.read(1 * 1024 * 1024):
                if cancel_event and cancel_event.is_set():
                    raise CancelledError("Delta archive creation cancelled")
                h.update(chunk)
        return h.hexdigest()

    all_files = [src for src in full_dir.rglob("*") if src.is_file()]
    total = len(all_files)
    for i, src in enumerate(all_files, 1):
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Delta archive creation cancelled")
        rel = src.relative_to(full_dir)
        base_file = base_dir / rel
        if base_file.is_file():
            try:
                if _hash_file(src) == _hash_file(base_file):
                    if file_cb:
                        file_cb(i, total)
                    if progress_cb and total > 0:
                        progress_cb(progress_start + i / total * (progress_end - progress_start))
                    continue  # identical content → exclude from delta
            except CancelledError:
                raise
            except OSError:
                pass  # unreadable → include defensively
        out = delta_out / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, out)
        if file_cb:
            file_cb(i, total)
        if progress_cb and total > 0:
            progress_cb(progress_start + i / total * (progress_end - progress_start))


def add_catalogue_category(repo_root: Path, category: str) -> None:
    """Append *category* to the top-level ``categories`` list in ``catalogue.json``.

    Does nothing if the category is already in :data:`BASE_CATEGORIES` or
    already present in the stored list.  Creates the ``categories`` key if it
    does not exist yet.
    """
    if category in BASE_CATEGORIES:
        return
    cat_path = repo_root / "catalogue.json"
    if cat_path.exists():
        raw = json.loads(cat_path.read_text())
    else:
        raw = {"cellar_version": 1, "apps": []}
    if not isinstance(raw, dict):
        return
    stored: list[str] = raw.get("categories") or []
    if category in stored:
        return
    stored.append(category)
    raw["categories"] = stored
    raw["generated_at"] = datetime.now(timezone.utc).isoformat()
    cat_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))


def save_category_icon(repo_root: Path, category: str, icon_name: str) -> None:
    """Store *icon_name* for *category* in ``catalogue.json``'s ``category_icons`` map.

    A no-op when *icon_name* is empty.
    """
    if not icon_name:
        return
    cat_path = repo_root / "catalogue.json"
    if cat_path.exists():
        raw = json.loads(cat_path.read_text())
    else:
        raw = {"cellar_version": 1, "apps": []}
    if not isinstance(raw, dict):
        return
    icons: dict[str, str] = dict(raw.get("category_icons") or {})
    if icons.get(category) == icon_name:
        return
    icons[category] = icon_name
    raw["category_icons"] = icons
    raw["generated_at"] = datetime.now(timezone.utc).isoformat()
    cat_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))


class CancelledError(Exception):
    """Raised when an import is cancelled via *cancel_event*."""

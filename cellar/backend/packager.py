"""Packaging helpers for writing apps into a local Cellar repo.

This module handles:
- Generating URL-safe app IDs from human names
- Writing a complete archive + images + catalogue entry into a repo
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tarfile
import time
import zlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Callable

from cellar.utils.images import content_hash as _content_hash
from cellar.utils.images import optimize_image as _optimize_image


import tempfile


def _output_ext(src: str, role: str) -> str:
    """Predict the output file extension that :func:`optimize_image` will produce."""
    ext = Path(src).suffix.lower()
    if ext in (".ico", ".bmp"):
        return ".png"
    if role == "logo":
        return ".png"
    # optimize_image downscales large covers/screenshots to JPEG, but only if
    # they exceed max dims — we can't know without opening the image.  Use the
    # source extension; the hash ensures correctness regardless.
    return ext or ".png"


def _optimize_and_hash(src: str, dest_dir, slot: str, role: str) -> str:
    """Optimise *src*, hash the result, and write to *dest_dir* with a
    content-hashed filename like ``icon_a1b2c3d4.png``.

    Returns the hashed filename (just the name, not a full path).

    *dest_dir* may be a :class:`pathlib.Path` or a remote path object (SmbPath /
    SshPath) that supports ``.mkdir()`` and ``/`` operator.
    """
    ext = _output_ext(src, role)
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tf:
        tmp = Path(tf.name)
    try:
        _optimize_image(src, tmp, role)
        # optimize_image may have written with a different extension then
        # renamed back — the content at *tmp* is authoritative.
        h = _content_hash(tmp)
        hashed_name = f"{slot}_{h}{ext}"
        dest = dest_dir / hashed_name
        if isinstance(dest_dir, Path):
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(tmp, dest)
        else:
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(tmp.read_bytes())
        return hashed_name
    finally:
        tmp.unlink(missing_ok=True)


def _rmtree(path, ignore_errors: bool = False) -> None:
    """Remove a directory tree; works for :class:`pathlib.Path`,
    :class:`~cellar.utils.smb.SmbPath`, and :class:`~cellar.utils.ssh.SshPath`."""
    from cellar.utils.smb import SmbPath
    from cellar.utils.ssh import SshPath
    if isinstance(path, (SmbPath, SshPath)):
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
BASE_CATEGORIES: list[str] = [
    "Games",
    "Productivity",
    "Graphics",
    "Video",
    "Audio",
    "Education",
    "Utility",
]

#: Default symbolic icon name for each built-in category.
BASE_CATEGORY_ICONS: dict[str, str] = {
    "Games": "input-gaming-symbolic",
    "Productivity": "document-edit-symbolic",
    "Graphics": "applications-graphics-symbolic",
    "Video": "video-x-generic-symbolic",
    "Audio": "audio-x-generic-symbolic",
    "Education": "applications-science-symbolic",
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
# Directory compression
# ---------------------------------------------------------------------------

class _CRCWriter:
    """Wraps a writable file object and accumulates CRC32 of all written bytes.

    If *bytes_cb* is provided it is called with the running total of bytes
    written, throttled to at most once every 0.5 s, so callers can show
    growing output-file size without hammering the UI thread.
    """

    __slots__ = ("_fp", "crc", "_bytes_cb", "_total_written", "_last_cb")

    def __init__(self, fp, bytes_cb=None):
        self._fp = fp
        self.crc = 0
        self._bytes_cb = bytes_cb
        self._total_written = 0
        self._last_cb = 0.0

    def write(self, data: bytes) -> int:
        n = self._fp.write(data)
        self.crc = zlib.crc32(data[:n], self.crc)
        self._total_written += n
        if self._bytes_cb:
            now = time.monotonic()
            if now - self._last_cb >= 0.5:
                self._last_cb = now
                self._bytes_cb(self._total_written)
        return n


#: Target size for each independent chunk archive (1 GB).
CHUNK_SIZE = 1 * 1024 * 1024 * 1024


class _ChunkWriter:
    """Splits compressed output into independent chunk files with per-chunk CRC32.

    Each chunk is named ``{dest_path}.001``, ``.002``, etc.  The caller
    is responsible for closing and reopening the zstd compressor and tarfile
    around calls to :meth:`rotate` so that every chunk is a self-contained
    ``.tar.zst`` archive.

    Works with :class:`pathlib.Path`, :class:`~cellar.utils.smb.SmbPath`,
    and :class:`~cellar.utils.ssh.SshPath`.
    """

    __slots__ = (
        "_dest_path", "_chunk_size", "_bytes_cb",
        "_chunk_index", "_fp", "_crc_writer",
        "_chunks", "_total_crc", "_total_written",
    )

    def __init__(self, dest_path, *, chunk_size: int = CHUNK_SIZE,
                 bytes_cb: Callable[[int], None] | None = None):
        self._dest_path = dest_path
        self._chunk_size = chunk_size
        self._bytes_cb = bytes_cb
        self._chunk_index = 0
        self._fp = None
        self._crc_writer: _CRCWriter | None = None
        self._chunks: list[dict] = []
        self._total_crc = 0
        self._total_written = 0
        self._open_next()

    # -- internal helpers --------------------------------------------------

    def _chunk_path(self, index: int):
        """Return the path for chunk *index* (1-based)."""
        name = f"{self._dest_path.name}.{index:03d}"
        return self._dest_path.parent / name

    def _open_next(self) -> None:
        self._chunk_index += 1
        p = self._chunk_path(self._chunk_index)
        p.parent.mkdir(parents=True, exist_ok=True)
        self._fp = p.open("wb")
        # Wrap bytes_cb so the UI sees a running total across all chunks
        # rather than resetting to zero each time a new chunk starts.
        if self._bytes_cb:
            prior = self._total_written
            cb = lambda n, _p=prior, _b=self._bytes_cb: _b(_p + n)
        else:
            cb = None
        self._crc_writer = _CRCWriter(self._fp, bytes_cb=cb)

    def _finalize_current(self) -> None:
        """Record metadata for the current chunk and close its file."""
        if self._crc_writer is None:
            return
        size = self._crc_writer._total_written
        crc = self._crc_writer.crc
        self._chunks.append({
            "size": size,
            "crc32": format(crc & 0xFFFFFFFF, "08x"),
        })
        self._total_crc = zlib.crc32(b"", self._total_crc)  # placeholder — see write()
        self._total_written += size
        self._fp.close()
        self._fp = None
        self._crc_writer = None

    # -- public API --------------------------------------------------------

    def write(self, data: bytes) -> int:
        """Write *data* to the current chunk, accumulating CRC32."""
        n = self._crc_writer.write(data)
        # Accumulate a total CRC across all chunks for archive_crc32 compat.
        # We can't use the per-chunk CRC values for this because CRC32 is
        # not simply composable.  Instead, feed every byte into a running
        # total maintained separately.
        self._total_crc = zlib.crc32(data[:n], self._total_crc)
        return n

    def should_rotate(self) -> bool:
        """Return ``True`` when the current chunk has reached the size limit."""
        return self._crc_writer._total_written >= self._chunk_size

    def rotate(self) -> None:
        """Finalise the current chunk and open the next one.

        The caller **must** close the zstd compressor and tarfile before
        calling this, and reopen them afterwards with the new writer.
        """
        self._finalize_current()
        self._open_next()

    def close(self) -> None:
        """Finalise the last chunk."""
        self._finalize_current()

    @property
    def chunks(self) -> list[dict]:
        return list(self._chunks)

    @property
    def total_written(self) -> int:
        """Total compressed bytes across all finalised chunks."""
        if self._crc_writer:
            return self._total_written + self._crc_writer._total_written
        return self._total_written

    @property
    def total_crc_hex(self) -> str:
        return format(self._total_crc & 0xFFFFFFFF, "08x")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- current writer (for zstd stream_writer wrapping) ------------------

    @property
    def current_writer(self) -> _CRCWriter:
        """The :class:`_CRCWriter` for the chunk currently being written."""
        return self._crc_writer


def _cleanup_old_archive(repo_root, new_entry) -> None:
    """Remove stale archive files left by a previous publish of the same app.

    When an app is re-published with ``archive_in_place=True`` the new chunk
    files are already on disk.  This helper reads the *existing* catalogue
    entry (if any) and removes its old archive artefacts — either the old
    unchunked file or the old chunk files — so they don't linger on the repo.
    """
    cat_path = repo_root / "catalogue.json"
    if not cat_path.exists():
        return
    try:
        raw = json.loads(cat_path.read_text())
        apps = raw.get("apps", raw) if isinstance(raw, dict) else raw
    except Exception:
        return
    old = next((a for a in apps if a.get("id") == new_entry.id), None)
    if not old or not old.get("archive"):
        return
    old_archive = repo_root / old["archive"]
    if old.get("archive_chunks"):
        # Old entry was chunked — remove old chunk files.
        _cleanup_chunks(old_archive)
    else:
        # Old entry was a single file — remove it if it still exists.
        if old_archive.exists():
            try:
                old_archive.unlink(missing_ok=True)
            except (OSError, TypeError):
                pass


def _cleanup_chunks(dest_path) -> None:
    """Remove all ``.NNN`` chunk files for *dest_path*."""
    parent = dest_path.parent
    base = dest_path.name
    try:
        children = parent.iterdir() if hasattr(parent, "iterdir") else parent.glob("*")
    except (OSError, Exception):
        return
    for p in children:
        name = p.name if hasattr(p, "name") else str(p).rsplit("/", 1)[-1]
        if name.startswith(base + ".") and name.rsplit(".", 1)[-1].isdigit():
            try:
                p.unlink(missing_ok=True)
            except (OSError, TypeError):
                try:
                    p.unlink()
                except OSError:
                    pass


def compress_prefix_zst(
    prefix_path: Path,
    dest_path: Path,
    *,
    cancel_event=None,
    progress_cb: Callable[[float], None] | None = None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    file_cb: Callable[[str], None] | None = None,
    bytes_cb: Callable[[int], None] | None = None,
) -> tuple[int, str, tuple[dict, ...]]:
    """Archive *prefix_path* as a Cellar-native ``prefix/``-rooted ``.tar.zst``.

    The archive is split into ~1 GB independently extractable chunks named
    ``{dest_path}.001``, ``.002``, etc.  Each chunk is a self-contained
    ``.tar.zst`` so the installer can download, extract, and delete one
    chunk at a time.

    The top-level directory in every chunk is always ``prefix/`` regardless
    of the actual directory name.  Symlinks under ``drive_c/users/``
    (home-dir pointers) are stripped — umu/Proton recreates them on first
    launch.

    Returns ``(total_size_bytes, total_crc32_hex, chunk_metadata)``.
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
        if (ti.issym() or ti.islnk()) and "drive_c/users/" in ti.name:
            return None
        if ti.isfile():
            done[0] += 1
            done_bytes[0] += ti.size
            if file_cb:
                file_cb(ti.name.split("/")[-1])
            if progress_cb and total_files:
                progress_cb(done[0] / total_files)
            if stats_cb:
                elapsed = time.monotonic() - start
                speed = done_bytes[0] / elapsed if elapsed > 0.1 else 0.0
                stats_cb(done[0], total_files, speed)
        return ti

    cctx = zstd.ZstdCompressor(level=3)
    cw = _ChunkWriter(dest_path, bytes_cb=bytes_cb)
    try:
        compressor = cctx.stream_writer(cw, closefd=False)
        tf = tarfile.open(fileobj=compressor, mode="w|")

        for dirpath, dirnames, filenames in os.walk(prefix_path):
            if cancel_event and cancel_event.is_set():
                raise CancelledError("Cancelled")
            rel = os.path.relpath(dirpath, prefix_path)
            arcname = "prefix" if rel == "." else f"prefix/{rel}"

            # Add directory entry itself
            tf.add(dirpath, arcname=arcname, recursive=False, filter=_filter)

            # Add directory symlinks (os.walk lists them but won't descend)
            for d in sorted(dirnames):
                full = os.path.join(dirpath, d)
                if os.path.islink(full):
                    tf.add(full, arcname=f"{arcname}/{d}", recursive=False,
                           filter=_filter)

            # Add files
            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                tf.add(full, arcname=f"{arcname}/{fn}", recursive=False,
                       filter=_filter)

                if cw.should_rotate():
                    tf.close()
                    compressor.close()
                    cw.rotate()
                    compressor = cctx.stream_writer(cw, closefd=False)
                    tf = tarfile.open(fileobj=compressor, mode="w|")

        tf.close()
        compressor.close()
        cw.close()
    except CancelledError:
        _cleanup_chunks(dest_path)
        raise
    except (tarfile.TarError, OSError) as exc:
        _cleanup_chunks(dest_path)
        raise RuntimeError(f"Failed to compress prefix: {exc}") from exc

    return cw.total_written, cw.total_crc_hex, tuple(cw.chunks)


def compress_runner_zst(
    runner_dir: Path,
    dest_path: Path,
    *,
    cancel_event=None,
    progress_cb: Callable[[float], None] | None = None,
    file_cb: Callable[[str], None] | None = None,
    bytes_cb: Callable[[int], None] | None = None,
) -> tuple[int, str, tuple[dict, ...]]:
    """Archive a runner directory as a ``.tar.zst``.

    The archive is split into ~1 GB independently extractable chunks.
    The top-level directory in every chunk matches ``runner_dir.name``
    (e.g. ``GE-Proton10-32/``).  No symlink stripping is performed.

    Returns ``(total_size_bytes, total_crc32_hex, chunk_metadata)``.
    """
    import zstandard as zstd  # noqa: PLC0415

    dest_path.parent.mkdir(parents=True, exist_ok=True)

    total_files = sum(1 for _ in runner_dir.rglob("*") if _.is_file())
    done = [0]
    top = runner_dir.name

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo | None:
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Cancelled")
        if ti.isfile():
            done[0] += 1
            if file_cb:
                file_cb(ti.name.split("/")[-1])
            if progress_cb and total_files:
                progress_cb(done[0] / total_files)
        return ti

    cctx = zstd.ZstdCompressor(level=3)
    cw = _ChunkWriter(dest_path, bytes_cb=bytes_cb)
    try:
        compressor = cctx.stream_writer(cw, closefd=False)
        tf = tarfile.open(fileobj=compressor, mode="w|")

        for dirpath, dirnames, filenames in os.walk(runner_dir):
            if cancel_event and cancel_event.is_set():
                raise CancelledError("Cancelled")
            rel = os.path.relpath(dirpath, runner_dir)
            arcname = top if rel == "." else f"{top}/{rel}"

            tf.add(dirpath, arcname=arcname, recursive=False, filter=_filter)

            for d in sorted(dirnames):
                full = os.path.join(dirpath, d)
                if os.path.islink(full):
                    tf.add(full, arcname=f"{arcname}/{d}", recursive=False,
                           filter=_filter)

            for fn in sorted(filenames):
                full = os.path.join(dirpath, fn)
                tf.add(full, arcname=f"{arcname}/{fn}", recursive=False,
                       filter=_filter)

                if cw.should_rotate():
                    tf.close()
                    compressor.close()
                    cw.rotate()
                    compressor = cctx.stream_writer(cw, closefd=False)
                    tf = tarfile.open(fileobj=compressor, mode="w|")

        tf.close()
        compressor.close()
        cw.close()
    except CancelledError:
        _cleanup_chunks(dest_path)
        raise
    except (tarfile.TarError, OSError) as exc:
        _cleanup_chunks(dest_path)
        raise RuntimeError(f"Failed to compress runner: {exc}") from exc

    return cw.total_written, cw.total_crc_hex, tuple(cw.chunks)


def compress_prefix_delta_zst(
    prefix_path: Path,
    base_dir: Path,
    dest_path: Path,
    *,
    cancel_event=None,
    phase_cb: Callable[[str], None] | None = None,
    progress_cb: Callable[[float], None] | None = None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    file_cb: Callable[[str], None] | None = None,
    bytes_cb: Callable[[int], None] | None = None,
) -> tuple[int, str, tuple[dict, ...]]:
    """Create a delta ``.tar.zst`` from *prefix_path* relative to *base_dir*.

    The archive is split into ~1 GB independently extractable chunks.
    Works directly on the prefix directory — no intermediate full archive.
    Files identical to *base_dir* (same relative path, same BLAKE2b-128 hash)
    are excluded.  A ``.cellar_delete`` manifest is included in the last chunk
    listing base files absent from the prefix.

    Symlinks under ``drive_c/users/`` are stripped (same as the full archive).
    The archive is ``prefix/``-rooted, compatible with :func:`compress_prefix_zst`
    and the installer's :func:`~cellar.backend.installer._overlay_delta`.

    Returns ``(total_size_bytes, total_crc32_hex, chunk_metadata)``.
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
        if file_cb:
            file_cb(src.name)
        base_file = base_dir / rel
        if base_file.is_file():
            try:
                if _hash_file(src) == _hash_file(base_file):
                    if progress_cb and total_scan:
                        progress_cb(i / total_scan)
                    continue  # identical — exclude from delta
            except CancelledError:
                raise
            except OSError:
                pass  # unreadable → include defensively
        delta_files.append((src, rel))
        if progress_cb and total_scan:
            progress_cb(i / total_scan)

    # Files in base absent from prefix → delete manifest.
    delete_paths = sorted(
        str(p.relative_to(base_dir))
        for p in base_dir.rglob("*")
        if p.is_file() and not p.is_symlink()
        and not (prefix_path / p.relative_to(base_dir)).exists()
    )

    # ── Phase 2: Pack delta files into chunked archive ───────────────────────
    if phase_cb:
        phase_cb("Compressing and uploading\u2026")

    delta_uncompressed = sum(src.stat().st_size for src, _ in delta_files)
    total_pack = len(delta_files)
    done = [0]
    done_bytes = [0]
    start = time.monotonic()

    cctx = zstd.ZstdCompressor(level=3)
    cw = _ChunkWriter(dest_path, bytes_cb=bytes_cb)
    try:
        compressor = cctx.stream_writer(cw, closefd=False)
        tf = tarfile.open(fileobj=compressor, mode="w|")

        for src, rel in sorted(delta_files, key=lambda t: t[1]):
            _chk()
            if file_cb:
                file_cb(src.name)
            tf.add(str(src), arcname=f"prefix/{rel}", recursive=False)
            done[0] += 1
            done_bytes[0] += src.stat().st_size
            if progress_cb and total_pack:
                progress_cb(done[0] / total_pack)
            if stats_cb:
                elapsed = time.monotonic() - start
                speed = done_bytes[0] / elapsed if elapsed > 0.1 else 0.0
                stats_cb(done[0], total_pack, speed)

            if cw.should_rotate():
                tf.close()
                compressor.close()
                cw.rotate()
                compressor = cctx.stream_writer(cw, closefd=False)
                tf = tarfile.open(fileobj=compressor, mode="w|")

        # Delete manifest goes in the last chunk
        if delete_paths:
            manifest = "\n".join(delete_paths).encode()
            ti = tarfile.TarInfo(name="prefix/.cellar_delete")
            ti.size = len(manifest)
            tf.addfile(ti, io.BytesIO(manifest))

        tf.close()
        compressor.close()
        cw.close()
    except CancelledError:
        _cleanup_chunks(dest_path)
        raise
    except (tarfile.TarError, OSError) as exc:
        _cleanup_chunks(dest_path)
        raise RuntimeError(f"Failed to create delta archive: {exc}") from exc

    return cw.total_written, cw.total_crc_hex, tuple(cw.chunks)


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
):
    """Copy archive + images into *repo_root* and update ``catalogue.json``.

    *images* is a dict with optional keys:
      ``"icon"``, ``"cover"`` → str path
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

    # ── Single images (icon, cover, logo) — content-hashed names ────────
    for key in ("icon", "cover", "logo"):
        src = images.get(key)
        if src:
            hashed = _optimize_and_hash(src, app_dir, key, key)
            entry = replace(entry, **{key: f"apps/{entry.id}/{hashed}"})

    # ── Screenshots — content-hashed names ────────────────────────────────
    ss_dir = app_dir / "screenshots"
    ss_rels: list[str] = []
    for src in images.get("screenshots", []):
        hashed = _optimize_and_hash(src, ss_dir, "ss", "screenshot")
        ss_rels.append(f"apps/{entry.id}/screenshots/{hashed}")
    if ss_rels:
        # Rebuild screenshot_sources with the new hashed paths.
        old_sources = entry.screenshot_sources or {}
        old_ss = entry.screenshots or ()
        new_sources: dict[str, str] = {}
        for idx, new_rel in enumerate(ss_rels):
            if idx < len(old_ss) and old_ss[idx] in old_sources:
                new_sources[new_rel] = old_sources[old_ss[idx]]
        entry = replace(entry, screenshots=tuple(ss_rels),
                        screenshot_sources=new_sources if new_sources else {})

    # ── catalogue.json ────────────────────────────────────────────────────
    if phase_cb:
        phase_cb("Writing catalogue\u2026")
    _upsert_catalogue(repo_root, entry)

    if progress_cb:
        progress_cb(1.0)

    return entry


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
):
    """Update an existing entry in *repo_root*.

    - If *new_archive_src* is given, the archive is replaced (chunked copy,
      old file removed if the path changed and the file still exists).
    - Only image keys with a non-empty value in *images* are overwritten.
    - Screenshots are fully replaced when *images["screenshots"]* is non-empty.
    - ``catalogue.json`` is updated in place (matched by ID).
    - Returns the final :class:`AppEntry` with content-hashed image paths.
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

        # Remove old archive files (single file or chunks)
        if old_entry.archive:
            if old_entry.archive_chunks:
                _cleanup_chunks(repo_root / old_entry.archive)
            else:
                old_archive = repo_root / old_entry.archive
                if old_archive != archive_dest and old_archive.exists():
                    old_archive.unlink(missing_ok=True)

    # ── Single images (icon, cover, logo) — content-hashed names ────────
    for key in ("icon", "cover", "logo"):
        src = images.get(key)
        if src:
            hashed = _optimize_and_hash(src, app_dir, key, key)
            new_rel = f"apps/{new_entry.id}/{hashed}"
            # Remove old image file if path changed
            old_rel = getattr(old_entry, key, "")
            if old_rel and old_rel != new_rel:
                old_file = repo_root / old_rel
                if isinstance(old_file, Path):
                    old_file.unlink(missing_ok=True)
                else:
                    try:
                        old_file.unlink()
                    except Exception:
                        pass
            new_entry = replace(new_entry, **{key: new_rel})

    # ── Screenshots — content-hashed names ────────────────────────────────
    # None = keep existing, [] = clear all, [...] = replace
    new_screenshots = images.get("screenshots")
    if new_screenshots is not None:
        ss_dir = repo_root / "apps" / new_entry.id / "screenshots"
        if ss_dir.exists():
            _rmtree(ss_dir)
        ss_rels: list[str] = []
        if new_screenshots:
            for src in new_screenshots:
                hashed = _optimize_and_hash(src, ss_dir, "ss", "screenshot")
                ss_rels.append(f"apps/{new_entry.id}/screenshots/{hashed}")
        # Rebuild screenshot_sources with the new hashed paths.
        old_sources = new_entry.screenshot_sources or {}
        old_ss = new_entry.screenshots or ()
        new_sources: dict[str, str] = {}
        for idx, new_rel in enumerate(ss_rels):
            if idx < len(old_ss) and old_ss[idx] in old_sources:
                new_sources[new_rel] = old_sources[old_ss[idx]]
        new_entry = replace(new_entry, screenshots=tuple(ss_rels),
                            screenshot_sources=new_sources if new_sources else {})

    # ── catalogue.json ────────────────────────────────────────────────────
    if phase_cb:
        phase_cb("Writing catalogue\u2026")
    _upsert_catalogue(repo_root, new_entry)

    if progress_cb:
        progress_cb(1.0)

    return new_entry


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
    runners: dict | None = None
    bases: dict | None = None
    category_icons: dict[str, str] | None = None
    if cat_path.exists():
        raw = json.loads(cat_path.read_text())
        apps = raw.get("apps", raw) if isinstance(raw, dict) else raw
        if isinstance(raw, dict):
            categories = raw.get("categories")
            runners = raw.get("runners")
            bases = raw.get("bases")
            category_icons = raw.get("category_icons")
    else:
        apps = []
    apps = [a for a in apps if a.get("id") != entry.id]
    apps.append(entry.to_index_dict())
    # Write full per-app metadata
    meta_dir = repo_root / "apps" / entry.id
    meta_dir.mkdir(parents=True, exist_ok=True)
    meta_path = meta_dir / "metadata.json"
    meta_path.write_text(json.dumps(entry.to_metadata_dict(), indent=2, ensure_ascii=False))
    # Auto-register custom category into the top-level categories list
    category = entry.category if hasattr(entry, "category") else ""
    if category and category not in BASE_CATEGORIES:
        if categories is None:
            categories = []
        if category not in categories:
            categories.append(category)
    _write_catalogue(cat_path, apps, categories, runners, bases, category_icons)


def _remove_from_catalogue(repo_root: Path, app_id: str) -> None:
    """Filter *app_id* out of ``catalogue.json``."""
    cat_path = repo_root / "catalogue.json"
    if not cat_path.exists():
        return
    raw = json.loads(cat_path.read_text())
    apps = raw.get("apps", raw) if isinstance(raw, dict) else raw
    categories = raw.get("categories") if isinstance(raw, dict) else None
    runners = raw.get("runners") if isinstance(raw, dict) else None
    bases = raw.get("bases") if isinstance(raw, dict) else None
    category_icons = raw.get("category_icons") if isinstance(raw, dict) else None
    apps = [a for a in apps if a.get("id") != app_id]
    _write_catalogue(cat_path, apps, categories, runners, bases, category_icons)


def _write_catalogue(
    cat_path: Path,
    apps: list,
    categories: list[str] | None = None,
    runners: dict | None = None,
    bases: dict | None = None,
    category_icons: dict[str, str] | None = None,
) -> None:
    data: dict = {
        "cellar_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "image_hashing": True,
        "apps": apps,
    }
    if categories is not None:
        data["categories"] = categories
    if runners is not None:
        data["runners"] = runners
    if bases is not None:
        data["bases"] = bases
    if category_icons is not None:
        data["category_icons"] = category_icons
    cat_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def upsert_runner(
    repo_root: Path,
    name: str,
    archive_path: str,
    archive_crc32: str = "",
    archive_size: int = 0,
    archive_chunks: tuple[dict, ...] = (),
) -> None:
    """Add or replace a runner entry in the ``runners`` section of ``catalogue.json``.

    *name* is the runner version string (e.g. ``"GE-Proton10-32"``).
    *archive_path* is the repo-relative path to the compressed runner
    (e.g. ``"runners/GE-Proton10-32.tar.zst"``).
    """
    cat_path = repo_root / "catalogue.json"
    if cat_path.exists():
        raw = json.loads(cat_path.read_text())
        apps = raw.get("apps", []) if isinstance(raw, dict) else []
        categories = raw.get("categories") if isinstance(raw, dict) else None
        runners: dict = dict(raw.get("runners") or {})
        bases = raw.get("bases") if isinstance(raw, dict) else None
        category_icons = raw.get("category_icons") if isinstance(raw, dict) else None
    else:
        apps, categories, runners, bases, category_icons = [], None, {}, None, None
    runners[name] = {"archive": archive_path}
    if archive_size:
        runners[name]["archive_size"] = archive_size
    if archive_crc32:
        runners[name]["archive_crc32"] = archive_crc32
    if archive_chunks:
        runners[name]["archive_chunks"] = [dict(c) for c in archive_chunks]
    _write_catalogue(cat_path, apps, categories, runners, bases, category_icons)


def upsert_base(
    repo_root: Path,
    name: str,
    runner: str,
    archive_path: str,
    archive_crc32: str = "",
    archive_size: int = 0,
    archive_chunks: tuple[dict, ...] = (),
) -> None:
    """Add or replace a base image entry in ``catalogue.json``.

    *name* is the catalogue key (display name, e.g. ``"GE-Proton10-32"`` or a
    custom label like ``"GE-Proton10-32-dotnet"``).  *runner* is the GE-Proton
    version used to create the base (e.g. ``"GE-Proton10-32"``), which must
    exist as a key in the ``runners`` section.

    *archive_path* must be a repo-relative path (e.g.
    ``"bases/GE-Proton10-32-base.tar.zst"``).  The physical archive must
    already have been copied to the repo before calling this.
    """
    cat_path = repo_root / "catalogue.json"
    if cat_path.exists():
        raw = json.loads(cat_path.read_text())
        apps = raw.get("apps", []) if isinstance(raw, dict) else []
        categories = raw.get("categories") if isinstance(raw, dict) else None
        runners = raw.get("runners") if isinstance(raw, dict) else None
        bases: dict = dict(raw.get("bases") or {})
        category_icons = raw.get("category_icons") if isinstance(raw, dict) else None
    else:
        apps, categories, runners, bases, category_icons = [], None, None, {}, None
    bases[name] = {"runner": runner, "archive": archive_path}
    if archive_size:
        bases[name]["archive_size"] = archive_size
    if archive_crc32:
        bases[name]["archive_crc32"] = archive_crc32
    if archive_chunks:
        bases[name]["archive_chunks"] = [dict(c) for c in archive_chunks]
    _write_catalogue(cat_path, apps, categories, runners, bases, category_icons)


def remove_base(repo_root: Path, name: str) -> None:
    """Remove a base image entry (keyed by *name*) from ``catalogue.json``.

    Also deletes the physical archive from the repo if it exists.
    """
    cat_path = repo_root / "catalogue.json"
    if not cat_path.exists():
        return
    raw = json.loads(cat_path.read_text())
    if not isinstance(raw, dict):
        return
    apps = raw.get("apps", [])
    categories = raw.get("categories")
    runners = raw.get("runners")
    bases = dict(raw.get("bases") or {})
    category_icons = raw.get("category_icons")

    entry = bases.pop(name, None)
    if entry and entry.get("archive"):
        if entry.get("archive_chunks"):
            _cleanup_chunks(repo_root / entry["archive"])
        else:
            try:
                (repo_root / entry["archive"]).unlink(missing_ok=True)
            except OSError:
                pass

    _write_catalogue(cat_path, apps, categories, runners, bases if bases else None, category_icons)


def remove_runner(repo_root: Path, runner_name: str) -> None:
    """Remove a runner entry and its archive from ``catalogue.json``.

    Deletes the physical archive (if it exists) and removes the runner
    from the ``runners`` section.  Base entries referencing this runner
    are **not** removed — they become unresolvable until a matching
    runner is re-published.
    """
    cat_path = repo_root / "catalogue.json"
    if not cat_path.exists():
        return
    raw = json.loads(cat_path.read_text())
    if not isinstance(raw, dict):
        return
    apps = raw.get("apps", [])
    categories = raw.get("categories")
    runners = dict(raw.get("runners") or {})
    bases = raw.get("bases")
    category_icons = raw.get("category_icons")

    entry = runners.pop(runner_name, None)
    if entry and entry.get("archive"):
        if entry.get("archive_chunks"):
            _cleanup_chunks(repo_root / entry["archive"])
        else:
            try:
                (repo_root / entry["archive"]).unlink(missing_ok=True)
            except OSError:
                pass

    _write_catalogue(cat_path, apps, categories, runners if runners else None, bases, category_icons)


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
    """Create a delta ``.tar.zst`` from a full prefix archive, relative to *base_dir*.

    Extracts *full_archive_path* to a temp directory, identifies files that
    differ from *base_dir* by content hash, and packs only those files into a
    new zstd-compressed archive at *dest*.

    The result has the same top-level prefix directory name as the original and
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
    # (often 8 GB) that cannot hold a multi-GB extracted prefix.
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
        # then fall back to the first subdirectory.
        subdirs = [d for d in extract_dir.iterdir() if d.is_dir()]
        if not subdirs:
            raise RuntimeError("No prefix directory found in archive")
        prefix_dir = subdirs[0]
        for d in subdirs:
            if d.name == "prefix":
                prefix_dir = d
                break

        prefix_name = prefix_dir.name
        delta_prefix = delta_dir / prefix_name
        delta_prefix.mkdir()

        # 3. Compute the delta (files that differ from base_dir).
        # progress_cb runs 0→1 for this phase only.
        if phase_cb:
            phase_cb("Scanning files\u2026")
        _compute_delta(
            prefix_dir, base_dir, delta_prefix,
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
            f.stat().st_size for f in delta_prefix.rglob("*") if f.is_file()
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
            delta_items = sorted(delta_prefix.rglob("*"))
            total_items = len(delta_items)
            with open(dest, "wb") as fh:
                crc_writer = _CRCWriter(fh)
                with cctx.stream_writer(crc_writer, closefd=False) as compressor:
                    with tarfile.open(fileobj=compressor, mode="w|") as tf:
                        # Add root dir, then all contents one item at a time
                        # so cancel_event is checked between each entry.
                        tf.add(delta_prefix, arcname=prefix_name, recursive=False)
                        for i, item in enumerate(delta_items, 1):
                            _chk()
                            rel = item.relative_to(delta_prefix)
                            tf.add(item, arcname=f"{prefix_name}/{rel}",
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
    because a base image installed on one day and an app prefix created on
    another day will share identical Windows system files but with different
    mtimes — a size-or-mtime heuristic would incorrectly include them.

    Also writes a ``.cellar_delete`` manifest listing every file that exists in
    *base_dir* but is absent from *full_dir*.  These are files that were present
    in the base image but removed before the app backup was taken (e.g. Windows
    setup temp files cleaned up by the installer).  ``_overlay_delta`` reads
    this manifest and removes the listed paths after seeding so the installed
    prefix matches the original backup exactly.
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

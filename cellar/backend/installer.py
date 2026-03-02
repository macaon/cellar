"""Download, verify, extract, and import a Bottles backup.

Install flow
------------
1. **Acquire** — for local archives (``file://`` or bare path) the file is
   used in-place; for HTTP(S) it is streamed to a temp file in 1 MB chunks
   with progress reporting and cancel support.  SSH/SMB/NFS archives raise
   ``InstallError`` (not yet supported).
2. **Verify** — CRC32 checksum checked against ``AppEntry.archive_crc32``
   (skipped when the field is empty).
3. **Extract** — ``tarfile`` extracts to a temporary directory.
4. **Identify** — the single top-level directory inside the archive is taken
   as the bottle source; if there are multiple, the one containing
   ``bottle.yml`` is preferred.
5. **Name** — a collision-safe bottle directory name is derived from the
   archive's top-level directory name (preserving original capitalisation),
   e.g. ``My-App``, then ``My-App-2``, ``My-App-3`` …
6. **Copy** — ``shutil.copytree`` moves the extracted bottle into the Bottles
   data path; a partial copy is cleaned up on failure.
7. **Fix paths** — absolute ``path:`` values inside ``bottle.yml``'s
   ``External_Programs`` section are rewritten to use the actual install
   location.  Bottles stores full host paths (e.g.
   ``/home/alice/.var/…/bottles/MyApp/drive_c/…``) so they must be updated
   whenever the target machine or user differs from the original.
8. **Return** — the caller receives the ``bottle_name`` string and is
   responsible for writing the DB record (``database.mark_installed``).

Threading
---------
All public functions are **blocking** and intended to run on a background
thread.  Progress is reported via an optional
``progress_cb(phase: str, fraction: float)`` callback that is safe to call
from any thread (the UI layer wraps it in ``GLib.idle_add``).
"""

from __future__ import annotations

import shutil
import sys
import tarfile
import tempfile
import threading
import time
import zlib
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import requests

from cellar.utils.http import DEFAULT_TIMEOUT, make_session


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class InstallError(Exception):
    """Raised when an install step fails unrecoverably."""


class InstallCancelled(Exception):
    """Raised when the user cancels a running install."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install_app(
    entry,                          # AppEntry — avoid circular import at module level
    archive_uri: str,               # resolved by Repo.resolve_asset_uri(entry.archive)
    bottles_install,                # BottlesInstall from detect_bottles()
    *,
    download_cb: Callable[[float], None] | None = None,
    download_stats_cb: Callable[[int, int, float], None] | None = None,
    install_cb: Callable[[float], None] | None = None,
    extract_name_cb: Callable[[str], None] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    verify_cb: Callable[[float], None] | None = None,
    cancel_event: threading.Event | None = None,
    token: str | None = None,
    ssl_verify: bool = True,
    ca_cert: str | None = None,
) -> str:
    """Download, verify, extract, and import *entry* into Bottles.

    Parameters
    ----------
    entry:
        The ``AppEntry`` to install.
    archive_uri:
        Absolute path or HTTP(S) URL of the archive, as returned by
        ``Repo.resolve_asset_uri(entry.archive)``.
    bottles_install:
        Active Bottles installation from ``detect_bottles()``.
    download_cb:
        Optional ``(fraction)`` callback for the download phase (0 → 1).
    install_cb:
        Optional ``(fraction)`` callback for the extract phase (0 → 1).
    phase_cb:
        Optional ``(label)`` callback; called at each phase transition
        so the UI can update the status label and reset the bar.
    verify_cb:
        Optional ``(fraction)`` callback for the CRC32 verify phase (0 → 1).
    cancel_event:
        ``threading.Event``; when set the operation is aborted and
        ``InstallCancelled`` is raised.

    Returns
    -------
    str
        The bottle directory name used (e.g. ``"my-app"`` or ``"my-app-2"``).
    """
    _check_cancel(cancel_event)

    with tempfile.TemporaryDirectory(prefix="cellar-install-") as tmp_str:
        tmp = Path(tmp_str)

        # ── Step 1: Acquire archive ────────────────────────────────────
        if phase_cb:
            phase_cb("Downloading\u2026")
        if download_cb:
            download_cb(0.0)
        archive_path = _acquire_archive(
            archive_uri,
            tmp / "archive.tar.gz",
            expected_size=entry.archive_size,
            progress_cb=download_cb,
            stats_cb=download_stats_cb,
            cancel_event=cancel_event,
            token=token,
            ssl_verify=ssl_verify,
            ca_cert=ca_cert,
        )
        if download_cb:
            download_cb(1.0)

        # ── Step 2: Verify CRC32 ──────────────────────────────────────
        if entry.archive_crc32:
            _check_cancel(cancel_event)
            if phase_cb:
                phase_cb("Verifying download\u2026")
            _verify_crc32(archive_path, entry.archive_crc32, progress_cb=verify_cb)

        # ── Step 3: Extract ────────────────────────────────────────────
        _check_cancel(cancel_event)
        if phase_cb:
            phase_cb("Extracting\u2026")
        if install_cb:
            install_cb(0.0)
        extract_dir = tmp / "extracted"
        extract_dir.mkdir()
        _extract_archive(
            archive_path, extract_dir, cancel_event,
            progress_cb=install_cb,
            name_cb=extract_name_cb,
        )

        # ── Step 4: Identify bottle directory ─────────────────────────
        bottle_src = _find_bottle_dir(extract_dir)

        # ── Step 5: Collision-safe name ────────────────────────────────
        # Use the directory name from the archive verbatim so that absolute
        # paths stored in bottle.yml (External_Programs entries etc.) remain
        # valid after installation.
        bottle_name = _safe_bottle_name(bottle_src.name, bottles_install.data_path)
        bottle_dest = bottles_install.data_path / bottle_name

        # ── Step 6: Copy into Bottles ──────────────────────────────────
        _check_cancel(cancel_event)
        if phase_cb:
            phase_cb("Copying to Bottles\u2026")
        try:
            shutil.copytree(bottle_src, bottle_dest)
        except Exception:
            shutil.rmtree(bottle_dest, ignore_errors=True)
            raise

        # ── Step 7: Fix absolute paths in bottle.yml ───────────────────
        if phase_cb:
            phase_cb("Finishing\u2026")
        _fix_program_paths(bottle_dest, bottles_install.data_path)

    if install_cb:
        install_cb(1.0)
    return bottle_name


# ---------------------------------------------------------------------------
# Acquire
# ---------------------------------------------------------------------------

def _acquire_archive(
    uri: str,
    dest: Path,
    *,
    expected_size: int,
    progress_cb: Callable[[float], None] | None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    cancel_event: threading.Event | None,
    token: str | None = None,
    ssl_verify: bool = True,
    ca_cert: str | None = None,
) -> Path:
    """Return a local path to the archive.

    - Local (bare path or ``file://``) → returned as-is; no copy.
    - HTTP(S) → streamed to *dest* in 1 MB chunks.
    - SMB/NFS → GVFS FUSE path used in-place when ``gvfsd-fuse`` is running
      (standard on GNOME); otherwise streamed via a GIO ``InputStream``.
    - Other schemes → ``InstallError``.
    """
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()

    if scheme in ("", "file"):
        if progress_cb:
            progress_cb(1.0)
        return Path(parsed.path if scheme == "file" else uri)

    if scheme in ("http", "https"):
        _http_stream(
            uri,
            dest,
            expected_size=expected_size,
            progress_cb=progress_cb,
            stats_cb=stats_cb,
            cancel_event=cancel_event,
            token=token,
            ssl_verify=ssl_verify,
            ca_cert=ca_cert,
        )
        return dest

    if scheme in ("smb", "nfs"):
        # Try to resolve a GVFS FUSE path so we can stream-copy with progress.
        # Even though gvfsd-fuse exposes the file as a local path, reading
        # through it is still network I/O, so we copy to a temp file to get
        # accurate download progress rather than returning the FUSE path
        # directly (which would make the download phase appear instant).
        fuse_path: str | None = None
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio
            fuse_path = Gio.File.new_for_uri(uri).get_path()
        except (ImportError, ValueError):
            pass

        if fuse_path:
            _file_stream(
                Path(fuse_path),
                dest,
                expected_size=expected_size,
                progress_cb=progress_cb,
                stats_cb=stats_cb,
                cancel_event=cancel_event,
            )
        else:
            # FUSE not available — stream via a GIO InputStream instead.
            _gio_stream(
                uri,
                dest,
                expected_size=expected_size,
                progress_cb=progress_cb,
                stats_cb=stats_cb,
                cancel_event=cancel_event,
            )
        return dest

    raise InstallError(
        f"Downloading from {scheme!r} repos is not yet supported. "
        "Use a local, HTTP(S), SMB, or NFS repo."
    )


def _file_stream(
    src: Path,
    dest: Path,
    *,
    expected_size: int,
    progress_cb: Callable[[float], None] | None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    cancel_event: threading.Event | None,
) -> None:
    """Copy *src* to *dest* in 1 MB chunks with progress.

    Used for GVFS FUSE paths where the file appears local but reads are
    actually network I/O.
    """
    chunk = 1 * 1024 * 1024
    total = expected_size if expected_size > 0 else src.stat().st_size
    copied = 0
    start = time.monotonic()
    try:
        with open(src, "rb") as fin, open(dest, "wb") as fout:
            while True:
                if cancel_event and cancel_event.is_set():
                    raise InstallCancelled("Download cancelled")
                buf = fin.read(chunk)
                if not buf:
                    break
                fout.write(buf)
                copied += len(buf)
                elapsed = time.monotonic() - start
                speed = copied / elapsed if elapsed > 0.1 else 0.0
                if stats_cb:
                    stats_cb(copied, total, speed)
                if progress_cb and total > 0:
                    progress_cb(min(copied / total, 1.0))
    except InstallCancelled:
        dest.unlink(missing_ok=True)
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise InstallError(f"Could not copy archive to disk: {exc}") from exc


def _http_stream(
    url: str,
    dest: Path,
    *,
    expected_size: int,
    progress_cb: Callable[[float], None] | None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    cancel_event: threading.Event | None,
    token: str | None = None,
    ssl_verify: bool = True,
    ca_cert: str | None = None,
) -> None:
    """Stream *url* to *dest* in 1 MB chunks."""
    chunk = 1 * 1024 * 1024
    downloaded = 0
    start = time.monotonic()
    session = make_session(token=token, ssl_verify=ssl_verify, ca_cert=ca_cert)
    try:
        resp = session.get(url, stream=True, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
        try:
            total = int(resp.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            total = 0
        total = total or expected_size
        with open(dest, "wb") as fh:
            for buf in resp.iter_content(chunk_size=chunk):
                if cancel_event and cancel_event.is_set():
                    raise InstallCancelled("Download cancelled")
                fh.write(buf)
                downloaded += len(buf)
                elapsed = time.monotonic() - start
                speed = downloaded / elapsed if elapsed > 0.1 else 0.0
                if stats_cb:
                    stats_cb(downloaded, total, speed)
                if progress_cb and total > 0:
                    progress_cb(min(downloaded / total, 1.0))
    except InstallCancelled:
        dest.unlink(missing_ok=True)
        raise
    except requests.HTTPError as exc:
        dest.unlink(missing_ok=True)
        code = exc.response.status_code if exc.response is not None else "?"
        raise InstallError(f"HTTP {code} downloading archive") from exc
    except requests.RequestException as exc:
        dest.unlink(missing_ok=True)
        raise InstallError(f"Network error downloading archive: {exc}") from exc
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise InstallError(f"Could not write archive to disk: {exc}") from exc


def _gio_stream(
    uri: str,
    dest: Path,
    *,
    expected_size: int,
    progress_cb: Callable[[float], None] | None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    cancel_event: threading.Event | None,
) -> None:
    """Stream *uri* to *dest* via a GIO InputStream (SMB/NFS without FUSE).

    GIO synchronous I/O is thread-safe and designed to be called from
    background threads.
    """
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError) as exc:
        raise InstallError("GIO is unavailable; cannot stream from SMB/NFS") from exc

    gfile = Gio.File.new_for_uri(uri)
    try:
        stream = gfile.read(None)
    except GLib.Error as exc:
        raise InstallError(f"Could not open {uri}: {exc.message}") from exc

    chunk = 1 * 1024 * 1024
    downloaded = 0
    start = time.monotonic()
    try:
        with open(dest, "wb") as fh:
            while True:
                if cancel_event and cancel_event.is_set():
                    raise InstallCancelled("Download cancelled")
                buf = stream.read_bytes(chunk, None)
                data = buf.get_data()
                if not data:
                    break
                fh.write(data)
                downloaded += len(data)
                elapsed = time.monotonic() - start
                speed = downloaded / elapsed if elapsed > 0.1 else 0.0
                if stats_cb:
                    stats_cb(downloaded, expected_size, speed)
                if progress_cb and expected_size > 0:
                    progress_cb(min(downloaded / expected_size, 1.0))
    except InstallCancelled:
        dest.unlink(missing_ok=True)
        raise
    except GLib.Error as exc:
        dest.unlink(missing_ok=True)
        raise InstallError(f"GIO read error: {exc.message}") from exc
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise InstallError(f"Could not write archive to disk: {exc}") from exc
    finally:
        try:
            stream.close(None)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------

def _verify_crc32(
    path: Path,
    expected: str,
    progress_cb: Callable[[float], None] | None = None,
) -> None:
    """Raise ``InstallError`` if *path* does not match *expected* CRC32 hex."""
    crc = 0
    total = path.stat().st_size or 1
    read = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            crc = zlib.crc32(chunk, crc)
            read += len(chunk)
            if progress_cb:
                progress_cb(min(read / total, 1.0))
    actual = format(crc & 0xFFFFFFFF, "08x")
    if actual != expected:
        raise InstallError(
            f"CRC32 mismatch — archive may be corrupt or tampered.\n"
            f"  expected: {expected}\n"
            f"  actual:   {actual}"
        )


# ---------------------------------------------------------------------------
# Extract
# ---------------------------------------------------------------------------

def _extract_archive(
    archive_path: Path,
    dest: Path,
    cancel_event: threading.Event | None,
    progress_cb: Callable[[float], None] | None = None,
    name_cb: Callable[[str], None] | None = None,
) -> None:
    """Extract *archive_path* into *dest*, reporting per-member progress.

    Uses ``filter='data'`` on Python 3.12+ to strip unsafe tar entries.
    Progress is based on compressed bytes consumed (file position) rather
    than calling ``getmembers()`` upfront, which would scan the entire
    archive before extracting anything — unacceptable for large files.

    *name_cb*, when provided, is called as ``name_cb(filename)`` before
    each member is extracted so the UI can show the current file name.
    """
    _check_cancel(cancel_event)
    use_filter = sys.version_info >= (3, 12)
    try:
        total = archive_path.stat().st_size or 1
        with open(archive_path, "rb") as raw:
            with tarfile.open(fileobj=raw, mode="r:gz") as tf:
                for member in tf:
                    _check_cancel(cancel_event)
                    if name_cb:
                        name_cb(Path(member.name).name or member.name)
                    if use_filter:
                        tf.extract(member, dest, filter="data")
                    else:
                        tf.extract(member, dest)  # noqa: S202
                    if progress_cb:
                        progress_cb(min(raw.tell() / total, 1.0))
    except tarfile.TarError as exc:
        raise InstallError(f"Failed to extract archive: {exc}") from exc


# ---------------------------------------------------------------------------
# Identify
# ---------------------------------------------------------------------------

def _find_bottle_dir(extract_dir: Path) -> Path:
    """Return the bottle source directory inside *extract_dir*.

    Expects a single top-level directory (the bottle).  When there are
    multiple, the one containing ``bottle.yml`` is preferred.
    """
    dirs = [d for d in extract_dir.iterdir() if d.is_dir()]

    if not dirs:
        raise InstallError(
            "Archive contains no directories; expected a top-level bottle directory."
        )

    if len(dirs) == 1:
        return dirs[0]

    with_yml = [d for d in dirs if (d / "bottle.yml").exists()]
    if len(with_yml) == 1:
        return with_yml[0]

    raise InstallError(
        f"Cannot identify bottle directory in archive "
        f"({len(dirs)} top-level directories found, "
        f"{len(with_yml)} contain bottle.yml)."
    )


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def _safe_bottle_name(app_id: str, data_path: Path) -> str:
    """Return a bottle directory name that does not collide with existing bottles.

    Tries *app_id* first, then ``app_id-2``, ``app_id-3``, and so on.
    """
    if not (data_path / app_id).exists():
        return app_id
    i = 2
    while (data_path / f"{app_id}-{i}").exists():
        i += 1
    return f"{app_id}-{i}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fix_program_paths(bottle_dest: Path, new_data_path: Path) -> None:
    """Rewrite absolute External_Programs path values in bottle.yml.

    Bottles stores full host paths in ``bottle.yml``, e.g.::

        path: /home/alice/.var/…/bottles/MyApp/drive_c/MyApp/MyApp.exe

    These break on any machine where the username or Bottles data directory
    differs from the original.  We replace the prefix up to and including
    ``/<bottle_dir_name>/`` with the actual install location.  If the paths
    already point to the correct location (same machine, same user) this is
    a no-op.
    """
    import re

    bottle_name = bottle_dest.name
    yml_path = bottle_dest / "bottle.yml"
    try:
        text = yml_path.read_text(encoding="utf-8", errors="replace")
        new_prefix = str(new_data_path / bottle_name) + "/"
        # Match any indented "path: <anything>/<bottle_name>/<rest>" line and
        # replace the prefix up to /<bottle_name>/ with the new location.
        pattern = re.compile(
            r"(^\s+path:\s+).*?" + re.escape(bottle_name) + r"/(.+)$",
            re.MULTILINE,
        )
        patched = pattern.sub(lambda m: m.group(1) + new_prefix + m.group(2), text)
        if patched != text:
            yml_path.write_text(patched, encoding="utf-8")
    except OSError:
        pass


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise InstallCancelled("Installation cancelled")



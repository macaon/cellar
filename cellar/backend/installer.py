"""Download, verify, extract, and import a Bottles backup.

Install flow
------------
1. **Acquire** — for local archives (``file://`` or bare path) the file is
   used in-place; for HTTP(S) it is streamed to a temp file in 1 MB chunks
   with progress reporting and cancel support.  SSH/SMB/NFS archives raise
   ``InstallError`` (not yet supported).
2. **Verify** — SHA-256 checksum checked against ``AppEntry.archive_sha256``
   (skipped when the field is empty).
3. **Extract** — ``tarfile`` extracts to a temporary directory.
4. **Identify** — the single top-level directory inside the archive is taken
   as the bottle source; if there are multiple, the one containing
   ``bottle.yml`` is preferred.
5. **Name** — a collision-safe bottle directory name is derived from the
   app ID (e.g. ``my-app``, then ``my-app-2``, ``my-app-3`` …).
6. **Copy** — ``shutil.copytree`` moves the extracted bottle into the Bottles
   data path; a partial copy is cleaned up on failure.
7. **Return** — the caller receives the ``bottle_name`` string and is
   responsible for writing the DB record (``database.mark_installed``).

Threading
---------
All public functions are **blocking** and intended to run on a background
thread.  Progress is reported via an optional
``progress_cb(phase: str, fraction: float)`` callback that is safe to call
from any thread (the UI layer wraps it in ``GLib.idle_add``).
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import tarfile
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


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
    progress_cb: Callable[[str, float], None] | None = None,
    cancel_event: threading.Event | None = None,
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
    progress_cb:
        Optional ``(phase, fraction)`` callback.  *phase* is a human-readable
        string (``"Downloading"``, ``"Verifying"``, ``"Extracting"``,
        ``"Installing"``); *fraction* is in ``[0, 1]``.
    cancel_event:
        ``threading.Event``; when set the operation is aborted and
        ``InstallCancelled`` is raised.

    Returns
    -------
    str
        The bottle directory name used (e.g. ``"my-app"`` or ``"my-app-2"``).
    """
    _check_cancel(cancel_event)
    _report(progress_cb, "Downloading", 0.0)

    with tempfile.TemporaryDirectory(prefix="cellar-install-") as tmp_str:
        tmp = Path(tmp_str)

        # ── Step 1: Acquire archive ────────────────────────────────────
        archive_path = _acquire_archive(
            archive_uri,
            tmp / "archive.tar.gz",
            expected_size=entry.archive_size,
            progress_cb=lambda f: _report(progress_cb, "Downloading", f),
            cancel_event=cancel_event,
        )
        _report(progress_cb, "Downloading", 1.0)

        # ── Step 2: Verify SHA-256 ─────────────────────────────────────
        if entry.archive_sha256:
            _check_cancel(cancel_event)
            _report(progress_cb, "Verifying", 0.0)
            _verify_sha256(archive_path, entry.archive_sha256)

        # ── Step 3: Extract ────────────────────────────────────────────
        _check_cancel(cancel_event)
        _report(progress_cb, "Installing", 0.0)
        extract_dir = tmp / "extracted"
        extract_dir.mkdir()
        _extract_archive(
            archive_path, extract_dir, cancel_event,
            progress_cb=lambda f: _report(progress_cb, "Installing", f * 0.7),
        )

        # ── Step 4: Identify bottle directory ─────────────────────────
        bottle_src = _find_bottle_dir(extract_dir)

        # ── Step 5: Collision-safe name ────────────────────────────────
        bottle_name = _safe_bottle_name(entry.id, bottles_install.data_path)
        bottle_dest = bottles_install.data_path / bottle_name

        # ── Step 6: Copy into Bottles ──────────────────────────────────
        _check_cancel(cancel_event)
        _report(progress_cb, "Installing", 0.7)
        try:
            shutil.copytree(bottle_src, bottle_dest)
        except Exception:
            shutil.rmtree(bottle_dest, ignore_errors=True)
            raise

    _report(progress_cb, "Installing", 1.0)
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
    cancel_event: threading.Event | None,
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
            cancel_event=cancel_event,
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
                cancel_event=cancel_event,
            )
        else:
            # FUSE not available — stream via a GIO InputStream instead.
            _gio_stream(
                uri,
                dest,
                expected_size=expected_size,
                progress_cb=progress_cb,
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
    cancel_event: threading.Event | None,
) -> None:
    """Copy *src* to *dest* in 1 MB chunks with progress.

    Used for GVFS FUSE paths where the file appears local but reads are
    actually network I/O.
    """
    chunk = 1 * 1024 * 1024
    total = expected_size if expected_size > 0 else src.stat().st_size
    copied = 0
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
    cancel_event: threading.Event | None,
) -> None:
    """Stream *url* to *dest* in 1 MB chunks."""
    chunk = 1 * 1024 * 1024
    downloaded = 0
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310
            with open(dest, "wb") as fh:
                while True:
                    if cancel_event and cancel_event.is_set():
                        raise InstallCancelled("Download cancelled")
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    fh.write(buf)
                    downloaded += len(buf)
                    if progress_cb and expected_size > 0:
                        progress_cb(min(downloaded / expected_size, 1.0))
    except InstallCancelled:
        dest.unlink(missing_ok=True)
        raise
    except urllib.error.HTTPError as exc:
        dest.unlink(missing_ok=True)
        raise InstallError(f"HTTP {exc.code} downloading archive: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        dest.unlink(missing_ok=True)
        raise InstallError(f"Network error downloading archive: {exc.reason}") from exc
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise InstallError(f"Could not write archive to disk: {exc}") from exc


def _gio_stream(
    uri: str,
    dest: Path,
    *,
    expected_size: int,
    progress_cb: Callable[[float], None] | None,
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

def _verify_sha256(path: Path, expected: str) -> None:
    """Raise ``InstallError`` if *path* does not match *expected* SHA-256 hex."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    actual = h.hexdigest()
    if actual != expected:
        raise InstallError(
            f"SHA256 mismatch — archive may be corrupt or tampered.\n"
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
) -> None:
    """Extract *archive_path* into *dest*, reporting per-member progress.

    Uses ``filter='data'`` on Python 3.12+ to strip unsafe tar entries.
    Progress is based on compressed bytes consumed (file position) rather
    than calling ``getmembers()`` upfront, which would scan the entire
    archive before extracting anything — unacceptable for large files.
    """
    _check_cancel(cancel_event)
    use_filter = sys.version_info >= (3, 12)
    try:
        total = archive_path.stat().st_size or 1
        with open(archive_path, "rb") as raw:
            with tarfile.open(fileobj=raw, mode="r:gz") as tf:
                for member in tf:
                    _check_cancel(cancel_event)
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

def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise InstallCancelled("Installation cancelled")


def _report(
    progress_cb: Callable[[str, float], None] | None,
    phase: str,
    fraction: float,
) -> None:
    if progress_cb:
        progress_cb(phase, fraction)

"""Download, verify, extract, and install a Cellar archive.

Install flow (Windows / umu apps)
----------------------------------
1. **Acquire** — for local archives (``file://`` or bare path) the file is
   used in-place; for HTTP(S) it is streamed to a temp file in 1 MB chunks
   with progress reporting and cancel support.  SSH/SMB/NFS archives raise
   ``InstallError`` (not yet supported).
2. **Verify** — CRC32 checksum checked against ``AppEntry.archive_crc32``
   (skipped when the field is empty).
3. **Extract** — ``tarfile`` extracts to a temporary directory.
4. **Identify** — the single top-level directory inside the archive is taken
   as the prefix source.  Both Cellar-native archives (``prefix/`` top-level)
   and legacy Bottles archives (arbitrary name, may contain ``bottle.yml``)
   are accepted.  ``bottle.yml`` is ignored if present.
5. **Copy** — the extracted directory is copied to
   ``umu.prefixes_dir() / app_id``; a partial copy is cleaned up on failure.
6. **Return** — the caller receives ``entry.id`` as the ``prefix_dir`` string
   and is responsible for writing the DB record (``database.mark_installed``).

Threading
---------
All public functions are **blocking** and intended to run on a background
thread.  Progress is reported via an optional
``progress_cb(phase: str, fraction: float)`` callback that is safe to call
from any thread (the UI layer wraps it in ``GLib.idle_add``).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zlib
from collections.abc import Iterable, Iterator
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
# Streaming pipeline
# ---------------------------------------------------------------------------

class _PipedSource:
    """File-like wrapper around a bytes-chunk iterator.

    Accumulates a running CRC32 as chunks are consumed and fires
    *progress_cb* / *stats_cb* after each chunk is ingested.
    Passed directly to ``tarfile.open(fileobj=…)`` or
    ``zstd.ZstdDecompressor().stream_reader(…)``.
    """

    def __init__(
        self,
        chunks: Iterator[bytes],
        total: int,
        progress_cb: Callable[[float], None] | None,
        stats_cb: Callable[[int, int, float], None] | None,
        cancel_event: threading.Event | None,
    ) -> None:
        self._chunks = chunks
        self._total = total
        self._progress_cb = progress_cb
        self._stats_cb = stats_cb
        self._cancel_event = cancel_event
        self._buf = bytearray()
        self._crc = 0
        self._received = 0
        self._start = time.monotonic()

    def read(self, n: int = -1) -> bytes:
        if n == -1:
            while True:
                try:
                    self._ingest(next(self._chunks))
                except StopIteration:
                    break
            data = bytes(self._buf)
            del self._buf[:]
            return data
        while len(self._buf) < n:
            if self._cancel_event and self._cancel_event.is_set():
                raise InstallCancelled("Download cancelled")
            try:
                self._ingest(next(self._chunks))
            except StopIteration:
                break
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def _ingest(self, chunk: bytes) -> None:
        self._crc = zlib.crc32(chunk, self._crc)
        self._buf.extend(chunk)
        self._received += len(chunk)
        elapsed = time.monotonic() - self._start
        speed = self._received / elapsed if elapsed > 0.1 else 0.0
        if self._stats_cb:
            self._stats_cb(self._received, self._total, speed)
        if self._progress_cb and self._total > 0:
            self._progress_cb(min(self._received / self._total, 1.0))

    @property
    def crc(self) -> int:
        return self._crc & 0xFFFFFFFF


def _file_chunks(path: Path) -> Iterator[bytes]:
    """Yield 1 MB chunks from a local *path*."""
    _CHUNK = 1 * 1024 * 1024
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                yield chunk
    except OSError as exc:
        raise InstallError(f"Could not read archive: {exc}") from exc


def _http_source(
    url: str,
    *,
    expected_size: int,
    token: str | None,
    ssl_verify: bool,
    ca_cert: str | None,
) -> tuple[Iterator[bytes], int]:
    """Open *url* and return ``(chunk_iterator, content_length)``."""
    session = make_session(token=token, ssl_verify=ssl_verify, ca_cert=ca_cert)
    try:
        resp = session.get(url, stream=True, timeout=DEFAULT_TIMEOUT)
        resp.raise_for_status()
    except requests.HTTPError as exc:
        code = exc.response.status_code if exc.response is not None else "?"
        raise InstallError(f"HTTP {code} downloading archive") from exc
    except requests.RequestException as exc:
        raise InstallError(f"Network error downloading archive: {exc}") from exc
    try:
        total = int(resp.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        total = 0
    total = total or expected_size

    def _iter() -> Iterator[bytes]:
        try:
            for chunk in resp.iter_content(chunk_size=1 * 1024 * 1024):
                yield chunk
        except requests.RequestException as exc:
            raise InstallError(f"Network error downloading archive: {exc}") from exc

    return _iter(), total


def _smb_chunks(unc: str) -> Iterator[bytes]:
    """Yield 1 MB chunks from an SMB UNC path via smbprotocol."""
    import smbclient  # type: ignore[import]
    _CHUNK = 1 * 1024 * 1024
    try:
        with smbclient.open_file(unc, mode="rb", share_access="r") as fh:
            for chunk in iter(lambda: fh.read(_CHUNK), b""):
                yield chunk
    except Exception as exc:
        raise InstallError(f"SMB read error for {unc}: {exc}") from exc




def _build_source(
    uri: str,
    *,
    expected_size: int,
    token: str | None = None,
    ssl_verify: bool = True,
    ca_cert: str | None = None,
) -> tuple[Iterator[bytes], int]:
    """Return ``(chunk_iterator, total_bytes)`` for *uri*.

    Raises ``InstallError`` for unsupported schemes (e.g. SSH).
    """
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()

    if scheme in ("", "file"):
        local = Path(parsed.path if scheme == "file" else uri)
        size = expected_size if expected_size > 0 else local.stat().st_size
        return _file_chunks(local), size

    if scheme in ("http", "https"):
        return _http_source(
            uri,
            expected_size=expected_size,
            token=token,
            ssl_verify=ssl_verify,
            ca_cert=ca_cert,
        )

    if scheme == "smb":
        # Use smbprotocol directly.  The _SmbFetcher already called
        # smbclient.register_session() for this host, so no credentials
        # are needed here — they are already cached by the library.
        try:
            import smbclient  # type: ignore[import]
            from cellar.utils.smb import smb_uri_to_unc
        except ImportError as exc:
            raise InstallError(
                "smbprotocol is not installed; cannot stream from smb:// URIs"
            ) from exc
        unc = smb_uri_to_unc(uri)
        size = expected_size
        if not size:
            try:
                size = smbclient.stat(unc).st_size
            except Exception:
                size = 0
        return _smb_chunks(unc), size

    raise InstallError(
        f"Downloading from {scheme!r} repos is not yet supported. "
        "Use a local, HTTP(S), SMB, or NFS repo."
    )


def _stream_and_extract(
    chunks: Iterable[bytes],
    total_bytes: int,
    is_zst: bool,
    dest: Path,
    expected_crc32: str,
    cancel_event: threading.Event | None,
    progress_cb: Callable[[float], None] | None,
    stats_cb: Callable[[int, int, float], None] | None,
    name_cb: Callable[[str], None] | None,
    *,
    strip_top_dir: bool = False,
) -> None:
    """Stream *chunks* through CRC32 → decompressor → tarfile in a single pass.

    *expected_crc32* can be ``""`` to skip the checksum comparison.
    When *strip_top_dir* is ``True`` the single top-level directory present in
    the archive is stripped so members land directly inside *dest* (e.g.
    ``prefix/drive_c/…`` becomes ``drive_c/…``).
    Raises ``InstallError`` on extraction failure or checksum mismatch.
    Raises ``InstallCancelled`` if *cancel_event* is set mid-stream.
    """
    use_filter = sys.version_info >= (3, 12)
    pipe = _PipedSource(chunks, total_bytes, progress_cb, stats_cb, cancel_event)

    def _extract_member(tf: tarfile.TarFile, member: tarfile.TarInfo) -> None:
        if strip_top_dir:
            parts = Path(member.name).parts
            if len(parts) <= 1:
                return  # skip the top-level directory entry itself
            member.name = str(Path(*parts[1:]))
        if name_cb:
            name_cb(Path(member.name).name or member.name)
        if use_filter:
            tf.extract(member, dest, filter="tar")
        else:
            tf.extract(member, dest)  # noqa: S202

    try:
        if is_zst:
            try:
                import zstandard as zstd  # noqa: PLC0415
            except ImportError as exc:
                raise InstallError(
                    "zstandard is not installed; cannot extract .tar.zst archives"
                ) from exc
            dctx = zstd.ZstdDecompressor()
            with dctx.stream_reader(pipe) as decompressed:
                with tarfile.open(fileobj=decompressed, mode="r|") as tf:
                    for member in tf:
                        if cancel_event and cancel_event.is_set():
                            raise InstallCancelled("Cancelled during extraction")
                        _extract_member(tf, member)
        else:
            with tarfile.open(fileobj=pipe, mode="r:gz") as tf:
                for member in tf:
                    if cancel_event and cancel_event.is_set():
                        raise InstallCancelled("Cancelled during extraction")
                    _extract_member(tf, member)
    except InstallCancelled:
        raise
    except tarfile.TarError as exc:
        raise InstallError(f"Failed to extract archive: {exc}") from exc

    if expected_crc32:
        actual = format(pipe.crc, "08x")
        if actual != expected_crc32:
            raise InstallError(
                f"CRC32 mismatch — archive may be corrupt or tampered.\n"
                f"  expected: {expected_crc32}\n"
                f"  actual:   {actual}"
            )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def install_app(
    entry,                          # AppEntry — avoid circular import at module level
    archive_uri: str,               # resolved by Repo.resolve_asset_uri(entry.archive)
    *,
    base_entry=None,                # BaseEntry if entry.base_runner is set
    base_archive_uri: str = "",     # resolved URI for the base archive
    download_cb: Callable[[float], None] | None = None,
    download_stats_cb: Callable[[int, int, float], None] | None = None,
    install_cb: Callable[[float], None] | None = None,
    extract_name_cb: Callable[[str], None] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    token: str | None = None,
    ssl_verify: bool = True,
    ca_cert: str | None = None,
) -> str:
    """Download, verify, extract, and install *entry* to the Cellar prefix store.

    CRC32 verification is performed inline during streaming via ``_PipedSource``.

    Parameters
    ----------
    entry:
        The ``AppEntry`` to install.
    archive_uri:
        Absolute path or HTTP(S) URL of the archive, as returned by
        ``Repo.resolve_asset_uri(entry.archive)``.
    download_cb:
        Optional ``(fraction)`` callback for the download phase (0 → 1).
    install_cb:
        Optional ``(fraction)`` callback for the extract phase (0 → 1).
    phase_cb:
        Optional ``(label)`` callback; called at each phase transition
        so the UI can update the status label and reset the bar.
    cancel_event:
        ``threading.Event``; when set the operation is aborted and
        ``InstallCancelled`` is raised.

    Returns
    -------
    str
        ``entry.id`` — the prefix directory name under ``umu.prefixes_dir()``.
    """
    _check_cancel(cancel_event)

    from cellar.backend.umu import prefixes_dir  # noqa: PLC0415
    from cellar.backend.config import install_data_dir  # noqa: PLC0415
    bottle_dest = prefixes_dir() / entry.id

    # ── Step 0 (delta only): Ensure base image is installed ────────────
    if entry.base_runner:
        _ensure_base_installed(
            entry.base_runner,
            base_entry=base_entry,
            base_archive_uri=base_archive_uri,
            phase_cb=phase_cb,
            download_cb=download_cb,
            download_stats_cb=download_stats_cb,
            install_cb=install_cb,
            cancel_event=cancel_event,
            token=token,
            ssl_verify=ssl_verify,
            ca_cert=ca_cert,
        )

    # ── Steps 1-3: Stream, verify CRC32, extract (single pass) ─────────
    if phase_cb:
        phase_cb("Downloading & extracting\u2026")
    if download_cb:
        download_cb(0.0)
    if install_cb:
        install_cb(0.0)
    _check_cancel(cancel_event)

    chunks, total = _build_source(
        archive_uri,
        expected_size=entry.archive_size,
        token=token,
        ssl_verify=ssl_verify,
        ca_cert=ca_cert,
    )

    if entry.base_runner:
        # Delta path: extract to a temp dir on the same filesystem, then
        # seed from base + overlay.  The delta is small so temp space is fine.
        with tempfile.TemporaryDirectory(
            prefix="cellar-delta-", dir=install_data_dir()
        ) as tmp_str:
            delta_dir = Path(tmp_str) / "delta"
            delta_dir.mkdir()
            _stream_and_extract(
                chunks, total,
                is_zst=archive_uri.endswith(".tar.zst"),
                dest=delta_dir,
                expected_crc32=entry.archive_crc32,
                cancel_event=cancel_event,
                progress_cb=download_cb,
                stats_cb=download_stats_cb,
                name_cb=extract_name_cb,
            )
            delta_src = _find_bottle_dir(delta_dir)

            from cellar.backend.base_store import base_path  # noqa: PLC0415
            if phase_cb:
                phase_cb("Applying delta\u2026")
            try:
                bottle_dest.mkdir(parents=True, exist_ok=True)
                _seed_from_base(base_path(entry.base_runner), bottle_dest,
                                cancel_event=cancel_event)
                _overlay_delta(delta_src, bottle_dest, cancel_event=cancel_event)
            except Exception:
                shutil.rmtree(bottle_dest, ignore_errors=True)
                raise
    else:
        # Full archive path: stream directly into the final prefix directory.
        # strip_top_dir removes the single top-level "prefix/" component so
        # drive_c/, dosdevices/, etc. land directly under bottle_dest.
        if phase_cb:
            phase_cb("Installing\u2026")
        bottle_dest.mkdir(parents=True, exist_ok=True)
        try:
            _stream_and_extract(
                chunks, total,
                is_zst=archive_uri.endswith(".tar.zst"),
                dest=bottle_dest,
                expected_crc32=entry.archive_crc32,
                cancel_event=cancel_event,
                progress_cb=download_cb,
                stats_cb=download_stats_cb,
                name_cb=extract_name_cb,
                strip_top_dir=True,
            )
        except Exception:
            shutil.rmtree(bottle_dest, ignore_errors=True)
            raise

    if phase_cb:
        phase_cb("Done")

    from cellar.backend.manifest import write_manifest  # noqa: PLC0415
    write_manifest(bottle_dest)

    if install_cb:
        install_cb(1.0)
    return entry.id


# ---------------------------------------------------------------------------
# Linux native app installer
# ---------------------------------------------------------------------------

def install_linux_app(
    entry,                          # AppEntry with platform == "linux"
    archive_uri: str,
    *,
    download_cb: Callable[[float], None] | None = None,
    download_stats_cb: Callable[[int, int, float], None] | None = None,
    install_cb: Callable[[float], None] | None = None,
    extract_name_cb: Callable[[str], None] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    token: str | None = None,
    ssl_verify: bool = True,
    ca_cert: str | None = None,
) -> tuple[str, Path]:
    """Download, verify, extract, and install a Linux native app.

    Installs to ``~/.local/share/cellar/native/<entry.id>/``.

    Returns ``(entry.id, install_dest)``.

    The caller is responsible for writing the DB record via
    ``database.mark_installed`` with ``platform="linux"``.
    """
    from cellar.backend.umu import native_dir  # noqa: PLC0415
    _check_cancel(cancel_event)

    install_dest = native_dir() / entry.id

    # ── Steps 1-3: Stream, verify CRC32, extract directly ──────────────
    if phase_cb:
        phase_cb("Downloading & extracting\u2026")
    if download_cb:
        download_cb(0.0)
    if install_cb:
        install_cb(0.0)
    _check_cancel(cancel_event)

    chunks, total = _build_source(
        archive_uri,
        expected_size=entry.archive_size,
        token=token,
        ssl_verify=ssl_verify,
        ca_cert=ca_cert,
    )

    # Stream directly into the final install directory, stripping the single
    # top-level directory from the archive.
    install_dest.mkdir(parents=True, exist_ok=True)
    try:
        _stream_and_extract(
            chunks, total,
            is_zst=archive_uri.endswith(".tar.zst"),
            dest=install_dest,
            expected_crc32=entry.archive_crc32,
            cancel_event=cancel_event,
            progress_cb=download_cb,
            stats_cb=download_stats_cb,
            name_cb=extract_name_cb,
            strip_top_dir=True,
        )
    except Exception:
        shutil.rmtree(install_dest, ignore_errors=True)
        raise

    # ── Step 4: Ensure entry_point is executable ───────────────────────
    if entry.entry_point:
        ep = install_dest / entry.entry_point
        if ep.is_file():
            ep.chmod(ep.stat().st_mode | 0o111)

    from cellar.backend.manifest import write_manifest  # noqa: PLC0415
    write_manifest(install_dest)

    if install_cb:
        install_cb(1.0)
    return entry.id, install_dest


def _safe_linux_name(dir_name: str, base_path: Path) -> str:
    """Return an install directory name that does not collide with existing dirs."""
    if not (base_path / dir_name).exists():
        return dir_name
    i = 2
    while (base_path / f"{dir_name}-{i}").exists():
        i += 1
    return f"{dir_name}-{i}"




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

    Supports ``.tar.gz`` and ``.tar.zst`` (via the ``zstandard`` package).
    Uses ``filter='tar'`` on Python 3.12+ to block path traversal while allowing
    absolute symlinks (needed for Wine prefix device entries like dosdevices/).
    Progress is based on compressed bytes consumed so that large archives
    report progress without needing an upfront member scan.

    *name_cb*, when provided, is called as ``name_cb(filename)`` before
    each member is extracted so the UI can show the current file name.
    """
    _check_cancel(cancel_event)
    use_filter = sys.version_info >= (3, 12)
    if str(archive_path).endswith(".tar.zst"):
        _extract_zst(archive_path, dest, cancel_event, progress_cb, name_cb, use_filter)
    else:
        _extract_gz(archive_path, dest, cancel_event, progress_cb, name_cb, use_filter)


def _extract_gz(
    archive_path: Path,
    dest: Path,
    cancel_event: threading.Event | None,
    progress_cb: Callable[[float], None] | None,
    name_cb: Callable[[str], None] | None,
    use_filter: bool,
) -> None:
    """Extract a .tar.gz archive with progress via compressed file position."""
    try:
        total = archive_path.stat().st_size or 1
        with open(archive_path, "rb") as raw:
            with tarfile.open(fileobj=raw, mode="r:gz") as tf:
                for member in tf:
                    _check_cancel(cancel_event)
                    if name_cb:
                        name_cb(Path(member.name).name or member.name)
                    if use_filter:
                        tf.extract(member, dest, filter="tar")
                    else:
                        tf.extract(member, dest)  # noqa: S202
                    if progress_cb:
                        progress_cb(min(raw.tell() / total, 1.0))
    except tarfile.TarError as exc:
        raise InstallError(f"Failed to extract archive: {exc}") from exc


def _extract_zst(
    archive_path: Path,
    dest: Path,
    cancel_event: threading.Event | None,
    progress_cb: Callable[[float], None] | None,
    name_cb: Callable[[str], None] | None,
    use_filter: bool,
) -> None:
    """Extract a .tar.zst archive with progress via compressed bytes read."""
    try:
        import zstandard as zstd  # noqa: PLC0415
    except ImportError as exc:
        raise InstallError(
            "zstandard is not installed; cannot extract .tar.zst archives"
        ) from exc

    total = archive_path.stat().st_size or 1

    class _CountingReader:
        """Wraps a binary file and counts compressed bytes read for progress."""
        def __init__(self, fh):
            self._fh = fh
            self.pos = 0

        def read(self, n: int = -1) -> bytes:
            data = self._fh.read(n)
            self.pos += len(data)
            return data

    try:
        dctx = zstd.ZstdDecompressor()
        with open(archive_path, "rb") as raw:
            reader = _CountingReader(raw)
            with dctx.stream_reader(reader) as decompressed:
                with tarfile.open(fileobj=decompressed, mode="r|") as tf:
                    for member in tf:
                        _check_cancel(cancel_event)
                        if name_cb:
                            name_cb(Path(member.name).name or member.name)
                        if use_filter:
                            tf.extract(member, dest, filter="tar")
                        else:
                            tf.extract(member, dest)  # noqa: S202
                        if progress_cb:
                            progress_cb(min(reader.pos / total, 1.0))
    except tarfile.TarError as exc:
        raise InstallError(f"Failed to extract archive: {exc}") from exc


# ---------------------------------------------------------------------------
# Identify
# ---------------------------------------------------------------------------

def _find_bottle_dir(extract_dir: Path) -> Path:
    """Return the prefix source directory inside *extract_dir*.

    Expects a single top-level directory.  Both Cellar-native archives
    (``prefix/`` top-level) and legacy Bottles archives (arbitrary name,
    may contain ``bottle.yml``) are accepted; ``bottle.yml`` is ignored.

    When there are multiple top-level directories the one named ``prefix``
    is preferred (Cellar-native format), then one containing ``bottle.yml``
    (legacy Bottles format), otherwise raises ``InstallError``.
    """
    dirs = [d for d in extract_dir.iterdir() if d.is_dir()]

    if not dirs:
        raise InstallError(
            "Archive contains no directories; expected a top-level prefix directory."
        )

    if len(dirs) == 1:
        return dirs[0]

    # Prefer the Cellar-native top-level name.
    for d in dirs:
        if d.name == "prefix":
            return d

    # Fall back to a Bottles-format archive.
    with_yml = [d for d in dirs if (d / "bottle.yml").exists()]
    if len(with_yml) == 1:
        return with_yml[0]

    raise InstallError(
        f"Cannot identify prefix directory in archive "
        f"({len(dirs)} top-level directories found; expected exactly one)."
    )


def _check_cancel(cancel_event: threading.Event | None) -> None:
    if cancel_event and cancel_event.is_set():
        raise InstallCancelled("Installation cancelled")


# ---------------------------------------------------------------------------
# Delta helpers
# ---------------------------------------------------------------------------

def _ensure_base_installed(
    runner: str,
    *,
    base_entry,
    base_archive_uri: str,
    phase_cb: Callable[[str], None] | None,
    download_cb: Callable[[float], None] | None,
    download_stats_cb: Callable[[int, int, float], None] | None,
    install_cb: Callable[[float], None] | None,
    cancel_event: threading.Event | None,
    token: str | None,
    ssl_verify: bool,
    ca_cert: str | None,
) -> None:
    """Download, verify, and install the base image for *runner* if not already present.

    Streams directly into ``bases_dir()/runner/`` — no staging in /tmp.
    CRC32 verification is performed inline during streaming.
    """
    from cellar.backend.base_store import base_path, is_base_installed  # noqa: PLC0415
    from cellar.backend import database  # noqa: PLC0415

    if is_base_installed(runner):
        return

    if not base_archive_uri:
        raise InstallError(
            f"No base image installed for {runner!r} and no download URI provided."
        )

    _check_cancel(cancel_event)
    if phase_cb:
        phase_cb(f"Downloading & extracting base image ({runner})\u2026")
    if download_cb:
        download_cb(0.0)

    expected_size = base_entry.archive_size if base_entry else 0
    expected_crc32 = (base_entry.archive_crc32 if base_entry else "") or ""

    dest = base_path(runner)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        chunks, total = _build_source(
            base_archive_uri,
            expected_size=expected_size,
            token=token,
            ssl_verify=ssl_verify,
            ca_cert=ca_cert,
        )
        _stream_and_extract(
            chunks, total,
            is_zst=base_archive_uri.endswith(".tar.zst"),
            dest=dest,
            expected_crc32=expected_crc32,
            cancel_event=cancel_event,
            progress_cb=download_cb,
            stats_cb=download_stats_cb,
            name_cb=None,
            strip_top_dir=True,
        )
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise

    database.mark_base_installed(runner, base_archive_uri)

    if download_cb:
        download_cb(1.0)
    if install_cb:
        install_cb(1.0)


def _seed_from_base(
    base_dir: Path,
    bottle_dest: Path,
    cancel_event: threading.Event | None = None,
) -> None:
    """Populate *bottle_dest* with copies of every file in *base_dir*.

    Uses ``cp --reflink=auto`` which creates copy-on-write clones on btrfs
    and XFS, so Wine can freely update system files after a runner change
    without touching the shared base image.  On other filesystems
    ``--reflink=auto`` silently falls back to a regular copy.

    Falls back to a pure-Python copy walk if ``cp`` is unavailable.
    """
    cp = shutil.which("cp")
    if cp:
        proc = subprocess.Popen(
            [cp, "-a", "--reflink=auto", f"{base_dir}/.", f"{bottle_dest}/"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                proc.kill()
                proc.wait()
                raise InstallCancelled("Cancelled during base seeding")
            time.sleep(0.05)
        if proc.returncode == 0:
            return
        # cp failed — fall through to the Python path.

    # Python fallback: copy file-by-file.
    for src in base_dir.rglob("*"):
        _check_cancel(cancel_event)
        rel = src.relative_to(base_dir)
        dst = bottle_dest / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _overlay_delta(
    delta_src: Path,
    bottle_dest: Path,
    cancel_event: threading.Event | None = None,
) -> None:
    """Overlay delta files onto *bottle_dest*.

    For each file in *delta_src*, any existing file at the destination is
    removed before copying so the base content is not modified in-place.
    Directories are created as needed.

    After copying, applies the ``.cellar_delete`` manifest (if present) to
    remove any base files that were absent from the original app backup.
    """
    if shutil.which("rsync"):
        proc = subprocess.Popen(
            ["rsync", "-a", "--exclude=.cellar_delete",
             f"{delta_src}/", f"{bottle_dest}/"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        while proc.poll() is None:
            if cancel_event and cancel_event.is_set():
                proc.kill()
                proc.wait()
                raise InstallCancelled("Cancelled during delta overlay")
            time.sleep(0.05)
        if proc.returncode != 0:
            _overlay_delta_python(delta_src, bottle_dest, cancel_event)
    else:
        _overlay_delta_python(delta_src, bottle_dest, cancel_event)

    # Apply delete manifest: remove base files absent from the original backup.
    _check_cancel(cancel_event)
    delete_manifest = delta_src / ".cellar_delete"
    if delete_manifest.exists():
        for line in delete_manifest.read_text().splitlines():
            rel = line.strip()
            if rel:
                (bottle_dest / rel).unlink(missing_ok=True)


def _overlay_delta_python(
    delta_src: Path,
    bottle_dest: Path,
    cancel_event: threading.Event | None = None,
) -> None:
    """Python fallback for delta overlay."""
    for src in delta_src.rglob("*"):
        _check_cancel(cancel_event)
        if src.name == ".cellar_delete":
            continue
        rel = src.relative_to(delta_src)
        dst = bottle_dest / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.unlink(missing_ok=True)   # break hardlink to base inode
            shutil.copy2(src, dst)



"""Download, verify, extract, and install a Cellar archive.

Install flow (Windows / umu apps)
----------------------------------
1. **Acquire** — for local archives (``file://`` or bare path) the file is
   used in-place; for HTTP(S) it is streamed to a temp file in 1 MB chunks
   with progress reporting and cancel support.  SFTP and SMB archives are
   streamed via their respective pure-Python transports.
2. **Verify** — CRC32 checksum checked against ``AppEntry.archive_crc32``
   (skipped when the field is empty).
3. **Extract** — delta archive extracted to a temporary directory.
4. **Seed + overlay** — prefix is seeded from the base image via CoW copy,
   then the delta is overlaid on top.
5. **Return** — the caller receives ``entry.id`` as the ``prefix_dir`` string
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


def _ssh_chunks(
    host: str,
    remote_path: str,
    *,
    user: str | None = None,
    port: int | None = None,
    identity: str | None = None,
) -> Iterator[bytes]:
    """Stream *remote_path* from *host* via ``paramiko`` SFTP, yielding 1 MB chunks."""
    from cellar.utils.ssh import _get_sftp, _return_sftp

    _CHUNK = 1 * 1024 * 1024
    _port = port or 22
    sftp = _get_sftp(host, _port, user, identity)
    try:
        with sftp.open(remote_path, "rb", bufsize=_CHUNK) as f:
            f.MAX_REQUEST_SIZE = _CHUNK  # 1 MB per request vs 32 KB default
            f.prefetch()
            while True:
                chunk = f.read(_CHUNK)
                if not chunk:
                    break
                yield chunk
    except FileNotFoundError:
        raise InstallError(f"SSH file not found: {remote_path}")
    except Exception as exc:
        raise InstallError(f"SSH stream error for {remote_path}: {exc}") from exc
    finally:
        _return_sftp(host, _port, user, identity, sftp)




def _build_source(
    uri: str,
    *,
    expected_size: int,
    token: str | None = None,
    ssl_verify: bool = True,
    ca_cert: str | None = None,
    ssh_identity: str | None = None,
) -> tuple[Iterator[bytes], int]:
    """Return ``(chunk_iterator, total_bytes)`` for *uri*."""
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

    if scheme == "sftp":
        if not parsed.hostname:
            raise InstallError(f"Invalid SFTP URI (no host): {uri!r}")
        return _ssh_chunks(
            parsed.hostname,
            parsed.path,
            user=parsed.username or None,
            port=parsed.port or None,
            identity=ssh_identity,
        ), expected_size

    raise InstallError(
        f"Downloading from {scheme!r} repos is not supported. "
        "Use a local, HTTP(S), SFTP, or SMB repo."
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
# Chunked archive helpers
# ---------------------------------------------------------------------------

def _preflight_check_chunks(
    archive_uri: str,
    n_chunks: int,
    *,
    token: str | None = None,
    ssl_verify: bool = True,
    ca_cert: str | None = None,
    ssh_identity: str | None = None,
) -> None:
    """Verify that all chunk files exist before starting a download.

    Raises ``InstallError`` listing any missing chunks.  For HTTP(S) repos
    this uses HEAD requests; for local/SMB/SSH it uses stat-like checks.
    """
    from cellar.models.app_entry import chunk_filename  # noqa: PLC0415

    parsed = urlparse(archive_uri)
    scheme = parsed.scheme.lower()
    missing: list[str] = []

    for i in range(1, n_chunks + 1):
        uri = chunk_filename(archive_uri, i)
        try:
            if scheme in ("", "file"):
                p = Path(parsed.path if scheme == "file" else uri)
                if not p.exists():
                    missing.append(uri)
            elif scheme in ("http", "https"):
                session = make_session(
                    token=token, ssl_verify=ssl_verify, ca_cert=ca_cert,
                )
                resp = session.head(uri, timeout=DEFAULT_TIMEOUT)
                if resp.status_code >= 400:
                    missing.append(uri)
            elif scheme == "smb":
                import smbclient  # type: ignore[import]

                from cellar.utils.smb import smb_uri_to_unc
                unc = smb_uri_to_unc(uri)
                try:
                    smbclient.stat(unc)
                except Exception:
                    missing.append(uri)
            elif scheme == "sftp":
                from cellar.utils.ssh import _get_sftp, _return_sftp
                host = parsed.hostname or ""
                port = parsed.port or 22
                user = parsed.username or None
                sftp = _get_sftp(host, port, user, ssh_identity)
                try:
                    chunk_path = urlparse(uri).path
                    sftp.stat(chunk_path)
                except FileNotFoundError:
                    missing.append(uri)
                finally:
                    _return_sftp(host, port, user, ssh_identity, sftp)
        except Exception:
            # If the check itself fails, skip preflight rather than
            # blocking the install — the download will fail with a
            # more specific error anyway.
            return

    if missing:
        names = ", ".join(Path(m).name for m in missing)
        raise InstallError(
            f"Missing archive chunks on server: {names}. "
            f"The repository may be incomplete or corrupted."
        )


def _install_chunks(
    archive_uri: str,
    archive_chunks: tuple[dict, ...],
    dest: Path,
    *,
    strip_top_dir: bool = False,
    cancel_event: threading.Event | None = None,
    progress_cb: Callable[[float], None] | None = None,
    stats_cb: Callable[[int, int, float], None] | None = None,
    name_cb: Callable[[str], None] | None = None,
    token: str | None = None,
    ssl_verify: bool = True,
    ca_cert: str | None = None,
    ssh_identity: str | None = None,
) -> None:
    """Download, verify, extract, and delete chunks one at a time.

    Each chunk is an independently extractable ``.tar.zst`` archive.
    Peak temporary disk usage is one chunk (~1 GB) plus the already-
    extracted files.

    Progress is reported cumulatively: chunk *i* of *N* reports
    ``(i-1)/N + fraction/N`` so the overall bar advances smoothly.
    """
    from cellar.backend.config import install_data_dir  # noqa: PLC0415
    from cellar.models.app_entry import chunk_filename  # noqa: PLC0415

    n = len(archive_chunks)

    # Verify all chunks exist before downloading anything.
    _preflight_check_chunks(
        archive_uri, n,
        token=token, ssl_verify=ssl_verify,
        ca_cert=ca_cert, ssh_identity=ssh_identity,
    )

    total_size = sum(c["size"] for c in archive_chunks)
    cumulative = 0

    for i, chunk_meta in enumerate(archive_chunks, 1):
        _check_cancel(cancel_event)
        chunk_uri = chunk_filename(archive_uri, i)

        # Download chunk to a temp file on the same filesystem as dest
        # so that we can clean up easily.
        tmp_dir = install_data_dir()
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp = tmp_dir / f".cellar-chunk-{i:03d}.tmp"

        try:
            # ── Download ──────────────────────────────────────────────
            source, sz = _build_source(
                chunk_uri,
                expected_size=chunk_meta["size"],
                token=token,
                ssl_verify=ssl_verify,
                ca_cert=ca_cert,
                ssh_identity=ssh_identity,
            )

            def _wrap_progress(frac: float, _base=cumulative, _chunk_sz=chunk_meta["size"],
                               _total=total_size) -> None:
                if progress_cb and _total > 0:
                    progress_cb((_base + frac * _chunk_sz) / _total)

            def _wrap_stats(received: int, total_bytes: int, speed: float,
                            _base=cumulative, _total=total_size) -> None:
                if stats_cb and _total > 0:
                    stats_cb(_base + received, _total, speed)

            _stream_to_file(source, tmp, chunk_meta["size"], chunk_meta.get("crc32", ""),
                            cancel_event, _wrap_progress, _wrap_stats)

            # ── Extract ───────────────────────────────────────────────
            _stream_and_extract(
                _file_chunks(tmp), chunk_meta["size"],
                is_zst=True,
                dest=dest,
                expected_crc32="",  # already verified during download
                cancel_event=cancel_event,
                progress_cb=None,
                stats_cb=None,
                name_cb=name_cb,
                strip_top_dir=strip_top_dir,
            )
        finally:
            # Always clean up the chunk temp file
            tmp.unlink(missing_ok=True)

        cumulative += chunk_meta["size"]

    if progress_cb:
        progress_cb(1.0)


def _stream_to_file(
    source: Iterator[bytes],
    dest: Path,
    expected_size: int,
    expected_crc32: str,
    cancel_event: threading.Event | None,
    progress_cb: Callable[[float], None] | None,
    stats_cb: Callable[[int, int, float], None] | None,
) -> None:
    """Stream *source* to *dest*, verify CRC32, report progress."""
    crc = 0
    received = 0
    start = time.monotonic()
    try:
        with open(dest, "wb") as f:
            for chunk in source:
                if cancel_event and cancel_event.is_set():
                    raise InstallCancelled("Cancelled during chunk download")
                f.write(chunk)
                crc = zlib.crc32(chunk, crc)
                received += len(chunk)
                elapsed = time.monotonic() - start
                speed = received / elapsed if elapsed > 0.1 else 0.0
                if stats_cb:
                    stats_cb(received, expected_size, speed)
                if progress_cb and expected_size > 0:
                    progress_cb(min(received / expected_size, 1.0))
    except InstallCancelled:
        dest.unlink(missing_ok=True)
        raise
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise InstallError(f"Failed to write chunk: {exc}") from exc

    if expected_crc32:
        actual = format(crc & 0xFFFFFFFF, "08x")
        if actual != expected_crc32:
            dest.unlink(missing_ok=True)
            raise InstallError(
                f"CRC32 mismatch on chunk — archive may be corrupt.\n"
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
    base_entry=None,                # BaseEntry if entry.base_image is set
    base_archive_uri: str = "",     # resolved URI for the base archive
    runner_entry=None,              # RunnerEntry if base_entry is set
    runner_archive_uri: str = "",   # resolved URI for the runner archive
    download_cb: Callable[[float], None] | None = None,
    download_stats_cb: Callable[[int, int, float], None] | None = None,
    install_cb: Callable[[float], None] | None = None,
    extract_name_cb: Callable[[str], None] | None = None,
    phase_cb: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    token: str | None = None,
    ssl_verify: bool = True,
    ca_cert: str | None = None,
    ssh_identity: str | None = None,
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

    from cellar.backend.config import install_data_dir  # noqa: PLC0415
    from cellar.backend.umu import prefixes_dir  # noqa: PLC0415
    prefix_dest = prefixes_dir() / entry.id

    # ── Step 0a: Ensure runner is installed ────────────────────────────
    if base_entry and base_entry.runner:
        _ensure_runner_installed(
            base_entry.runner,
            runner_entry=runner_entry,
            runner_archive_uri=runner_archive_uri,
            phase_cb=phase_cb,
            download_cb=download_cb,
            download_stats_cb=download_stats_cb,
            install_cb=install_cb,
            cancel_event=cancel_event,
            token=token,
            ssl_verify=ssl_verify,
            ca_cert=ca_cert,
            ssh_identity=ssh_identity,
        )

    # ── Step 0b (delta only): Ensure base image is installed ───────────
    if entry.base_image:
        _ensure_base_installed(
            entry.base_image,
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
            ssh_identity=ssh_identity,
        )

    # ── Steps 1-3: Stream, verify CRC32, extract ────────────────────────
    if phase_cb:
        phase_cb("Downloading & extracting package\u2026")
    if download_cb:
        download_cb(0.0)
    if install_cb:
        install_cb(0.0)
    _check_cancel(cancel_event)

    _transport_kw = dict(token=token, ssl_verify=ssl_verify, ca_cert=ca_cert,
                         ssh_identity=ssh_identity)

    # Delta path: extract to a temp dir on the same filesystem, then
    # seed from base + overlay.  The delta is small so temp space is fine.
    with tempfile.TemporaryDirectory(
        prefix="cellar-delta-", dir=install_data_dir()
    ) as tmp_str:
        delta_dir = Path(tmp_str) / "delta"
        delta_dir.mkdir()
        if entry.archive_chunks:
            _install_chunks(
                archive_uri, entry.archive_chunks, delta_dir,
                cancel_event=cancel_event,
                progress_cb=download_cb, stats_cb=download_stats_cb,
                name_cb=extract_name_cb, **_transport_kw,
            )
        else:
            chunks, total = _build_source(
                archive_uri, expected_size=entry.archive_size, **_transport_kw)
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
        delta_src = _find_top_dir(delta_dir)

        from cellar.backend.base_store import base_path  # noqa: PLC0415
        if phase_cb:
            phase_cb("Applying delta\u2026")
        try:
            prefix_dest.mkdir(parents=True, exist_ok=True)
            _seed_from_base(base_path(entry.base_image), prefix_dest,
                            cancel_event=cancel_event)
            _overlay_delta(delta_src, prefix_dest, cancel_event=cancel_event)
        except Exception:
            shutil.rmtree(prefix_dest, ignore_errors=True)
            raise

    from cellar.backend.manifest import write_manifest  # noqa: PLC0415
    write_manifest(prefix_dest)

    # Pre-download steamrt3 on first install so the first launch is instant.
    # Disabled: the prefix is already initialised during the build/package step,
    # and the launch monitor (detail.py) shows progress if umu needs to download
    # steamrt3 on first launch.  Re-enable if clean-system testing reveals issues.
    # from cellar.backend.umu import init_prefix, is_runtime_ready  # noqa: PLC0415
    # if not is_runtime_ready():
    #     if phase_cb:
    #         phase_cb("Initialising prefix\u2026")
    #     init_prefix(prefix_dest, base_entry.runner, steam_appid=entry.steam_appid)

    if phase_cb:
        phase_cb("Done")

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
    ssh_identity: str | None = None,
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
        phase_cb("Downloading & extracting package\u2026")
    if download_cb:
        download_cb(0.0)
    if install_cb:
        install_cb(0.0)
    _check_cancel(cancel_event)

    _transport_kw = dict(token=token, ssl_verify=ssl_verify, ca_cert=ca_cert,
                         ssh_identity=ssh_identity)

    # Stream directly into the final install directory, stripping the single
    # top-level directory from the archive.
    install_dest.mkdir(parents=True, exist_ok=True)
    try:
        if entry.archive_chunks:
            _install_chunks(
                archive_uri, entry.archive_chunks, install_dest,
                strip_top_dir=True,
                cancel_event=cancel_event,
                progress_cb=download_cb, stats_cb=download_stats_cb,
                name_cb=extract_name_cb, **_transport_kw,
            )
        else:
            chunks, total = _build_source(
                archive_uri, expected_size=entry.archive_size, **_transport_kw)
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

def _find_top_dir(extract_dir: Path) -> Path:
    """Return the single top-level directory inside *extract_dir*.

    Archives produced by the packager wrap content in one top-level
    directory.  When there are multiple, the one named ``prefix`` is
    preferred (Windows apps), otherwise raises ``InstallError``.
    """
    dirs = [d for d in extract_dir.iterdir() if d.is_dir()]

    if not dirs:
        raise InstallError(
            "Archive contains no directories; expected a top-level directory."
        )

    if len(dirs) == 1:
        return dirs[0]

    for d in dirs:
        if d.name == "prefix":
            return d

    raise InstallError(
        f"Cannot identify content directory in archive "
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
    ssh_identity: str | None = None,
) -> None:
    """Download, verify, and install the base image for *runner* if not already present.

    Streams directly into ``bases_dir()/runner/`` — no staging in /tmp.
    CRC32 verification is performed inline during streaming.
    """
    from cellar.backend import database  # noqa: PLC0415
    from cellar.backend.base_store import base_path, is_base_installed  # noqa: PLC0415

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

    _transport_kw = dict(token=token, ssl_verify=ssl_verify, ca_cert=ca_cert,
                         ssh_identity=ssh_identity)
    archive_chunks = base_entry.archive_chunks if base_entry else ()

    dest = base_path(runner)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if archive_chunks:
            _install_chunks(
                base_archive_uri, archive_chunks, dest,
                strip_top_dir=True,
                cancel_event=cancel_event,
                progress_cb=download_cb, stats_cb=download_stats_cb,
                **_transport_kw,
            )
        else:
            chunks, total = _build_source(
                base_archive_uri, expected_size=expected_size, **_transport_kw)
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


def _ensure_runner_installed(
    runner_name: str,
    *,
    runner_entry,
    runner_archive_uri: str,
    phase_cb: Callable[[str], None] | None,
    download_cb: Callable[[float], None] | None,
    download_stats_cb: Callable[[int, int, float], None] | None,
    install_cb: Callable[[float], None] | None,
    cancel_event: threading.Event | None,
    token: str | None,
    ssl_verify: bool,
    ca_cert: str | None,
    ssh_identity: str | None = None,
) -> None:
    """Download and install the runner (GE-Proton) if not already present.

    Streams directly into ``runners_dir()/runner_name/``.
    """
    from cellar.backend.umu import resolve_runner_path, runners_dir  # noqa: PLC0415

    if resolve_runner_path(runner_name):
        return  # already installed

    if not runner_archive_uri:
        raise InstallError(
            f"Runner {runner_name!r} is not installed and no download URI provided."
        )

    _check_cancel(cancel_event)
    if phase_cb:
        phase_cb(f"Downloading & extracting runner ({runner_name})\u2026")
    if download_cb:
        download_cb(0.0)

    expected_size = runner_entry.archive_size if runner_entry else 0
    expected_crc32 = (runner_entry.archive_crc32 if runner_entry else "") or ""
    _transport_kw = dict(token=token, ssl_verify=ssl_verify, ca_cert=ca_cert,
                         ssh_identity=ssh_identity)
    archive_chunks = runner_entry.archive_chunks if runner_entry else ()

    dest = runners_dir() / runner_name
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if archive_chunks:
            _install_chunks(
                runner_archive_uri, archive_chunks, dest,
                strip_top_dir=True,
                cancel_event=cancel_event,
                progress_cb=download_cb, stats_cb=download_stats_cb,
                **_transport_kw,
            )
        else:
            chunks, total = _build_source(
                runner_archive_uri, expected_size=expected_size, **_transport_kw)
            _stream_and_extract(
                chunks, total,
                is_zst=runner_archive_uri.endswith(".tar.zst"),
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

    if download_cb:
        download_cb(1.0)
    if install_cb:
        install_cb(1.0)


def _seed_from_base(
    base_dir: Path,
    prefix_dest: Path,
    cancel_event: threading.Event | None = None,
) -> None:
    """Populate *prefix_dest* with copies of every file in *base_dir*.

    Uses ``cp --reflink=auto`` which creates copy-on-write clones on btrfs
    and XFS, so Wine can freely update system files after a runner change
    without touching the shared base image.  On other filesystems
    ``--reflink=auto`` silently falls back to a regular copy.

    Falls back to a pure-Python copy walk if ``cp`` is unavailable.
    """
    cp = shutil.which("cp")
    if cp:
        proc = subprocess.Popen(
            [cp, "-a", "--reflink=auto", f"{base_dir}/.", f"{prefix_dest}/"],
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
        dst = prefix_dest / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)


def _overlay_delta(
    delta_src: Path,
    prefix_dest: Path,
    cancel_event: threading.Event | None = None,
) -> None:
    """Overlay delta files onto *prefix_dest*.

    For each file in *delta_src*, any existing file at the destination is
    removed before copying so the base content is not modified in-place.
    Directories are created as needed.

    After copying, applies the ``.cellar_delete`` manifest (if present) to
    remove any base files that were absent from the original app backup.
    """
    if shutil.which("rsync"):
        proc = subprocess.Popen(
            ["rsync", "-a", "--exclude=.cellar_delete",
             f"{delta_src}/", f"{prefix_dest}/"],
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
            _overlay_delta_python(delta_src, prefix_dest, cancel_event)
    else:
        _overlay_delta_python(delta_src, prefix_dest, cancel_event)

    # Apply delete manifest: remove base files absent from the original backup.
    _check_cancel(cancel_event)
    delete_manifest = delta_src / ".cellar_delete"
    if delete_manifest.exists():
        resolved_dest = prefix_dest.resolve()
        for line in delete_manifest.read_text().splitlines():
            rel = line.strip()
            if not rel:
                continue
            target = (prefix_dest / rel).resolve()
            try:
                target.relative_to(resolved_dest)
            except ValueError:
                log.warning("Skipping out-of-prefix delete entry: %r", rel)
                continue
            target.unlink(missing_ok=True)


def _overlay_delta_python(
    delta_src: Path,
    prefix_dest: Path,
    cancel_event: threading.Event | None = None,
) -> None:
    """Python fallback for delta overlay."""
    for src in delta_src.rglob("*"):
        _check_cancel(cancel_event)
        if src.name == ".cellar_delete":
            continue
        rel = src.relative_to(delta_src)
        dst = prefix_dest / rel
        if src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        elif src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.unlink(missing_ok=True)   # break hardlink to base inode
            shutil.copy2(src, dst)



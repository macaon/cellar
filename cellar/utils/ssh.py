"""SSH path utilities for reading and writing to remote hosts via paramiko.

``SshPath`` mimics the subset of :class:`pathlib.Path` used by
``packager.py`` and related code so that the same functions work for
local filesystem repos, SMB share repos, and SSH repos.

``paramiko`` is a pure-Python SSHv2 implementation — no system ``ssh``
binary is required, making this fully self-contained inside the Flatpak.
Authentication is handled via the SSH agent, ``~/.ssh/config``, or an
explicit identity file.

Connection management
---------------------
``SshPath`` instances share a single :class:`paramiko.SFTPClient` per
(host, port, user, identity) tuple, cached in ``_sftp_cache``.  The
connection is created on first use and reused for all subsequent calls.
"""

from __future__ import annotations

import contextlib
import logging
import stat as _stat
import threading
from pathlib import Path
from typing import IO, TYPE_CHECKING

if TYPE_CHECKING:
    import paramiko

from cellar.utils._remote_path import RemotePathMixin

log = logging.getLogger(__name__)


# ── Connection pool ────────────────────────────────────────────────────
#
# paramiko.SFTPClient is NOT thread-safe — concurrent requests on the same
# channel corrupt the request/response sequence.  We maintain a small pool
# of SFTP channels per connection, all sharing a single SSH transport
# (which IS thread-safe).  Callers that need concurrency (e.g. parallel
# screenshot downloads) each check out their own channel.

_MAX_POOL = 4       # max idle SFTP channels per connection
_WINDOW_SIZE = 2 * 1024 * 1024 * 1024 - 1  # ~2 GB SSH channel window (max allowed)
_MAX_PACKET = 32768  # max SSH packet payload

_transport_cache: dict[tuple, "paramiko.Transport"] = {}
_sftp_pool: dict[tuple, list["paramiko.SFTPClient"]] = {}
_sftp_lock = threading.Lock()


def _get_sftp(
    host: str,
    port: int,
    user: str | None,
    identity: str | None,
    password: str | None = None,
):
    """Check out an :class:`paramiko.SFTPClient` from the connection pool.

    Returns a channel that is safe to use from the calling thread.  When done,
    call :func:`_return_sftp` to return it to the pool for reuse.  If not
    returned, the channel is simply discarded (no leak — the transport stays).
    """
    import paramiko  # type: ignore[import]

    conn_key = (host, port, user, identity)
    with _sftp_lock:
        # Try to pop an idle channel from the pool.
        pool = _sftp_pool.get(conn_key, [])
        while pool:
            sftp = pool.pop()
            try:
                sftp.stat(".")
                return sftp
            except Exception:
                try:
                    sftp.close()
                except Exception:
                    pass

        # Get or create the shared SSH transport for this connection.
        transport = _transport_cache.get(conn_key)
        if transport is not None and not transport.is_active():
            transport = None
            del _transport_cache[conn_key]

        if transport is None:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.WarningPolicy())
            connect_kw: dict = {
                "hostname": host,
                "port": port,
                "username": user,
                "timeout": 30,
                "allow_agent": True,
                "look_for_keys": True,
            }
            if identity:
                connect_kw["key_filename"] = identity
            if password:
                connect_kw["password"] = password
            ssh.load_system_host_keys()
            try:
                ssh.connect(**connect_kw)
            except Exception as exc:
                raise OSError(f"SSH connection to {host}:{port} failed: {exc}") from exc
            transport = ssh.get_transport()
            _transport_cache[conn_key] = transport

        sftp = paramiko.SFTPClient.from_transport(
            transport, window_size=_WINDOW_SIZE, max_packet_size=_MAX_PACKET,
        )
        return sftp


def _return_sftp(
    host: str,
    port: int,
    user: str | None,
    identity: str | None,
    sftp: "paramiko.SFTPClient",
) -> None:
    """Return a checked-out SFTP channel to the pool for reuse."""
    conn_key = (host, port, user, identity)
    with _sftp_lock:
        pool = _sftp_pool.setdefault(conn_key, [])
        if len(pool) < _MAX_POOL:
            pool.append(sftp)
        else:
            try:
                sftp.close()
            except Exception:
                pass


def get_transport(
    host: str,
    port: int,
    user: str | None,
    identity: str | None,
):
    """Return the underlying :class:`paramiko.Transport` for streaming."""
    sftp = _get_sftp(host, port, user, identity)
    transport = sftp.get_channel().get_transport()
    _return_sftp(host, port, user, identity, sftp)
    return transport


# ── Stat result ─────────────────────────────────────────────────────────

class _SshStat:
    """Minimal stat result holding ``st_size`` and ``st_mode``."""

    __slots__ = ("st_size", "st_mode")

    def __init__(self, st_size: int, st_mode: int = 0) -> None:
        self.st_size = st_size
        self.st_mode = st_mode


# ── Write stream ────────────────────────────────────────────────────────

class _SshWriteStream:
    """Writable file-like object wrapping a paramiko SFTP file handle.

    When *sftp* and *conn* are provided, the SFTP channel is returned to the
    connection pool on :meth:`close`.
    """

    __slots__ = ("_fh", "_remote", "_closed", "_sftp", "_conn")

    def __init__(self, fh, remote: str, *,
                 sftp=None, conn: tuple | None = None) -> None:
        self._fh = fh
        self._remote = remote
        self._closed = False
        self._sftp = sftp
        self._conn = conn  # (host, port, user, identity)

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("I/O operation on closed file")
        self._fh.write(data)
        return len(data)

    def flush(self) -> None:
        if not self._closed:
            self._fh.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._fh.close()
        if self._sftp and self._conn:
            _return_sftp(*self._conn, self._sftp)
            self._sftp = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ── SshPath ─────────────────────────────────────────────────────────────

class SshPath(RemotePathMixin):
    """A path-like object backed by ``paramiko`` SFTP.

    Mimics the ``pathlib.Path`` subset used by ``packager.py``:
    path arithmetic (``/``), ``mkdir``, ``read_bytes``, ``write_bytes``,
    ``read_text``, ``write_text``, ``open``, ``unlink``, ``exists``,
    ``is_dir``, ``is_file``, ``stat``, ``iterdir``, plus ``rmtree``
    (equivalent to ``shutil.rmtree``).
    """

    __slots__ = ("_host", "_path", "_user", "_port", "_identity", "_password")

    def __init__(
        self,
        host: str,
        remote_path: str,
        *,
        user: str | None = None,
        port: int | None = None,
        identity: str | None = None,
        password: str | None = None,
    ) -> None:
        self._host = host
        self._path: str = remote_path.rstrip("/") or "/"
        self._user = user
        self._port = port or 22
        self._identity = identity
        self._password = password

    @property
    def _remote_str(self) -> str:
        return self._path

    # ── SFTP access ────────────────────────────────────────────────────

    @contextlib.contextmanager
    def _sftp(self):
        """Check out an SFTP channel from the pool; return it when done."""
        sftp = _get_sftp(self._host, self._port, self._user, self._identity, self._password)
        try:
            yield sftp
        finally:
            _return_sftp(self._host, self._port, self._user, self._identity, sftp)

    def _child(self, name: str) -> "SshPath":
        return SshPath(
            self._host,
            f"{self._path}/{name}",
            user=self._user,
            port=self._port,
            identity=self._identity,
            password=self._password,
        )

    # ── Path arithmetic ────────────────────────────────────────────────

    def __truediv__(self, other: str | Path) -> "SshPath":
        part = str(other).replace("\\", "/").strip("/")
        return SshPath(
            self._host,
            f"{self._path}/{part}",
            user=self._user,
            port=self._port,
            identity=self._identity,
            password=self._password,
        )

    def __str__(self) -> str:
        return self._path

    def __repr__(self) -> str:
        dest = f"{self._user}@{self._host}" if self._user else self._host
        return f"SshPath({dest}:{self._path!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SshPath):
            return (
                self._host == other._host
                and self._path == other._path
                and self._user == other._user
                and self._port == other._port
            )
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self._host, self._path, self._user, self._port))

    @property
    def parent(self) -> "SshPath":
        idx = self._path.rfind("/")
        if idx <= 0:
            return SshPath(
                self._host, "/",
                user=self._user, port=self._port, identity=self._identity,
                password=self._password,
            )
        return SshPath(
            self._host, self._path[:idx],
            user=self._user, port=self._port, identity=self._identity,
            password=self._password,
        )

    # ── Filesystem operations ──────────────────────────────────────────

    def exists(self) -> bool:
        try:
            with self._sftp() as sftp:
                sftp.stat(self._path)
            return True
        except FileNotFoundError:
            return False

    def is_dir(self) -> bool:
        try:
            with self._sftp() as sftp:
                return _stat.S_ISDIR(sftp.stat(self._path).st_mode)
        except Exception:
            return False

    def is_file(self) -> bool:
        try:
            with self._sftp() as sftp:
                return _stat.S_ISREG(sftp.stat(self._path).st_mode)
        except Exception:
            return False

    def stat(self) -> _SshStat:
        with self._sftp() as sftp:
            st = sftp.stat(self._path)
        return _SshStat(st.st_size or 0, st.st_mode or 0)

    def mkdir(self, *, parents: bool = False, exist_ok: bool = False) -> None:
        with self._sftp() as sftp:
            if parents:
                parts = self._path.split("/")
                current = ""
                for part in parts:
                    if not part:
                        current = "/"
                        continue
                    current = f"{current}/{part}" if current != "/" else f"/{part}"
                    try:
                        sftp.stat(current)
                    except FileNotFoundError:
                        sftp.mkdir(current)
            else:
                try:
                    sftp.mkdir(self._path)
                except OSError:
                    if exist_ok and _stat.S_ISDIR(sftp.stat(self._path).st_mode):
                        return
                    raise

    def read_bytes(self) -> bytes:
        with self._sftp() as sftp:
            with sftp.open(self._path, "rb") as f:
                f.MAX_REQUEST_SIZE = 1024 * 1024
                f.prefetch()
                return f.read()

    def write_bytes(self, data: bytes) -> None:
        with self._sftp() as sftp:
            with sftp.open(self._path, "wb") as f:
                f.set_pipelined(True)
                f.write(data)

    def open(self, mode: str = "r", encoding: str | None = None, **kwargs) -> IO:
        if "w" in mode:
            # For write streams the SFTP channel stays checked out until
            # the stream is closed — _SshWriteStream handles the return.
            sftp = _get_sftp(self._host, self._port, self._user, self._identity, self._password)
            fh = sftp.open(self._path, "wb")
            fh.set_pipelined(True)  # queue writes without waiting for ACKs
            return _SshWriteStream(
                fh, f"{self._host}:{self._path}",
                sftp=sftp, conn=(self._host, self._port, self._user, self._identity),
            )
        else:
            import io
            data = self.read_bytes()
            if "b" in mode:
                return io.BytesIO(data)
            return io.StringIO(data.decode(encoding or "utf-8"))

    def unlink(self, missing_ok: bool = False) -> None:
        try:
            with self._sftp() as sftp:
                sftp.remove(self._path)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def iterdir(self):
        """Yield child :class:`SshPath` objects (non-recursive)."""
        with self._sftp() as sftp:
            entries = sftp.listdir_attr(self._path)
        for attr in entries:
            yield self._child(attr.filename)

    def rmtree(self) -> None:
        """Recursively remove this directory (like :func:`shutil.rmtree`)."""
        with self._sftp() as sftp:
            self._rmtree_inner(sftp, self._path)

    def _rmtree_inner(self, sftp, path: str) -> None:
        try:
            entries = sftp.listdir_attr(path)
        except Exception:
            log.debug("rmtree: failed to list %s", path)
            return
        for attr in entries:
            child = f"{path}/{attr.filename}"
            if _stat.S_ISDIR(attr.st_mode or 0):
                self._rmtree_inner(sftp, child)
            else:
                try:
                    sftp.remove(child)
                except Exception:
                    log.debug("rmtree: failed to remove file %s", child)
        try:
            sftp.rmdir(path)
        except Exception:
            log.debug("rmtree: failed to remove dir %s", path)

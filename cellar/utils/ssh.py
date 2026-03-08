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

import stat as _stat
import threading
from pathlib import Path
from typing import IO


# ── Connection cache ────────────────────────────────────────────────────

_transport_cache: dict[tuple, "paramiko.Transport"] = {}  # type: ignore[name-defined]
_sftp_cache: dict[tuple, "paramiko.SFTPClient"] = {}  # type: ignore[name-defined]
_sftp_lock = threading.Lock()


def _get_sftp(
    host: str,
    port: int,
    user: str | None,
    identity: str | None,
    password: str | None = None,
):
    """Return a per-thread cached :class:`paramiko.SFTPClient`.

    ``paramiko.SFTPClient`` is **not** thread-safe — concurrent requests on the
    same channel corrupt the request/response sequence.  To support parallel
    downloads (e.g. screenshot thumbnails) we open a separate SFTP channel per
    thread, all sharing the same underlying SSH transport (which *is* thread-safe).
    """
    import paramiko  # type: ignore[import]

    conn_key = (host, port, user, identity)
    thread_key = (*conn_key, threading.get_ident())
    with _sftp_lock:
        # Try to reuse the per-thread SFTP channel.
        sftp = _sftp_cache.get(thread_key)
        if sftp is not None:
            try:
                sftp.stat(".")
                return sftp
            except Exception:
                try:
                    sftp.close()
                except Exception:
                    pass
                del _sftp_cache[thread_key]

        # Get or create the shared SSH transport for this connection.
        transport = _transport_cache.get(conn_key)
        if transport is not None and not transport.is_active():
            transport = None
            del _transport_cache[conn_key]

        if transport is None:
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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

        sftp = paramiko.SFTPClient.from_transport(transport)
        _sftp_cache[thread_key] = sftp
        return sftp


def get_transport(
    host: str,
    port: int,
    user: str | None,
    identity: str | None,
):
    """Return the underlying :class:`paramiko.Transport` for streaming."""
    sftp = _get_sftp(host, port, user, identity)
    return sftp.get_channel().get_transport()


# ── Stat result ─────────────────────────────────────────────────────────

class _SshStat:
    """Minimal stat result holding ``st_size`` and ``st_mode``."""

    __slots__ = ("st_size", "st_mode")

    def __init__(self, st_size: int, st_mode: int = 0) -> None:
        self.st_size = st_size
        self.st_mode = st_mode


# ── Write stream ────────────────────────────────────────────────────────

class _SshWriteStream:
    """Writable file-like object wrapping a paramiko SFTP file handle."""

    __slots__ = ("_fh", "_remote", "_closed")

    def __init__(self, fh, remote: str) -> None:
        self._fh = fh
        self._remote = remote
        self._closed = False

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

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


# ── SshPath ─────────────────────────────────────────────────────────────

class SshPath:
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

    # ── SFTP access ────────────────────────────────────────────────────

    def _sftp(self):
        return _get_sftp(self._host, self._port, self._user, self._identity, self._password)

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

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self) -> int:
        return hash((self._host, self._path, self._user, self._port))

    # ── Name components ────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._path.rsplit("/", 1)[-1]

    @property
    def stem(self) -> str:
        name = self.name
        idx = name.rfind(".")
        return name[:idx] if idx > 0 else name

    @property
    def suffix(self) -> str:
        name = self.name
        idx = name.rfind(".")
        return name[idx:] if idx > 0 else ""

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
            self._sftp().stat(self._path)
            return True
        except FileNotFoundError:
            return False

    def is_dir(self) -> bool:
        try:
            return _stat.S_ISDIR(self._sftp().stat(self._path).st_mode)
        except Exception:
            return False

    def is_file(self) -> bool:
        try:
            return _stat.S_ISREG(self._sftp().stat(self._path).st_mode)
        except Exception:
            return False

    def stat(self) -> _SshStat:
        st = self._sftp().stat(self._path)
        return _SshStat(st.st_size or 0, st.st_mode or 0)

    def mkdir(self, *, parents: bool = False, exist_ok: bool = False) -> None:
        sftp = self._sftp()
        if parents:
            # Walk from root, creating each component as needed.
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
                if exist_ok and self.is_dir():
                    return
                raise

    def read_bytes(self) -> bytes:
        with self._sftp().open(self._path, "rb") as f:
            f.prefetch()
            return f.read()

    def write_bytes(self, data: bytes) -> None:
        with self._sftp().open(self._path, "wb") as f:
            f.write(data)

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.read_bytes().decode(encoding)

    def write_text(self, text: str, encoding: str = "utf-8") -> None:
        self.write_bytes(text.encode(encoding))

    def open(self, mode: str = "r", encoding: str | None = None, **kwargs) -> IO:
        if "w" in mode:
            fh = self._sftp().open(self._path, "wb")
            return _SshWriteStream(fh, f"{self._host}:{self._path}")
        else:
            import io
            data = self.read_bytes()
            if "b" in mode:
                return io.BytesIO(data)
            return io.StringIO(data.decode(encoding or "utf-8"))

    def unlink(self, missing_ok: bool = False) -> None:
        try:
            self._sftp().remove(self._path)
        except FileNotFoundError:
            if not missing_ok:
                raise

    def iterdir(self):
        """Yield child :class:`SshPath` objects (non-recursive)."""
        for attr in self._sftp().listdir_attr(self._path):
            yield self._child(attr.filename)

    def rmtree(self) -> None:
        """Recursively remove this directory (like :func:`shutil.rmtree`)."""
        self._rmtree_inner(self._path)

    def _rmtree_inner(self, path: str) -> None:
        sftp = self._sftp()
        try:
            entries = sftp.listdir_attr(path)
        except Exception:
            return
        for attr in entries:
            child = f"{path}/{attr.filename}"
            if _stat.S_ISDIR(attr.st_mode or 0):
                self._rmtree_inner(child)
            else:
                try:
                    sftp.remove(child)
                except Exception:
                    pass
        try:
            sftp.rmdir(path)
        except Exception:
            pass

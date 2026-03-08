"""SSH path utilities for reading and writing to remote hosts via system ssh.

``SshPath`` mimics the subset of :class:`pathlib.Path` used by
``packager.py`` and related code so that the same functions work for
local filesystem repos, SMB share repos, and SSH repos.

All operations use the system ``ssh`` executable with ``BatchMode=yes``
so they fail fast instead of hanging on a password prompt.  Authentication
is handled by the SSH agent, ``~/.ssh/config``, or an explicit identity file.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import IO


class _SshStat:
    """Minimal stat result holding only ``st_size``."""

    __slots__ = ("st_size",)

    def __init__(self, st_size: int) -> None:
        self.st_size = st_size


class _SshWriteStream:
    """Writable file-like object that streams bytes to a remote file via ssh.

    Spawns ``ssh host "cat > /remote/path"`` and pipes data to stdin.
    """

    __slots__ = ("_proc", "_remote", "_closed")

    def __init__(self, proc: subprocess.Popen, remote: str) -> None:
        self._proc = proc
        self._remote = remote
        self._closed = False

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("I/O operation on closed file")
        self._proc.stdin.write(data)
        return len(data)

    def flush(self) -> None:
        if not self._closed:
            self._proc.stdin.flush()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._proc.stdin.close()
        rc = self._proc.wait()
        if rc != 0:
            raise OSError(f"SSH write to {self._remote} failed (exit {rc})")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class SshPath:
    """A path-like object backed by the system ``ssh`` client.

    Mimics the ``pathlib.Path`` subset used by ``packager.py``:
    path arithmetic (``/``), ``mkdir``, ``read_bytes``, ``write_bytes``,
    ``read_text``, ``write_text``, ``open``, ``unlink``, ``exists``,
    ``is_dir``, ``is_file``, ``stat``, ``iterdir``, plus ``rmtree``
    (equivalent to ``shutil.rmtree``).
    """

    __slots__ = ("_host", "_path", "_user", "_port", "_identity")

    def __init__(
        self,
        host: str,
        remote_path: str,
        *,
        user: str | None = None,
        port: int | None = None,
        identity: str | None = None,
    ) -> None:
        self._host = host
        self._path: str = remote_path.rstrip("/") or "/"
        self._user = user
        self._port = port
        self._identity = identity

    # ── SSH subprocess helpers ──────────────────────────────────────────

    def _dest(self) -> str:
        return f"{self._user}@{self._host}" if self._user else self._host

    def _base_args(self) -> list[str]:
        args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new"]
        if self._port:
            args += ["-p", str(self._port)]
        if self._identity:
            args += ["-i", self._identity]
        args.append(self._dest())
        return args

    def _run(self, remote_cmd: str, *, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
        cmd = self._base_args() + [remote_cmd]
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, check=False)
        if check and result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise OSError(f"SSH command failed: {remote_cmd}: {stderr or 'unknown error'}")
        return result

    def _child(self, name: str) -> "SshPath":
        return SshPath(
            self._host,
            f"{self._path}/{name}",
            user=self._user,
            port=self._port,
            identity=self._identity,
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
        )

    def __str__(self) -> str:
        return self._path

    def __repr__(self) -> str:
        return f"SshPath({self._dest()}:{self._path!r})"

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
            )
        return SshPath(
            self._host, self._path[:idx],
            user=self._user, port=self._port, identity=self._identity,
        )

    # ── Filesystem operations ──────────────────────────────────────────

    def exists(self) -> bool:
        result = self._run(f"test -e {_quote(self._path)}", check=False)
        return result.returncode == 0

    def is_dir(self) -> bool:
        result = self._run(f"test -d {_quote(self._path)}", check=False)
        return result.returncode == 0

    def is_file(self) -> bool:
        result = self._run(f"test -f {_quote(self._path)}", check=False)
        return result.returncode == 0

    def stat(self) -> _SshStat:
        result = self._run(f"stat -c %s {_quote(self._path)}")
        return _SshStat(int(result.stdout.strip()))

    def mkdir(self, *, parents: bool = False, exist_ok: bool = False) -> None:
        if parents:
            self._run(f"mkdir -p {_quote(self._path)}")
        else:
            if exist_ok:
                self._run(f"mkdir -p {_quote(self._path)}")
            else:
                self._run(f"mkdir {_quote(self._path)}")

    def read_bytes(self) -> bytes:
        result = self._run(f"cat {_quote(self._path)}")
        return result.stdout

    def write_bytes(self, data: bytes) -> None:
        cmd = self._base_args() + [f"cat > {_quote(self._path)}"]
        proc = subprocess.run(cmd, input=data, capture_output=True, timeout=60, check=False)
        if proc.returncode != 0:
            stderr = proc.stderr.decode(errors="replace").strip()
            raise OSError(f"SSH write failed for {self._path}: {stderr or 'unknown error'}")

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.read_bytes().decode(encoding)

    def write_text(self, text: str, encoding: str = "utf-8") -> None:
        self.write_bytes(text.encode(encoding))

    def open(self, mode: str = "r", encoding: str | None = None, **kwargs) -> IO:
        if "w" in mode:
            remote = _quote(self._path)
            cmd = self._base_args() + [f"cat > {remote}"]
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return _SshWriteStream(proc, f"{self._dest()}:{self._path}")
        else:
            # Read mode — return a BytesIO / StringIO with the full content.
            import io
            data = self.read_bytes()
            if "b" in mode:
                return io.BytesIO(data)
            return io.StringIO(data.decode(encoding or "utf-8"))

    def unlink(self, missing_ok: bool = False) -> None:
        result = self._run(f"rm -f {_quote(self._path)}" if missing_ok else f"rm {_quote(self._path)}", check=False)
        if result.returncode != 0 and not missing_ok:
            stderr = result.stderr.decode(errors="replace").strip()
            raise OSError(f"SSH unlink failed for {self._path}: {stderr}")

    def iterdir(self):
        """Yield child :class:`SshPath` objects (non-recursive)."""
        result = self._run(f"ls -1 {_quote(self._path)}", check=False)
        if result.returncode != 0:
            return
        for name in result.stdout.decode(errors="replace").splitlines():
            name = name.strip()
            if name:
                yield self._child(name)

    def rmtree(self) -> None:
        """Recursively remove this directory (like :func:`shutil.rmtree`)."""
        self._run(f"rm -rf {_quote(self._path)}", check=False)


def _quote(path: str) -> str:
    """Shell-quote a path for use in remote SSH commands."""
    # Use single quotes with escaped internal single quotes.
    return "'" + path.replace("'", "'\\''") + "'"

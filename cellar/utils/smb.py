"""SMB path utilities for reading and writing to SMB shares via smbprotocol.

``SmbPath`` mimics the subset of :class:`pathlib.Path` used by
``packager.py`` and related code so that the same functions work for
both local filesystem repos and SMB share repos.

UNC path convention
-------------------
``smbclient`` accepts forward-slash UNC paths like ``//server/share/path``.
``smb_uri_to_unc("smb://server/share/path")`` returns ``//server/share/path``.
"""

from __future__ import annotations

import stat as _stat
from pathlib import Path
from typing import IO
from urllib.parse import urlparse


def smb_uri_to_unc(uri: str) -> str:
    """Convert an ``smb://server/share/path`` URI to a UNC path string.

    Returns a forward-slash UNC string: ``//server/share/path``.
    """
    parsed = urlparse(uri)
    host = parsed.hostname or ""
    path = parsed.path or "/"
    return f"//{host}{path}"


class SmbPath:
    """A path-like object backed by ``smbclient`` for SMB share access.

    Mimics the ``pathlib.Path`` subset used by ``packager.py``:
    path arithmetic (``/``), ``mkdir``, ``read_bytes``, ``write_bytes``,
    ``read_text``, ``write_text``, ``open``, ``unlink``, ``exists``,
    ``is_dir``, ``is_file``, ``stat``, ``iterdir``, plus ``rmtree``
    (equivalent to ``shutil.rmtree``).

    The SMB session must be registered (via :func:`smbclient.register_session`)
    before calling any methods.  :class:`~cellar.backend.repo._SmbFetcher`
    does this automatically when the :class:`~cellar.backend.repo.Repo` is
    constructed.
    """

    __slots__ = ("_unc",)

    def __init__(self, unc: str) -> None:
        # Normalise to forward slashes; strip trailing slash unless root.
        unc = unc.replace("\\", "/")
        self._unc: str = unc.rstrip("/") or "//"

    # ── Path arithmetic ────────────────────────────────────────────────

    def __truediv__(self, other: str | Path) -> "SmbPath":
        part = str(other).replace("\\", "/").strip("/")
        return SmbPath(f"{self._unc}/{part}")

    def __str__(self) -> str:
        return self._unc

    def __repr__(self) -> str:
        return f"SmbPath({self._unc!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SmbPath):
            return self._unc == other._unc
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self) -> int:
        return hash(self._unc)

    # ── Name components ────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._unc.rsplit("/", 1)[-1]

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
    def parent(self) -> "SmbPath":
        idx = self._unc.rfind("/")
        # Keep at least //server/share — never return an empty path.
        if idx <= 1:
            return SmbPath(self._unc)
        return SmbPath(self._unc[:idx])

    # ── Filesystem operations ──────────────────────────────────────────

    def exists(self) -> bool:
        import smbclient  # type: ignore[import]
        try:
            smbclient.stat(self._unc)
            return True
        except Exception:
            return False

    def is_dir(self) -> bool:
        import smbclient  # type: ignore[import]
        try:
            return _stat.S_ISDIR(smbclient.stat(self._unc).st_mode)
        except Exception:
            return False

    def is_file(self) -> bool:
        import smbclient  # type: ignore[import]
        try:
            return _stat.S_ISREG(smbclient.stat(self._unc).st_mode)
        except Exception:
            return False

    def stat(self):
        import smbclient  # type: ignore[import]
        return smbclient.stat(self._unc)

    def mkdir(self, *, parents: bool = False, exist_ok: bool = False) -> None:
        import smbclient  # type: ignore[import]
        if parents:
            try:
                smbclient.makedirs(self._unc, exist_ok=exist_ok)
            except Exception as exc:
                if exist_ok and self.is_dir():
                    return
                raise OSError(str(exc)) from exc
        else:
            try:
                smbclient.mkdir(self._unc)
            except Exception as exc:
                if exist_ok and self.is_dir():
                    return
                raise OSError(str(exc)) from exc

    def read_bytes(self) -> bytes:
        import smbclient  # type: ignore[import]
        with smbclient.open_file(self._unc, mode="rb", share_access="r") as f:
            return f.read()

    def write_bytes(self, data: bytes) -> None:
        import smbclient  # type: ignore[import]
        with smbclient.open_file(self._unc, mode="wb") as f:
            f.write(data)

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.read_bytes().decode(encoding)

    def write_text(self, text: str, encoding: str = "utf-8") -> None:
        self.write_bytes(text.encode(encoding))

    def open(self, mode: str = "r", encoding: str | None = None, **kwargs) -> IO:
        import smbclient  # type: ignore[import]
        if "share_access" not in kwargs and "r" in mode and "w" not in mode and "+" not in mode:
            kwargs["share_access"] = "r"
        if "b" in mode:
            return smbclient.open_file(self._unc, mode=mode, **kwargs)
        return smbclient.open_file(
            self._unc, mode=mode, encoding=encoding or "utf-8", **kwargs
        )

    def unlink(self, missing_ok: bool = False) -> None:
        import smbclient  # type: ignore[import]
        try:
            smbclient.remove(self._unc)
        except Exception:
            if not missing_ok:
                raise

    def iterdir(self):
        """Yield child :class:`SmbPath` objects (non-recursive)."""
        import smbclient  # type: ignore[import]
        for entry in smbclient.scandir(self._unc):
            yield SmbPath(f"{self._unc}/{entry.name}")

    def rmtree(self) -> None:
        """Recursively remove this directory (like :func:`shutil.rmtree`)."""
        import smbclient  # type: ignore[import]
        for root, dirs, files in smbclient.walk(self._unc, topdown=False):
            root_str = root if isinstance(root, str) else str(root)
            for name in files:
                try:
                    smbclient.remove(f"{root_str}/{name}")
                except Exception:
                    pass
            for name in dirs:
                try:
                    smbclient.rmdir(f"{root_str}/{name}")
                except Exception:
                    pass
        try:
            smbclient.rmdir(self._unc)
        except Exception:
            pass

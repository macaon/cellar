"""Shared mixin for remote path classes (SmbPath, SshPath).

Provides common path-component properties and text convenience methods
so they don't have to be duplicated across transport implementations.
"""

from __future__ import annotations


class RemotePathMixin:
    """Mixin supplying ``name``, ``stem``, ``suffix``, ``read_text``,
    ``write_text``, and ``__ne__`` for remote path objects.

    Subclasses must set ``_remote_str`` to the forward-slash-separated
    remote path string (e.g. ``self._unc`` or ``self._path``), and
    implement ``read_bytes`` / ``write_bytes``.
    """

    __slots__ = ()

    _remote_str: str

    # ── Name components ────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._remote_str.rsplit("/", 1)[-1]

    @property
    def stem(self) -> str:
        n = self.name
        idx = n.rfind(".")
        return n[:idx] if idx > 0 else n

    @property
    def suffix(self) -> str:
        n = self.name
        idx = n.rfind(".")
        return n[idx:] if idx > 0 else ""

    # ── Equality helpers ───────────────────────────────────────────────

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    # ── Text convenience ───────────────────────────────────────────────

    def read_text(self, encoding: str = "utf-8") -> str:
        return self.read_bytes().decode(encoding)  # type: ignore[attr-defined]

    def write_text(self, text: str, encoding: str = "utf-8") -> None:
        self.write_bytes(text.encode(encoding))  # type: ignore[attr-defined]

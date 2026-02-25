"""GIO-based file and network helpers."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

log = logging.getLogger(__name__)


def gio_read_bytes(uri: str) -> bytes:
    """Read the full contents of *uri* via GIO (blocking).

    Works for any URI scheme that GVFS supports: ``file://``, ``smb://``,
    ``nfs://``, ``sftp://``, etc.  Raises ``OSError`` on failure.
    """
    gfile = Gio.File.new_for_uri(uri)
    try:
        _ok, contents, _etag = gfile.load_contents(None)
    except GLib.Error as exc:
        raise OSError(f"GIO could not read {uri}: {exc.message}") from exc
    return bytes(contents)


def gio_file_exists(uri: str) -> bool:
    """Return ``True`` if *uri* exists and is queryable via GIO."""
    gfile = Gio.File.new_for_uri(uri)
    try:
        gfile.query_info("standard::type", Gio.FileQueryInfoFlags.NONE, None)
        return True
    except GLib.Error:
        return False

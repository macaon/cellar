"""GIO-based file and network helpers."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio, GLib  # noqa: E402

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal: mount helper
# ---------------------------------------------------------------------------

def _ensure_mounted(gfile: Gio.File, mount_op: object | None = None) -> None:
    """Mount the volume enclosing *gfile* via a nested GLib main loop.

    Raises ``OSError`` on failure.  ``ALREADY_MOUNTED`` is silently ignored.
    *mount_op* should be a ``Gtk.MountOperation`` so that credential dialogs
    can be shown; a bare ``Gio.MountOperation`` is used when it is ``None``.
    """
    main_loop = GLib.MainLoop()
    _err: list = [None]

    def _on_mounted(src, result, _):
        try:
            src.mount_enclosing_volume_finish(result)
        except GLib.Error as exc:
            if not exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                _err[0] = exc
        finally:
            main_loop.quit()

    op = mount_op or Gio.MountOperation()
    gfile.mount_enclosing_volume(Gio.MountMountFlags.NONE, op, None, _on_mounted, None)
    main_loop.run()

    if _err[0] is not None:
        exc = _err[0]
        if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.FAILED_HANDLED):
            raise OSError("Authentication cancelled by user")
        raise OSError(f"Could not mount: {exc.message}")


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

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


def gio_makedirs(uri: str, *, mount_op: object | None = None) -> None:
    """Create *uri* as a directory, including all parents (like ``mkdir -p``).

    Raises ``OSError`` on failure.  An already-existing directory is not an
    error.  If the volume is not yet mounted, a mount attempt is made first
    using *mount_op*.
    """
    gfile = Gio.File.new_for_uri(uri)
    try:
        gfile.make_directory_with_parents(None)
    except GLib.Error as exc:
        if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.EXISTS):
            return
        if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_MOUNTED):
            _ensure_mounted(gfile, mount_op)
            try:
                gfile.make_directory_with_parents(None)
            except GLib.Error as exc2:
                if not exc2.matches(Gio.io_error_quark(), Gio.IOErrorEnum.EXISTS):
                    raise OSError(
                        f"GIO could not create directory {uri}: {exc2.message}"
                    ) from exc2
        else:
            raise OSError(
                f"GIO could not create directory {uri}: {exc.message}"
            ) from exc


def gio_write_bytes(uri: str, data: bytes, *, mount_op: object | None = None) -> None:
    """Write *data* to *uri* via GIO (blocking), creating or replacing the file.

    Raises ``OSError`` on failure.  If the volume is not yet mounted, a mount
    attempt is made first using *mount_op*.
    """
    gfile = Gio.File.new_for_uri(uri)

    def _open_stream() -> Gio.FileOutputStream:
        return gfile.replace(None, False, Gio.FileCreateFlags.NONE, None)

    try:
        stream = _open_stream()
    except GLib.Error as exc:
        if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_MOUNTED):
            _ensure_mounted(gfile, mount_op)
            try:
                stream = _open_stream()
            except GLib.Error as exc2:
                raise OSError(
                    f"GIO could not open {uri} for writing: {exc2.message}"
                ) from exc2
        else:
            raise OSError(
                f"GIO could not open {uri} for writing: {exc.message}"
            ) from exc

    try:
        stream.write_all(data, None)
        stream.close(None)
    except GLib.Error as exc:
        raise OSError(f"GIO could not write {uri}: {exc.message}") from exc

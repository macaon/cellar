"""Catalogue fetching and repo management.

Supported URI schemes
---------------------
- Bare local path or ``file://`` → reads directly from the filesystem
- ``http://`` / ``https://``     → fetches via urllib (no extra deps); **read-only**
- ``ssh://[user@]host[:port]/path`` → streams files through the system ssh client;
  key auth handled by SSH agent / ``~/.ssh/config``;
  explicit identity file via ``ssh_identity=``
- ``smb://`` / ``nfs://``        → delegates to GVFS through GIO

HTTP(S) repos are always read-only — the client cannot initialise or modify
them.  All other transports support write operations (phase 9).

catalogue.json format
---------------------
The repo root must contain a ``catalogue.json`` in the following shape::

    {
      "cellar_version": 1,
      "generated_at": "<ISO-8601 timestamp>",
      "apps": [ { ...AppEntry fields... }, ... ]
    }

A bare JSON array is also accepted for backwards compatibility.
"""

from __future__ import annotations

import json
import logging
import ssl
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable
from urllib.parse import urlparse

from cellar.models.app_entry import AppEntry

log = logging.getLogger(__name__)

CATALOGUE_VERSION = 1


class RepoError(Exception):
    """Raised when a repo operation fails."""


# ---------------------------------------------------------------------------
# Fetcher protocol & implementations
# ---------------------------------------------------------------------------

@runtime_checkable
class _Fetcher(Protocol):
    """Reads raw bytes from a repo by relative path and resolves asset URIs."""

    def fetch_bytes(self, rel_path: str) -> bytes: ...
    def resolve_uri(self, rel_path: str) -> str: ...


class _LocalFetcher:
    """Serves files from a local directory (bare path or ``file://``)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def fetch_bytes(self, rel_path: str) -> bytes:
        path = self._root / rel_path.lstrip("/")
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise RepoError(f"File not found: {path}") from exc
        except OSError as exc:
            raise RepoError(f"Could not read {path}: {exc}") from exc

    def resolve_uri(self, rel_path: str) -> str:
        return str(self._root / rel_path.lstrip("/"))


class _HttpFetcher:
    """Serves files from an HTTP or HTTPS server (read-only)."""

    def __init__(
        self,
        base_url: str,
        *,
        ssl_verify: bool = True,
        ca_cert: str | None = None,
        token: str | None = None,
    ) -> None:
        self._base = base_url.rstrip("/") + "/"
        self._ssl_ctx: ssl.SSLContext | None = None
        self._token = token
        if ca_cert:
            # Load the user-supplied CA bundle and verify normally against it.
            ctx = ssl.create_default_context(cafile=ca_cert)
            # Python 3.10+ with OpenSSL 3.x sets X509_V_FLAG_X509_STRICT by
            # default, which makes the Authority Key Identifier extension
            # mandatory.  Many home/self-signed CAs omit it, causing
            # "Missing Authority Key Identifier" failures even with a valid
            # chain.  curl and browsers don't enforce this.  Clear the flag so
            # full chain validation (hostname, expiry, trust anchor) still runs.
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
            self._ssl_ctx = ctx
        elif not ssl_verify:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            self._ssl_ctx = ctx

    def fetch_bytes(self, rel_path: str) -> bytes:
        url = self._base + rel_path.lstrip("/")
        req = urllib.request.Request(url)  # noqa: S310
        if self._token:
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._ssl_ctx) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            raise RepoError(f"HTTP {exc.code} fetching {url}: {exc.reason}") from exc
        except urllib.error.URLError as exc:
            raise RepoError(f"Network error fetching {url}: {exc.reason}") from exc

    def resolve_uri(self, rel_path: str) -> str:
        return self._base + rel_path.lstrip("/")


class _SshFetcher:
    """Reads files from a remote host via the system ``ssh`` client.

    Key authentication is handled transparently by the SSH agent and
    ``~/.ssh/config``.  An explicit identity file can be provided via
    the *identity* parameter.

    The connection uses ``BatchMode=yes`` so it fails fast instead of
    hanging on a password prompt.
    """

    def __init__(
        self,
        host: str,
        remote_root: str,
        *,
        user: str | None = None,
        port: int | None = None,
        identity: str | None = None,
    ) -> None:
        self._host = host
        self._root = remote_root.rstrip("/") or "/"
        self._user = user
        self._port = port
        self._identity = identity

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

    def fetch_bytes(self, rel_path: str) -> bytes:
        remote = f"{self._root}/{rel_path.lstrip('/')}"
        cmd = self._base_args() + ["cat", remote]
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=30, check=False)
        except FileNotFoundError:
            raise RepoError(
                "ssh executable not found; install OpenSSH client to use ssh:// repos"
            )
        except subprocess.TimeoutExpired as exc:
            raise RepoError(f"SSH connection timed out fetching {rel_path}") from exc
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            raise RepoError(
                f"SSH fetch failed for {rel_path}: {stderr or 'unknown error'}"
            )
        return result.stdout

    def resolve_uri(self, rel_path: str) -> str:
        user_part = f"{self._user}@" if self._user else ""
        port_part = f":{self._port}" if self._port else ""
        return (
            f"ssh://{user_part}{self._host}{port_part}"
            f"{self._root}/{rel_path.lstrip('/')}"
        )


class _GioFetcher:
    """Reads files via GVFS through GIO — used for ``smb://`` and ``nfs://``.

    GIO is imported lazily so that the rest of the backend can be used
    (and tested) without a display or GObject environment.

    *mount_op* should be a ``Gtk.MountOperation`` (or any ``Gio.MountOperation``
    subclass) created by the UI layer with the parent window set.  When the
    share is not yet mounted, the fetcher will attempt to mount it via GIO and
    block using a nested GLib main loop so that any credential dialog shown by
    the mount operation can be interacted with.  Credentials entered this way
    are stored in the GNOME Keyring automatically when the user ticks
    "Remember Password".

    If *mount_op* is ``None`` a bare ``Gio.MountOperation`` is used instead —
    this works for shares that are already mounted or need no credentials, but
    will not show a password prompt.
    """

    def __init__(self, base_uri: str, *, mount_op: object | None = None) -> None:
        try:
            import gi
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio, GLib  # type: ignore[import]
        except (ImportError, ValueError) as exc:
            raise RepoError(
                "GIO is unavailable; cannot use smb:// or nfs:// URIs"
            ) from exc
        self._Gio = Gio
        self._GLib = GLib
        self._base = base_uri.rstrip("/")
        self._mount_op = mount_op  # Gio.MountOperation (or Gtk.MountOperation subclass)

    # ------------------------------------------------------------------
    # Mount helpers
    # ------------------------------------------------------------------

    def _ensure_mounted(self, gfile: object) -> None:
        """Mount the enclosing GVFS volume, blocking via a nested GLib loop.

        Uses *self._mount_op* to present credential prompts.  Raises
        ``RepoError`` on failure.  ``ALREADY_MOUNTED`` is silently ignored.
        """
        main_loop = self._GLib.MainLoop()
        _err: list = [None]

        def _on_mounted(src, result, _):
            try:
                src.mount_enclosing_volume_finish(result)
            except self._GLib.Error as exc:
                if not exc.matches(
                    self._Gio.io_error_quark(),
                    self._Gio.IOErrorEnum.ALREADY_MOUNTED,
                ):
                    _err[0] = exc
            finally:
                main_loop.quit()

        mount_op = self._mount_op or self._Gio.MountOperation()
        gfile.mount_enclosing_volume(
            self._Gio.MountMountFlags.NONE,
            mount_op,
            None,       # cancellable
            _on_mounted,
            None,       # user_data
        )
        main_loop.run()  # spins nested loop; GTK events (dialogs) are processed

        if _err[0] is not None:
            exc = _err[0]
            # FAILED_HANDLED means the user cancelled an auth dialog — GIO has
            # already shown any error UI, so give a terse message.
            if exc.matches(
                self._Gio.io_error_quark(),
                self._Gio.IOErrorEnum.FAILED_HANDLED,
            ):
                raise RepoError("Authentication cancelled by user")
            raise RepoError(f"Could not mount {self._base}: {exc.message}")

    # ------------------------------------------------------------------
    # _Fetcher interface
    # ------------------------------------------------------------------

    def fetch_bytes(self, rel_path: str) -> bytes:
        uri = f"{self._base}/{rel_path.lstrip('/')}"
        gfile = self._Gio.File.new_for_uri(uri)
        try:
            _ok, contents, _etag = gfile.load_contents(None)
            return bytes(contents)
        except self._GLib.Error as exc:
            if exc.matches(
                self._Gio.io_error_quark(),
                self._Gio.IOErrorEnum.NOT_MOUNTED,
            ):
                # The GVFS backend exists but the share hasn't been mounted
                # yet.  Attempt a mount (may prompt for credentials) then retry.
                self._ensure_mounted(gfile)
                try:
                    _ok, contents, _etag = gfile.load_contents(None)
                    return bytes(contents)
                except self._GLib.Error as exc2:
                    raise RepoError(
                        f"GIO could not read {uri}: {exc2.message}"
                    ) from exc2
            raise RepoError(f"GIO could not read {uri}: {exc.message}") from exc

    def resolve_uri(self, rel_path: str) -> str:
        uri = f"{self._base}/{rel_path.lstrip('/')}"
        # Prefer the GVFS FUSE path — a plain filesystem path that GdkPixbuf,
        # os.path.isfile, and tarfile can use without any GIO calls.  The share
        # is already mounted at this point (we fetched the catalogue through it),
        # so get_path() returns the gvfsd-fuse path immediately.
        try:
            fuse_path = self._Gio.File.new_for_uri(uri).get_path()
            if fuse_path:
                return fuse_path
        except Exception:
            pass
        return uri


# ---------------------------------------------------------------------------
# Fetcher factory
# ---------------------------------------------------------------------------

_SUPPORTED_SCHEMES = "local path, file://, http(s)://, ssh://, smb://, nfs://"


def _make_fetcher(
    uri: str,
    *,
    ssh_identity: str | None = None,
    mount_op: object | None = None,
    ssl_verify: bool = True,
    ca_cert: str | None = None,
    token: str | None = None,
) -> _Fetcher:
    """Return the appropriate fetcher for *uri*."""
    parsed = urlparse(uri)
    scheme = parsed.scheme.lower()

    if scheme in ("", "file"):
        root = Path(parsed.path if parsed.path else uri).expanduser().resolve()
        if not root.is_dir():
            raise RepoError(
                f"Repo root does not exist or is not a directory: {root}"
            )
        return _LocalFetcher(root)

    if scheme in ("http", "https"):
        return _HttpFetcher(uri, ssl_verify=ssl_verify, ca_cert=ca_cert, token=token)

    if scheme == "ssh":
        if not parsed.hostname:
            raise RepoError(f"Invalid SSH URI (no host): {uri!r}")
        return _SshFetcher(
            host=parsed.hostname,
            remote_root=parsed.path or "/",
            user=parsed.username or None,
            port=parsed.port or None,
            identity=ssh_identity,
        )

    if scheme in ("smb", "nfs"):
        return _GioFetcher(uri, mount_op=mount_op)

    raise RepoError(
        f"Unsupported URI scheme {scheme!r}. Supported: {_SUPPORTED_SCHEMES}"
    )


# ---------------------------------------------------------------------------
# Repo
# ---------------------------------------------------------------------------

class Repo:
    """Represents a single Cellar repository source.

    *uri* can be any of the supported schemes listed at the top of this module.
    For ``ssh://`` repos, an explicit identity file path can be passed via
    *ssh_identity*; otherwise the system SSH agent / ``~/.ssh/config`` is used.

    HTTP(S) repos are **read-only** — ``is_writable`` returns ``False`` and
    write operations will raise ``RepoError``.  All other transports are
    considered writable.
    """

    def __init__(
        self,
        uri: str,
        name: str = "",
        *,
        ssh_identity: str | None = None,
        mount_op: object | None = None,
        ssl_verify: bool = True,
        ca_cert: str | None = None,
        token: str | None = None,
    ) -> None:
        self.uri = uri
        self.name = name or uri
        self._token = token
        self._fetcher: _Fetcher = _make_fetcher(
            uri,
            ssh_identity=ssh_identity,
            mount_op=mount_op,
            ssl_verify=ssl_verify,
            ca_cert=ca_cert,
            token=token,
        )

    @property
    def token(self) -> str | None:
        """Bearer token for HTTP(S) authentication, or ``None``."""
        return self._token

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def is_writable(self) -> bool:
        """``False`` for HTTP(S) repos; ``True`` for all other transports."""
        return urlparse(self.uri).scheme.lower() not in ("http", "https")

    # ------------------------------------------------------------------
    # Public API — reading
    # ------------------------------------------------------------------

    def fetch_catalogue(self) -> list[AppEntry]:
        """Load and parse ``catalogue.json``, returning all entries.

        Accepts both the current wrapper format
        ``{"cellar_version": …, "apps": […]}`` and a legacy bare JSON array.
        """
        raw = self._fetch_json("catalogue.json")

        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("apps", [])
        else:
            raise RepoError("catalogue.json has an unexpected top-level type")

        entries: list[AppEntry] = []
        for item in items:
            try:
                entries.append(AppEntry.from_dict(item))
            except (KeyError, TypeError, ValueError) as exc:
                log.warning(
                    "Skipping malformed catalogue entry %r: %s", item.get("id"), exc
                )
        log.info("Loaded %d entries from %s", len(entries), self.uri)
        return entries

    def fetch_entry_by_id(self, app_id: str) -> AppEntry:
        """Load the catalogue and return the entry matching *app_id*."""
        for entry in self.fetch_catalogue():
            if entry.id == app_id:
                return entry
        raise RepoError(f"App {app_id!r} not found in catalogue at {self.uri}")

    def resolve_asset_uri(self, repo_relative: str) -> str:
        """Return a URI/path string for a repo-relative asset (icon, screenshot…)."""
        return self._fetcher.resolve_uri(repo_relative)

    def fetch_categories(self) -> list[str]:
        """Return the ordered category list for this repo.

        The result is :data:`~cellar.backend.packager.BASE_CATEGORIES` plus any
        custom categories stored in the top-level ``categories`` key of
        ``catalogue.json``, with duplicates removed while preserving order.

        Falls back to :data:`~cellar.backend.packager.BASE_CATEGORIES` alone if
        the catalogue cannot be read or contains no ``categories`` key.
        """
        from cellar.backend.packager import BASE_CATEGORIES
        try:
            raw = self._fetch_json("catalogue.json")
            stored: list[str] = raw.get("categories", []) if isinstance(raw, dict) else []
        except RepoError:
            stored = []
        seen: set[str] = set(BASE_CATEGORIES)
        custom = [c for c in stored if c not in seen]
        return BASE_CATEGORIES + custom

    def iter_categories(self) -> Iterator[str]:
        """Yield the distinct categories present in the catalogue."""
        seen: set[str] = set()
        for entry in self.fetch_catalogue():
            if entry.category not in seen:
                seen.add(entry.category)
                yield entry.category

    def local_path(self, rel_path: str = "") -> Path:
        """Return the absolute local filesystem path for a repo-relative path.

        Raises :exc:`RepoError` for non-local repos (HTTP, SSH, SMB, NFS).
        """
        if not isinstance(self._fetcher, _LocalFetcher):
            raise RepoError("local_path() is only available for local repos")
        return self._fetcher._root / rel_path.lstrip("/")

    def writable_path(self, rel_path: str = "") -> Path:
        """Return a writable local filesystem path to the repo root (or a sub-path).

        For local repos this is identical to :meth:`local_path`.

        For SMB/NFS repos the share must already be mounted.  GVFS exposes
        every mounted network share through ``gvfsd-fuse`` under
        ``/run/user/<uid>/gvfs/``; ``Gio.File.get_path()`` returns that FUSE
        path transparently.  ``packager.py`` can then use ordinary
        :class:`pathlib.Path` operations on it without any GIO-specific code.

        Raises :exc:`RepoError` for HTTP/SSH repos or when GVFS FUSE is
        unavailable (non-GNOME systems without gvfsd-fuse).
        """
        if isinstance(self._fetcher, _LocalFetcher):
            return self._fetcher._root / rel_path.lstrip("/")

        if isinstance(self._fetcher, _GioFetcher):
            try:
                import gi
                gi.require_version("Gio", "2.0")
                from gi.repository import Gio
            except (ImportError, ValueError) as exc:
                raise RepoError("GIO is unavailable") from exc

            gfile = Gio.File.new_for_uri(
                self._fetcher._base.rstrip("/") + "/" + rel_path.lstrip("/")
                if rel_path else self._fetcher._base
            )
            fuse_path = gfile.get_path()
            if fuse_path:
                return Path(fuse_path)
            raise RepoError(
                "GVFS FUSE mount is not available for this share. "
                "Ensure gvfsd-fuse is running (standard on GNOME)."
            )

        scheme = urlparse(self.uri).scheme.lower()
        raise RepoError(
            f"{scheme!r} repositories do not support local write operations"
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_json(self, rel_path: str) -> dict | list:
        data = self._fetcher.fetch_bytes(rel_path)
        try:
            return json.loads(data)
        except json.JSONDecodeError as exc:
            raise RepoError(f"Invalid JSON at {rel_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# RepoManager
# ---------------------------------------------------------------------------

class RepoManager:
    """Manages the collection of configured repos.

    Wraps the ``repos`` DB table (phase 5). For now it holds repos
    in memory so the backend can be exercised before the DB layer exists.
    """

    def __init__(self) -> None:
        self._repos: list[Repo] = []

    def add(self, repo: Repo) -> None:
        self._repos.append(repo)

    def remove(self, uri: str) -> None:
        self._repos = [r for r in self._repos if r.uri != uri]

    def __iter__(self) -> Iterator[Repo]:
        return iter(self._repos)

    def fetch_all_catalogues(self) -> list[AppEntry]:
        """Merge catalogues from all enabled repos.

        Later entries with the same app ID from different repos win
        (last-repo-wins policy).
        """
        seen: dict[str, AppEntry] = {}
        for repo in self._repos:
            try:
                for entry in repo.fetch_catalogue():
                    seen[entry.id] = entry
            except RepoError as exc:
                log.warning("Could not load catalogue from %s: %s", repo.uri, exc)
        return list(seen.values())

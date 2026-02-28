"""Catalogue fetching and repo management.

Supported URI schemes
---------------------
- Bare local path or ``file://`` → reads directly from the filesystem
- ``http://`` / ``https://``     → fetches via urllib (no extra deps); **read-only**
- ``ssh://[user@]host[:port]/path`` → streams files through the system ssh client;
  key auth handled by SSH agent / ``~/.ssh/config``;
  explicit identity file via ``ssh_identity=``
- ``smb://[user[:pass]@]host/share[/path]`` → reads via the ``smbclient``
  command (samba-client package); never creates a GVFS mount point; **read-only**

HTTP(S) and SMB repos are read-only — the client cannot initialise or modify
them.  Local and SSH repos support write operations.

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
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable
from urllib.parse import urlparse

from cellar.models.app_entry import AppEntry

log = logging.getLogger(__name__)

CATALOGUE_VERSION = 1

# Sent as the User-Agent on all outbound HTTP(S) requests.  A generic browser-
# like string avoids CDN / WAF bot-protection rules that block Python-urllib.
_USER_AGENT = "Mozilla/5.0 (compatible; Cellar/1.0)"

# File extensions treated as image assets.  These are downloaded to a per-session
# temp cache by resolve_asset_uri so that GdkPixbuf can load them from a local
# path (it cannot pass auth headers when given an http:// URL).
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".ico"}


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
        req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})  # noqa: S310
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


class _SmbclientFetcher:
    """Reads files from an SMB share via the ``smbclient`` command.

    Unlike the previous GIO/GVFS approach, smbclient never creates a
    persistent mount point — files are streamed directly without the share
    appearing in the Files app sidebar.

    Credentials may be embedded in the URI
    (``smb://user:pass@host/share``).  When a username is given without a
    password, a null/anonymous session is attempted (``--no-pass``); the
    share must allow guest read access for this to succeed.  The password,
    if provided, is passed via the ``PASSWD`` environment variable so it
    does not appear in ``ps`` output.

    ``stdin`` is always redirected to ``/dev/null`` so that smbclient
    never hangs waiting for an interactive password prompt.
    """

    def __init__(self, uri: str) -> None:
        parsed = urlparse(uri)
        if not parsed.hostname:
            raise RepoError(f"Invalid SMB URI (no host): {uri!r}")
        path_parts = parsed.path.lstrip("/").split("/", 1)
        if not path_parts or not path_parts[0]:
            raise RepoError(f"Invalid SMB URI (no share name): {uri!r}")
        self._host = parsed.hostname
        self._port = parsed.port
        self._share = path_parts[0]
        self._subpath = path_parts[1].strip("/") if len(path_parts) > 1 else ""
        self._user = parsed.username or None
        self._password = parsed.password or None
        self._base_uri = uri.rstrip("/")

    def _remote_path(self, rel_path: str) -> str:
        """Combine the share sub-path with a repo-relative path."""
        parts = [p for p in [self._subpath, rel_path.lstrip("/")] if p]
        return "/".join(parts)

    def _base_args(self) -> list[str]:
        unc = f"//{self._host}/{self._share}"
        args = ["smbclient", unc]
        if self._port:
            args += ["-p", str(self._port)]
        if self._user:
            args += ["-U", self._user]
        # Always suppress the interactive password prompt.  When a password
        # is supplied it travels via the PASSWD env var; without one we try
        # a null/guest session.
        if not self._password:
            args.append("--no-pass")
        return args

    def _env(self) -> dict | None:
        if self._password:
            import os
            return {**os.environ, "PASSWD": self._password}
        return None

    def fetch_bytes(self, rel_path: str) -> bytes:
        remote = self._remote_path(rel_path)
        # Write to a temp file; 'get file -' (stdout) is not supported by all
        # smbclient versions and mixes status output with file content.
        with tempfile.NamedTemporaryFile(prefix="cellar-smb-", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            cmd = self._base_args() + ["-c", f'get "{remote}" "{tmp_path}"']
            try:
                result = subprocess.run(
                    cmd,
                    stdin=subprocess.DEVNULL,   # never block on password prompt
                    capture_output=True,
                    timeout=30,
                    check=False,
                    env=self._env(),
                )
            except FileNotFoundError:
                raise RepoError(
                    "smbclient not found; install samba-client to use smb:// repos"
                )
            except subprocess.TimeoutExpired as exc:
                raise RepoError(
                    f"smbclient timed out fetching {rel_path}"
                ) from exc
            if result.returncode != 0:
                # smbclient sometimes writes errors to stdout, sometimes stderr.
                stderr = result.stderr.decode(errors="replace").strip()
                stdout = result.stdout.decode(errors="replace").strip()
                detail = stderr or stdout or "unknown error"
                raise RepoError(
                    f"smbclient fetch failed for {rel_path}: {detail}\n"
                    "Tip: if the share requires a password, embed credentials "
                    "in the URI: smb://user:password@host/share"
                )
            return Path(tmp_path).read_bytes()
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def resolve_uri(self, rel_path: str) -> str:
        return f"{self._base_uri}/{rel_path.lstrip('/')}"


# ---------------------------------------------------------------------------
# Fetcher factory
# ---------------------------------------------------------------------------

_SUPPORTED_SCHEMES = "local path, file://, http(s)://, ssh://, smb://"


def _make_fetcher(
    uri: str,
    *,
    ssh_identity: str | None = None,
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

    if scheme == "smb":
        return _SmbclientFetcher(uri)

    if scheme == "nfs":
        raise RepoError(
            "nfs:// repositories are no longer supported. "
            "Use a local path, http(s)://, ssh://, or smb:// repo instead."
        )

    raise RepoError(
        f"Unsupported URI scheme {scheme!r}. Supported: {_SUPPORTED_SCHEMES}"
    )


# ---------------------------------------------------------------------------
# SMB write helper
# ---------------------------------------------------------------------------

def _smb_writable_path(uri: str, rel_path: str = "") -> Path:
    """Return a writable GVFS FUSE path for an SMB URI.

    Mounts the share via GIO on demand (credentials from GNOME Keyring if
    available, or a bare Gio.MountOperation otherwise).  This is intentionally
    called only for write operations — browsing uses :class:`_SmbclientFetcher`
    which never creates a mount point.

    Raises :exc:`RepoError` on failure.
    """
    try:
        import gi
        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib
    except (ImportError, ValueError) as exc:
        raise RepoError("GIO is unavailable; cannot write to smb:// repo") from exc

    target = uri.rstrip("/") + ("/" + rel_path.lstrip("/") if rel_path else "")
    gfile = Gio.File.new_for_uri(target)

    # Try to get the FUSE path immediately (share already mounted).
    fuse_path = gfile.get_path()
    if fuse_path:
        return Path(fuse_path)

    # Share not mounted yet — attempt to mount it.
    main_loop = GLib.MainLoop()
    _err: list = [None]

    def _on_mounted(src, result, _user_data):
        try:
            src.mount_enclosing_volume_finish(result)
        except GLib.Error as exc:
            if not exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.ALREADY_MOUNTED):
                _err[0] = exc
        finally:
            main_loop.quit()

    mount_op = Gio.MountOperation()
    gfile.mount_enclosing_volume(
        Gio.MountMountFlags.NONE, mount_op, None, _on_mounted, None
    )
    main_loop.run()

    if _err[0] is not None:
        exc = _err[0]
        if exc.matches(Gio.io_error_quark(), Gio.IOErrorEnum.FAILED_HANDLED):
            raise RepoError("Authentication cancelled")
        raise RepoError(f"Could not mount {uri}: {exc.message}")

    fuse_path = Gio.File.new_for_uri(target).get_path()
    if fuse_path:
        return Path(fuse_path)
    raise RepoError(
        "GVFS FUSE mount is not available for this share. "
        "Ensure gvfsd-fuse is running (standard on GNOME)."
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
        ssl_verify: bool = True,
        ca_cert: str | None = None,
        token: str | None = None,
    ) -> None:
        self.uri = uri
        self.name = name or uri
        self._token = token
        self._asset_cache: tempfile.TemporaryDirectory | None = None
        self._fetcher: _Fetcher = _make_fetcher(
            uri,
            ssh_identity=ssh_identity,
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
        """``False`` for HTTP(S) repos; ``True`` for local, SSH, and SMB."""
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
        """Return a URI/path string for a repo-relative asset (icon, screenshot…).

        For HTTP(S) repos, image assets (png, jpg, …) are downloaded to a
        per-session temporary cache directory and the local path is returned.
        This lets GdkPixbuf load them correctly — it cannot pass an
        ``Authorization`` header when given a bare http:// URL — and makes
        ``os.path.isfile()`` return ``True`` for screenshots and hero images.

        Non-image assets (archives) are returned as URLs so the installer's
        own auth-aware download code handles them.
        """
        if (
            repo_relative
            and isinstance(self._fetcher, (_HttpFetcher, _SmbclientFetcher))
            and Path(repo_relative).suffix.lower() in _IMAGE_EXTENSIONS
        ):
            return self._fetch_to_cache(repo_relative)
        return self._fetcher.resolve_uri(repo_relative)

    def _fetch_to_cache(self, rel_path: str) -> str:
        """Download *rel_path* via the HTTP fetcher (with auth) to a temp file.

        Returns the local file path on success, or an empty string on failure
        (callers treat missing images as optional).  Results are cached for the
        lifetime of this ``Repo`` instance so each asset is only downloaded once
        per session.
        """
        if self._asset_cache is None:
            self._asset_cache = tempfile.TemporaryDirectory(prefix="cellar-assets-")
        parts = rel_path.lstrip("/").split("/")
        dest = Path(self._asset_cache.name).joinpath(*parts)
        if dest.exists():
            return str(dest)
        try:
            data = self._fetcher.fetch_bytes(rel_path)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return str(dest)
        except RepoError as exc:
            log.warning("Could not cache image asset %r: %s", rel_path, exc)
            return ""

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

        For SMB repos the share is mounted on demand via GIO/GVFS.  Unlike
        the old read path (which mounted on every catalogue load, creating a
        persistent Files sidebar entry), this method is only called by the
        packager during explicit write operations (add / edit / remove app).
        The GVFS FUSE path is returned so that ``packager.py`` can use
        ordinary :class:`pathlib.Path` operations.

        Raises :exc:`RepoError` for HTTP repos or when GVFS FUSE is
        unavailable.
        """
        if isinstance(self._fetcher, _LocalFetcher):
            return self._fetcher._root / rel_path.lstrip("/")

        if isinstance(self._fetcher, _SmbclientFetcher):
            return _smb_writable_path(self.uri, rel_path)

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

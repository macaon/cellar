"""Catalogue fetching and repo management.

Supported URI schemes
---------------------
- Bare local path or ``file://`` → reads directly from the filesystem
- ``http://`` / ``https://``     → fetches via urllib (no extra deps); **read-only**
- ``ssh://[user@]host[:port]/path`` → streams files through the system ssh client;
  key auth handled by SSH agent / ``~/.ssh/config``;
  explicit identity file via ``ssh_identity=``
- ``smb://``                     → pure-Python SMBv2/v3 via ``smbprotocol``

HTTP(S) repos are always read-only — the client cannot initialise or modify
them.  All other transports support write operations.

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

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable
from urllib.parse import urlparse

import requests

from cellar.models.app_entry import AppEntry, BaseEntry
from cellar.utils.http import DEFAULT_TIMEOUT, make_session

log = logging.getLogger(__name__)

CATALOGUE_VERSION = 1
_ASSET_CACHE_ROOT = Path.home() / ".cache" / "cellar" / "assets"

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
        self._session = make_session(
            token=token, ssl_verify=ssl_verify, ca_cert=ca_cert,
        )

    def fetch_bytes(self, rel_path: str) -> bytes:
        url = self._base + rel_path.lstrip("/")
        try:
            resp = self._session.get(url, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            return resp.content
        except requests.HTTPError as exc:
            code = exc.response.status_code if exc.response is not None else "?"
            raise RepoError(f"HTTP {code} fetching {url}") from exc
        except requests.RequestException as exc:
            raise RepoError(f"Network error fetching {url}: {exc}") from exc

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


class _SmbFetcher:
    """Reads files from an SMB share via ``smbprotocol`` (no GVFS/FUSE mount).

    ``smbprotocol`` is a pure-Python SMBv2/v3 implementation that works on
    GNOME *and* KDE without creating mount points visible in the file manager.

    *username* and *password* are optional; many SMB shares accept guest/anonymous
    access when omitted.  The session is registered once on construction and
    reused for all subsequent calls.
    """

    def __init__(
        self,
        base_uri: str,
        *,
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        from cellar.utils.smb import smb_uri_to_unc
        self._base_uri = base_uri.rstrip("/")
        self._base_unc = smb_uri_to_unc(base_uri).rstrip("/")
        self._server = urlparse(base_uri).hostname or ""
        self._username = username
        self._password = password
        self._connect()

    def _connect(self) -> None:
        try:
            import smbclient  # type: ignore[import]
        except ImportError as exc:
            raise RepoError(
                "smbprotocol is not installed; cannot use smb:// repos. "
                "Install it with: pip install smbprotocol"
            ) from exc
        try:
            kwargs: dict = {}
            if self._username:
                kwargs["username"] = self._username
            if self._password:
                kwargs["password"] = self._password
            smbclient.register_session(self._server, **kwargs)
        except Exception as exc:
            raise RepoError(
                f"Could not connect to SMB server {self._server!r}: {exc}"
            ) from exc

    def fetch_bytes(self, rel_path: str) -> bytes:
        import smbclient  # type: ignore[import]
        unc = self._base_unc + "/" + rel_path.lstrip("/")
        try:
            with smbclient.open_file(unc, mode="rb") as f:
                return f.read()
        except Exception as exc:
            raise RepoError(f"SMB could not read {unc}: {exc}") from exc

    def resolve_uri(self, rel_path: str) -> str:
        return self._base_uri + "/" + rel_path.lstrip("/")


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
    smb_username: str | None = None,
    smb_password: str | None = None,
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
        return _SmbFetcher(
            uri,
            username=smb_username or parsed.username or None,
            password=smb_password or parsed.password or None,
        )

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
        ssl_verify: bool = True,
        ca_cert: str | None = None,
        token: str | None = None,
        smb_username: str | None = None,
        smb_password: str | None = None,
        # mount_op is accepted but ignored — kept for call-site compatibility
        # while callers are updated to remove it.
        mount_op: object | None = None,
    ) -> None:
        self.uri = uri
        self.name = name or uri
        self._token = token
        self._ssl_verify = ssl_verify
        self._ca_cert = ca_cert
        self._smb_username = smb_username
        self._cache_dir: Path | None = None
        self._bases: dict[str, BaseEntry] = {}
        self._fetcher: _Fetcher = _make_fetcher(
            uri,
            ssh_identity=ssh_identity,
            ssl_verify=ssl_verify,
            ca_cert=ca_cert,
            token=token,
            smb_username=smb_username,
            smb_password=smb_password,
        )

    @property
    def token(self) -> str | None:
        """Bearer token for HTTP(S) authentication, or ``None``."""
        return self._token

    @property
    def ssl_verify(self) -> bool:
        """Whether SSL certificate verification is enabled."""
        return self._ssl_verify

    @property
    def ca_cert(self) -> str | None:
        """Path to a custom CA certificate file, or ``None``."""
        return self._ca_cert

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
        Category icons from ``category_icons`` are injected into each entry.
        """
        import dataclasses
        from cellar.backend.packager import BASE_CATEGORY_ICONS

        raw = self._fetch_json("catalogue.json")

        category_icons_raw: dict[str, str] = {}
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("apps", [])
            self._init_asset_cache(raw.get("generated_at"))
            self._bases = {
                runner: BaseEntry.from_dict(runner, data)
                for runner, data in raw.get("bases", {}).items()
            }
            category_icons_raw = raw.get("category_icons") or {}
        else:
            raise RepoError("catalogue.json has an unexpected top-level type")

        entries: list[AppEntry] = []
        for item in items:
            try:
                e = AppEntry.from_dict(item)
                icon = category_icons_raw.get(e.category) or BASE_CATEGORY_ICONS.get(e.category, "")
                if icon:
                    e = dataclasses.replace(e, category_icon=icon)
                entries.append(e)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning(
                    "Skipping malformed catalogue entry %r: %s", item.get("id"), exc
                )
        log.info("Loaded %d entries from %s", len(entries), self.uri)
        return entries

    def fetch_bases(self) -> dict[str, BaseEntry]:
        """Return the base-image map for this repo.

        Always re-reads catalogue.json so callers see bases that were
        uploaded after the catalogue was first fetched.
        Keys are runner name strings (e.g. ``"soda-9.0-1"``).
        """
        self.fetch_catalogue()
        return dict(self._bases)

    def fetch_entry_by_id(self, app_id: str) -> AppEntry:
        """Load the catalogue and return the entry matching *app_id*."""
        for entry in self.fetch_catalogue():
            if entry.id == app_id:
                return entry
        raise RepoError(f"App {app_id!r} not found in catalogue at {self.uri}")

    def resolve_asset_uri(self, repo_relative: str) -> str:
        """Return a URI/path string for a repo-relative asset (icon, screenshot…).

        For non-local repos (HTTP, SSH, SMB), image assets are downloaded
        to a persistent cache directory and the local path is returned so that
        Pillow and ``os.path.isfile()`` work correctly.  Archives are returned
        as-is so the installer's own download code handles them.
        """
        if (
            repo_relative
            and not isinstance(self._fetcher, _LocalFetcher)
            and Path(repo_relative).suffix.lower() in _IMAGE_EXTENSIONS
        ):
            return self._fetch_to_cache(repo_relative)
        return self._fetcher.resolve_uri(repo_relative)

    def _init_asset_cache(self, generated_at: str | None = None) -> None:
        """Set up the persistent asset cache directory for this repo.

        Uses ``~/.cache/cellar/assets/<sha256-prefix>/`` keyed on the repo URI.
        If *generated_at* has changed since the cache was last written, the
        entire cache directory is wiped so stale images are never served.
        Local repos are excluded (files are already on disk).
        """
        if isinstance(self._fetcher, _LocalFetcher):
            return
        key = hashlib.sha256(self.uri.encode()).hexdigest()[:16]
        cache_dir = _ASSET_CACHE_ROOT / key
        sentinel = cache_dir / ".generated_at"
        if generated_at and cache_dir.exists():
            try:
                if sentinel.read_text().strip() != generated_at.strip():
                    shutil.rmtree(cache_dir, ignore_errors=True)
                    log.info("Asset cache invalidated for %s (catalogue updated)", self.uri)
            except OSError:
                pass
        cache_dir.mkdir(parents=True, exist_ok=True)
        if generated_at:
            try:
                sentinel.write_text(generated_at)
            except OSError:
                pass
        self._cache_dir = cache_dir

    def _fetch_to_cache(self, rel_path: str) -> str:
        """Download *rel_path* to the persistent asset cache and return its local path.

        Returns an empty string on failure; callers treat missing images as
        optional.  The file is only downloaded once per *generated_at* epoch —
        subsequent calls return the cached path instantly.
        """
        if self._cache_dir is None:
            self._init_asset_cache()
        if self._cache_dir is None:
            return ""
        parts = rel_path.lstrip("/").split("/")
        dest = self._cache_dir.joinpath(*parts)
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

    def peek_asset_cache(self, repo_relative: str) -> str:
        """Return the local path for *repo_relative* if it is already on disk.

        For local repos this is the file path itself.  For remote repos it is
        the path inside ``~/.cache/cellar/assets/``, but only if the file has
        already been downloaded.  Returns an empty string when the asset is not
        yet cached — the caller should then fetch it asynchronously.  Never
        triggers a download.
        """
        if not repo_relative:
            return ""
        if isinstance(self._fetcher, _LocalFetcher):
            return self._fetcher.resolve_uri(repo_relative)
        if Path(repo_relative).suffix.lower() not in _IMAGE_EXTENSIONS:
            return ""
        if self._cache_dir is None:
            return ""
        dest = self._cache_dir.joinpath(*repo_relative.lstrip("/").split("/"))
        return str(dest) if dest.exists() else ""

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

    def fetch_category_icons(self) -> dict[str, str]:
        """Return the category→icon-name mapping for this repo.

        :data:`~cellar.backend.packager.BASE_CATEGORY_ICONS` defaults are
        merged with any overrides stored in ``category_icons`` of
        ``catalogue.json``, with stored values taking precedence.
        """
        from cellar.backend.packager import BASE_CATEGORY_ICONS
        try:
            raw = self._fetch_json("catalogue.json")
            stored: dict[str, str] = raw.get("category_icons", {}) if isinstance(raw, dict) else {}
        except RepoError:
            stored = {}
        return {**BASE_CATEGORY_ICONS, **stored}

    def iter_categories(self) -> Iterator[str]:
        """Yield the distinct categories present in the catalogue."""
        seen: set[str] = set()
        for entry in self.fetch_catalogue():
            if entry.category not in seen:
                seen.add(entry.category)
                yield entry.category

    def local_path(self, rel_path: str = "") -> Path:
        """Return the absolute local filesystem path for a repo-relative path.

        Raises :exc:`RepoError` for non-local repos (HTTP, SSH, SMB).
        """
        if not isinstance(self._fetcher, _LocalFetcher):
            raise RepoError("local_path() is only available for local repos")
        return self._fetcher._root / rel_path.lstrip("/")

    def writable_path(self, rel_path: str = ""):
        """Return a writable path to the repo root (or a sub-path).

        For local repos returns a :class:`pathlib.Path`.

        For SMB repos returns a :class:`~cellar.utils.smb.SmbPath` — a
        path-like object that delegates all I/O to ``smbclient`` without
        creating a GVFS/FUSE mount point.  ``packager.py`` can use the same
        path arithmetic (``/``, ``.mkdir()``, ``.open()``, etc.) on either
        object type.

        Raises :exc:`RepoError` for HTTP/SSH repos.
        """
        if isinstance(self._fetcher, _LocalFetcher):
            return self._fetcher._root / rel_path.lstrip("/")

        if isinstance(self._fetcher, _SmbFetcher):
            from cellar.utils.smb import SmbPath
            base = SmbPath(self._fetcher._base_unc)
            if rel_path:
                return base / rel_path.lstrip("/")
            return base

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

"""Catalogue fetching and repo management.

Supported URI schemes
---------------------
- Bare local path or ``file://`` → reads directly from the filesystem
- ``http://`` / ``https://``     → fetches via urllib (no extra deps); **read-only**
- ``sftp://[user@]host[:port]/path`` → pure-Python SSHv2 via ``paramiko``;
  key auth handled by SSH agent / ``~/.ssh/config``;
  explicit identity file via ``ssh_identity=``
- ``smb://``                     → pure-Python SMBv2/v3 via ``smbprotocol``

HTTP(S) repos are always read-only — the client cannot initialise or modify
them.  All other transports support write operations.

catalogue.json format (v2)
--------------------------
The repo root contains a slim ``catalogue.json`` index::

    {
      "cellar_version": 2,
      "generated_at": "<ISO-8601 timestamp>",
      "apps": [ { ...index fields only... }, ... ]
    }

Full per-app metadata lives in ``apps/<id>/metadata.json``.
See :data:`~cellar.models.app_entry.INDEX_FIELDS` for what's in the index.
"""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Iterator, Protocol, runtime_checkable
from urllib.parse import urlparse

import requests

from cellar.models.app_entry import AppEntry, BaseEntry, RunnerEntry
from cellar.utils.http import DEFAULT_TIMEOUT, make_session

log = logging.getLogger(__name__)

CATALOGUE_VERSION = 2
_METADATA_CACHE_ROOT = Path.home() / ".cache" / "cellar" / "metadata"

def _is_file_not_found(err: str) -> bool:
    """Return True if the error indicates a missing file, not a connection issue."""
    low = err.lower()
    return any(kw in low for kw in (
        "no such file", "not found", "does not exist",
        "object name not found", "object path not found",
    ))
_ASSET_CACHE_ROOT = Path.home() / ".cache" / "cellar" / "assets"
_CATALOGUE_CACHE_ROOT = Path.home() / ".cache" / "cellar" / "catalogues"

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
    """Reads files from a remote host via ``paramiko`` (pure-Python SSHv2).

    Key authentication is handled transparently by the SSH agent and
    ``~/.ssh/config``.  An explicit identity file can be provided via
    the *identity* parameter.
    """

    def __init__(
        self,
        host: str,
        remote_root: str,
        *,
        user: str | None = None,
        port: int | None = None,
        identity: str | None = None,
        password: str | None = None,
    ) -> None:
        self._host = host
        self._root = remote_root.rstrip("/") or "/"
        self._user = user
        self._port = port or 22
        self._identity = identity
        self._password = password

    def _sftp(self):
        import contextlib

        from cellar.utils.ssh import _get_sftp, _return_sftp

        @contextlib.contextmanager
        def _checkout():
            sftp = _get_sftp(self._host, self._port, self._user, self._identity, self._password)
            try:
                yield sftp
            finally:
                _return_sftp(self._host, self._port, self._user, self._identity, sftp)
        return _checkout()

    def fetch_bytes(self, rel_path: str) -> bytes:
        remote = f"{self._root}/{rel_path.lstrip('/')}"
        log.debug("SFTP fetch: %s", remote)
        try:
            with self._sftp() as sftp:
                with sftp.open(remote, "rb") as f:
                    f.MAX_REQUEST_SIZE = 1024 * 1024
                    f.prefetch()
                    return f.read()
        except FileNotFoundError:
            raise RepoError(
                f"SSH fetch failed for {rel_path}: "
                f"file not found at {remote}"
            )
        except IOError as exc:
            raise RepoError(
                f"SSH fetch failed for {rel_path} at {remote}: {exc}"
            ) from exc
        except Exception as exc:
            raise RepoError(f"SSH fetch failed for {rel_path}: {exc}") from exc

    def resolve_uri(self, rel_path: str) -> str:
        user_part = f"{self._user}@" if self._user else ""
        port_part = f":{self._port}" if self._port != 22 else ""
        return (
            f"sftp://{user_part}{self._host}{port_part}"
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
            with smbclient.open_file(unc, mode="rb", share_access="r") as f:
                return f.read()
        except Exception as exc:
            raise RepoError(f"SMB could not read {unc}: {exc}") from exc

    def resolve_uri(self, rel_path: str) -> str:
        return self._base_uri + "/" + rel_path.lstrip("/")


# ---------------------------------------------------------------------------
# Fetcher factory
# ---------------------------------------------------------------------------

_SUPPORTED_SCHEMES = "local path, file://, http(s)://, sftp://, smb://"


def _make_fetcher(
    uri: str,
    *,
    ssh_identity: str | None = None,
    ssh_username: str | None = None,
    ssh_password: str | None = None,
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

    if scheme == "sftp":
        if not parsed.hostname:
            raise RepoError(f"Invalid SFTP URI (no host): {uri!r}")
        return _SshFetcher(
            host=parsed.hostname,
            remote_root=parsed.path or "/",
            user=ssh_username or parsed.username or None,
            port=parsed.port or None,
            identity=ssh_identity,
            password=ssh_password,
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
    For ``sftp://`` repos, an explicit identity file path can be passed via
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
        ssh_username: str | None = None,
        ssh_password: str | None = None,
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
        self._ssh_identity = ssh_identity
        self._smb_username = smb_username
        self._cache_dir: Path | None = None
        self._runners: dict[str, RunnerEntry] = {}
        self._bases: dict[str, BaseEntry] = {}
        self._is_offline: bool = False
        self._catalogue_missing: bool = False
        # Only catch constructor errors for remote transports that have a
        # catalogue cache (SMB, SSH).  Local repos and unsupported schemes
        # must still raise immediately.
        _cacheable_scheme = urlparse(uri).scheme.lower() in ("smb", "sftp", "http", "https")
        try:
            self._fetcher: _Fetcher | None = _make_fetcher(
                uri,
                ssh_identity=ssh_identity,
                ssh_username=ssh_username,
                ssh_password=ssh_password,
                ssl_verify=ssl_verify,
                ca_cert=ca_cert,
                token=token,
                smb_username=smb_username,
                smb_password=smb_password,
            )
        except RepoError:
            if not _cacheable_scheme:
                raise
            log.warning("Could not connect to %s — will use cache if available", uri)
            self._fetcher = None
            self._is_offline = True

    @property
    def token(self) -> str | None:
        """Bearer token for HTTP(S) authentication, or ``None``."""
        return self._token

    @property
    def ssh_identity(self) -> str | None:
        """Path to the SSH identity file, or ``None`` (use agent/config)."""
        return self._ssh_identity

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
    def is_offline(self) -> bool:
        """``True`` when the last ``fetch_catalogue()`` fell back to a cached copy."""
        return self._is_offline

    @property
    def is_catalogue_missing(self) -> bool:
        """``True`` when the remote is reachable but ``catalogue.json`` doesn't exist."""
        return self._catalogue_missing

    @property
    def is_writable(self) -> bool:
        """``False`` for HTTP(S) repos or when offline; ``True`` otherwise."""
        if self._is_offline:
            return False
        return urlparse(self.uri).scheme.lower() not in ("http", "https")

    @property
    def _cache_key(self) -> str:
        """Short hash of the repo URI, used as a cache directory name."""
        return hashlib.sha256(self.uri.encode()).hexdigest()[:16]

    @property
    def _is_local(self) -> bool:
        return self._fetcher is not None and isinstance(self._fetcher, _LocalFetcher)

    # ------------------------------------------------------------------
    # Public API — reading
    # ------------------------------------------------------------------

    def _catalogue_cache_path(self) -> Path | None:
        """Return the local cache path for this repo's catalogue.json, or None for local repos."""
        if self._is_local:
            return None
        return _CATALOGUE_CACHE_ROOT / self._cache_key / "catalogue.json"

    @staticmethod
    def clear_catalogue_cache(uri: str) -> None:
        """Remove any cached catalogue for *uri* so the next fetch hits the remote."""
        import shutil
        key = hashlib.sha256(uri.encode()).hexdigest()[:16]
        cache_dir = _CATALOGUE_CACHE_ROOT / key
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)

    @staticmethod
    def clear_asset_cache(uri: str) -> None:
        """Remove cached image assets for *uri*."""
        import shutil
        key = hashlib.sha256(uri.encode()).hexdigest()[:16]
        cache_dir = _ASSET_CACHE_ROOT / key
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)

    def fetch_catalogue(self, *, use_cache: bool = True) -> list[AppEntry]:
        """Load and parse ``catalogue.json``, returning all entries.

        Accepts both the current wrapper format
        ``{"cellar_version": …, "apps": […]}`` and a legacy bare JSON array.
        Category icons from ``category_icons`` are injected into each entry.

        On network/transport failure, falls back to a locally cached copy of
        the catalogue written on the last successful fetch.  Pass
        *use_cache=False* to skip the fallback (e.g. when validating a newly
        added repo) so the real remote error propagates.
        """
        import dataclasses

        from cellar.backend.packager import BASE_CATEGORY_ICONS

        cache_path = self._catalogue_cache_path()
        try:
            if self._fetcher is None:
                raise RepoError(f"Repo {self.uri} is not reachable")
            raw = self._fetch_json("catalogue.json")
            self._is_offline = False
            self._catalogue_missing = False
            if cache_path is not None:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
                except OSError as exc:
                    log.warning("Could not write catalogue cache for %s: %s", self.uri, exc)
        except RepoError as exc:
            catalogue_missing = _is_file_not_found(str(exc))
            if use_cache and cache_path is not None and cache_path.exists():
                self._is_offline = True
                self._catalogue_missing = catalogue_missing
                if catalogue_missing:
                    # Server reachable but catalogue.json gone — only keep
                    # cache if there are locally installed apps from this repo.
                    from cellar.backend import database as _db
                    has_installed = any(
                        r.get("repo_source") == self.uri
                        for r in _db.get_all_installed()
                    )
                    if not has_installed:
                        log.info(
                            "Catalogue missing on %s with no installed apps; clearing stale cache",
                            self.uri,
                        )
                        cache_path.unlink(missing_ok=True)
                        raise
                log.warning(
                    "Could not fetch catalogue from %s (%s); using cached copy", self.uri, exc
                )
                try:
                    raw = json.loads(cache_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as cache_exc:
                    raise RepoError(
                        f"Catalogue fetch failed and cache is unreadable: {cache_exc}"
                    ) from exc
            else:
                raise

        category_icons_raw: dict[str, str] = {}
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, dict):
            items = raw.get("apps", [])
            self._init_asset_cache()
            self._runners = {
                name: RunnerEntry.from_dict(name, data)
                for name, data in raw.get("runners", {}).items()
            }
            self._bases = {
                name: BaseEntry.from_dict(name, data)
                for name, data in raw.get("bases", {}).items()
            }
            category_icons_raw = raw.get("category_icons") or {}
        else:
            raise RepoError("catalogue.json has an unexpected top-level type")

        entries: list[AppEntry] = []
        for item in items:
            try:
                e = AppEntry.from_dict(item)
                icon = (
                    category_icons_raw.get(e.category)
                    or BASE_CATEGORY_ICONS.get(e.category, "")
                )
                if icon:
                    e = dataclasses.replace(e, category_icon=icon)
                entries.append(e)
            except (KeyError, TypeError, ValueError) as exc:
                log.warning(
                    "Skipping malformed catalogue entry %r: %s", item.get("id"), exc
                )
        log.debug("Loaded %d entries from %s", len(entries), self.uri)
        # Garbage-collect stale cached images after a successful online fetch.
        if not self._is_offline and self._cache_dir is not None:
            try:
                self.gc_asset_cache(entries)
            except Exception as exc:
                log.warning("Asset cache GC failed for %s: %s", self.uri, exc)
        return entries

    def fetch_runners(self) -> dict[str, RunnerEntry]:
        """Return the runner map for this repo.

        Always re-reads catalogue.json so callers see runners that were
        uploaded after the catalogue was first fetched.
        Keys are runner version strings (e.g. ``"GE-Proton10-32"``).
        """
        self.fetch_catalogue()
        return dict(self._runners)

    def fetch_bases(self) -> dict[str, BaseEntry]:
        """Return the base-image map for this repo.

        Always re-reads catalogue.json so callers see bases that were
        uploaded after the catalogue was first fetched.
        Keys are base name strings (e.g. ``"GE-Proton10-32"`` or
        ``"GE-Proton10-32-dotnet"``).
        """
        self.fetch_catalogue()
        return dict(self._bases)

    def fetch_entry_by_id(self, app_id: str) -> AppEntry:
        """Load the catalogue and return the entry matching *app_id*."""
        for entry in self.fetch_catalogue():
            if entry.id == app_id:
                return entry
        raise RepoError(f"App {app_id!r} not found in catalogue at {self.uri}")

    def fetch_app_metadata(self, app_id: str) -> AppEntry:
        """Fetch the full metadata for a single app from ``apps/<id>/metadata.json``.

        For remote repos, the result is cached to
        ``~/.cache/cellar/metadata/<hash>/<app_id>.json`` and the cache is
        used as a fallback when the repo is offline.

        The returned :class:`AppEntry` has ``category_icon`` injected the
        same way :meth:`fetch_catalogue` does.
        """
        import dataclasses

        from cellar.backend.packager import BASE_CATEGORY_ICONS

        rel_path = f"apps/{app_id}/metadata.json"
        cache_path = self._metadata_cache_path(app_id)

        try:
            if self._fetcher is None:
                raise RepoError(f"Repo {self.uri} is not reachable")
            raw = self._fetch_json(rel_path)
            # Update cache on success
            if cache_path is not None:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(
                        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
                    )
                except OSError as exc:
                    log.warning("Could not cache metadata for %s: %s", app_id, exc)
        except RepoError:
            if cache_path is not None and cache_path.exists():
                log.warning(
                    "Could not fetch metadata for %s from %s; using cache",
                    app_id, self.uri,
                )
                try:
                    raw = json.loads(cache_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    raise
            else:
                raise

        entry = AppEntry.from_dict(raw)

        # Inject category icon
        try:
            cat_icons = self.fetch_category_icons()
        except RepoError:
            cat_icons = BASE_CATEGORY_ICONS
        icon = cat_icons.get(entry.category, "")
        if icon:
            entry = dataclasses.replace(entry, category_icon=icon)
        return entry

    def _metadata_cache_path(self, app_id: str) -> Path | None:
        """Return the cache file path for per-app metadata, or ``None`` for local repos."""
        if self._is_local:
            return None
        return _METADATA_CACHE_ROOT / self._cache_key / f"{app_id}.json"

    def resolve_asset_uri(self, repo_relative: str) -> str:
        """Return a URI/path string for a repo-relative asset (icon, screenshot…).

        For non-local repos (HTTP, SSH, SMB), image assets are downloaded
        to a persistent cache directory and the local path is returned so that
        Pillow and ``os.path.isfile()`` work correctly.  Archives are returned
        as-is so the installer's own download code handles them.

        When the repo is offline (fetcher unavailable), returns cached assets
        if available; empty string otherwise.
        """
        if self._fetcher is None:
            return self.peek_asset_cache(repo_relative)
        if (
            repo_relative
            and not self._is_local
            and Path(repo_relative).suffix.lower() in _IMAGE_EXTENSIONS
        ):
            return self._fetch_to_cache(repo_relative)
        return self._fetcher.resolve_uri(repo_relative)

    def _init_asset_cache(self) -> None:
        """Set up the persistent asset cache directory for this repo.

        Uses ``~/.cache/cellar/assets/<sha256-prefix>/`` keyed on the repo URI.
        Image filenames are content-hashed, so the cache is never bulk-wiped;
        stale files are removed by :meth:`gc_asset_cache` after a successful
        catalogue fetch.  Local repos are excluded (files are already on disk).
        """
        if self._is_local:
            return
        cache_dir = _ASSET_CACHE_ROOT / self._cache_key
        cache_dir.mkdir(parents=True, exist_ok=True)
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
            if dest.stat().st_size > 0:
                return str(dest)
            # Zero-byte file left by a failed prior download — evict and re-fetch.
            dest.unlink(missing_ok=True)
            log.debug("Evicting zero-byte cached asset %r; re-fetching", rel_path)
        try:
            data = self._fetcher.fetch_bytes(rel_path)
            if not data:
                log.warning("Empty response for asset %r; skipping cache write", rel_path)
                return ""
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
        if self._is_local:
            return self._fetcher.resolve_uri(repo_relative)
        if Path(repo_relative).suffix.lower() not in _IMAGE_EXTENSIONS:
            return ""
        if self._cache_dir is None:
            return ""
        dest = self._cache_dir.joinpath(*repo_relative.lstrip("/").split("/"))
        return str(dest) if dest.exists() else ""

    def evict_asset_cache(self, repo_relative: str) -> None:
        """Delete the cached copy of *repo_relative* so the next resolve re-fetches it.

        No-op for local repos or when the file is not currently cached.
        """
        if self._is_local or not repo_relative:
            return
        if self._cache_dir is None:
            return
        dest = self._cache_dir.joinpath(*repo_relative.lstrip("/").split("/"))
        dest.unlink(missing_ok=True)

    def gc_asset_cache(self, entries) -> int:
        """Remove cached image files no longer referenced by *entries*.

        Also removes stale per-app metadata cache entries for app IDs
        that are no longer in the catalogue.

        Returns the number of files removed.  No-op for local repos or when
        the cache directory has not been initialised.
        """
        if self._cache_dir is None:
            return 0
        referenced: set[str] = set()
        app_ids: set[str] = set()
        for e in entries:
            app_ids.add(e.id)
            for attr in ("icon", "cover", "logo", "category_icon"):
                val = getattr(e, attr, "")
                if val:
                    referenced.add(val)
            for ss in getattr(e, "screenshots", ()):
                if ss:
                    referenced.add(ss)
        # Check whether we have full metadata (with screenshots) or only
        # partial index entries.  Partial entries lack screenshot paths, so
        # we must not GC files under apps/<id>/ subdirs — they may still be
        # valid cached screenshots that would be immediately re-downloaded.
        have_full = any(not getattr(e, "is_partial", False) for e in entries)
        # Walk the cache directory and remove unreferenced image files.
        removed = 0
        for cached in self._cache_dir.rglob("*"):
            if not cached.is_file():
                continue
            if cached.suffix.lower() not in _IMAGE_EXTENSIONS:
                continue
            rel = str(cached.relative_to(self._cache_dir))
            if rel in referenced:
                continue
            # With only partial entries, skip files inside app asset dirs
            # (screenshots) — we don't know which are still referenced.
            if not have_full and rel.startswith("apps/"):
                continue
            cached.unlink(missing_ok=True)
            removed += 1
        # GC stale metadata cache entries.
        meta_cache = self._metadata_cache_path("")
        if meta_cache is not None:
            meta_dir = meta_cache.parent
            if meta_dir.is_dir():
                for cached in meta_dir.iterdir():
                    if cached.suffix == ".json" and cached.stem not in app_ids:
                        cached.unlink(missing_ok=True)
                        removed += 1
        if removed:
            log.info("Asset cache GC: removed %d stale file(s) for %s", removed, self.uri)
        return removed

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
        if not self._is_local:
            raise RepoError("local_path() is only available for local repos")
        return self._fetcher._root / rel_path.lstrip("/")

    def writable_path(self, rel_path: str = ""):
        """Return a writable path to the repo root (or a sub-path).

        For local repos returns a :class:`pathlib.Path`.

        For SMB repos returns a :class:`~cellar.utils.smb.SmbPath` — a
        path-like object that delegates all I/O to ``smbclient`` without
        creating a GVFS/FUSE mount point.

        For SFTP repos returns a :class:`~cellar.utils.ssh.SshPath` — a
        path-like object that delegates all I/O to ``paramiko`` SFTP.

        ``packager.py`` can use the same path arithmetic (``/``,
        ``.mkdir()``, ``.open()``, etc.) on any of these object types.

        Raises :exc:`RepoError` for HTTP repos (read-only).
        """
        if self._fetcher is None:
            raise RepoError(f"Repo {self.uri} is offline — write operations unavailable")

        if self._is_local:
            return self._fetcher._root / rel_path.lstrip("/")

        if isinstance(self._fetcher, _SmbFetcher):
            from cellar.utils.smb import SmbPath
            base = SmbPath(self._fetcher._base_unc)
            if rel_path:
                return base / rel_path.lstrip("/")
            return base

        if isinstance(self._fetcher, _SshFetcher):
            from cellar.utils.ssh import SshPath
            base = SshPath(
                self._fetcher._host,
                self._fetcher._root,
                user=self._fetcher._user,
                port=self._fetcher._port,
                identity=self._fetcher._identity,
                password=self._fetcher._password,
            )
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
        if self._fetcher is None:
            raise RepoError(f"Repo {self.uri} is not reachable")
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

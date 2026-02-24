"""Catalogue and manifest fetching/parsing.

Phase 1: local file paths only.
Phase 7 will add GIO-based SMB/NFS and HTTP support.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Iterator
from urllib.parse import urlparse

from cellar.models.app_entry import AppEntry
from cellar.models.manifest import Manifest

log = logging.getLogger(__name__)


class RepoError(Exception):
    """Raised when a repo operation fails."""


class Repo:
    """Represents a single Cellar repository source.

    ``uri`` can currently be:
    - An absolute local path  (``/mnt/share/repo``)
    - A ``file://`` URL

    SMB, NFS, and HTTP support will be layered on in phase 7.
    """

    def __init__(self, uri: str, name: str = "") -> None:
        self.uri = uri
        self.name = name or uri
        self._root = self._resolve_root(uri)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def fetch_catalogue(self) -> list[AppEntry]:
        """Load and parse catalogue.json, returning all entries."""
        path = self._root / "catalogue.json"
        raw = self._read_json(path)
        entries: list[AppEntry] = []
        for item in raw:
            try:
                entries.append(AppEntry.from_dict(item))
            except (KeyError, TypeError) as exc:
                log.warning("Skipping malformed catalogue entry %r: %s", item.get("id"), exc)
        log.info("Loaded %d entries from %s", len(entries), self.uri)
        return entries

    def fetch_manifest(self, entry: AppEntry) -> Manifest:
        """Load and parse the manifest for a specific app entry."""
        path = self._root / entry.manifest
        raw = self._read_json(path)
        try:
            return Manifest.from_dict(raw)
        except (KeyError, TypeError, ValueError) as exc:
            raise RepoError(f"Malformed manifest for {entry.id!r}: {exc}") from exc

    def fetch_manifest_by_id(self, app_id: str) -> Manifest:
        """Convenience: load the catalogue, find ``app_id``, fetch its manifest."""
        entries = self.fetch_catalogue()
        for entry in entries:
            if entry.id == app_id:
                return self.fetch_manifest(entry)
        raise RepoError(f"App {app_id!r} not found in catalogue at {self.uri}")

    def resolve_path(self, repo_relative: str) -> Path:
        """Return the absolute local Path for a repo-relative asset path."""
        return self._root / repo_relative

    def iter_categories(self) -> Iterator[str]:
        """Yield the distinct categories present in the catalogue."""
        seen: set[str] = set()
        for entry in self.fetch_catalogue():
            if entry.category not in seen:
                seen.add(entry.category)
                yield entry.category

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_root(uri: str) -> Path:
        parsed = urlparse(uri)
        if parsed.scheme in ("", "file"):
            root = Path(parsed.path if parsed.path else uri).expanduser().resolve()
        else:
            raise RepoError(
                f"URI scheme {parsed.scheme!r} is not yet supported. "
                "Use a local path or file:// URI for now."
            )
        if not root.is_dir():
            raise RepoError(f"Repo root does not exist or is not a directory: {root}")
        return root

    @staticmethod
    def _read_json(path: Path) -> dict | list:
        try:
            with path.open(encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError as exc:
            raise RepoError(f"File not found: {path}") from exc
        except json.JSONDecodeError as exc:
            raise RepoError(f"Invalid JSON in {path}: {exc}") from exc


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
        (last-repo-wins policy; may be revisited when multi-repo is
        fully specified).
        """
        seen: dict[str, AppEntry] = {}
        for repo in self._repos:
            try:
                for entry in repo.fetch_catalogue():
                    seen[entry.id] = entry
            except RepoError as exc:
                log.warning("Could not load catalogue from %s: %s", repo.uri, exc)
        return list(seen.values())

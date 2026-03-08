"""Unified app/game catalogue entry.

A single ``AppEntry`` carries everything the client needs — browse grid
data, detail view metadata, and installer configuration — in one object.
The ``catalogue.json`` at the repo root is the sole source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal



@dataclass(frozen=True, slots=True)
class RunnerEntry:
    """A GE-Proton runner archived in the repository.

    Stored in the top-level ``runners`` dict of ``catalogue.json``.
    The dict key is the runner version string (e.g. ``"GE-Proton10-32"``).
    """

    name: str           # catalogue key, e.g. "GE-Proton10-32"
    archive: str        # repo-relative path, e.g. "runners/GE-Proton10-32.tar.zst"
    archive_size: int = 0
    archive_crc32: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "RunnerEntry":
        return cls(
            name=name,
            archive=data.get("archive", ""),
            archive_size=int(data.get("archive_size", 0)),
            archive_crc32=data.get("archive_crc32", ""),
        )

    def to_dict(self) -> dict:
        d: dict = {"archive": self.archive}
        if self.archive_size:
            d["archive_size"] = self.archive_size
        if self.archive_crc32:
            d["archive_crc32"] = self.archive_crc32
        return d


@dataclass(frozen=True, slots=True)
class BaseEntry:
    """A base bottle image used as the shared foundation for delta packages.

    Stored in the top-level ``bases`` dict of ``catalogue.json``.  The dict
    key is the base's display *name* (e.g. ``"GE-Proton10-32"`` or a custom
    label like ``"GE-Proton10-32-dotnet"``).  The *runner* field references
    a key in the ``runners`` section — the GE-Proton version baked into this
    base image and required at runtime.
    """

    name: str           # catalogue key / display name, e.g. "GE-Proton10-32-dotnet"
    runner: str         # references runners dict key, e.g. "GE-Proton10-32"
    archive: str        # repo-relative path to the base archive
    archive_size: int = 0
    archive_crc32: str = ""

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "BaseEntry":
        return cls(
            name=name,
            runner=data.get("runner", name),
            archive=data.get("archive", ""),
            archive_size=int(data.get("archive_size", 0)),
            archive_crc32=data.get("archive_crc32", ""),
        )

    def to_dict(self) -> dict:
        d: dict = {"runner": self.runner, "archive": self.archive}
        if self.archive_size:
            d["archive_size"] = self.archive_size
        if self.archive_crc32:
            d["archive_crc32"] = self.archive_crc32
        return d


@dataclass(frozen=True, slots=True)
class AppEntry:
    """Complete record for one app or game in the catalogue.

    Fields are grouped by concern:

    *Identity* — required; used by the browse grid and as stable keys.
    *Display* — metadata shown in the detail view.
    *Attribution* — developer/publisher info and external links.
    *Media* — icon, cover art, screenshots (repo-relative paths).
    *Installation* — archive location, hashes, Bottles component config.
    """

    # ── Identity (required) ───────────────────────────────────────────────
    id: str
    name: str
    version: str
    category: str
    # Injected by Repo.fetch_catalogue() from catalogue.json category_icons;
    # never written to per-app JSON.
    category_icon: str = ""

    # ── Display ───────────────────────────────────────────────────────────
    summary: str = ""
    description: str = ""

    # ── Attribution ───────────────────────────────────────────────────────
    developer: str = ""
    publisher: str = ""
    release_year: int | None = None
    content_rating: str = ""
    languages: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    website: str = ""
    store_links: dict[str, str] = field(default_factory=dict)

    # ── Media (repo-relative paths) ───────────────────────────────────────
    icon: str = ""       # square icon — browse grid
    cover: str = ""      # portrait cover — browse grid (games)
    logo: str = ""       # transparent logo (Steam-style) — replaces icon+name in detail view
    hide_title: bool = False   # suppress name label when logo is present
    screenshots: tuple[str, ...] = ()
    # Maps repo-relative screenshot path → original source URL (e.g. Steam CDN).
    # Sparse: only screenshots downloaded from a remote URL have an entry.
    # Used by the edit dialog to filter already-downloaded screenshots from
    # Steam suggestions on re-open, preventing duplicates.
    screenshot_sources: dict[str, str] = field(default_factory=dict)

    # ── Installation ──────────────────────────────────────────────────────
    archive: str = ""
    archive_size: int = 0
    archive_crc32: str = ""
    install_size_estimate: int = 0
    update_strategy: Literal["safe", "full"] = "safe"
    # Delta packaging — when set, this archive is a delta against the named
    # base image; the installer must seed the prefix from that base first.
    base_image: str = ""
    # Steam App ID — used to set GAMEID=umu-<id> for umu-launcher / protonfixes.
    # None means GAMEID=0 (no protonfixes applied).
    steam_appid: int | None = None
    # Platform: "windows" (umu/Wine) or "linux" (native Linux app).
    # For Linux apps, entry_point is the executable
    # path relative to the installed app directory (e.g. "bin/mygame").
    platform: str = "windows"
    # Launch targets — each dict has {"name": str, "path": str, "args": str}.
    # For Windows: path is relative to drive_c (e.g. "Program Files/App/app.exe").
    # For Linux: path is relative to the installed app directory.
    # The first target is the primary/default launch target.
    launch_targets: tuple[dict, ...] = ()
    compatibility_notes: str = ""
    changelog: str = ""
    lock_runner: bool = False

    # ------------------------------------------------------------------
    # Convenience accessors (primary = first target)
    # ------------------------------------------------------------------

    @property
    def entry_point(self) -> str:
        return self.launch_targets[0]["path"] if self.launch_targets else ""

    @property
    def launch_args(self) -> str:
        return self.launch_targets[0].get("args", "") if self.launch_targets else ""

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> "AppEntry":
        strategy = data.get("update_strategy", "safe")
        if strategy not in ("safe", "full"):
            raise ValueError(f"Unknown update_strategy: {strategy!r}")

        return cls(
            id=data["id"],
            name=data["name"],
            version=data["version"],
            category=data["category"],
            summary=data.get("summary", ""),
            description=data.get("description", ""),
            developer=data.get("developer", ""),
            publisher=data.get("publisher", ""),
            release_year=data.get("release_year"),
            content_rating=data.get("content_rating", ""),
            languages=tuple(data.get("languages", [])),
            genres=tuple(data.get("genres", [])),
            website=data.get("website", ""),
            store_links=dict(data.get("store_links", {})),
            icon=data.get("icon", ""),
            cover=data.get("cover", ""),
            logo=data.get("logo", ""),
            hide_title=bool(data.get("hide_title", False)),
            screenshots=tuple(data.get("screenshots", [])),
            screenshot_sources=dict(data.get("screenshot_sources", {})),
            archive=data.get("archive", ""),
            archive_size=int(data.get("archive_size", 0)),
            archive_crc32=data.get("archive_crc32", data.get("archive_sha256", "")),
            install_size_estimate=int(data.get("install_size_estimate", 0)),
            update_strategy=strategy,
            base_image=data.get("base_image", ""),
            steam_appid=data.get("steam_appid"),
            platform=data.get("platform", "windows"),
            launch_targets=tuple(data.get("launch_targets", [])),
            compatibility_notes=data.get("compatibility_notes", ""),
            changelog=data.get("changelog", ""),
            lock_runner=bool(data.get("lock_runner", False)),
        )

    def to_dict(self) -> dict:
        """Serialise to a ``catalogue.json``-compatible dict.

        Empty strings, empty collections, and ``None`` values are omitted
        to keep the JSON readable.
        """
        d: dict = {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "category": self.category,
        }
        _opt_str(d, "summary", self.summary)
        _opt_str(d, "description", self.description)
        _opt_str(d, "developer", self.developer)
        _opt_str(d, "publisher", self.publisher)
        if self.release_year is not None:
            d["release_year"] = self.release_year
        _opt_str(d, "content_rating", self.content_rating)
        _opt_seq(d, "languages", self.languages)
        _opt_seq(d, "genres", self.genres)
        _opt_str(d, "website", self.website)
        if self.store_links:
            d["store_links"] = dict(self.store_links)
        _opt_str(d, "icon", self.icon)
        _opt_str(d, "cover", self.cover)
        _opt_str(d, "logo", self.logo)
        if self.hide_title:
            d["hide_title"] = True
        _opt_seq(d, "screenshots", self.screenshots)
        if self.screenshot_sources:
            d["screenshot_sources"] = dict(self.screenshot_sources)
        _opt_str(d, "archive", self.archive)
        if self.archive_size:
            d["archive_size"] = self.archive_size
        _opt_str(d, "archive_crc32", self.archive_crc32)
        if self.install_size_estimate:
            d["install_size_estimate"] = self.install_size_estimate
        d["update_strategy"] = self.update_strategy
        _opt_str(d, "base_image", self.base_image)
        if self.steam_appid is not None:
            d["steam_appid"] = self.steam_appid
        d["platform"] = self.platform
        if self.launch_targets:
            d["launch_targets"] = [dict(t) for t in self.launch_targets]
        _opt_str(d, "compatibility_notes", self.compatibility_notes)
        _opt_str(d, "changelog", self.changelog)
        if self.lock_runner:
            d["lock_runner"] = True
        return d


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _opt_str(d: dict, key: str, value: str) -> None:
    if value:
        d[key] = value


def _opt_seq(d: dict, key: str, value: tuple) -> None:
    if value:
        d[key] = list(value)

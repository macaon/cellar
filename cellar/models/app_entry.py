"""Unified app/game catalogue entry.

A single ``AppEntry`` carries everything the client needs — browse grid
data, detail view metadata, and installer configuration — in one object.
The ``catalogue.json`` at the repo root is the sole source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class BuiltWith:
    """Bottle component versions the archive was built against."""

    runner: str
    dxvk: str = ""
    vkd3d: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "BuiltWith":
        return cls(
            runner=data.get("runner", ""),
            dxvk=data.get("dxvk", ""),
            vkd3d=data.get("vkd3d", ""),
        )

    def to_dict(self) -> dict:
        d: dict = {"runner": self.runner}
        if self.dxvk:
            d["dxvk"] = self.dxvk
        if self.vkd3d:
            d["vkd3d"] = self.vkd3d
        return d


@dataclass(frozen=True, slots=True)
class AppEntry:
    """Complete record for one app or game in the catalogue.

    Fields are grouped by concern:

    *Identity* — required; used by the browse grid and as stable keys.
    *Display* — metadata shown in the detail view.
    *Attribution* — developer/publisher info and external links.
    *Media* — icon, cover art, hero banner, screenshots (repo-relative paths).
    *Installation* — archive location, hashes, Bottles component config.
    """

    # ── Identity (required) ───────────────────────────────────────────────
    id: str
    name: str
    version: str
    category: str

    # ── Display ───────────────────────────────────────────────────────────
    summary: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()

    # ── Attribution ───────────────────────────────────────────────────────
    developer: str = ""
    publisher: str = ""
    release_year: int | None = None
    content_rating: str = ""
    languages: tuple[str, ...] = ()
    website: str = ""
    store_links: dict[str, str] = field(default_factory=dict)

    # ── Media (repo-relative paths) ───────────────────────────────────────
    icon: str = ""       # square icon — browse grid
    cover: str = ""      # portrait cover — detail view
    hero: str = ""       # wide banner — detail view header
    screenshots: tuple[str, ...] = ()

    # ── Installation ──────────────────────────────────────────────────────
    archive: str = ""
    archive_size: int = 0
    archive_sha256: str = ""
    install_size_estimate: int = 0
    built_with: BuiltWith | None = None
    update_strategy: Literal["safe", "full"] = "safe"
    # Path to the main executable relative to drive_c — used for shortcuts
    # and optional smoke tests after install.
    entry_point: str = ""
    compatibility_notes: str = ""
    changelog: str = ""

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> "AppEntry":
        strategy = data.get("update_strategy", "safe")
        if strategy not in ("safe", "full"):
            raise ValueError(f"Unknown update_strategy: {strategy!r}")

        built_with_raw = data.get("built_with")

        return cls(
            id=data["id"],
            name=data["name"],
            version=data["version"],
            category=data["category"],
            summary=data.get("summary", ""),
            description=data.get("description", ""),
            tags=tuple(data.get("tags", [])),
            developer=data.get("developer", ""),
            publisher=data.get("publisher", ""),
            release_year=data.get("release_year"),
            content_rating=data.get("content_rating", ""),
            languages=tuple(data.get("languages", [])),
            website=data.get("website", ""),
            store_links=dict(data.get("store_links", {})),
            icon=data.get("icon", ""),
            cover=data.get("cover", ""),
            hero=data.get("hero", ""),
            screenshots=tuple(data.get("screenshots", [])),
            archive=data.get("archive", ""),
            archive_size=int(data.get("archive_size", 0)),
            archive_sha256=data.get("archive_sha256", ""),
            install_size_estimate=int(data.get("install_size_estimate", 0)),
            built_with=BuiltWith.from_dict(built_with_raw) if built_with_raw else None,
            update_strategy=strategy,
            entry_point=data.get("entry_point", ""),
            compatibility_notes=data.get("compatibility_notes", ""),
            changelog=data.get("changelog", ""),
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
        _opt_seq(d, "tags", self.tags)
        _opt_str(d, "developer", self.developer)
        _opt_str(d, "publisher", self.publisher)
        if self.release_year is not None:
            d["release_year"] = self.release_year
        _opt_str(d, "content_rating", self.content_rating)
        _opt_seq(d, "languages", self.languages)
        _opt_str(d, "website", self.website)
        if self.store_links:
            d["store_links"] = dict(self.store_links)
        _opt_str(d, "icon", self.icon)
        _opt_str(d, "cover", self.cover)
        _opt_str(d, "hero", self.hero)
        _opt_seq(d, "screenshots", self.screenshots)
        _opt_str(d, "archive", self.archive)
        if self.archive_size:
            d["archive_size"] = self.archive_size
        _opt_str(d, "archive_sha256", self.archive_sha256)
        if self.install_size_estimate:
            d["install_size_estimate"] = self.install_size_estimate
        if self.built_with is not None:
            d["built_with"] = self.built_with.to_dict()
        d["update_strategy"] = self.update_strategy
        _opt_str(d, "entry_point", self.entry_point)
        _opt_str(d, "compatibility_notes", self.compatibility_notes)
        _opt_str(d, "changelog", self.changelog)
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

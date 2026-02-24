"""Lightweight catalogue entry, as returned by catalogue.json."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AppEntry:
    """One row from catalogue.json — minimal data for the browse grid."""

    id: str
    name: str
    category: str
    summary: str
    icon: str          # repo-relative path, e.g. "apps/appname/icon.png"
    version: str
    manifest: str      # repo-relative path to manifest.json

    @classmethod
    def from_dict(cls, data: dict) -> "AppEntry":
        return cls(
            id=data["id"],
            name=data["name"],
            category=data["category"],
            summary=data["summary"],
            icon=data["icon"],
            version=data["version"],
            manifest=data["manifest"],
        )

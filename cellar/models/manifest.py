"""Full app manifest, as returned by an app's manifest.json."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True, slots=True)
class BuiltWith:
    """Component versions the bottle was built against."""

    runner: str
    dxvk: str = ""
    vkd3d: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "BuiltWith":
        return cls(
            runner=data["runner"],
            dxvk=data.get("dxvk", ""),
            vkd3d=data.get("vkd3d", ""),
        )


@dataclass(frozen=True, slots=True)
class Manifest:
    """Full metadata for a single app — fetched on detail view or install."""

    id: str
    name: str
    version: str
    category: str
    description: str
    icon: str                          # repo-relative path
    screenshots: tuple[str, ...]       # repo-relative paths
    archive: str                       # repo-relative path to .tar.gz
    archive_size: int                  # bytes
    archive_sha256: str
    built_with: BuiltWith
    update_strategy: Literal["safe", "full"]
    changelog: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> "Manifest":
        strategy = data.get("update_strategy", "safe")
        if strategy not in ("safe", "full"):
            raise ValueError(f"Unknown update_strategy: {strategy!r}")

        return cls(
            id=data["id"],
            name=data["name"],
            version=data["version"],
            category=data["category"],
            description=data["description"],
            icon=data["icon"],
            screenshots=tuple(data.get("screenshots", [])),
            archive=data["archive"],
            archive_size=int(data["archive_size"]),
            archive_sha256=data["archive_sha256"],
            built_with=BuiltWith.from_dict(data["built_with"]),
            update_strategy=strategy,
            changelog=data.get("changelog", ""),
        )

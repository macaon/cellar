"""Base types for launcher scanners."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DetectedGame:
    """A game detected inside a launcher's Wine prefix."""

    manifest_id: str        # raw ID from the launcher (e.g. Epic's AppName)
    display_name: str       # user-facing title
    install_location: str   # absolute Windows path to the game directory
    launch_exe: str         # relative exe path within install_location
    install_size: int       # bytes (0 if unknown)
    icon_url: str           # artwork URL if available, else ""
    icon_path: str          # local .ico/.png path found in prefix, else ""
    exe_path: str           # full Linux path to the exe (icon extraction source)
    catalog_id: str         # store-specific dedup key, else ""
    launch_uri: str         # protocol URI for launcher-mediated launch, else ""


class LauncherScanner(Protocol):
    """Interface for detecting games within a launcher's Wine prefix."""

    launcher_id: str

    def detect(self, prefix_path: Path) -> bool:
        """Quick check: does *prefix_path* contain this launcher's data?"""
        ...

    def scan(self, prefix_path: Path) -> list[DetectedGame]:
        """Parse manifest files and return detected games.

        Skips incomplete installs.  Returns an empty list if the
        launcher is not present.
        """
        ...

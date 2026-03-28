"""Epic Games Store scanner.

Detects games installed by the Epic Games Launcher within a Wine prefix
by reading the JSON ``.item`` manifest files that Epic writes to
``drive_c/ProgramData/Epic/EpicGamesLauncher/Data/Manifests/``.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from cellar.backend.scanners.base import DetectedGame

log = logging.getLogger(__name__)

MANIFESTS_REL = (
    "drive_c/ProgramData/Epic/EpicGamesLauncher/Data/Manifests"
)


class EpicScanner:
    """Scan a Wine prefix for Epic Games Store installations."""

    launcher_id: str = "epic"

    def detect(self, prefix_path: Path) -> bool:
        """Return True if the Epic manifest directory exists."""
        return (prefix_path / MANIFESTS_REL).is_dir()

    def scan(self, prefix_path: Path) -> list[DetectedGame]:
        """Parse all .item manifests and return detected games."""
        manifest_dir = prefix_path / MANIFESTS_REL
        if not manifest_dir.is_dir():
            return []

        games: list[DetectedGame] = []
        for item_file in sorted(manifest_dir.glob("*.item")):
            game = _parse_manifest(item_file, prefix_path)
            if game is not None:
                games.append(game)
        return games


def _win_to_linux_path(prefix_path: Path, win_path: str) -> Path:
    """Convert a Windows path to a Linux path within the prefix.

    Handles both ``C:\\`` and ``C:/`` style paths.
    """
    # Strip drive letter (e.g. "C:/" or "C:\\").
    cleaned = win_path.replace("\\", "/")
    if len(cleaned) >= 2 and cleaned[1] == ":":
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    return prefix_path / "drive_c" / cleaned


def _find_icon(game_dir: Path) -> str:
    """Search *game_dir* for the best .ico or .png file.

    Prefers the largest .ico file (likely highest resolution).
    Returns the path as a string, or "" if nothing found.
    """
    if not game_dir.is_dir():
        return ""

    # Collect .ico and .png files in the top-level game directory.
    candidates: list[Path] = []
    for ext in ("*.ico", "*.png"):
        candidates.extend(game_dir.glob(ext))

    if not candidates:
        return ""

    # Prefer .ico files, then largest file (most likely to be high-res).
    def sort_key(p: Path) -> tuple[int, int]:
        is_ico = 1 if p.suffix.lower() == ".ico" else 0
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        return (is_ico, size)

    best = max(candidates, key=sort_key)
    return str(best)


def _parse_manifest(path: Path, prefix_path: Path) -> DetectedGame | None:
    """Parse a single .item manifest file.  Returns None on error or
    if the install is incomplete."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Failed to parse Epic manifest %s: %s", path.name, exc)
        return None

    # Skip incomplete installs.
    if data.get("bIsIncompleteInstall", False):
        log.debug("Skipping incomplete install: %s", data.get("AppName", "?"))
        return None

    app_name = data.get("AppName", "")
    display_name = data.get("DisplayName", "")
    if not app_name or not display_name:
        log.warning("Manifest %s missing AppName or DisplayName", path.name)
        return None

    install_location = data.get("InstallLocation", "")
    launch_exe = data.get("LaunchExecutable", "")
    install_size = data.get("InstallSize", 0)
    icon_url = data.get("VaultThumbnailUrl", "")

    # Build a store-specific dedup key from namespace + item ID.
    namespace = data.get("CatalogNamespace", "")
    item_id = data.get("CatalogItemId", "")
    catalog_id = f"{namespace}:{item_id}" if namespace and item_id else ""

    # Protocol URI for launcher-mediated launch.
    launch_uri = ""
    if namespace and item_id and app_name:
        launch_uri = (
            f"com.epicgames.launcher://apps/"
            f"{namespace}:{item_id}:{app_name}"
            f"?action=launch&silent=true"
        )

    # Resolve local paths for icon discovery.
    game_dir = _win_to_linux_path(prefix_path, install_location)
    icon_path = _find_icon(game_dir)
    exe_path = str(game_dir / launch_exe) if launch_exe else ""

    return DetectedGame(
        manifest_id=app_name,
        display_name=display_name,
        install_location=install_location,
        launch_exe=launch_exe,
        install_size=install_size,
        icon_url=icon_url,
        icon_path=icon_path,
        exe_path=exe_path,
        catalog_id=catalog_id,
        launch_uri=launch_uri,
    )


# -- CLI runner for testing ---------------------------------------------------

def _fmt_size(nbytes: int) -> str:
    """Human-readable file size."""
    if nbytes <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def main() -> None:
    import sys

    if len(sys.argv) < 2:
        print(f"Usage: python -m cellar.backend.scanners.epic <prefix_path>")
        sys.exit(1)

    prefix = Path(sys.argv[1])
    if not prefix.is_dir():
        print(f"Error: {prefix} is not a directory")
        sys.exit(1)

    scanner = EpicScanner()
    if not scanner.detect(prefix):
        print(f"No Epic Games Launcher detected in {prefix}")
        sys.exit(0)

    games = scanner.scan(prefix)
    if not games:
        print("Epic Games Launcher detected, but no installed games found.")
        sys.exit(0)

    print(f"\nFound {len(games)} game(s) in {prefix}\n")
    print(f"{'Name':<40} {'Exe':<50} {'Size':>10}  Icon")
    print("─" * 120)
    for g in games:
        exe_display = g.launch_exe or "(none)"
        if g.icon_path:
            icon_display = f"LOCAL: {Path(g.icon_path).name}"
        elif g.icon_url:
            icon_display = f"URL: {g.icon_url[:40]}"
        else:
            icon_display = f"EXE: {Path(g.exe_path).name}" if g.exe_path else "—"
        print(
            f"{g.display_name:<40} {exe_display:<50} "
            f"{_fmt_size(g.install_size):>10}  {icon_display}"
        )
    print()

    for g in games:
        print(f"── {g.display_name} ──")
        print(f"  manifest_id:      {g.manifest_id}")
        print(f"  install_location: {g.install_location}")
        print(f"  launch_exe:       {g.launch_exe}")
        print(f"  exe_path:         {g.exe_path or '—'}")
        print(f"  install_size:     {_fmt_size(g.install_size)}")
        print(f"  catalog_id:       {g.catalog_id or '—'}")
        print(f"  launch_uri:       {g.launch_uri or '—'}")
        print(f"  icon_url:         {g.icon_url or '—'}")
        print(f"  icon_path:        {g.icon_path or '—'}")
        print()


if __name__ == "__main__":
    main()

"""Shared profile matching utilities for game detection.

Provides file fingerprinting and GOG ID matching used by both DOSBox
and ScummVM profile detection modules.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)


def content_root(game_dir: Path) -> Path:
    """Return the game content root (``hdd/`` if present, else *game_dir*)."""
    hdd = game_dir / "hdd"
    return hdd if hdd.is_dir() else game_dir


def find_file_casefold(root: Path, rel_path: str) -> Path | None:
    """Find *rel_path* under *root* using case-insensitive matching.

    Walks each path segment individually so ``SKY.EXE`` matches ``sky.exe``,
    ``Sky.Exe``, etc.  Returns the resolved ``Path`` or ``None``.
    """
    parts = Path(rel_path).parts
    current = root
    for part in parts:
        want = part.lower()
        try:
            match = None
            for entry in current.iterdir():
                if entry.name.lower() == want:
                    match = entry
                    break
            if match is None:
                return None
            current = match
        except OSError:
            return None
    return current if current.is_file() else None


def find_file_recursive(root: Path, filename: str) -> Path | None:
    """Find *filename* anywhere under *root* (case-insensitive, recursive).

    Used for bare filenames (no path separator) where the install directory
    name is unpredictable.
    """
    want = filename.lower()
    try:
        for path in root.rglob("*"):
            if path.is_file() and path.name.lower() == want:
                return path
    except OSError:
        pass
    return None


def match_gog_ids(root: Path, gog_ids: list[str]) -> bool:
    """Return ``True`` if any ``goggame-*.info`` file has a matching gameId."""
    if not gog_ids:
        return False
    want = set(str(g) for g in gog_ids)
    for info_path in root.glob("goggame-*.info"):
        try:
            data = json.loads(
                info_path.read_text(encoding="utf-8", errors="replace")
            )
            if str(data.get("gameId", "")) in want:
                return True
        except (json.JSONDecodeError, OSError):
            continue
    return False


def match_files(root: Path, files: list[str]) -> bool:
    """Return ``True`` if ALL listed files exist (case-insensitive).

    Requires at least 2 fingerprint files to reduce false positives.
    Bare filenames (no ``/``) are searched recursively under the content root.
    Paths containing ``/`` are matched from the content root directly.
    """
    if len(files) < 2:
        return False
    for f in files:
        if "/" in f:
            if find_file_casefold(root, f) is None:
                return False
        else:
            if find_file_recursive(root, f) is None:
                return False
    return True

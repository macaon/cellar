"""Launcher scanner registry.

Detects games installed within a Wine prefix by a third-party launcher
(Epic Games Store, Ubisoft Connect, Battle.net, etc.).  Each launcher
has its own scanner module that knows where to find manifest files and
how to parse them.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cellar.backend.scanners.base import DetectedGame, LauncherScanner

# Lazy imports to avoid circular deps and keep startup fast.
_SCANNER_FACTORIES: dict[str, str] = {
    "epic": "cellar.backend.scanners.epic",
}


def _get_scanner(launcher_id: str) -> LauncherScanner:
    """Import and instantiate a scanner by launcher ID."""
    module_path = _SCANNER_FACTORIES[launcher_id]
    import importlib

    mod = importlib.import_module(module_path)
    return mod.EpicScanner() if launcher_id == "epic" else mod.Scanner()


def detect_launchers(prefix_path: Path) -> list[str]:
    """Return launcher IDs whose manifest directories exist in *prefix_path*."""
    found: list[str] = []
    for launcher_id in _SCANNER_FACTORIES:
        scanner = _get_scanner(launcher_id)
        if scanner.detect(prefix_path):
            found.append(launcher_id)
    return found


def scan_prefix(prefix_path: Path, launcher_id: str) -> list[DetectedGame]:
    """Run a specific launcher's scanner against *prefix_path*."""
    scanner = _get_scanner(launcher_id)
    return scanner.scan(prefix_path)

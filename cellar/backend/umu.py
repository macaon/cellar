"""umu-launcher integration — path helpers, detection, and launch.

umu-launcher (https://github.com/Open-Wine-Components/umu-launcher) replaces
Bottles as the Windows compatibility layer for Cellar.  It runs arbitrary
Windows executables inside a GE-Proton environment without requiring Steam.

Storage layout
--------------
~/.local/share/cellar/
  runners/
    ge-proton10-32/      ← GE-Proton; managed by backend/runners.py
  prefixes/
    <app-id>/            ← one WINEPREFIX per installed app
  projects/
    <slug>/              ← Package Builder working area (Phase 3)
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

_FLATPAK_INFO = Path("/.flatpak-info")


def is_cellar_sandboxed() -> bool:
    """Return True if Cellar is running inside a Flatpak sandbox."""
    return _FLATPAK_INFO.exists()


def detect_umu(override: str | None = None) -> str | None:
    """Return the path to the umu-run binary, or None if not found.

    Search order:
    1. *override* (from ``config.json`` ``umu_path`` key)
    2. ``umu-run`` on ``$PATH``
    3. ``/app/bin/umu-run`` (Flatpak bundle location)
    """
    if override:
        return override
    found = shutil.which("umu-run")
    if found:
        return found
    bundled = Path("/app/bin/umu-run")
    if bundled.is_file():
        return str(bundled)
    return None


def runners_dir() -> Path:
    """Return (and create if needed) the Cellar runners directory."""
    from cellar.backend.config import data_dir
    d = data_dir() / "runners"
    d.mkdir(parents=True, exist_ok=True)
    return d


def prefixes_dir() -> Path:
    """Return (and create if needed) the Cellar prefixes directory."""
    from cellar.backend.config import data_dir
    d = data_dir() / "prefixes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def projects_dir() -> Path:
    """Return (and create if needed) the Cellar projects directory."""
    from cellar.backend.config import data_dir
    d = data_dir() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_runner_path(runner_name: str) -> Path | None:
    """Return the path for *runner_name* if installed, else None."""
    p = runners_dir() / runner_name
    return p if p.is_dir() else None


def build_env(
    app_id: str,
    runner_name: str,
    steam_appid: int | None,
    entry_point: str,
) -> dict[str, str]:
    """Return the environment variables dict for a umu invocation."""
    gameid = f"umu-{steam_appid}" if steam_appid else "0"
    return {
        "WINEPREFIX": str(prefixes_dir() / app_id),
        "PROTONPATH": str(runners_dir() / runner_name),
        "GAMEID": gameid,
        "EXE": entry_point,
    }


def _umu_cmd() -> list[str]:
    """Return the base umu-run command, prefixed with flatpak-spawn if sandboxed."""
    from cellar.backend.config import load_umu_path
    umu = detect_umu(load_umu_path()) or "umu-run"
    if is_cellar_sandboxed():
        return ["flatpak-spawn", "--host", umu]
    return [umu]


def launch_app(
    app_id: str,
    entry_point: str,
    runner_name: str,
    steam_appid: int | None,
) -> None:
    """Launch *entry_point* inside the *app_id* prefix.  Fire-and-forget."""
    import os
    env = {**os.environ, **build_env(app_id, runner_name, steam_appid, entry_point)}
    subprocess.Popen(_umu_cmd(), env=env, start_new_session=True)


def run_in_prefix(
    prefix_path: Path,
    runner_name: str,
    exe_or_verb: str,
    *,
    gameid: int = 0,
    extra_env: dict[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """Run *exe_or_verb* inside *prefix_path* using *runner_name*.  Blocking.

    Used by the Package Builder for wineboot, winetricks, and installer runs.

    Parameters
    ----------
    prefix_path:
        Full path to the WINEPREFIX directory.
    runner_name:
        Name of a runner inside ``runners_dir()`` (e.g. ``"ge-proton10-32"``).
    exe_or_verb:
        Path to an executable or a winetricks verb.
    gameid:
        umu GAMEID integer.  0 means no protonfixes.
    extra_env:
        Additional environment variables merged on top of the umu env.
    timeout:
        Subprocess timeout in seconds.
    """
    import os
    base_env: dict[str, str] = {
        "WINEPREFIX": str(prefix_path),
        "PROTONPATH": str(runners_dir() / runner_name),
        "GAMEID": str(gameid) if gameid else "0",
        "EXE": exe_or_verb,
    }
    if extra_env:
        base_env.update(extra_env)
    env = {**os.environ, **base_env}
    return subprocess.run(_umu_cmd(), env=env, timeout=timeout)

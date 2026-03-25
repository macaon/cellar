"""ScummVM runtime detection and launch helpers.

Detects system-installed ScummVM (Flatpak or native package), builds
launch commands, and manages per-game config files.  ScummVM is NOT
bundled - the user must install it separately.
"""

from __future__ import annotations

import configparser
import logging
import shlex
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_FLATPAK_ID = "org.scummvm.ScummVM"


# ---------------------------------------------------------------------------
# ScummVM detection
# ---------------------------------------------------------------------------

def _run_host(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Run a command on the host (escaping the Flatpak sandbox if needed)."""
    from cellar.backend.umu import is_cellar_sandboxed

    if is_cellar_sandboxed():
        cmd = ["flatpak-spawn", "--host"] + cmd
    return subprocess.run(cmd, capture_output=True, timeout=10, **kwargs)


def find_scummvm() -> str | None:
    """Return the ScummVM launch method, or ``None`` if not installed.

    Returns one of:
    - ``"flatpak"`` - ScummVM is installed as a Flatpak
    - ``"native"`` - ``scummvm`` is on the host PATH
    - ``None`` - not found
    """
    # Check Flatpak first (preferred - self-contained with codecs/soundfonts)
    try:
        result = _run_host(["flatpak", "info", _FLATPAK_ID])
        if result.returncode == 0:
            return "flatpak"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    # Check native binary
    try:
        result = _run_host(["which", "scummvm"])
        if result.returncode == 0:
            return "native"
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass

    return None


def is_scummvm_available() -> bool:
    """Return ``True`` if ScummVM is installed on the system."""
    return find_scummvm() is not None


# ---------------------------------------------------------------------------
# Launch command building
# ---------------------------------------------------------------------------

def _build_scummvm_cmd(
    game_dir: Path,
    scummvm_id: str,
) -> list[str]:
    """Build the ScummVM command list."""
    from cellar.backend.config import data_dir as cellar_data_dir
    from cellar.backend.umu import is_cellar_sandboxed

    method = find_scummvm()
    conf_path = game_dir / "config" / "scummvm.ini"
    data_dir = game_dir / "data"

    if method == "flatpak":
        # Grant ScummVM access to game data and shared MIDI assets
        # (soundfonts, MT-32 ROMs) which live in Cellar's data dir.
        cmd = [
            "flatpak", "run",
            f"--filesystem={game_dir}",
            f"--filesystem={cellar_data_dir()}",
            _FLATPAK_ID,
        ]
    else:
        cmd = ["scummvm"]

    if conf_path.is_file():
        cmd += ["-c", str(conf_path)]
    else:
        cmd += ["-p", str(data_dir)]

    cmd.append(scummvm_id)

    if is_cellar_sandboxed():
        cmd = ["flatpak-spawn", "--host"] + cmd

    return cmd


def build_scummvm_launch_cmd(
    game_dir: Path,
    scummvm_id: str,
) -> tuple[list[str], None]:
    """Build the ScummVM command line for launching a game.

    Returns ``(cmd, None)`` - the second element matches the DOSBox
    signature (tmp_conf) for API compatibility.
    """
    return _build_scummvm_cmd(game_dir, scummvm_id), None


def build_scummvm_exec_line(
    game_dir: Path,
    scummvm_id: str,
) -> str:
    """Return a shell-escaped Exec string for a ``.desktop`` entry."""
    return " ".join(shlex.quote(a) for a in _build_scummvm_cmd(
        game_dir, scummvm_id,
    ))


# ---------------------------------------------------------------------------
# Config file helpers
# ---------------------------------------------------------------------------

def write_scummvm_conf(
    game_dir: Path,
    scummvm_id: str,
    settings: dict | None = None,
) -> Path:
    """Write a per-game ``scummvm.ini`` to ``config/scummvm.ini``.

    *settings* is a dict of ``{section: {key: value}}`` overrides.
    Returns the path to the written config file.
    """
    conf_dir = game_dir / "config"
    conf_dir.mkdir(parents=True, exist_ok=True)
    conf_path = conf_dir / "scummvm.ini"

    config = configparser.ConfigParser()
    config.optionxform = str  # preserve key case

    # Read existing config if present
    if conf_path.is_file():
        config.read(str(conf_path), encoding="utf-8")

    # Ensure the game target section exists with required keys
    if not config.has_section(scummvm_id):
        config.add_section(scummvm_id)
    config.set(scummvm_id, "gameid", scummvm_id)
    config.set(scummvm_id, "path", str(game_dir / "data"))

    # Apply any profile settings
    if settings:
        for section, keys in settings.items():
            if not config.has_section(section):
                config.add_section(section)
            for key, value in keys.items():
                config.set(section, key, str(value))

    with open(conf_path, "w", encoding="utf-8") as f:
        f.write(f"# ScummVM config for {scummvm_id}\n")
        f.write("# Edit to customise game-specific settings.\n\n")
        config.write(f)

    log.info("Wrote ScummVM config: %s", conf_path)
    return conf_path


def read_scummvm_id(game_dir: Path) -> str:
    """Read the ScummVM game ID from the per-game config.

    Returns the game ID string, or empty string if not found.
    """
    conf_path = game_dir / "config" / "scummvm.ini"
    if not conf_path.is_file():
        return ""
    config = configparser.ConfigParser()
    config.optionxform = str
    try:
        config.read(str(conf_path), encoding="utf-8")
    except (configparser.Error, OSError):
        return ""
    # The game ID is the section name (excluding DEFAULT and scummvm)
    for section in config.sections():
        if section.lower() not in ("scummvm", "default"):
            return section
    return ""

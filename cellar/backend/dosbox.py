"""DOSBox Staging runtime and GOG DOSBox game detection/conversion.

DOSBox Staging is managed as a transparent runtime — a single shared
installation, auto-downloaded on first use, invisible to the end user.
Maintainers see an update banner when a newer version is available.

All functions are pure Python with no GTK dependency.
"""

from __future__ import annotations

import configparser
import json
import logging
import os
import re
import shlex
import shutil
import stat
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RELEASES_URL = (
    "https://api.github.com/repos/dosbox-staging/dosbox-staging/releases"
)
_NOUNIVBE_RELEASES_URL = (
    "https://api.github.com/repos/LowLevelMahn/NoUniVBE/releases"
)
_CACHE_TTL = 3600.0  # one hour
_ASSET_PATTERN = re.compile(
    r"dosbox-staging-linux-x86_64-.*\.tar\.xz$", re.IGNORECASE
)

_cache: tuple[float, dict] | None = None
_cache_lock = threading.Lock()
_fetch_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MountCmd:
    """A parsed ``mount`` command from a DOSBox autoexec section."""

    drive: str  # e.g. "C"
    path: str  # e.g. ".." or "..\\cloud_saves"
    flags: str = ""  # e.g. "-t overlay"


@dataclass(frozen=True, slots=True)
class AutoexecInfo:
    """Parsed ``[autoexec]`` section from a DOSBox config."""

    mounts: tuple[MountCmd, ...] = ()
    game_commands: tuple[str, ...] = ()
    raw_lines: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GogDosboxInfo:
    """Information extracted from a GOG game's ``goggame-*.info`` file."""

    game_id: str
    game_name: str
    dosbox_dir: str  # relative path to DOSBOX/ subdirectory
    conf_args: tuple[str, ...]  # -conf arguments (relative to working_dir)
    working_dir: str  # working directory from the play task
    play_tasks: tuple[dict, ...] = ()  # all game-category play tasks


# ---------------------------------------------------------------------------
# DOSBox Staging runtime
# ---------------------------------------------------------------------------


def dosbox_staging_dir() -> Path:
    """Return (and create if needed) the shared DOSBox Staging directory."""
    from cellar.backend.config import data_dir

    d = data_dir() / "dosbox-staging"
    d.mkdir(parents=True, exist_ok=True)
    return d


def dos_dir() -> Path:
    """Return (and create if needed) the Cellar DOS games directory."""
    from cellar.backend.config import data_dir

    d = data_dir() / "dos"
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_dosbox_staging(
    *,
    progress_cb: Callable[[int, int], None] | None = None,
) -> Path:
    """Return path to the ``dosbox`` binary, downloading if needed.

    DOSBox Staging runs in **portable mode**: a ``dosbox-staging.conf`` next
    to the binary makes DOSBox read/write config there instead of
    ``~/.config/dosbox/``.  This avoids conflicts with any user-installed
    DOSBox.  On first download, we touch an empty conf and let DOSBox
    auto-populate it with current defaults.

    *progress_cb(downloaded_bytes, total_bytes)* is called during download.
    """
    staging = dosbox_staging_dir()
    binary = staging / "dosbox"
    if binary.is_file():
        return binary

    release = _fetch_latest_release()
    if release is None:
        raise RuntimeError(
            "Could not fetch DOSBox Staging releases from GitHub"
        )

    _download_and_extract(release, staging, progress_cb=progress_cb)

    if not binary.is_file():
        raise RuntimeError(
            f"DOSBox Staging binary not found after extraction at {binary}"
        )

    # Enable portable mode and generate default config
    _init_portable_config(staging)

    # Download NoUniVBE alongside DOSBox Staging
    _ensure_nounivbe(staging)

    return binary


def check_update() -> tuple[str, str] | None:
    """Return ``(installed_tag, latest_tag)`` if an update is available.

    Returns ``None`` if up-to-date, not installed, or unable to check.
    """
    version_file = dosbox_staging_dir() / ".version"
    if not version_file.is_file():
        return None
    installed = version_file.read_text().strip()
    release = _fetch_latest_release()
    if release is None:
        return None
    latest = release["tag"]
    if latest and latest != installed:
        return (installed, latest)
    return None


def update_dosbox_staging(
    *,
    progress_cb: Callable[[int, int], None] | None = None,
) -> Path:
    """Download the latest DOSBox Staging, replacing the current install."""
    staging = dosbox_staging_dir()

    release = _fetch_latest_release()
    if release is None:
        raise RuntimeError(
            "Could not fetch DOSBox Staging releases from GitHub"
        )

    # Remove old installation (keep .version until new one is written)
    for child in staging.iterdir():
        if child.name == ".version":
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()

    _download_and_extract(release, staging, progress_cb=progress_cb)

    binary = staging / "dosbox"
    if not binary.is_file():
        raise RuntimeError("DOSBox Staging binary not found after update")

    # Re-generate portable config with new version's defaults
    _init_portable_config(staging)

    # Re-download NoUniVBE (in case of updates)
    _ensure_nounivbe(staging)

    return binary


# ---------------------------------------------------------------------------
# GOG DOSBox detection
# ---------------------------------------------------------------------------


def detect_gog_dosbox_in_prefix(prefix_path: Path) -> tuple[Path, GogDosboxInfo] | None:
    """Scan a WINEPREFIX's ``drive_c`` for a GOG DOSBox game.

    Searches all immediate subdirectories of common GOG install locations
    (``drive_c/GOG Games/``, ``drive_c/Program Files/``, etc.) for
    ``goggame-*.info`` files referencing DOSBox.

    Returns ``(game_folder, info)`` or ``None``.
    """
    drive_c = prefix_path / "drive_c"
    if not drive_c.is_dir():
        return None

    # Search all goggame-*.info files anywhere under drive_c
    for info_file in drive_c.rglob("goggame-*.info"):
        game_folder = info_file.parent
        result = detect_gog_dosbox(game_folder)
        if result is not None:
            return (game_folder, result)

    return None


def detect_gog_dosbox(folder: Path) -> GogDosboxInfo | None:
    """Detect a GOG game that uses DOSBox from ``goggame-*.info`` files.

    Returns ``GogDosboxInfo`` if the primary play task references
    ``dosbox.exe``, otherwise ``None``.
    """
    info_files = list(folder.glob("goggame-*.info"))
    if not info_files:
        return None

    for info_path in info_files:
        try:
            data = json.loads(
                info_path.read_text(encoding="utf-8", errors="replace")
            )
        except (json.JSONDecodeError, OSError):
            continue

        play_tasks = data.get("playTasks", [])
        if not play_tasks:
            continue

        # Find the primary game task
        primary = None
        game_tasks: list[dict] = []
        for task in play_tasks:
            if task.get("type") != "FileTask":
                continue
            cat = task.get("category", "")
            if cat == "game":
                game_tasks.append(task)
                if task.get("isPrimary"):
                    primary = task

        if primary is None and game_tasks:
            primary = game_tasks[0]

        if primary is None:
            continue

        task_path = primary.get("path", "")
        if "dosbox" not in task_path.lower():
            continue

        # Extract DOSBox directory from path (e.g. "DOSBOX\\dosbox.exe" → "DOSBOX")
        dosbox_dir = task_path.rsplit("\\", 1)[0] if "\\" in task_path else ""
        if not dosbox_dir:
            dosbox_dir = task_path.rsplit("/", 1)[0] if "/" in task_path else ""

        # Parse -conf arguments from the play task
        args_str = primary.get("arguments", "")
        conf_args = _parse_conf_args(args_str)

        working_dir = primary.get("workingDir", dosbox_dir)

        return GogDosboxInfo(
            game_id=data.get("gameId", ""),
            game_name=data.get("name", ""),
            dosbox_dir=dosbox_dir,
            conf_args=tuple(conf_args),
            working_dir=working_dir,
            play_tasks=tuple(game_tasks),
        )

    return None


# ---------------------------------------------------------------------------
# DOSBox config parsing & generation
# ---------------------------------------------------------------------------

# Settings we extract from GOG configs to put in overrides.
_EXTRACT_SECTIONS = {
    "cpu": {"cycles", "core", "cputype"},
    "dosbox": {"machine", "memsize"},
    "sblaster": {"sbtype", "sbbase", "irq", "dma", "hdma", "oplmode"},
    "gus": {"gus", "gusbase", "gusirq", "gusdma"},
    "midi": {"mpu401", "mididevice", "midiconfig"},
    "mixer": {"rate", "blocksize"},
    "render": {"aspect", "scaler"},
}


def parse_gog_confs(conf_paths: list[Path]) -> dict:
    """Read GOG ``.conf`` files and extract meaningful settings.

    Returns a dict with:
    - ``"settings"``: dict of ``{section: {key: value}}`` for non-default values
    - ``"autoexec"``: :class:`AutoexecInfo` merged from all confs (later wins)
    """
    settings: dict[str, dict[str, str]] = {}
    autoexec = AutoexecInfo()

    for conf_path in conf_paths:
        if not conf_path.is_file():
            log.warning("GOG conf not found: %s", conf_path)
            continue

        text = conf_path.read_text(encoding="utf-8", errors="replace")

        # Extract [autoexec] manually — configparser doesn't handle it well
        autoexec_info = _parse_autoexec_from_text(text)
        if autoexec_info.raw_lines:
            autoexec = autoexec_info  # later conf wins

        # Parse remaining sections with configparser
        # Strip the [autoexec] section first (it confuses configparser)
        text_no_autoexec = _strip_autoexec(text)
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(text_no_autoexec)

        for section, keys in _EXTRACT_SECTIONS.items():
            if not parser.has_section(section):
                continue
            for key in keys:
                if parser.has_option(section, key):
                    val = parser.get(section, key).strip()
                    if val:
                        settings.setdefault(section, {})[key] = val

    return {"settings": settings, "autoexec": autoexec}


def generate_overrides_conf(
    gog_settings: dict,
    dosbox_subdir: str,
) -> str:
    """Generate ``dosbox-overrides.conf`` from extracted GOG settings.

    *dosbox_subdir* is the relative path of the DOSBOX/ subdirectory in the
    original GOG layout (e.g. ``"DOSBOX"``).  Paths in the autoexec that
    reference ``..`` (parent of DOSBOX/) are rewritten to ``.`` (game root).
    """
    lines: list[str] = []
    lines.append("# DOSBox overrides — extracted from GOG configuration")
    lines.append("# Edit this file to customise game-specific settings.\n")

    # Quiet launch — no splash or banners
    lines.append("[dosbox]")
    lines.append("startup_verbosity = quiet")
    lines.append("")

    settings = gog_settings.get("settings", {})
    for section in sorted(settings):
        lines.append(f"[{section}]")
        for key in sorted(settings[section]):
            lines.append(f"{key} = {settings[section][key]}")
        lines.append("")

    autoexec: AutoexecInfo = gog_settings.get("autoexec", AutoexecInfo())
    if autoexec.raw_lines:
        rewritten = _rewrite_autoexec(autoexec.raw_lines, dosbox_subdir)
        lines.append("[autoexec]")
        # Inject NoUniVBE before game commands to bypass bundled UniVBE drivers
        # Insert after mount commands, before the first game command
        mount_done = False
        nounivbe_injected = False
        for line in rewritten:
            lower = line.strip().lower()
            # Track when we've passed the mount/drive-change section
            if lower.startswith("mount ") or (len(lower) == 2 and lower.endswith(":")):
                mount_done = True
                lines.append(line)
                continue
            # Inject NoUniVBE right before the first non-mount command
            if mount_done and not nounivbe_injected and lower and not lower.startswith("#"):
                lines.append("nounivbe\\NOUNIVBE.EXE")
                nounivbe_injected = True
            lines.append(line)
        if not nounivbe_injected and rewritten:
            # If no clear injection point, prepend before all commands
            lines.append("nounivbe\\NOUNIVBE.EXE")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def convert_gog_dosbox(
    source: Path,
    dest: Path,
    info: GogDosboxInfo,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """Convert a GOG Windows+DOSBox folder to a native Linux DOSBox game.

    Returns the ``entry_points`` list for the project.
    """
    # Step 1: Ensure DOSBox Staging is available (downloads + portable config + NoUniVBE)
    dosbox_binary = ensure_dosbox_staging(progress_cb=progress_cb)
    staging = dosbox_binary.parent

    # Step 2: Copy game files, excluding Windows DOSBOX directory
    dosbox_dir_lower = info.dosbox_dir.lower()

    def _ignore(directory: str, contents: list[str]) -> set[str]:
        # At the source root, skip the Windows DOSBOX/ directory
        if Path(directory) == source:
            return {
                name
                for name in contents
                if name.lower() == dosbox_dir_lower
            }
        return set()

    shutil.copytree(source, dest, ignore=_ignore, dirs_exist_ok=True)

    # Step 3: Copy DOSBox Staging binary + resources
    dosbox_dest = dest / "dosbox"
    dosbox_dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(staging / "dosbox", dosbox_dest / "dosbox")
    # Ensure binary is executable
    (dosbox_dest / "dosbox").chmod(
        (dosbox_dest / "dosbox").stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
    )
    # Copy resources if present (codepage mappings, etc.)
    staging_resources = staging / "resources"
    if staging_resources.is_dir():
        shutil.copytree(
            staging_resources,
            dosbox_dest / "resources",
            dirs_exist_ok=True,
        )
    # Copy soundfonts if present (for FluidSynth MIDI)
    staging_soundfonts = staging / "soundfonts"
    if staging_soundfonts.is_dir():
        shutil.copytree(
            staging_soundfonts,
            dosbox_dest / "soundfonts",
            dirs_exist_ok=True,
        )
    # Copy glshaders if present
    staging_glshaders = staging / "glshaders"
    if staging_glshaders.is_dir():
        shutil.copytree(
            staging_glshaders,
            dosbox_dest / "glshaders",
            dirs_exist_ok=True,
        )

    # Step 3b: Copy NoUniVBE
    nounivbe_src = staging / "nounivbe"
    if nounivbe_src.is_dir():
        shutil.copytree(nounivbe_src, dest / "nounivbe", dirs_exist_ok=True)

    # Step 4: Generate config files
    config_dest = dest / "config"
    config_dest.mkdir(parents=True, exist_ok=True)

    # Copy base config — use the portable-mode config generated by DOSBox
    # Staging itself (always up-to-date with the installed version's defaults).
    # Falls back to our shipped data file if portable config doesn't exist.
    portable_conf = staging / "dosbox-staging.conf"
    if portable_conf.is_file() and portable_conf.stat().st_size > 0:
        shutil.copy2(portable_conf, config_dest / "dosbox-staging.conf")
    else:
        from cellar.utils.paths import dosbox_conf
        shutil.copy2(dosbox_conf(), config_dest / "dosbox-staging.conf")

    # Parse GOG configs and generate overrides
    conf_paths = _resolve_conf_paths(source, info)
    gog_settings = parse_gog_confs(conf_paths)
    overrides = generate_overrides_conf(gog_settings, info.dosbox_dir)
    (config_dest / "dosbox-overrides.conf").write_text(
        overrides, encoding="utf-8"
    )

    # Step 5: Generate launch script
    launch_script = dest / "launch.sh"
    launch_script.write_text(
        '#!/bin/bash\n'
        'cd "$(dirname "$0")"\n'
        'exec dosbox/dosbox'
        ' -conf config/dosbox-staging.conf'
        ' -conf config/dosbox-overrides.conf\n',
        encoding="utf-8",
    )
    launch_script.chmod(
        launch_script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
    )

    # Step 6: Return entry points
    return [{"name": info.game_name or "Game", "path": "launch.sh", "args": ""}]


# ---------------------------------------------------------------------------
# Private helpers — runtime
# ---------------------------------------------------------------------------


def _fetch_latest_release() -> dict | None:
    """Return the latest stable DOSBox Staging release info, or ``None``."""
    global _cache
    with _cache_lock:
        if _cache is not None:
            age, cached = _cache
            if time.monotonic() - age < _CACHE_TTL:
                return cached

    with _fetch_lock:
        # Double-check after acquiring lock.
        with _cache_lock:
            if _cache is not None:
                age, cached = _cache
                if time.monotonic() - age < _CACHE_TTL:
                    return cached

        from cellar.utils.http import make_session

        try:
            session = make_session()
            resp = session.get(
                _RELEASES_URL,
                params={"per_page": 10},
                timeout=15,
            )
            resp.raise_for_status()
        except Exception as exc:  # noqa: BLE001
            log.warning("Failed to fetch DOSBox Staging releases: %s", exc)
            return _cache[1] if _cache else None

        for rel in resp.json():
            tag = rel.get("tag_name", "")
            # Skip pre-releases / alphas
            if rel.get("prerelease") or "alpha" in tag.lower():
                continue

            assets = rel.get("assets", [])
            for asset in assets:
                aname = asset.get("name", "")
                if _ASSET_PATTERN.search(aname):
                    result = {
                        "tag": tag,
                        "name": rel.get("name", "") or tag,
                        "url": asset.get("browser_download_url", ""),
                        "size": asset.get("size", 0),
                    }
                    with _cache_lock:
                        _cache = (time.monotonic(), result)
                    return result

        log.warning("No suitable DOSBox Staging release found")
        return None


def _download_and_extract(
    release: dict,
    dest: Path,
    *,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Download and extract a DOSBox Staging release tarball."""
    import lzma
    import tempfile

    from cellar.utils.http import make_session

    url = release["url"]
    total_size = release.get("size", 0)
    tag = release.get("tag", "unknown")

    log.info("Downloading DOSBox Staging %s from %s", tag, url)

    session = make_session()
    resp = session.get(url, stream=True, timeout=30)
    resp.raise_for_status()

    # Stream to a temp file
    with tempfile.NamedTemporaryFile(suffix=".tar.xz", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        downloaded = 0
        for chunk in resp.iter_content(chunk_size=1024 * 64):
            tmp.write(chunk)
            downloaded += len(chunk)
            if progress_cb and total_size:
                progress_cb(downloaded, total_size)

    try:
        # Extract — the tarball has a top-level directory we need to strip
        log.info("Extracting DOSBox Staging to %s", dest)
        with tarfile.open(tmp_path, "r:xz") as tf:
            # Find the common prefix (top-level directory in the tarball)
            members = tf.getmembers()
            prefix = ""
            if members:
                first = members[0].name
                if "/" in first:
                    prefix = first.split("/")[0]
                elif members[0].isdir():
                    prefix = first

            for member in members:
                # Strip the top-level directory prefix
                if prefix and member.name.startswith(prefix + "/"):
                    member.name = member.name[len(prefix) + 1 :]
                elif member.name == prefix:
                    continue  # skip the top-level dir itself

                if not member.name:
                    continue

                # Path traversal protection
                target = dest / member.name
                if not str(target.resolve()).startswith(
                    str(dest.resolve()) + os.sep
                ) and target.resolve() != dest.resolve():
                    log.warning("Skipping path-traversal member: %s", member.name)
                    continue

                tf.extract(member, dest, filter="data")

        # Write version stamp
        (dest / ".version").write_text(tag + "\n", encoding="utf-8")
        log.info("DOSBox Staging %s installed to %s", tag, dest)
    finally:
        tmp_path.unlink(missing_ok=True)


def _init_portable_config(staging: Path) -> None:
    """Create a portable-mode config by touching an empty file next to the binary.

    DOSBox Staging auto-populates it with current defaults on first run.
    We run ``dosbox --printconf`` briefly to trigger the generation.
    """
    conf = staging / "dosbox-staging.conf"
    if not conf.exists():
        conf.touch()

    binary = staging / "dosbox"
    if not binary.is_file():
        return

    # Run DOSBox briefly to generate the default config.
    # --printconf just prints the config path and exits.
    import subprocess

    try:
        subprocess.run(
            [str(binary), "--printconf"],
            cwd=str(staging),
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to generate portable DOSBox config: %s", exc)

    if conf.stat().st_size == 0:
        log.warning("DOSBox Staging did not populate portable config at %s", conf)


def _ensure_nounivbe(staging: Path) -> None:
    """Download NoUniVBE from GitHub releases if not already cached.

    NoUniVBE is distributed as a zip archive containing NOUNIVBE.EXE.
    """
    import zipfile

    nounivbe_dir = staging / "nounivbe"
    exe = nounivbe_dir / "NOUNIVBE.EXE"
    if exe.is_file():
        return

    from cellar.utils.http import make_session

    try:
        session = make_session()
        resp = session.get(
            _NOUNIVBE_RELEASES_URL,
            params={"per_page": 5},
            timeout=15,
        )
        resp.raise_for_status()

        # Find the zip asset in the latest release
        for rel in resp.json():
            for asset in rel.get("assets", []):
                aname = asset.get("name", "")
                if aname.lower().endswith(".zip"):
                    url = asset.get("browser_download_url", "")
                    if not url:
                        continue
                    dl_resp = session.get(url, timeout=15)
                    dl_resp.raise_for_status()

                    # Extract NOUNIVBE.EXE from the zip
                    import io

                    with zipfile.ZipFile(io.BytesIO(dl_resp.content)) as zf:
                        for name in zf.namelist():
                            if name.upper().endswith("NOUNIVBE.EXE"):
                                nounivbe_dir.mkdir(parents=True, exist_ok=True)
                                exe.write_bytes(zf.read(name))
                                log.info("NoUniVBE downloaded to %s", exe)
                                return

        log.warning("NOUNIVBE.EXE not found in any release zip")
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to download NoUniVBE: %s", exc)


# ---------------------------------------------------------------------------
# Private helpers — GOG detection
# ---------------------------------------------------------------------------


def _parse_conf_args(args_str: str) -> list[str]:
    """Extract ``-conf`` file paths from a DOSBox command-line string.

    Handles both ``-conf "path"`` and ``-conf path`` forms.
    Strips ``-noconsole``, ``-c "exit"``, and other non-conf arguments.
    """
    conf_files: list[str] = []
    try:
        tokens = shlex.split(args_str, posix=False)
    except ValueError:
        tokens = args_str.split()

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.lower() == "-conf" and i + 1 < len(tokens):
            i += 1
            path = tokens[i].strip('"').strip("'")
            # Normalise Windows backslashes
            path = path.replace("\\", "/")
            conf_files.append(path)
        i += 1

    return conf_files


def _resolve_conf_paths(source: Path, info: GogDosboxInfo) -> list[Path]:
    """Resolve conf file paths from GOG info relative to the source folder."""
    results: list[Path] = []
    for conf_arg in info.conf_args:
        # Conf args are relative to working_dir (usually DOSBOX/).
        # They often start with ".." to reference the game root.
        working = source / info.working_dir if info.working_dir else source
        resolved = (working / conf_arg).resolve()

        # Fall back to source root if not found relative to working_dir
        if not resolved.is_file():
            resolved = (source / conf_arg).resolve()

        if resolved.is_file():
            results.append(resolved)
        else:
            log.warning("GOG conf file not found: %s", conf_arg)

    return results


# ---------------------------------------------------------------------------
# Private helpers — config parsing
# ---------------------------------------------------------------------------


def _parse_autoexec_from_text(text: str) -> AutoexecInfo:
    """Extract the ``[autoexec]`` section from raw config text."""
    lines: list[str] = []
    mounts: list[MountCmd] = []
    game_commands: list[str] = []
    in_autoexec = False

    for raw_line in text.splitlines():
        stripped = raw_line.strip()

        if stripped.lower() == "[autoexec]":
            in_autoexec = True
            continue

        if in_autoexec:
            # A new section header ends autoexec
            if stripped.startswith("[") and stripped.endswith("]"):
                break

            lines.append(raw_line)

            # Skip comments and blanks for semantic parsing
            if not stripped or stripped.startswith("#") or stripped.startswith("REM "):
                continue

            lower = stripped.lower()

            # Skip control flow that doesn't matter for our purposes
            if lower.startswith("@echo") or lower.startswith("echo"):
                continue
            if lower in ("cls", "@echo off"):
                continue
            if lower.startswith("goto ") or lower.startswith(":"):
                continue
            if lower.startswith("choice ") or lower.startswith("if "):
                continue
            if lower == "exit":
                continue

            # Parse mount commands
            mount_match = re.match(
                r'mount\s+([a-zA-Z])\s+"?([^"]*)"?\s*(.*)',
                stripped,
                re.IGNORECASE,
            )
            if mount_match:
                mounts.append(
                    MountCmd(
                        drive=mount_match.group(1).upper(),
                        path=mount_match.group(2).strip(),
                        flags=mount_match.group(3).strip(),
                    )
                )
                continue

            # Drive change (e.g. "c:" or "C:")
            if re.match(r"^[a-zA-Z]:$", stripped):
                continue

            # Config commands (DOSBox internal)
            if lower.startswith("config "):
                continue

            # imgmount commands
            if lower.startswith("imgmount "):
                continue

            # Everything else is a game command
            game_commands.append(stripped)

    return AutoexecInfo(
        mounts=tuple(mounts),
        game_commands=tuple(game_commands),
        raw_lines=tuple(lines),
    )


def _strip_autoexec(text: str) -> str:
    """Remove the ``[autoexec]`` section from config text for configparser."""
    result: list[str] = []
    in_autoexec = False

    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped == "[autoexec]":
            in_autoexec = True
            continue
        if in_autoexec:
            if stripped.startswith("[") and stripped.endswith("]"):
                in_autoexec = False
                result.append(line)
            continue
        result.append(line)

    return "\n".join(result)


def _rewrite_autoexec(
    raw_lines: tuple[str, ...],
    dosbox_subdir: str,
) -> list[str]:
    """Rewrite autoexec lines for the converted layout.

    - ``mount C ".."`` → ``mount C "."``  (parent of DOSBOX/ → game root)
    - ``mount C "..\\subdir"`` → ``mount C "subdir"``
    - ``exit`` commands are dropped
    - GOG launcher menu scaffolding (goto, choice, labels) is stripped
    - Actual game commands are preserved
    """
    rewritten: list[str] = []
    # Track whether we're inside a GOG batch launcher structure
    seen_game_section = False

    for line in raw_lines:
        stripped = line.strip()
        lower = stripped.lower()

        # Drop blank lines at the start
        if not rewritten and not stripped:
            continue

        # Drop exit commands
        if lower == "exit":
            continue

        # Drop @echo off (we don't need batch scaffolding)
        if lower == "@echo off":
            continue

        # Drop cls
        if lower == "cls":
            continue

        # Drop GOG launcher menu structure
        if lower.startswith("goto "):
            continue
        if stripped.startswith(":"):
            # Keep :game section content by tracking it
            if lower == ":game":
                seen_game_section = True
            continue
        if lower.startswith("choice "):
            continue
        if lower.startswith("if errorlevel"):
            continue
        if lower.startswith("echo ") or lower.startswith("@echo "):
            # Drop echo lines that are part of the menu
            continue

        # Rewrite mount paths
        mount_match = re.match(
            r'(mount\s+[a-zA-Z]\s+)"?([^"]*)"?(.*)',
            stripped,
            re.IGNORECASE,
        )
        if mount_match:
            prefix = mount_match.group(1)
            path = mount_match.group(2).strip()
            suffix = mount_match.group(3).strip()

            # Normalise separators
            path = path.replace("\\", "/")

            # Rewrite parent references
            if path == "..":
                path = "."
            elif path.startswith("../"):
                path = path[3:]  # strip "../"

            rewritten.append(f'{prefix}"{path}" {suffix}'.rstrip())
            continue

        # Pass through everything else (game commands, drive changes, etc.)
        rewritten.append(stripped)

    return rewritten

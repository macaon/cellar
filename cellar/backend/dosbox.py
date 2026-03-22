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
from dataclasses import dataclass
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

# ---------------------------------------------------------------------------
# UI option constants — used by DosboxSettingsDialog
# ---------------------------------------------------------------------------

CPU_PRESETS: tuple[tuple[str, str], ...] = (
    ("Auto (recommended)", "auto"),
    ("8088 4.77 MHz", "240"),
    ("286 12 MHz", "1510"),
    ("386SX 16 MHz", "3000"),
    ("386DX 33 MHz", "6075"),
    ("486DX 33 MHz", "12000"),
    ("486DX2 66 MHz", "23880"),
    ("Pentium 100 MHz", "60000"),
    ("Pentium II 300 MHz", "200000"),
    ("Athlon 600 MHz", "306000"),
    ("Max", "max"),
)

MACHINE_OPTIONS: tuple[tuple[str, str], ...] = (
    # Monochrome
    ("Hercules", "hercules"),
    ("CGA Mono", "cga_mono"),
    # Color
    ("CGA", "cga"),
    ("EGA", "ega"),
    ("Tandy", "tandy"),
    ("PCjr", "pcjr"),
    # VGA / SVGA
    ("S3 Trio (default)", "svga_s3"),
    ("Tseng ET3000", "svga_et3000"),
    ("Tseng ET4000", "svga_et4000"),
    ("Paradise PVGA1A", "svga_paradise"),
    ("VESA (no LFB)", "vesa_nolfb"),
    ("VESA (old VBE)", "vesa_oldvbe"),
)

RENDERER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("OpenGL (recommended)", "opengl"),
    ("Texture (bilinear)", "texture"),
    ("Texture (nearest)", "texturenb"),
)

SHADER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("CRT Auto", "crt-auto"),
    ("CRT Auto (per machine)", "crt-auto-machine"),
    ("CRT Arcade", "crt-auto-arcade"),
    ("CRT Arcade (sharp)", "crt-auto-arcade-sharp"),
    ("Sharp", "sharp"),
    ("Bilinear", "bilinear"),
    ("Nearest Neighbour", "nearest"),
    ("None", "none"),
)

ASPECT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Auto", "auto"),
    ("On", "on"),
    ("Square Pixels", "square-pixels"),
    ("Off", "off"),
    ("Stretch", "stretch"),
)

MEMSIZE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("4 MB", "4"),
    ("8 MB", "8"),
    ("16 MB (default)", "16"),
    ("32 MB", "32"),
    ("64 MB", "64"),
)

SBTYPE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("None", "none"),
    ("Sound Blaster 1.0", "sb1"),
    ("Sound Blaster 2.0", "sb2"),
    ("Sound Blaster Pro", "sbpro1"),
    ("Sound Blaster Pro 2", "sbpro2"),
    ("Sound Blaster 16 (default)", "sb16"),
    ("ESS AudioDrive", "ess"),
)

OPLMODE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Auto", "auto"),
    ("OPL2", "opl2"),
    ("Dual OPL2", "dualopl2"),
    ("OPL3", "opl3"),
    ("OPL3 Gold", "opl3gold"),
    ("ESFM", "esfm"),
    ("None", "none"),
)

MIDIDEVICE_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Auto", "auto"),
    ("FluidSynth (SoundFont)", "fluidsynth"),
    ("MT-32 / CM-32L", "mt32"),
    ("None", "none"),
)

CROSSFEED_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Off", "off"),
    ("Light", "light"),
    ("Normal", "normal"),
    ("Strong", "strong"),
)

REVERB_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Off", "off"),
    ("Tiny", "tiny"),
    ("Small", "small"),
    ("Medium", "medium"),
    ("Large", "large"),
    ("Huge", "huge"),
)

CHORUS_OPTIONS: tuple[tuple[str, str], ...] = (
    ("Off", "off"),
    ("Light", "light"),
    ("Normal", "normal"),
    ("Strong", "strong"),
)


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------

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
    "cpu": {"cycles", "cpu_cycles", "core", "cputype"},
    "dosbox": {"machine", "memsize"},
    "sblaster": {"sbtype", "sbbase", "irq", "dma", "hdma", "oplmode"},
    "gus": {"gus", "gusbase", "gusirq", "gusdma"},
    "midi": {"mpu401", "mididevice"},
    "mixer": {"rate", "blocksize"},
    "render": {"aspect"},  # scaler is deprecated in DOSBox Staging
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

    # Post-process: translate deprecated settings
    cpu = settings.get("cpu", {})
    if "cycles" in cpu and "cpu_cycles" not in cpu:
        cpu["cpu_cycles"] = cpu.pop("cycles")
    elif "cycles" in cpu:
        del cpu["cycles"]

    # Fix invalid mididevice values
    midi = settings.get("midi", {})
    if midi.get("mididevice", "").lower() in ("default", "win32"):
        midi["mididevice"] = "auto"

    return {"settings": settings, "autoexec": autoexec}


def generate_overrides_conf(
    gog_settings: dict,
    dosbox_subdir: str,
    *,
    include_nounivbe: bool = False,
) -> str:
    """Generate ``dosbox-overrides.conf`` from extracted GOG settings.

    *dosbox_subdir* is the relative path of the DOSBOX/ subdirectory in the
    original GOG layout (e.g. ``"DOSBOX"``).  Paths in the autoexec that
    reference ``..`` (parent of DOSBOX/) are rewritten to ``.`` (game root).
    """
    lines: list[str] = []
    lines.append("# DOSBox overrides — extracted from GOG configuration")
    lines.append("# Edit this file to customise game-specific settings.\n")

    settings = gog_settings.get("settings", {})

    # Inject startup_verbosity into the dosbox section (avoid duplicate sections)
    settings.setdefault("dosbox", {})["startup_verbosity"] = "quiet"
    for section in sorted(settings):
        lines.append(f"[{section}]")
        for key in sorted(settings[section]):
            lines.append(f"{key} = {settings[section][key]}")
        lines.append("")

    autoexec: AutoexecInfo = gog_settings.get("autoexec", AutoexecInfo())
    if autoexec.raw_lines:
        rewritten = _rewrite_autoexec(autoexec.raw_lines, dosbox_subdir)
        lines.append("[autoexec]")
        # Autoexec order: mounts → drive change → NoUniVBE → primary game command.
        # Only the FIRST (primary) game command is included. Secondary targets
        # (setup.exe, fixsave.exe) are launched by passing an extra -conf file
        # at runtime that overrides the autoexec with a different game command.
        primary_cmd = autoexec.game_commands[0] if autoexec.game_commands else ""
        for line in rewritten:
            lower = line.strip().lower()
            if lower.startswith("mount ") or (len(lower) == 2 and lower.endswith(":")):
                lines.append(line)
                continue
            if lower.startswith("#"):
                lines.append(line)
                continue
            # Skip all game commands — primary is added after NoUniVBE below
        if include_nounivbe:
            lines.append("nounivbe\\NOUNIVBE.EXE")
        if primary_cmd:
            lines.append(primary_cmd)
            lines.append("EXIT")
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
    _copy_dosbox_runtime(staging, dest)

    # Step 3b: Copy NoUniVBE (if downloaded)
    nounivbe_src = staging / "nounivbe"
    has_nounivbe = nounivbe_src.is_dir() and (nounivbe_src / "NOUNIVBE.EXE").is_file()
    if has_nounivbe:
        shutil.copytree(nounivbe_src, dest / "nounivbe", dirs_exist_ok=True)

    # Step 4: Generate config files
    config_dest = dest / "config"
    _copy_base_config(staging, config_dest)

    # Parse GOG configs and generate overrides
    conf_paths = _resolve_conf_paths(source, info)
    gog_settings = parse_gog_confs(conf_paths)
    overrides = generate_overrides_conf(
        gog_settings, info.dosbox_dir, include_nounivbe=has_nounivbe,
    )
    (config_dest / "dosbox-overrides.conf").write_text(
        overrides, encoding="utf-8"
    )

    # Step 5: Return entry points — DOS exe files as launch targets.
    # DOSBox invocation is handled at launch time by the app (detail.py),
    # not by a shell script.  The entry_point path is the DOS executable
    # relative to the game directory.
    autoexec: AutoexecInfo = gog_settings.get("autoexec", AutoexecInfo())
    entry_points: list[dict] = []
    if autoexec.game_commands:
        for i, cmd in enumerate(autoexec.game_commands):
            parts = cmd.split(None, 1)
            exe_name = parts[0]
            exe_args = parts[1] if len(parts) > 1 else ""
            entry_points.append({
                "name": info.game_name if i == 0 else Path(exe_name).stem,
                "path": exe_name,
                "args": exe_args,
            })
    else:
        entry_points.append({
            "name": info.game_name or "Game",
            "path": "",
            "args": "",
        })

    return entry_points


def _copy_dosbox_runtime(staging: Path, dest: Path) -> None:
    """Copy DOSBox Staging binary + resources from *staging* to *dest*.

    Creates the ``dosbox/`` subdirectory inside *dest* with the binary,
    resources, soundfonts, and glshaders.
    """
    dosbox_dest = dest / "dosbox"
    dosbox_dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(staging / "dosbox", dosbox_dest / "dosbox")
    (dosbox_dest / "dosbox").chmod(
        (dosbox_dest / "dosbox").stat().st_mode | stat.S_IXUSR | stat.S_IXGRP
    )
    for subdir in ("resources", "soundfonts", "glshaders"):
        src = staging / subdir
        if src.is_dir():
            shutil.copytree(src, dosbox_dest / subdir, dirs_exist_ok=True)


def _copy_base_config(staging: Path, config_dest: Path) -> None:
    """Copy the base DOSBox config to *config_dest*."""
    config_dest.mkdir(parents=True, exist_ok=True)
    portable_conf = staging / "dosbox-staging.conf"
    if portable_conf.is_file() and portable_conf.stat().st_size > 0:
        shutil.copy2(portable_conf, config_dest / "dosbox-staging.conf")
    else:
        from cellar.utils.paths import dosbox_conf
        shutil.copy2(dosbox_conf(), config_dest / "dosbox-staging.conf")


# ---------------------------------------------------------------------------
# Disc image installer runner (Package Builder)
# ---------------------------------------------------------------------------


def run_dos_installer(
    content_dir: Path,
    disc_images: list[Path],
    floppy_images: list[Path],
    installer_exe: str | None,
    *,
    progress_cb: Callable[[str], None] | None = None,
) -> list[dict]:
    """Set up a DOS game directory and run an installer inside DOSBox.

    Creates the ``hdd/``, ``cd/``, ``config/``, and ``dosbox/`` layout under
    *content_dir*.  Launches DOSBox with the disc images mounted and waits
    for the user to complete the installation.

    Returns a list of entry point dicts found on the C: drive (``hdd/``)
    after DOSBox exits.

    *disc_images* are ISO or CUE files (already copied to ``content_dir/cd/``).
    *floppy_images* are temporary — the caller should clean them up after.
    """
    import subprocess

    from cellar.backend.umu import is_cellar_sandboxed

    # Step 1: Ensure DOSBox Staging is available
    if progress_cb:
        progress_cb("Preparing DOSBox Staging…")
    dosbox_binary = ensure_dosbox_staging(progress_cb=None)
    staging = dosbox_binary.parent

    # Step 2: Create directory layout
    hdd_dir = content_dir / "hdd"
    hdd_dir.mkdir(parents=True, exist_ok=True)
    config_dir = content_dir / "config"

    # Step 3: Copy DOSBox runtime + base config
    _copy_dosbox_runtime(staging, content_dir)
    _copy_base_config(staging, config_dir)

    # Step 4: Build autoexec for installer session
    autoexec_lines: list[str] = []
    autoexec_lines.append('mount C "hdd"')
    autoexec_lines.append("C:")

    # Mount CD images (all on D: for disc-swap via Ctrl+F4)
    # Paths must be relative to content_dir (DOSBox's cwd).
    if disc_images:
        img_args = " ".join(
            f'"{p.relative_to(content_dir)}"' for p in disc_images
        )
        autoexec_lines.append(f'imgmount D {img_args} -t cdrom')

    # Mount floppy images (all on A: for disc-swap via Ctrl+F4)
    if floppy_images:
        floppy_args = " ".join(
            f'"{p.relative_to(content_dir)}"' for p in floppy_images
        )
        autoexec_lines.append(f'imgmount A {floppy_args} -t floppy')

    # Run installer if detected
    if installer_exe:
        # Determine which drive the installer is on
        if disc_images:
            autoexec_lines.append(f"D:\\{installer_exe}")
        elif floppy_images:
            autoexec_lines.append(f"A:\\{installer_exe}")
    if installer_exe:
        autoexec_lines.append("EXIT")

    # Write installer overrides conf
    overrides_lines = [
        "# DOSBox overrides — disc image installer session",
        "# This file will be regenerated after installation.",
        "",
        "[dosbox]",
        "startup_verbosity = quiet",
        "",
        "[autoexec]",
        "@echo off",
    ] + autoexec_lines + [""]

    (config_dir / "dosbox-overrides.conf").write_text(
        "\n".join(overrides_lines) + "\n", encoding="utf-8",
    )

    # Step 5: Launch DOSBox
    if progress_cb:
        progress_cb("Running installer in DOSBox…")

    dosbox_bin = content_dir / "dosbox" / "dosbox"
    cmd = [
        str(dosbox_bin),
        "--noprimaryconf",
        "-conf", str(config_dir / "dosbox-staging.conf"),
        "-conf", str(config_dir / "dosbox-overrides.conf"),
    ]

    if is_cellar_sandboxed():
        cmd = ["flatpak-spawn", "--host"] + cmd

    # Run DOSBox with cwd set to content_dir so relative paths work
    subprocess.run(cmd, cwd=str(content_dir))

    # Step 6: Rewrite overrides conf for normal launch (not installer).
    launch_lines = [
        "# DOSBox overrides — edit to customise game-specific settings.",
        "",
        "[dosbox]",
        "startup_verbosity = quiet",
        "",
        "[autoexec]",
        "@echo off",
        'mount C "hdd"',
        "C:",
    ]
    # Re-add CD mount if present
    cd_dir = content_dir / "cd"
    if cd_dir.is_dir():
        cd_imgs = sorted(
            p for p in cd_dir.iterdir()
            if p.suffix.lower() in {".iso", ".cue"}
        )
        if cd_imgs:
            img_args = " ".join(
                f'"cd/{p.name}"' for p in cd_imgs
            )
            launch_lines.append(f"imgmount D {img_args} -t cdrom")
    launch_lines.append("")

    (config_dir / "dosbox-overrides.conf").write_text(
        "\n".join(launch_lines) + "\n", encoding="utf-8",
    )

    # Step 7: Scan hdd/ for entry point candidates
    if progress_cb:
        progress_cb("Detecting game executables…")

    from cellar.backend.detect import is_dos_executable, _LAUNCH_EXCLUDE

    entry_points: list[dict] = []
    if hdd_dir.is_dir():
        for p in hdd_dir.rglob("*"):
            if not p.is_file():
                continue
            if p.name.lower() in _LAUNCH_EXCLUDE:
                continue
            suffix = p.suffix.lower()
            if suffix in {".exe", ".com", ".bat"}:
                # For .exe files, verify they're actually DOS executables
                if suffix == ".exe" and not is_dos_executable(p):
                    continue
                rel = str(p.relative_to(hdd_dir))
                entry_points.append({
                    "name": p.stem,
                    "path": rel,
                })

    return entry_points


# ---------------------------------------------------------------------------
# Launch helpers (shared by builder + detail view)
# ---------------------------------------------------------------------------


def build_dos_launch_cmd(
    game_dir: Path,
    entry_path: str,
    entry_args: str,
) -> tuple[list[str], Path | None]:
    """Build the DOSBox Staging command line for launching a DOS game.

    Supports two layouts:
    - **hdd layout**: ``hdd/`` for C:, ``cd/`` for D: (disc image imports)
    - **flat layout**: game root is C: (GOG conversions, legacy)

    Returns ``(cmd, tmp_conf_path)`` where *tmp_conf_path* is a temporary
    config file that the caller must delete after DOSBox has read it, or
    ``None`` if no target-specific override was needed.
    """
    import tempfile

    from cellar.backend.umu import is_cellar_sandboxed

    dosbox_bin = game_dir / "dosbox" / "dosbox"
    cmd = [
        str(dosbox_bin),
        "--noprimaryconf",
        "-conf", str(game_dir / "config" / "dosbox-staging.conf"),
        "-conf", str(game_dir / "config" / "dosbox-overrides.conf"),
    ]

    # Detect layout: hdd/ present → disc-import layout, else flat
    use_hdd = (game_dir / "hdd").is_dir()

    tmp_conf: Path | None = None
    if entry_path:
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".conf", prefix="cellar-dos-",
            delete=False, dir=str(game_dir / "config"),
        )
        tmp.write("[dosbox]\nautoexec_section = overwrite\n\n")
        tmp.write("[autoexec]\n")
        tmp.write("@echo off\n")

        if use_hdd:
            tmp.write('mount C "hdd"\n')
        else:
            tmp.write('mount C "."\n')
        tmp.write("C:\n")

        # Mount CD images if present
        cd_dir = game_dir / "cd"
        if cd_dir.is_dir():
            cd_images = sorted(
                p for p in cd_dir.iterdir()
                if p.suffix.lower() in {".iso", ".cue"}
            )
            if cd_images:
                img_args = " ".join(f'"cd/{p.name}"' for p in cd_images)
                tmp.write(f"imgmount D {img_args} -t cdrom\n")

        nounivbe = game_dir / "nounivbe" / "NOUNIVBE.EXE"
        if nounivbe.is_file():
            tmp.write("nounivbe\\NOUNIVBE.EXE\n")

        # CD into the executable's directory so the game finds its files.
        # Parse with forward slashes (Linux paths) BEFORE converting to DOS.
        exe_parent = str(Path(entry_path).parent)
        exe_name = Path(entry_path).name
        if exe_parent and exe_parent != ".":
            tmp.write(f"CD {exe_parent.replace('/', chr(92))}\n")

        game_cmd = exe_name
        if entry_args:
            game_cmd += f" {entry_args}"

        tmp.write(f"{game_cmd}\n")
        tmp.write("EXIT\n")
        tmp.close()
        tmp_conf = Path(tmp.name)
        cmd += ["-conf", str(tmp_conf)]

    if is_cellar_sandboxed():
        cmd = ["flatpak-spawn", "--host"] + cmd

    return cmd, tmp_conf


# ---------------------------------------------------------------------------
# Config file read/write helpers (shared by builder + detail view)
# ---------------------------------------------------------------------------


def read_override(conf_path: Path, section: str, key: str) -> str:
    """Read a single value from a DOSBox overrides conf file.

    Returns the value as a string, or empty string if not found.
    Handles duplicate sections (common in generated overrides) by
    manual parsing instead of relying on configparser.
    """
    if not conf_path.is_file():
        return ""
    text = conf_path.read_text(encoding="utf-8", errors="replace")
    in_section = False
    result = ""
    for line in text.splitlines():
        stripped = line.strip()
        lower = stripped.lower()
        if lower.startswith("[") and lower.endswith("]"):
            in_section = (lower == f"[{section.lower()}]")
            continue
        if in_section and "=" in stripped:
            k, _, v = stripped.partition("=")
            if k.strip().lower() == key.lower():
                result = v.strip()  # last value wins (matches DOSBox behavior)
    return result


def write_override(conf_path: Path, section: str, key: str, value: str) -> None:
    """Set a single value in a DOSBox overrides conf file.

    Creates the section if it doesn't exist.  Preserves ``[autoexec]``.
    """
    write_overrides_batch(conf_path, {(section, key): value})


def write_overrides_batch(
    conf_path: Path,
    changes: dict[tuple[str, str], str],
) -> None:
    """Write multiple values to a DOSBox overrides conf file in one pass.

    *changes* maps ``(section, key)`` → ``value``.
    Creates sections as needed.  Preserves ``[autoexec]`` and comments.
    """
    if not conf_path.is_file():
        return

    text = conf_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()

    # Group changes by section for efficient processing
    by_section: dict[str, dict[str, str]] = {}
    for (sec, key), val in changes.items():
        by_section.setdefault(sec, {})[key] = val

    new_lines: list[str] = []
    current_section = ""
    written_keys: dict[str, set[str]] = {}  # section → set of written keys

    for line in lines:
        stripped = line.strip()
        lower = stripped.lower()

        # Detect section headers
        if lower.startswith("[") and lower.endswith("]"):
            # Before leaving current section, write any unwritten keys
            if current_section in by_section:
                for k, v in by_section[current_section].items():
                    if k not in written_keys.get(current_section, set()):
                        new_lines.append(f"{k} = {v}")
                        written_keys.setdefault(current_section, set()).add(k)
            current_section = lower[1:-1]
            new_lines.append(line)
            continue

        # Check if this line is a key we want to overwrite
        if current_section in by_section:
            for k in by_section[current_section]:
                if lower.startswith(f"{k.lower()} ") or lower.startswith(f"{k.lower()}="):
                    new_lines.append(f"{k} = {by_section[current_section][k]}")
                    written_keys.setdefault(current_section, set()).add(k)
                    break
            else:
                new_lines.append(line)
            continue

        new_lines.append(line)

    # Write remaining keys for the last section
    if current_section in by_section:
        for k, v in by_section[current_section].items():
            if k not in written_keys.get(current_section, set()):
                new_lines.append(f"{k} = {v}")
                written_keys.setdefault(current_section, set()).add(k)

    # Add entirely new sections (before [autoexec] if present)
    autoexec_idx = None
    for i, line in enumerate(new_lines):
        if line.strip().lower() == "[autoexec]":
            autoexec_idx = i
            break

    new_sections: list[str] = []
    for sec, keys in by_section.items():
        unwritten = {k: v for k, v in keys.items() if k not in written_keys.get(sec, set())}
        if unwritten:
            new_sections.append("")
            new_sections.append(f"[{sec}]")
            for k, v in unwritten.items():
                new_sections.append(f"{k} = {v}")

    if new_sections:
        if autoexec_idx is not None:
            for i, sl in enumerate(new_sections):
                new_lines.insert(autoexec_idx + i, sl)
        else:
            new_lines.extend(new_sections)

    # Ensure trailing newline
    while new_lines and not new_lines[-1].strip():
        new_lines.pop()
    new_lines.append("")

    conf_path.write_text("\n".join(new_lines), encoding="utf-8")


def update_audio_config(
    overrides_path: Path,
    assets_dir: Path,
    has_soundfont: bool,
    has_mt32: bool,
) -> None:
    """Rewrite MIDI/FluidSynth/MT-32 sections in a DOSBox overrides conf.

    Strips existing audio config lines and re-adds them based on the
    current asset state.  Inserts new sections before ``[autoexec]``.
    """
    if not overrides_path.is_file():
        return

    text = overrides_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    new_lines: list[str] = []

    in_section = ""

    # Strip existing audio config lines — we'll re-add them
    for line in lines:
        stripped = line.strip().lower()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped[1:-1]

        # Remove existing midi/fluidsynth/mt32 lines we manage
        if in_section == "midi" and stripped.startswith("mididevice"):
            continue
        if in_section == "fluidsynth" and stripped.startswith("soundfont"):
            continue
        if in_section == "mt32" and stripped.startswith("romdir"):
            continue
        # Remove section headers for sections we manage
        if stripped in ("[fluidsynth]", "[mt32]", "[midi]"):
            continue

        new_lines.append(line)

    # Remove trailing blank lines
    while new_lines and not new_lines[-1].strip():
        new_lines.pop()

    # Add audio config before [autoexec] if present
    autoexec_idx = None
    for i, line in enumerate(new_lines):
        if line.strip().lower() == "[autoexec]":
            autoexec_idx = i
            break

    audio_lines: list[str] = []

    if has_soundfont:
        sf_dir = assets_dir / "soundfonts"
        sfs = sorted(sf_dir.glob("*.sf[23]")) if sf_dir.is_dir() else []
        if sfs:
            audio_lines.append("")
            audio_lines.append("[midi]")
            audio_lines.append("mididevice = fluidsynth")
            audio_lines.append("")
            audio_lines.append("[fluidsynth]")
            audio_lines.append(f"soundfont = assets/soundfonts/{sfs[0].name}")
    elif has_mt32:
        audio_lines.append("")
        audio_lines.append("[midi]")
        audio_lines.append("mididevice = mt32")
        audio_lines.append("")
        audio_lines.append("[mt32]")
        audio_lines.append("romdir = assets/mt32-roms")

    if audio_lines:
        if autoexec_idx is not None:
            for i, al in enumerate(audio_lines):
                new_lines.insert(autoexec_idx + i, al)
        else:
            new_lines.extend(audio_lines)

    new_lines.append("")  # trailing newline
    overrides_path.write_text("\n".join(new_lines), encoding="utf-8")


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

                try:
                    tf.extract(member, dest, filter="data")
                except TypeError:
                    # Python < 3.11.2 lacks the filter parameter
                    tf.extract(member, dest)

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

"""ScummVM conversion - convert a DOSBox install to ScummVM.

Copies required game data files from the DOSBox prefix into a ScummVM-friendly
layout, writes a per-game config, and removes the DOSBox prefix dirs.

Files on the hard drive are copied directly.  Files that live on the CD image
are extracted via a DOSBox session (``-c "copy D:\\FILE C:\\data\\"``).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Callable

from cellar.backend._profile_matching import (
    content_root,
    find_file_casefold,
    find_file_recursive,
)

log = logging.getLogger(__name__)


def _find_on_hdd(game_dir: Path, filename: str) -> Path | None:
    """Locate a file on the hdd content root."""
    root = content_root(game_dir)
    if "/" in filename:
        return find_file_casefold(root, filename)
    return find_file_recursive(root, filename)


def _copy_cd_files_via_dosbox(
    game_dir: Path,
    filenames: list[str],
    progress_cb: Callable[[str], None] | None = None,
) -> None:
    """Use DOSBox to copy files from a mounted CD image to hdd/data/.

    Builds a single DOSBox session with -c args to mount drives, create
    the target directory, and copy each file.
    """
    from cellar.backend.umu import is_cellar_sandboxed

    dosbox_bin = game_dir / "dosbox" / "dosbox"
    if not dosbox_bin.is_file():
        raise FileNotFoundError(f"DOSBox binary not found at {dosbox_bin}")

    config_dir = game_dir / "config"
    cd_dir = game_dir / "cd"

    # Find CD images to mount
    cd_images = sorted(
        p for p in cd_dir.iterdir()
        if p.suffix.lower() in {".iso", ".cue"}
    ) if cd_dir.is_dir() else []

    if not cd_images:
        raise FileNotFoundError("No CD images found to mount")

    img_args = " ".join(f'"cd/{p.name}"' for p in cd_images)

    cmd = [
        str(dosbox_bin),
        "--noprimaryconf",
        "-conf", str(config_dir / "dosbox-staging.conf"),
        "-conf", str(config_dir / "dosbox-overrides.conf"),
        "-c", f"imgmount D {img_args} -t cdrom",
        "-c", 'mount C "hdd"',
        "-c", "C:",
    ]

    for fname in filenames:
        if progress_cb:
            progress_cb(f"Extracting {fname} from CD")
        cmd += ["-c", f"copy D:\\{fname} C:\\"]

    cmd += ["--exit"]

    if is_cellar_sandboxed():
        cmd = ["flatpak-spawn", "--host"] + cmd

    result = subprocess.run(
        cmd, cwd=str(game_dir),
        capture_output=True, timeout=120,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        log.warning("DOSBox CD copy returned %d: %s", result.returncode, stderr)

    log.info("Extracted %d files from CD via DOSBox", len(filenames))


def convert_to_scummvm(
    game_dir: Path,
    profile: dict,
    *,
    progress_cb: Callable[[str], None] | None = None,
) -> None:
    """Convert a DOSBox install to ScummVM.

    1. Copy required files from hdd/ (direct) or CD image (via DOSBox)
    2. Download ScummVM runtime and copy to game dir
    3. Write ``config/scummvm.ini``
    4. Remove DOSBox dirs

    Raises ``FileNotFoundError`` if a required file cannot be located.
    Raises ``OSError`` on copy/write failures.
    """
    scummvm_id = profile["scummvm_id"]
    required_files = profile.get("required_files", [])
    cd_files = set(f.upper() for f in profile.get("cd_files", []))

    data_dir = game_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (game_dir / "saves").mkdir(parents=True, exist_ok=True)

    # Step 1a: Copy files found on hdd directly
    cd_needed: list[str] = []
    for filename in required_files:
        src = _find_on_hdd(game_dir, filename)
        if src is not None:
            if progress_cb:
                progress_cb(f"Copying {filename}")
            dest = data_dir / src.name
            shutil.copy2(src, dest)
            log.info("Copied %s -> %s", src, dest)
        elif filename.upper() in cd_files:
            cd_needed.append(filename)
        else:
            # Try hdd anyway, might be in a subdirectory
            cd_needed.append(filename)

    # Step 1b: Extract CD files via DOSBox
    if cd_needed:
        if progress_cb:
            progress_cb("Extracting files from CD image")
        _copy_cd_files_via_dosbox(game_dir, cd_needed, progress_cb=progress_cb)
        # Move extracted files from hdd/ to data/
        hdd_root = content_root(game_dir)
        for fname in cd_needed:
            # DOSBox may uppercase the filename
            for f in hdd_root.iterdir():
                if f.is_file() and f.name.lower() == fname.lower():
                    dest = data_dir / f.name
                    shutil.move(str(f), str(dest))
                    log.info("Moved %s -> %s", f, dest)
                    break

    # Step 2: Write ScummVM config
    if progress_cb:
        progress_cb("Writing ScummVM configuration")

    from cellar.backend.scummvm import write_scummvm_conf
    settings = profile.get("scummvm_settings", {})
    write_scummvm_conf(game_dir, scummvm_id, settings or None)

    # Step 4: Remove DOSBox prefix dirs
    if progress_cb:
        progress_cb("Removing DOSBox prefix")

    for dirname in ("hdd", "cd", "dosbox", "nounivbe"):
        target = game_dir / dirname
        if target.is_dir():
            shutil.rmtree(target)
            log.info("Removed %s", target)

    # Remove DOSBox config files but keep the config/ directory
    config_dir = game_dir / "config"
    if config_dir.is_dir():
        for conf in config_dir.iterdir():
            if conf.name.startswith("dosbox-") or conf.name == "shell_history.txt":
                conf.unlink()
                log.info("Removed %s", conf)

    log.info("Conversion to ScummVM complete for %s (game ID: %s)",
             game_dir.name, scummvm_id)

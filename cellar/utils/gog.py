"""GOG Linux installer (.sh) detection and extraction.

GOG Linux installers are Makeself self-extracting archives with a ZIP
appended after the tar payload.  The ZIP contains the full game
directory under ``data/noarch/`` — game binaries in ``data/noarch/game/``,
the launch script ``data/noarch/start.sh``, metadata in
``data/noarch/gameinfo``, docs, and support scripts.

Python's :mod:`zipfile` can open these directly — it scans from the end
of the file for the ZIP central directory, skipping the shell header and
Makeself tar payload.
"""

from __future__ import annotations

import logging
import subprocess
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable

log = logging.getLogger(__name__)

# Markers found in the first few KB of every GOG Linux installer.
_GOG_MARKERS = (
    b"makeself",
    b"Makeself",
    b"MAKESELF",
    b"MojoSetup",
    b"mojosetup",
    b"GOG",
)

# Prefix inside the ZIP that contains the full game directory.
_DATA_PREFIX = "data/noarch/"

# Path to the gameinfo metadata file inside the ZIP.
_GAMEINFO_PATH = "data/noarch/gameinfo"


def is_gog_installer(path: Path) -> bool:
    """Return True if *path* looks like a GOG Linux ``.sh`` installer.

    Checks for Makeself/GOG markers in the file header, then verifies
    that a valid ZIP archive is appended.
    """
    try:
        with path.open("rb") as f:
            header = f.read(4096)
    except OSError:
        return False

    if not any(marker in header for marker in _GOG_MARKERS):
        return False

    return zipfile.is_zipfile(path)


def read_gog_gameinfo(path: Path) -> dict[str, str] | None:
    """Read the ``gameinfo`` file from a GOG installer ZIP.

    Returns ``{"name": str, "version": str}`` or ``None`` if not found.
    The version includes the build number in parentheses when available.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            try:
                raw = zf.read(_GAMEINFO_PATH)
            except KeyError:
                return None
            lines = raw.decode("utf-8", errors="replace").splitlines()
            name = lines[0].strip() if len(lines) > 0 else ""
            version = lines[1].strip() if len(lines) > 1 else ""
            build = lines[2].strip() if len(lines) > 2 else ""
            if not name:
                return None
            ver = f"{version} ({build})" if version and build else version
            return {"name": name, "version": ver}
    except (zipfile.BadZipFile, OSError):
        return None


def list_game_files(path: Path) -> list[str]:
    """Return relative paths of files inside the GOG installer ZIP.

    Paths are relative to the game root (``data/noarch/`` prefix
    stripped).  Directories are excluded.
    """
    try:
        with zipfile.ZipFile(path, "r") as zf:
            result: list[str] = []
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if info.filename.startswith(_DATA_PREFIX):
                    rel = info.filename[len(_DATA_PREFIX):]
                    if rel:
                        result.append(rel)
            return result
    except (zipfile.BadZipFile, OSError):
        return []


def extract_gog_game_data(
    src: Path,
    dest: Path,
    progress_cb: Callable[[int, int], None] | None = None,
) -> None:
    """Extract game files from a GOG installer to *dest*.

    Extracts everything under ``data/noarch/`` from the appended ZIP,
    stripping the ``data/noarch/`` prefix so the result matches a native
    GOG install layout (``game/``, ``start.sh``, ``gameinfo``, ``support/``,
    ``docs/`` all at root level).

    After extraction, runs ``support/postinst.sh`` if present (the GOG
    post-install script that handles tasks like reassembling split files).

    *progress_cb*, if provided, is called with ``(extracted_bytes, total_bytes)``
    after each file.
    """
    dest.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(src, "r") as zf:
        # Collect data/noarch entries and total size.
        game_entries = [
            info for info in zf.infolist()
            if not info.is_dir() and info.filename.startswith(_DATA_PREFIX)
        ]
        total = sum(e.file_size for e in game_entries)
        extracted = 0

        for info in game_entries:
            rel = info.filename[len(_DATA_PREFIX):]
            if not rel:
                continue

            out_path = dest / PurePosixPath(rel)
            out_path.parent.mkdir(parents=True, exist_ok=True)

            with zf.open(info) as src_f, out_path.open("wb") as dst_f:
                while True:
                    chunk = src_f.read(1024 * 256)
                    if not chunk:
                        break
                    dst_f.write(chunk)
                    # Yield the GIL so the UI thread can process events
                    # (pulse animation, progress bar updates).  Without this,
                    # pure-Python ZIP decompression starves the main loop.
                    time.sleep(0)

            # Preserve executable bits from the ZIP external attributes.
            unix_mode = info.external_attr >> 16
            if unix_mode & 0o111:
                out_path.chmod(out_path.stat().st_mode | 0o111)

            extracted += info.file_size
            if progress_cb is not None:
                progress_cb(extracted, total)

    _run_postinst(dest)


def _run_postinst(dest: Path) -> None:
    """Run the GOG post-install script if present.

    GOG Linux installers ship a ``support/postinst.sh`` that handles
    post-extraction tasks (e.g. reassembling split files).  We run it
    from the ``support/`` directory, matching the environment the GOG
    installer would provide.
    """
    postinst = dest / "support" / "postinst.sh"
    if not postinst.is_file():
        return

    postinst.chmod(postinst.stat().st_mode | 0o111)

    try:
        result = subprocess.run(
            ["bash", str(postinst)],
            cwd=str(dest / "support"),
            capture_output=True,
            timeout=300,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            log.warning("postinst.sh exited %d: %s", result.returncode, stderr)
        else:
            log.info("postinst.sh completed successfully")
    except subprocess.TimeoutExpired:
        log.warning("postinst.sh timed out after 300s")
    except OSError as exc:
        log.warning("Could not run postinst.sh: %s", exc)

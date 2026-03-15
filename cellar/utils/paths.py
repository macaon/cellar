"""Resolve paths to data files for both development and installed contexts."""

import os
from pathlib import Path

# When running from the source tree, data/ sits two levels above this file.
_SRC_DATA = Path(__file__).parent.parent.parent / "data"

# When installed via pip (hatchling wheel), data/ is bundled inside the package as cellar/data/.
_PKG_DATA = Path(__file__).parent.parent / "data"


def _installed_data_dirs() -> list[Path]:
    """Return candidate installed data directories in XDG preference order."""
    xdg_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    xdg_sys = os.environ.get("XDG_DATA_DIRS", "/usr/local/share:/usr/share")
    dirs = [xdg_home] + [Path(d) for d in xdg_sys.split(":") if d]
    dirs.append(Path("/app/share"))  # Flatpak
    return [d / "cellar" for d in dirs]


def icons_dir() -> str:
    """Return the directory to register with the icon theme search path.

    The directory contains a ``hicolor/`` subtree with bundled symbolic icons.
    Checked in the source tree first so ``PYTHONPATH=. python3 -m cellar.main``
    works without a build step.
    """
    for candidate in (
        [_SRC_DATA / "icons", _PKG_DATA / "icons"]
        + [d / "icons" for d in _installed_data_dirs()]
    ):
        if candidate.is_dir():
            return str(candidate)
    return str(_SRC_DATA / "icons")


def dir_size_bytes(path: Path | str) -> int:
    """Return the allocated disk usage of *path* in bytes (equivalent to ``du -sb``).

    Uses ``st_blocks`` (512-byte units) rather than ``st_size`` so the result
    reflects actual disk usage including filesystem block rounding.  Symlinks
    are skipped so that Wine prefix Z:-drive links to ``/`` don't inflate the
    result to the size of the whole filesystem.
    """
    total = 0
    try:
        with os.scandir(path) as it:
            for entry in it:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    total += dir_size_bytes(entry.path)
                else:
                    try:
                        total += entry.stat(follow_symlinks=False).st_blocks * 512
                    except OSError:
                        pass
    except OSError:
        pass
    return total


def short_path(path) -> str:
    """Return *path* with the home directory replaced by ``~``."""
    return str(path).replace(os.path.expanduser("~"), "~", 1)


def to_win32_path(abs_path: str, drive_c: str) -> str:
    """Convert a POSIX path under *drive_c* to a ``C:\\`` Windows-style path.

    Falls back to *abs_path* unchanged if it doesn't live under *drive_c*.
    """
    try:
        rel = os.path.relpath(abs_path, drive_c)
        return "C:\\" + rel.replace("/", "\\")
    except ValueError:
        return abs_path


def dosbox_conf() -> Path:
    """Return the path to the base DOSBox Staging configuration file.

    Checked in the source tree first, then installed data directories.
    """
    candidates = (
        [_SRC_DATA / "dosbox-staging.conf", _PKG_DATA / "dosbox-staging.conf"]
        + [d / "dosbox-staging.conf" for d in _installed_data_dirs()]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return _SRC_DATA / "dosbox-staging.conf"


def ui_file(name: str) -> str:
    """Return the absolute path string for a UI template file.

    Checks the source tree first so the app can be run directly with
    ``PYTHONPATH=. python3 -m cellar.main`` during development.
    """
    candidates = (
        [_SRC_DATA / "ui" / name, _PKG_DATA / "ui" / name]
        + [d / "ui" / name for d in _installed_data_dirs()]
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError(f"UI file '{name}' not found in any data directory")

"""Resolve paths to data files for both development and installed contexts."""

import logging
import os
import re as _re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# CoW / reflink filesystem detection
# ---------------------------------------------------------------------------

# Cache per st_dev so we check at most once per mount per session.
_cow_cache: dict[int, bool] = {}


def is_cow_filesystem(path: Path | str) -> bool:
    """Return ``True`` if *path* resides on a filesystem that supports reflinks.

    Detects btrfs (always CoW) and tests other filesystems (XFS, bcachefs)
    by attempting a real ``cp --reflink=always`` on a temporary file pair.
    Results are cached per device so the check runs at most once per mount.
    Returns ``False`` on any error (safe default — overestimates disk usage).
    """
    try:
        dev = os.stat(path).st_dev
    except OSError:
        return False

    if dev in _cow_cache:
        return _cow_cache[dev]

    result = _detect_cow(path, dev)
    _cow_cache[dev] = result
    return result


def _detect_cow(path: Path | str, dev: int) -> bool:
    """Probe whether the filesystem at *path* supports reflinks."""
    # Fast path: check filesystem type name via stat -f.
    try:
        out = subprocess.run(
            ["stat", "-f", "-c", "%T", str(path)],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0:
            fstype = out.stdout.strip().lower()
            if fstype == "btrfs":
                return True
            # ext2/3/4, tmpfs, etc. — definitely no reflink.
            if fstype in ("ext2/ext3", "tmpfs", "nfs", "fuse"):
                return False
    except (OSError, subprocess.TimeoutExpired):
        pass

    # Slow path for XFS, bcachefs, or unknown fs: try an actual reflink.
    try:
        target = Path(path) if not isinstance(path, Path) else path
        if not target.is_dir():
            target = target.parent
        with tempfile.TemporaryDirectory(dir=target) as td:
            src = Path(td) / "cow_test_src"
            dst = Path(td) / "cow_test_dst"
            src.write_bytes(b"\x00" * 4096)
            cp = subprocess.run(
                ["cp", "--reflink=always", str(src), str(dst)],
                capture_output=True, timeout=5,
            )
            return cp.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        pass

    return False


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


# Characters that are unsafe or awkward on Linux / Windows / macOS filesystems.
_UNSAFE_CHARS = _re.compile(r'[/:?*"<>|\\]+')


def sanitize_dirname(name: str, fallback: str) -> str:
    """Return a filesystem-safe directory name derived from *name*.

    Replaces runs of unsafe characters with a single space, strips leading /
    trailing dots and whitespace, and collapses consecutive spaces.  Returns
    *fallback* if the result is empty.
    """
    clean = _UNSAFE_CHARS.sub(" ", name)
    clean = clean.strip(". ")
    clean = _re.sub(r"\s+", " ", clean)
    return clean or fallback


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

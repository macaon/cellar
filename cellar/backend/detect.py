"""File/folder inspection helpers for Smart Import.

All functions are pure Python with no GTK dependency.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WIN_EXTS = {".exe", ".msi", ".bat", ".cmd", ".com", ".lnk"}
_LIN_SCRIPT_EXTS = {".sh", ".run"}
_LIN_BINARY_EXTS = {".x86_64", ".x86", ".x64"}
_LIN_EXTS = _LIN_SCRIPT_EXTS | _LIN_BINARY_EXTS
_ELF_MAGIC = b"\x7fELF"

_ARCHIVE_EXTS = {
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".zst", ".7z", ".rar",
    ".tgz", ".tbz2", ".txz", ".tzst", ".lzma", ".lz4", ".cab",
}

_STRIP_PREFIX_RE = re.compile(
    r"^(?:setup|install|gog_games|gog|galaxy)[_\-]",
    re.IGNORECASE,
)

# Trailing version / build / hash / GoG-id noise.
#
# Strategy: the chain must START with a "significant" token (semver, hex hash,
# parenthesised number, build tag, v-tag, or bare number preceding a
# parenthesised number).  After that first anchor token, bare 4+ digit
# numbers (e.g. GoG build IDs like 4055) are also allowed.
# This prevents stripping meaningful subtitle numbers like "2077" in
# "Cyberpunk 2077" when they appear alone without a preceding semver/hash.
_NOISE_SIG = (
    r"(?:"
    r"\d+\.\d[\d.a-zA-Z]*"  # semver: 1.9.1, 1.0.0b2
    r"|[0-9a-f]{7,}"        # hex hash ≥7 chars: a10783a599
    r"|\(\d+\)"             # (89220) parenthesised number
    r"|build\d+"            # build1234
    r"|v\d[\d.]*"           # v2, v1.0
    r"|\d+(?=[_\-]\(\d+\))" # bare number preceding (NNN) — GoG version+build
    r")"
)
_NOISE_ANY = (
    r"(?:"
    r"\d+\.\d[\d.a-zA-Z]*"
    r"|[0-9a-f]{4,}"        # hex hash ≥4 chars (safe — only after a SIG anchor)
    r"|\([^)]+\)"           # any parenthesised token: (89220), (64bit), (Installer)
    r"|build\d+"
    r"|v\d[\d.]*"
    r"|\d{4,}"              # bare 4+ digit IDs only allowed after a sig token
    r")"
)
# Must start with a significant token; any additional tokens follow.
_STRIP_SUFFIX_RE = re.compile(
    rf"(?:[_\-]{_NOISE_SIG})(?:[_\-]{_NOISE_ANY})*$",
    re.IGNORECASE,
)

_VERSION_RE = re.compile(r"\d+\.\d+(?:\.\d+)*")

DetectResult = Literal["windows", "linux", "ambiguous", "unsupported"]

# Human-readable messages for each "unsupported" sub-case, keyed by reason tag
UNSUPPORTED_MESSAGES: dict[str, str] = {
    "sh_run": (
        "Linux installer scripts are not supported. "
        "Drop or browse for the already-installed application folder instead."
    ),
    "archive": (
        "Archives are not supported. "
        "Drop a .exe installer or an already-installed app folder."
    ),
    "unknown_file": (
        "Unrecognised file type. "
        "Drop a .exe installer or an app folder."
    ),
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def detect_platform(path: Path) -> DetectResult:
    """Return the likely platform for *path*.

    Returns one of:
        "windows"     – Windows installer/executable
        "linux"       – Linux app (folder or portable ELF binary)
        "ambiguous"   – Cannot determine; ask the user
        "unsupported" – Rejected input (archive, .sh installer, etc.)
                        Call :func:`unsupported_reason` for the error message.
    """
    if path.is_file():
        return _detect_file(path)
    if path.is_dir():
        return _detect_folder(path)
    return "unsupported"


def unsupported_reason(path: Path) -> str:
    """Return a human-readable message explaining why *path* is unsupported."""
    if path.is_file():
        suffix = path.suffix.lower()
        # Multi-part suffixes like .tar.gz
        stem_suffix = Path(path.stem).suffix.lower()
        combined = stem_suffix + suffix
        if combined in {".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"} or suffix in _ARCHIVE_EXTS:
            return UNSUPPORTED_MESSAGES["archive"]
        if suffix in _LIN_SCRIPT_EXTS:
            return UNSUPPORTED_MESSAGES["sh_run"]
    return UNSUPPORTED_MESSAGES["unknown_file"]


def parse_app_name(path: Path) -> str:
    """Derive a clean display name from a file or folder path.

    Examples:
        setup_songs_of_conquest_1.9.1_a10783a599_4055_(89220).exe
            → "Songs Of Conquest"
        ShadowOfTheTombRaider/
            → "Shadow Of The Tomb Raider"  (camelCase split not implemented)
        Cyberpunk 2077/
            → "Cyberpunk 2077"
    """
    stem = path.stem if path.is_file() else path.name
    if not stem:
        stem = path.name

    # Strip one leading installer prefix (one pass only)
    cleaned = _STRIP_PREFIX_RE.sub("", stem)

    # Iteratively strip trailing version/build noise
    prev = None
    while prev != cleaned:
        prev = cleaned
        cleaned = _STRIP_SUFFIX_RE.sub("", cleaned)

    # Normalise separators → spaces
    cleaned = cleaned.replace("_", " ").replace("-", " ")

    # Collapse multiple spaces
    cleaned = " ".join(cleaned.split())

    # Title-case
    result = cleaned.title()

    # Fall back to original stem if stripping produced an empty string
    return result if result.strip() else stem.replace("_", " ").replace("-", " ").title()


def parse_version_hint(path: Path) -> str | None:
    """Extract a semver-like version string from a filename, or return None."""
    stem = path.stem if path.is_file() else path.name
    m = _VERSION_RE.search(stem)
    return m.group(0) if m else None


def find_exe_files(folder: Path) -> list[Path]:
    """Return .exe and .msi files found in *folder* (full recursive scan)."""
    return [
        p for p in folder.rglob("*")
        if p.is_file() and p.suffix.lower() in {".exe", ".msi"}
    ]


def scan_prefix_exes(prefix_path: Path) -> set[Path]:
    """Return all .exe/.msi files in a WINEPREFIX, excluding Wine system dirs.

    Unlike :func:`find_exe_files`, this does a full recursive walk with no
    depth limit — suitable for snapshotting a prefix before/after an installer
    to detect newly created executables.
    """
    from cellar.backend.manifest import _is_wine_system

    drive_c = prefix_path / "drive_c"
    if not drive_c.is_dir():
        return set()

    result: set[Path] = set()
    for fp in drive_c.rglob("*"):
        if not fp.is_file() or fp.suffix.lower() not in {".exe", ".msi"}:
            continue
        rel = str(fp.relative_to(prefix_path)).lower()
        if _is_wine_system(rel):
            continue
        result.add(fp)
    return result


def find_linux_executables(folder: Path) -> list[Path]:
    """Return Linux executable candidates found in *folder* (full recursive scan).

    Includes *.sh, *.run, and extension-less ELF binaries.
    These are valid *entry points* for an already-installed Linux app;
    they are NOT treated as installer scripts here.
    """
    result: list[Path] = []

    for p in folder.rglob("*"):
        if not p.is_file():
            continue
        suffix = p.suffix.lower()
        if suffix in _LIN_EXTS:
            result.append(p)
        elif suffix == "" and _is_elf(p):
            result.append(p)

    return result


def find_gameinfo(prefix_path: Path) -> dict[str, str] | None:
    """Search a folder for a GoG *gameinfo* file and parse it.

    Searches the folder itself first, then ``drive_c/`` if present
    (WINEPREFIX layout).  Returns ``{"name": str, "version": str}``
    or ``None``.
    """
    search_roots = [prefix_path]
    drive_c = prefix_path / "drive_c"
    if drive_c.is_dir():
        search_roots.append(drive_c)

    candidates: list[Path] = []
    for root in search_roots:
        try:
            candidates.extend(
                p for p in root.rglob("*")
                if p.is_file() and p.name.lower() == "gameinfo"
            )
        except OSError:
            continue

    if not candidates:
        return None

    for candidate in candidates:
        try:
            lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
            name = lines[0].strip() if len(lines) > 0 else ""
            version = lines[1].strip() if len(lines) > 1 else ""
            build = lines[2].strip() if len(lines) > 2 else ""
            if name:
                ver = f"{version} ({build})" if version and build else version
                return {"name": name, "version": ver}
        except OSError:
            continue

    return None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _detect_file(path: Path) -> DetectResult:
    suffix = path.suffix.lower()
    # Check for multi-part archive suffixes (.tar.gz etc.)
    stem_suffix = Path(path.stem).suffix.lower()
    combined = stem_suffix + suffix
    if combined in {".tar.gz", ".tar.bz2", ".tar.xz", ".tar.zst"}:
        return "unsupported"

    if suffix in _WIN_EXTS:
        return "windows"
    if suffix in _ARCHIVE_EXTS:
        return "unsupported"
    if suffix in _LIN_SCRIPT_EXTS:
        from cellar.utils.gog import is_gog_installer

        if is_gog_installer(path):
            return "linux"
        return "unsupported"  # single installer script — can't control install dir
    if suffix in _LIN_BINARY_EXTS or _is_elf(path):
        return "linux"
    return "unsupported"


def _detect_folder(folder: Path) -> DetectResult:
    win_count = len(find_exe_files(folder))
    lin_count = len(find_linux_executables(folder))

    if win_count > lin_count:
        return "windows"
    if lin_count > win_count:
        return "linux"
    # Tie (including both zero) — ask the user
    return "ambiguous"


def _is_elf(path: Path) -> bool:
    """Return True if *path* starts with the ELF magic bytes."""
    try:
        with path.open("rb") as f:
            return f.read(4) == _ELF_MAGIC
    except OSError:
        return False



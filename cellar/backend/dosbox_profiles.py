"""DOSBox game profile auto-detection and configuration.

Detects known DOS games at install time and merges proven-good settings
into ``dosbox-overrides.conf`` with a ``# Profile:`` header comment.

The profile database lives in ``data/dosbox-profiles.json`` (user-supplied).
It is not shipped with the app — users create their own profiles or export
settings from the DOSBox settings dialog.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# Sentinel so we only log "no profiles found" once.
_warned_missing = False


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _user_profiles_dir() -> Path:
    """Return the user-writable directory for profiles."""
    import os
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "cellar"


def _bundled_profiles_path() -> Path | None:
    """Return the path to the profiles JSON, or ``None``.

    Resolution order: user data dir → source data dir → installed data dirs.
    """
    from cellar.utils.paths import _SRC_DATA, _PKG_DATA, _installed_data_dirs

    candidates = [
        _user_profiles_dir() / "dosbox-profiles.json",
        _SRC_DATA / "dosbox-profiles.json",
        _PKG_DATA / "dosbox-profiles.json",
    ] + [d / "dosbox-profiles.json" for d in _installed_data_dirs()]

    for c in candidates:
        if c.is_file():
            return c
    return None


def load_profiles() -> dict:
    """Load the profiles database from the local data directory.

    Returns the full JSON dict, or an empty ``{"profiles": {}}`` fallback
    when no file exists.
    """
    global _warned_missing  # noqa: PLW0603

    path = _bundled_profiles_path()
    if path is not None and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if "profiles" in data:
                return data
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Failed to load profiles from %s: %s", path, exc)

    if not _warned_missing:
        log.debug("No dosbox-profiles.json found; game detection disabled")
        _warned_missing = True
    return {"profiles": {}}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------

from cellar.backend._profile_matching import (  # noqa: E402
    content_root as _content_root,
    match_files as _match_files,
    match_gog_ids as _match_gog_ids,
)


def detect_profile(game_dir: Path) -> str | None:
    """Detect which profile matches the DOS game in *game_dir*.

    Detection priority:
    1. GOG ID match (fast, exact)
    2. File fingerprint match on HDD content (recursive for bare names)

    Returns the profile slug (dict key) or ``None``.
    """
    db = load_profiles()
    profiles = db.get("profiles", {})
    if not profiles:
        return None

    root = _content_root(game_dir)

    # Pass 1: GOG ID match (highest priority)
    for slug, profile in profiles.items():
        match = profile.get("match", {})
        if _match_gog_ids(root, match.get("gog_ids", [])):
            log.info("Detected DOS profile %r via GOG ID", slug)
            return slug

    # Pass 2: file fingerprint match (HDD content only)
    for slug, profile in profiles.items():
        match = profile.get("match", {})
        if _match_files(root, match.get("files", [])):
            log.info("Detected DOS profile %r via file fingerprint", slug)
            return slug

    return None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def remove_profile(game_dir: Path) -> None:
    """Remove the profile marker from ``dosbox-overrides.conf``."""
    conf = game_dir / "config" / "dosbox-overrides.conf"
    if not conf.is_file():
        return
    try:
        text = conf.read_text(encoding="utf-8")
        lines = text.splitlines()
        # Remove the profile header comment if present
        new_lines = [l for l in lines if not l.startswith("# Profile: ")]
        conf.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    # Clean up legacy marker file
    legacy = game_dir / "config" / "dosbox-profile.conf"
    if legacy.is_file():
        legacy.unlink()


def read_profile_name(game_dir: Path) -> str | None:
    """Read the profile name from the ``dosbox-overrides.conf`` header.

    Returns the human-readable name, or ``None`` if no profile is applied.
    """
    conf = game_dir / "config" / "dosbox-overrides.conf"
    if not conf.is_file():
        return None
    try:
        for line in conf.read_text(encoding="utf-8").splitlines():
            prefix = "# Profile: "
            if line.startswith(prefix):
                return line[len(prefix):].strip()
    except OSError:
        pass
    return None


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------

def apply_profile(game_dir: Path) -> str | None:
    """Detect and apply a DOSBox profile for the game in *game_dir*.

    Merges the detected settings into ``dosbox-overrides.conf`` and writes
    a ``# Profile:`` header comment so the settings dialog can show which
    profile is active.

    Returns the profile slug if a profile was applied, ``None`` otherwise.
    """
    # Only apply once — if a profile header already exists, don't touch it.
    overrides_conf = game_dir / "config" / "dosbox-overrides.conf"
    if overrides_conf.is_file():
        first_line = overrides_conf.read_text(encoding="utf-8").split("\n", 1)[0]
        if first_line.startswith("# Profile: "):
            return None

    slug = detect_profile(game_dir)
    if slug is None:
        return None

    db = load_profiles()
    profile = db.get("profiles", {}).get(slug)
    if profile is None:
        return None

    overrides_conf = game_dir / "config" / "dosbox-overrides.conf"

    # Write profile name as header comment.
    if overrides_conf.is_file():
        text = overrides_conf.read_text(encoding="utf-8")
        # Remove any existing profile header
        lines = [l for l in text.splitlines() if not l.startswith("# Profile: ")]
        name = profile.get("name", slug)
        lines.insert(0, f"# Profile: {name}")
        overrides_conf.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Merge profile settings into overrides.
    settings = profile.get("settings", {})
    if settings:
        from cellar.backend.dosbox import write_overrides_batch

        changes: dict[tuple[str, str], str] = {}
        for section, keys in settings.items():
            for key, value in keys.items():
                changes[(section, key)] = str(value)
        write_overrides_batch(overrides_conf, changes)
        log.info("Applied profile %r settings to overrides conf", slug)

    return slug



# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_profile(
    game_dir: Path,
    name: str,
    slug: str,
    match_files: list[str] | None = None,
    match_gog_ids: list[str] | None = None,
) -> dict:
    """Export the current DOSBox overrides as a profile dict.

    Reads non-default settings from ``dosbox-overrides.conf`` and returns
    a profile entry ready to be inserted into ``dosbox-profiles.json``.
    """
    overrides_conf = game_dir / "config" / "dosbox-overrides.conf"
    settings: dict[str, dict[str, str]] = {}

    if overrides_conf.is_file():
        text = overrides_conf.read_text(encoding="utf-8", errors="replace")
        current_section = ""
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                current_section = stripped[1:-1].strip()
                continue
            if "=" in stripped and current_section:
                key, _, value = stripped.partition("=")
                key = key.strip()
                value = value.strip()
                if key and value:
                    settings.setdefault(current_section, {})[key] = value

    profile: dict = {"name": name}
    match: dict = {}
    if match_files:
        match["files"] = match_files
    if match_gog_ids:
        match["gog_ids"] = match_gog_ids
    if match:
        profile["match"] = match
    if settings:
        profile["settings"] = settings

    return {slug: profile}


def save_profile_to_db(profile_entry: dict) -> None:
    """Merge a profile entry into the user's ``dosbox-profiles.json``.

    Reads from the existing profiles file (if any) and writes to the
    user data dir (``~/.local/share/cellar/``).  Creates the file if
    it doesn't exist.
    """
    # Read existing profiles from wherever they currently live.
    path = _bundled_profiles_path()
    # Always write to the user-writable location.
    write_path = _user_profiles_dir() / "dosbox-profiles.json"

    if path is not None and path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {"profiles": {}}
    else:
        data = {"profiles": {}}

    data.setdefault("profiles", {}).update(profile_entry)

    write_path.parent.mkdir(parents=True, exist_ok=True)
    write_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    log.info("Saved profile to %s", write_path)

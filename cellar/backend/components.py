"""Component index sync using dulwich (pure-Python git).

Clones/pulls ``https://github.com/bottlesdevs/components`` on startup so the
detail view can look up runner download URLs and verify whether a required
runner exists in the index.

Storage layout::

    ~/.local/share/cellar/components/
        .git/                 ← dulwich working tree / object store
        runners/
            wine/
                ge-proton10-32.yml
            soda/
                soda-9.0-1.yml
            ...

Runner YAML format (bottlesdevs/components)::

    Name: ge-proton10-32
    Channel: stable
    File:
      - file_name: GE-Proton10-32.tar.gz
        url: https://github.com/.../GE-Proton10-32.tar.gz
        checksum: sha256:abc123...
        rename: ge-proton10-32.tar.gz   # optional

Public API
----------
``sync_index()``   — clone (first time) or pull (subsequent calls)
``is_available()`` — True if local clone exists and runners/ dir is present
``get_runner_info(name)`` — return parsed YAML dict or None
"""

from __future__ import annotations

import logging
from pathlib import Path

from cellar.backend.config import data_dir

log = logging.getLogger(__name__)

REPO_URL = "https://github.com/bottlesdevs/components"


def _components_dir() -> Path:
    return data_dir() / "components"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sync_index() -> None:
    """Clone (first time) or pull (subsequent calls) the components repo.

    Designed to run on a daemon thread so it never blocks the UI.  Any
    exception is caught and logged as a warning; a missing or stale clone
    never prevents startup.
    """
    try:
        from dulwich import porcelain  # type: ignore[import]
    except ImportError:
        log.warning("dulwich not installed; component index unavailable")
        return

    target = _components_dir()
    git_dir = target / ".git"
    try:
        if git_dir.is_dir():
            log.debug("Pulling components index from %s", REPO_URL)
            porcelain.pull(str(target), remote_location=REPO_URL)
        else:
            log.debug("Cloning components index from %s", REPO_URL)
            target.mkdir(parents=True, exist_ok=True)
            porcelain.clone(REPO_URL, str(target), depth=1)
        log.debug("Components index sync complete")
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to sync components index: %s", exc)


def is_available() -> bool:
    """Return True if the local clone exists and has a runners/ directory."""
    d = _components_dir()
    return (d / ".git").is_dir() and (d / "runners").is_dir()


def get_runner_info(runner_name: str) -> dict | None:
    """Return the parsed YAML dict for *runner_name*, or ``None`` if not found.

    Searches all subdirectories of ``components/runners/`` for a YAML file
    whose stem matches *runner_name* case-insensitively.

    Relevant fields in the returned dict::

        Name                 str  — runner identifier
        File[0].url          str  — download URL
        File[0].file_name    str  — archive filename
        File[0].checksum     str  — 'sha256:<hex>'
        File[0].rename       str  — optional rename after extract
    """
    try:
        import yaml  # type: ignore[import]
    except ImportError:
        return None

    runners_dir = _components_dir() / "runners"
    if not runners_dir.is_dir():
        return None

    name_lower = runner_name.lower()
    for yaml_file in runners_dir.rglob("*.yml"):
        if yaml_file.stem.lower() == name_lower:
            try:
                data = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception as exc:  # noqa: BLE001
                log.debug("Failed to parse %s: %s", yaml_file, exc)
    return None

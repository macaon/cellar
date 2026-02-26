"""Persistent application configuration.

Stored as a JSON file in the user's XDG data directory:

    ~/.local/share/cellar/config.json

(or the Flatpak equivalent when running sandboxed)

Schema::

    {
      "repos": [
        {
          "uri": "https://nas.home.arpa/cellar",
          "name": "Home NAS",          // optional display name
          "ssh_identity": null          // path to SSH key, or null
        }
      ]
    }
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_CONFIG_FILE = "config.json"


def data_dir() -> Path:
    """Return (and create if needed) the Cellar data directory."""
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    d = base / "cellar"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _config_path() -> Path:
    return data_dir() / _CONFIG_FILE


# ---------------------------------------------------------------------------
# Low-level read/write
# ---------------------------------------------------------------------------

def _load() -> dict:
    path = _config_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read config: %s", exc)
        return {}


def _save(data: dict) -> None:
    _config_path().write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Repo list helpers
# ---------------------------------------------------------------------------

def load_repos() -> list[dict]:
    """Return the list of configured repo dicts."""
    return _load().get("repos", [])


def save_repos(repos: list[dict]) -> None:
    """Persist the repo list, preserving other config keys."""
    cfg = _load()
    cfg["repos"] = repos
    _save(cfg)


# ---------------------------------------------------------------------------
# Capsule size helpers
# ---------------------------------------------------------------------------

# Width → height is always width * 3 // 2  (2:3 portrait ratio, SteamGridDB spec)
CAPSULE_SIZES: dict[str, int] = {
    "small":    100,
    "medium":   200,
    "large":    400,
    "original": 600,
}
_DEFAULT_CAPSULE_SIZE = "medium"

CAPSULE_SIZE_LABELS: dict[str, str] = {
    "small":    "Small (100 × 150)",
    "medium":   "Medium (200 × 300)",
    "large":    "Large (400 × 600)",
    "original": "Original (600 × 900)",
}


def load_capsule_size() -> str:
    """Return the stored capsule size key, defaulting to 'medium'."""
    key = _load().get("capsule_size", _DEFAULT_CAPSULE_SIZE)
    return key if key in CAPSULE_SIZES else _DEFAULT_CAPSULE_SIZE


def save_capsule_size(size: str) -> None:
    """Persist the capsule size key."""
    cfg = _load()
    cfg["capsule_size"] = size
    _save(cfg)


# ---------------------------------------------------------------------------
# Bottles data path helpers
# ---------------------------------------------------------------------------

def load_bottles_data_path() -> str | None:
    """Return the user-overridden Bottles data path, or None if unset."""
    return _load().get("bottles_data_path")


def save_bottles_data_path(path: str | None) -> None:
    """Persist the Bottles data path override.

    Pass ``None`` to clear the override (auto-detection will be used).
    """
    cfg = _load()
    if path is None:
        cfg.pop("bottles_data_path", None)
    else:
        cfg["bottles_data_path"] = path
    _save(cfg)

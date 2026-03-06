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
    """Return (and create if needed) the Cellar metadata directory.

    Always ``~/.local/share/cellar/`` (or XDG equivalent).  Large install
    data (prefixes, native apps, bases) lives under ``install_data_dir()``.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    d = base / "cellar"
    d.mkdir(parents=True, exist_ok=True)
    return d


def install_data_dir() -> Path:
    """Return (and create if needed) the root for large install data.

    Defaults to ``data_dir()``.  When the user sets an install base in
    Preferences a ``Cellar/`` subdirectory is created there instead.
    """
    cfg = _load()
    base = cfg.get("install_base", "")
    if base:
        d = Path(base).expanduser() / "Cellar"
    else:
        d = data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def certs_dir() -> Path:
    """Return (and create if needed) the directory for stored CA certificates."""
    d = data_dir() / "certs"
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
    """Return the list of configured repo dicts.

    Each dict may contain:
      "uri"          – required
      "name"         – optional display name
      "ssh_identity" – optional path to SSH key
      "ssl_verify"   – optional bool (default True); set False for self-signed certs
    """
    return _load().get("repos", [])


def save_repos(repos: list[dict]) -> None:
    """Persist the repo list, preserving other config keys."""
    cfg = _load()
    cfg["repos"] = repos
    _save(cfg)


# ---------------------------------------------------------------------------
# umu-launcher path helpers
# ---------------------------------------------------------------------------

def load_umu_path() -> str | None:
    """Return the user-overridden umu-run binary path, or None (auto-detect)."""
    return _load().get("umu_path") or None


def save_umu_path(path: str | None) -> None:
    """Persist a umu-run binary path override.

    Pass ``None`` to clear the override (auto-detection will be used).
    """
    cfg = _load()
    if path is None:
        cfg.pop("umu_path", None)
    else:
        cfg["umu_path"] = path
    _save(cfg)


# ---------------------------------------------------------------------------
# Install location helpers
# ---------------------------------------------------------------------------

def load_install_base() -> str:
    """Return the user-configured install base directory, or '' (use default)."""
    return _load().get("install_base", "")


def save_install_base(path: str) -> None:
    """Persist an install base directory override.

    Pass an empty string to reset to the default (``data_dir()``).
    """
    cfg = _load()
    if path:
        cfg["install_base"] = path
    else:
        cfg.pop("install_base", None)
    _save(cfg)


# ---------------------------------------------------------------------------
# SMB credential helpers
# ---------------------------------------------------------------------------

_KEYRING_SERVICE = "cellar-repo"


def save_smb_password(uri: str, password: str) -> None:
    """Store *password* for *uri* in the system keyring.

    Falls back to ``config.json`` if the keyring is unavailable (e.g. on
    headless systems).  The fallback is logged as a warning.
    """
    try:
        import keyring  # type: ignore[import]
        keyring.set_password(_KEYRING_SERVICE, uri, password)
        return
    except Exception as exc:
        log.warning(
            "Keyring unavailable (%s); storing SMB password in config.json", exc
        )
    cfg = _load()
    cfg.setdefault("smb_passwords", {})[uri] = password
    _save(cfg)
    # Restrict permissions on config file so the password is not world-readable.
    try:
        import os
        _config_path().chmod(0o600)
    except OSError:
        pass


def load_smb_password(uri: str) -> str | None:
    """Return the stored SMB password for *uri*, or ``None`` if not found."""
    try:
        import keyring  # type: ignore[import]
        pw = keyring.get_password(_KEYRING_SERVICE, uri)
        if pw is not None:
            return pw
    except Exception:
        pass
    return _load().get("smb_passwords", {}).get(uri)


def clear_smb_password(uri: str) -> None:
    """Remove the stored SMB password for *uri* from keyring and config."""
    try:
        import keyring  # type: ignore[import]
        keyring.delete_password(_KEYRING_SERVICE, uri)
    except Exception:
        pass
    cfg = _load()
    passwords = cfg.get("smb_passwords", {})
    if uri in passwords:
        del passwords[uri]
        _save(cfg)

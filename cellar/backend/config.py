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

Credential storage
------------------
Passwords are stored via ``gi.repository.Secret`` (libsecret), which speaks
the ``org.freedesktop.secrets`` D-Bus spec.  This covers GNOME Keyring, KDE
KWallet, and any other compliant daemon, including the Flatpak portal.

Falls back to plaintext in ``config.json`` (chmod 0o600) only when no secret
service daemon is running (headless / CI).
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Secret storage helpers
# ---------------------------------------------------------------------------

_LIBSECRET_SERVICE = "io.github.cellar"

# Lazily-initialised libsecret schema objects.
_secret_schema: object | None = None
_secret_available: bool | None = None  # None = not yet probed


def _secret_schema_obj():
    """Return the lazily-created libsecret Schema, or None if unavailable."""
    global _secret_schema, _secret_available
    if _secret_available is False:
        return None
    if _secret_schema is not None:
        return _secret_schema
    try:
        from gi.repository import Secret  # type: ignore[import]
        _secret_schema = Secret.Schema.new(
            _LIBSECRET_SERVICE,
            Secret.SchemaFlags.NONE,
            {"service": Secret.SchemaAttributeType.STRING,
             "uri":     Secret.SchemaAttributeType.STRING},
        )
        _secret_available = True
        return _secret_schema
    except Exception as exc:
        log.debug("libsecret unavailable: %s", exc)
        _secret_available = False
        return None


def _libsecret_store(service: str, uri: str, password: str) -> bool:
    """Store *password* in GNOME Keyring. Returns True on success."""
    schema = _secret_schema_obj()
    if schema is None:
        return False
    try:
        from gi.repository import Secret  # type: ignore[import]
        ok = Secret.password_store_sync(
            schema,
            {"service": service, "uri": uri},
            Secret.COLLECTION_DEFAULT,
            f"Cellar — {service}: {uri}",
            password,
            None,
        )
        return bool(ok)
    except Exception as exc:
        log.debug("libsecret store failed: %s", exc)
        return False


def _libsecret_load(service: str, uri: str) -> str | None:
    """Return password from GNOME Keyring, or None."""
    schema = _secret_schema_obj()
    if schema is None:
        return None
    try:
        from gi.repository import Secret  # type: ignore[import]
        return Secret.password_lookup_sync(
            schema, {"service": service, "uri": uri}, None
        )
    except Exception as exc:
        log.debug("libsecret lookup failed: %s", exc)
        return None


def _libsecret_clear(service: str, uri: str) -> None:
    """Delete password from GNOME Keyring (best-effort)."""
    schema = _secret_schema_obj()
    if schema is None:
        return
    try:
        from gi.repository import Secret  # type: ignore[import]
        Secret.password_clear_sync(schema, {"service": service, "uri": uri}, None)
    except Exception as exc:
        log.debug("libsecret clear failed: %s", exc)


def save_password(uri: str, password: str) -> None:
    """Store *password* for *uri* via libsecret; fall back to config.json (chmod 0o600)."""
    if _libsecret_store(_LIBSECRET_SERVICE, uri, password):
        return
    log.warning(
        "Secret service unavailable; storing password for %s in config.json "
        "(file is mode 0600 — avoid world-readable mounts)",
        uri,
    )
    cfg = _load()
    cfg.setdefault("passwords", {})[uri] = password
    # Migrate legacy per-scheme keys on the way through.
    for old_key in ("smb_passwords", "ssh_passwords"):
        if uri in cfg.get(old_key, {}):
            del cfg[old_key][uri]
    _save(cfg)
    try:
        _config_path().chmod(0o600)
    except OSError as exc:
        log.error(
            "Could not restrict config.json permissions (%s). "
            "Plaintext credentials may be readable by other users.",
            exc,
        )


def load_password(uri: str) -> str | None:
    """Return the stored password for *uri*, or ``None`` if not found."""
    pw = _libsecret_load(_LIBSECRET_SERVICE, uri)
    if pw is not None:
        return pw
    cfg = _load()
    # Check unified key first, then legacy per-scheme fallbacks.
    return (
        cfg.get("passwords", {}).get(uri)
        or cfg.get("smb_passwords", {}).get(uri)
        or cfg.get("ssh_passwords", {}).get(uri)
    )


def clear_password(uri: str) -> None:
    """Remove the stored password for *uri* from all backends."""
    _libsecret_clear(_LIBSECRET_SERVICE, uri)
    cfg = _load()
    changed = False
    for key in ("passwords", "smb_passwords", "ssh_passwords"):
        if uri in cfg.get(key, {}):
            del cfg[key][uri]
            changed = True
    if changed:
        _save(cfg)

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
        resolved = Path(base).expanduser().resolve()
        if not resolved.is_absolute():
            log.warning("install_base %r is not absolute; ignoring", base)
            d = data_dir()
        elif ".." in Path(base).parts:
            log.warning("install_base %r contains traversal; ignoring", base)
            d = data_dir()
        else:
            d = resolved / "Cellar"
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
    dest = _config_path()
    tmp = dest.with_suffix(".tmp")
    # Open with restrictive permissions (0o600) so plaintext credentials
    # in the fallback path are never briefly world-readable.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, ensure_ascii=False))
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)


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
# Global Wine audio driver default
# ---------------------------------------------------------------------------

def load_audio_driver() -> str:
    """Return the global default Wine audio driver, or ``'auto'``."""
    return _load().get("audio_driver", "auto")


def save_audio_driver(driver: str) -> None:
    """Persist the global default Wine audio driver.

    Pass ``'auto'`` (or empty) to reset to the default.
    """
    cfg = _load()
    if driver and driver != "auto":
        cfg["audio_driver"] = driver
    else:
        cfg.pop("audio_driver", None)
    _save(cfg)


# ---------------------------------------------------------------------------
# Per-scheme aliases (kept for call-site readability)
# ---------------------------------------------------------------------------

save_smb_password = save_password
load_smb_password = load_password
clear_smb_password = clear_password

save_ssh_password = save_password
load_ssh_password = load_password
clear_ssh_password = clear_password


# ---------------------------------------------------------------------------
# SteamGridDB API key
# ---------------------------------------------------------------------------

def load_sgdb_key() -> str:
    """Return the stored SteamGridDB API key, or empty string."""
    pw = _libsecret_load(_LIBSECRET_SERVICE, "steamgriddb")
    if pw:
        return pw
    return _load().get("sgdb_key", "")


def save_sgdb_key(key: str) -> None:
    """Persist (or clear) the SteamGridDB API key."""
    if key:
        if _libsecret_store(_LIBSECRET_SERVICE, "steamgriddb", key):
            # Stored in keyring — remove any plaintext fallback
            cfg = _load()
            cfg.pop("sgdb_key", None)
            _save(cfg)
            return
        # Fallback to config.json
        cfg = _load()
        cfg["sgdb_key"] = key
        _save(cfg)
    else:
        _libsecret_clear(_LIBSECRET_SERVICE, "steamgriddb")
        cfg = _load()
        cfg.pop("sgdb_key", None)
        _save(cfg)

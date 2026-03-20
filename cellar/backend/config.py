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
Passwords are stored using the first available backend:

1. **libsecret** — ``org.freedesktop.secrets`` D-Bus API (GNOME Keyring,
   KeePassXC, Flatpak portal, or any other Secret Service provider).
2. **KWallet** — ``org.kde.KWallet`` D-Bus API (KDE Plasma).  Used when
   libsecret is unavailable, i.e. stock KDE without a Secret Service bridge.
3. **config.json** — plaintext fallback (chmod 0o600).  Only used when no
   keyring daemon is reachable (headless / CI).
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


# ---------------------------------------------------------------------------
# KWallet D-Bus helpers (KDE Plasma)
# ---------------------------------------------------------------------------

_KWALLET_VARIANTS = [
    ("org.kde.kwalletd6", "/modules/kwalletd6"),  # Plasma 6
    ("org.kde.kwalletd5", "/modules/kwalletd5"),  # Plasma 5
]
_KWALLET_IFACE = "org.kde.KWallet"
_KWALLET_FOLDER = "Cellar"
_KWALLET_APP = "io.github.cellar"

_kwallet_available: bool | None = None  # None = not yet probed
_kwallet_cached_proxy = None


def _kwallet_proxy():
    """Return a Gio.DBusProxy for KWallet, or None if unavailable.

    Tries kwalletd6 (Plasma 6) first, then kwalletd5 (Plasma 5).
    """
    global _kwallet_available, _kwallet_cached_proxy
    if _kwallet_available is False:
        return None
    if _kwallet_cached_proxy is not None:
        return _kwallet_cached_proxy
    try:
        from gi.repository import Gio  # type: ignore[import]
        for bus_name, obj_path in _KWALLET_VARIANTS:
            proxy = Gio.DBusProxy.new_for_bus_sync(
                Gio.BusType.SESSION,
                Gio.DBusProxyFlags.NONE,
                None,
                bus_name,
                obj_path,
                _KWALLET_IFACE,
                None,
            )
            if proxy.get_name_owner() is not None:
                _kwallet_available = True
                _kwallet_cached_proxy = proxy
                return proxy
        _kwallet_available = False
        return None
    except Exception as exc:
        log.debug("KWallet D-Bus unavailable: %s", exc)
        _kwallet_available = False
        return None


def _kwallet_open(proxy) -> int | None:
    """Open the default wallet and return its handle, or None on failure."""
    try:
        from gi.repository import GLib as _GLib  # type: ignore[import]
        wallet_name = proxy.call_sync(
            "localWallet", None, 0, -1, None,
        ).unpack()[0]
        handle = proxy.call_sync(
            "open",
            _GLib.Variant("(sxs)", (wallet_name, 0, _KWALLET_APP)),
            0, -1, None,
        ).unpack()[0]
        return handle if handle >= 0 else None
    except Exception as exc:
        log.debug("KWallet open failed: %s", exc)
        return None


def _kwallet_store(service: str, uri: str, password: str) -> bool:
    """Store *password* in KWallet. Returns True on success."""
    proxy = _kwallet_proxy()
    if proxy is None:
        return False
    handle = _kwallet_open(proxy)
    if handle is None:
        return False
    try:
        from gi.repository import GLib as _GLib  # type: ignore[import]
        key = f"{service}:{uri}"
        # Ensure the folder exists.
        proxy.call_sync(
            "createFolder",
            _GLib.Variant("(iss)", (handle, _KWALLET_FOLDER, _KWALLET_APP)),
            0, -1, None,
        )
        rc = proxy.call_sync(
            "writePassword",
            _GLib.Variant("(issss)", (handle, _KWALLET_FOLDER, key, password, _KWALLET_APP)),
            0, -1, None,
        ).unpack()[0]
        return rc == 0
    except Exception as exc:
        log.debug("KWallet store failed: %s", exc)
        return False


def _kwallet_load(service: str, uri: str) -> str | None:
    """Return password from KWallet, or None."""
    proxy = _kwallet_proxy()
    if proxy is None:
        return None
    handle = _kwallet_open(proxy)
    if handle is None:
        return None
    try:
        from gi.repository import GLib as _GLib  # type: ignore[import]
        key = f"{service}:{uri}"
        pw = proxy.call_sync(
            "readPassword",
            _GLib.Variant("(isss)", (handle, _KWALLET_FOLDER, key, _KWALLET_APP)),
            0, -1, None,
        ).unpack()[0]
        return pw if pw else None
    except Exception as exc:
        log.debug("KWallet lookup failed: %s", exc)
        return None


def _kwallet_clear(service: str, uri: str) -> None:
    """Delete password from KWallet (best-effort)."""
    proxy = _kwallet_proxy()
    if proxy is None:
        return
    handle = _kwallet_open(proxy)
    if handle is None:
        return
    try:
        from gi.repository import GLib as _GLib  # type: ignore[import]
        key = f"{service}:{uri}"
        proxy.call_sync(
            "removeEntry",
            _GLib.Variant("(isss)", (handle, _KWALLET_FOLDER, key, _KWALLET_APP)),
            0, -1, None,
        )
    except Exception as exc:
        log.debug("KWallet clear failed: %s", exc)


# ---------------------------------------------------------------------------
# Unified credential API
# ---------------------------------------------------------------------------


def save_password(uri: str, password: str) -> None:
    """Store *password* for *uri* via the best available backend."""
    if _libsecret_store(_LIBSECRET_SERVICE, uri, password):
        return
    if _kwallet_store(_LIBSECRET_SERVICE, uri, password):
        return
    log.warning(
        "No keyring available; storing password for %s in config.json "
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
    pw = _kwallet_load(_LIBSECRET_SERVICE, uri)
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
    _kwallet_clear(_LIBSECRET_SERVICE, uri)
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
    pw = _kwallet_load(_LIBSECRET_SERVICE, "steamgriddb")
    if pw:
        return pw
    return _load().get("sgdb_key", "")


def load_sgdb_language() -> str:
    """Return the preferred SteamGridDB asset language, or ``''`` (English default)."""
    return _load().get("sgdb_language", "")


def save_sgdb_language(language: str) -> None:
    """Persist the preferred SteamGridDB asset language.

    Pass ``''`` to reset to the default (English).
    """
    cfg = _load()
    if language:
        cfg["sgdb_language"] = language
    else:
        cfg.pop("sgdb_language", None)
    _save(cfg)


# ---------------------------------------------------------------------------
# Display mode (card / capsule)
# ---------------------------------------------------------------------------

def load_display_mode() -> str:
    """Return the persisted browse display mode, or ``'card'``."""
    mode = _load().get("display_mode", "card")
    return mode if mode in ("card", "capsule") else "card"


def save_display_mode(mode: str) -> None:
    """Persist the browse display mode (``'card'`` or ``'capsule'``)."""
    cfg = _load()
    if mode and mode != "card":
        cfg["display_mode"] = mode
    else:
        cfg.pop("display_mode", None)
    _save(cfg)


def save_sgdb_key(key: str) -> None:
    """Persist (or clear) the SteamGridDB API key."""
    if key:
        if _libsecret_store(_LIBSECRET_SERVICE, "steamgriddb", key):
            cfg = _load()
            cfg.pop("sgdb_key", None)
            _save(cfg)
            return
        if _kwallet_store(_LIBSECRET_SERVICE, "steamgriddb", key):
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
        _kwallet_clear(_LIBSECRET_SERVICE, "steamgriddb")
        cfg = _load()
        cfg.pop("sgdb_key", None)
        _save(cfg)

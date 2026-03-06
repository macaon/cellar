"""XDG .desktop entry helpers for Cellar-managed apps."""

from __future__ import annotations

import io
import logging
import subprocess
import zlib
from pathlib import Path

log = logging.getLogger(__name__)

_APPS_DIR = Path.home() / ".local/share/applications"
_ICONS_DIR = Path.home() / ".local/share/icons/cellar"

_CATEGORY_MAP: dict[str, str] = {
    "Games": "Game",
    "Productivity": "Office",
    "Graphics": "Graphics",
    "Utility": "Utility",
}


def desktop_entry_path(app_id: str) -> Path:
    return _APPS_DIR / f"cellar-{app_id}.desktop"


def has_desktop_entry(app_id: str) -> bool:
    return desktop_entry_path(app_id).exists()


def _remove_icons(app_id: str) -> None:
    """Delete all installed icon versions for *app_id*."""
    for p in _ICONS_DIR.glob(f"{app_id}_*.png"):
        p.unlink()
        log.info("Removed %s", p)


def _install_icon(app_id: str, src: str) -> str:
    """Process *src* and install it to ~/.local/share/icons/cellar/.

    The filename includes a CRC32 of the processed PNG bytes so that each
    distinct icon version gets a unique filename.  GNOME Shell caches icon
    textures by filename, so a new hash forces a fresh load without needing
    a shell restart.  Any previous icon versions for *app_id* are removed.

    Returns the absolute path to the installed file, or the empty string on
    failure.
    """
    _ICONS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image

        img = Image.open(src)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        # Trim transparent padding so the actual content fills the canvas.
        if img.mode == "RGBA":
            bbox = img.split()[3].getbbox()
            if bbox:
                img = img.crop(bbox)
        # Scale to fill 512×512 (up or down), preserving aspect ratio.
        _S = 512
        ratio = min(_S / img.width, _S / img.height)
        img = img.resize((round(img.width * ratio), round(img.height * ratio)), Image.LANCZOS)
        canvas = Image.new("RGBA", (_S, _S), (0, 0, 0, 0))
        canvas.paste(img, ((_S - img.width) // 2, (_S - img.height) // 2))

        # Encode to bytes, compute CRC32 for cache-busting filename.
        buf = io.BytesIO()
        canvas.save(buf, "PNG")
        png_bytes = buf.getvalue()
        crc = zlib.crc32(png_bytes) & 0xFFFFFFFF

        # Remove previous versions before writing the new one.
        _remove_icons(app_id)

        dest = _ICONS_DIR / f"{app_id}_{crc:08x}.png"
        dest.write_bytes(png_bytes)
        log.debug("Installed icon for %s → %s", app_id, dest)
        return str(dest)
    except Exception as exc:
        log.warning("Could not install icon for %s: %s", app_id, exc)
        return ""


def _refresh_desktop_db() -> None:
    """Rebuild the .desktop database so GNOME picks up changes immediately."""
    subprocess.run(
        ["update-desktop-database", str(_APPS_DIR)],
        capture_output=True,
        check=False,
    )


def create_desktop_entry(
    entry,               # AppEntry — avoid circular import at module level
    bottle_name: str,    # prefix_dir for Windows apps; install dir name for Linux
    program_name: str | None = None,  # unused; kept for API compatibility
    is_flatpak: bool = False,         # unused; kept for API compatibility
    icon_source: str | None = None,
    install_path: str = "",
) -> None:
    """Write a .desktop entry for *entry* to ~/.local/share/applications.

    For Windows apps: launches via umu-run with the correct prefix and runner.
    For Linux native apps: *install_path* is the base directory and the Exec
    line runs the entry_point directly.
    """
    _APPS_DIR.mkdir(parents=True, exist_ok=True)

    # Icon — absolute path with CRC32 suffix busts GNOME Shell's icon cache.
    icon_ref = "application-x-executable"
    if icon_source and Path(icon_source).is_file():
        installed = _install_icon(entry.id, icon_source)
        if installed:
            icon_ref = installed

    # Exec — branch on platform
    platform = getattr(entry, "platform", "windows")
    if platform == "linux":
        if install_path and bottle_name and entry.entry_point:
            exe = Path(install_path) / bottle_name / entry.entry_point
            exec_line = f'"{exe}"'
        else:
            exec_line = "true"  # placeholder; entry point unknown
        comment = (entry.summary or f"Launch {entry.name}.").replace("\n", " ")
    else:
        # umu-launcher launch: set env vars then invoke umu-run.
        from cellar.backend.umu import detect_umu, prefixes_dir, runners_dir  # noqa: PLC0415
        from cellar.backend.config import load_umu_path  # noqa: PLC0415
        umu_bin = detect_umu(load_umu_path()) or "umu-run"

        steam_appid = getattr(entry, "steam_appid", None)
        gameid = f"umu-{steam_appid}" if steam_appid else "0"

        # Use the runner stored in the DB if available; fall back to built_with.
        runner_name = ""
        try:
            from cellar.backend import database as _db  # noqa: PLC0415
            rec = _db.get_installed(entry.id) or {}
            runner_name = rec.get("runner_override") or rec.get("runner") or ""
        except Exception:  # noqa: BLE001
            pass
        if not runner_name and getattr(entry, "built_with", None):
            runner_name = entry.built_with.runner or ""

        prefix = str(prefixes_dir() / entry.id)
        proton = str(runners_dir() / runner_name) if runner_name else ""
        exe_path = entry.entry_point or ""

        env_prefix = (
            f'env WINEPREFIX="{prefix}"'
            + (f' PROTONPATH="{proton}"' if proton else "")
            + f' GAMEID="{gameid}"'
            + (f' EXE="{exe_path}"' if exe_path else "")
        )
        exec_line = f"{env_prefix} {umu_bin}"
        comment = (entry.summary or f"Launch {entry.name} via umu-launcher.").replace("\n", " ")

    # Categories
    xdg_cat = _CATEGORY_MAP.get(entry.category or "", "")
    categories = f"Application;{xdg_cat};" if xdg_cat else "Application;"

    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={entry.name}",
        f"Comment={comment}",
        f"Icon={icon_ref}",
        f"Exec={exec_line}",
        "Terminal=false",
        f"Categories={categories}",
        f"StartupWMClass={entry.name}",
        f"X-Cellar-AppId={entry.id}",
        f"X-Cellar-Platform={platform}",
    ]

    path = desktop_entry_path(entry.id)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    log.info("Created desktop entry: %s", path)
    _refresh_desktop_db()


def remove_desktop_entry(app_id: str) -> None:
    """Remove the .desktop file and all installed icons for *app_id*."""
    path = desktop_entry_path(app_id)
    if path.exists():
        path.unlink()
        log.info("Removed %s", path)
    _remove_icons(app_id)
    _refresh_desktop_db()

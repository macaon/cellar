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


def desktop_entry_path(app_id: str, target_idx: int = 0) -> Path:
    if target_idx == 0:
        return _APPS_DIR / f"cellar-{app_id}.desktop"
    return _APPS_DIR / f"cellar-{app_id}-{target_idx}.desktop"


def has_desktop_entry(app_id: str, target_idx: int = 0) -> bool:
    return desktop_entry_path(app_id, target_idx).exists()


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
    icon_source: str | None = None,
    install_path: str = "",
    target: dict | None = None,
    target_idx: int = 0,
) -> None:
    """Write a .desktop entry for *entry* to ~/.local/share/applications.

    For Windows apps: launches via umu-run with the correct prefix and runner.
    For Linux native apps: *install_path* is the base directory and the Exec
    line runs the entry_point directly.

    *target* overrides the entry's primary launch target (entry_point / launch_args).
    *target_idx* selects the .desktop filename suffix (0 = primary, no suffix).
    """
    _APPS_DIR.mkdir(parents=True, exist_ok=True)

    # Icon — absolute path with CRC32 suffix busts GNOME Shell's icon cache.
    icon_ref = "application-x-executable"
    if icon_source and Path(icon_source).is_file():
        installed = _install_icon(entry.id, icon_source)
        if installed:
            icon_ref = installed

    # Resolve entry_point / launch_args from target override or entry defaults.
    if target:
        exe_path = target.get("path", "")
        launch_args = target.get("args", "")
        target_name = target.get("name", "")
    else:
        exe_path = getattr(entry, "entry_point", "") or ""
        launch_args = getattr(entry, "launch_args", "") or ""
        target_name = ""

    # Desktop entry name — append target name for non-primary targets.
    desktop_name = entry.name
    if target_name and target_idx > 0:
        desktop_name = f"{entry.name} \u2014 {target_name}"

    # Exec — branch on platform
    platform = getattr(entry, "platform", "windows")
    if platform == "linux":
        if exe_path:
            from cellar.backend.umu import native_dir
            exe = native_dir() / entry.id / exe_path
            exec_line = f'"{exe}"'
            if launch_args:
                exec_line += f" {launch_args}"
        else:
            exec_line = "true"  # placeholder; entry point unknown
        comment = (entry.summary or f"Launch {entry.name}.").replace("\n", " ")
    else:
        # umu-launcher launch: set env vars then invoke umu-run.
        from cellar.backend.umu import (  # noqa: PLC0415
            detect_umu, is_cellar_sandboxed, prefixes_dir, runners_dir,
            _umu_data_env,
        )
        from cellar.backend.config import load_umu_path  # noqa: PLC0415

        steam_appid = getattr(entry, "steam_appid", None)
        gameid = f"umu-{steam_appid}" if steam_appid else "0"

        # Use the runner stored in the DB (runner_override takes priority).
        runner_name = ""
        try:
            from cellar.backend import database as _db  # noqa: PLC0415
            rec = _db.get_installed(entry.id) or {}
            runner_name = rec.get("runner_override") or rec.get("runner") or ""
        except Exception:  # noqa: BLE001
            pass

        prefix = str(prefixes_dir() / entry.id)
        proton = str(runners_dir() / runner_name) if runner_name else ""

        # Escape backslashes so GLib's shell parser (\\→\) preserves Windows paths.
        exe_escaped = exe_path.replace("\\", "\\\\")
        exe_arg = f' "{exe_escaped}"' if exe_escaped else ""
        args_str = f" {launch_args}" if launch_args else ""

        if is_cellar_sandboxed():
            # Inside a Flatpak: launch via flatpak run so umu-run is available.
            umu_data = _umu_data_env()
            env_parts = (
                f'--env=WINEPREFIX="{prefix}"'
                + (f' --env=PROTONPATH="{proton}"' if proton else "")
                + f' --env=GAMEID="{gameid}"'
                + f' --env=UMU_FOLDERS_PATH="{umu_data["UMU_FOLDERS_PATH"]}"'
            )
            exec_line = f"flatpak run --command=umu-run {env_parts} io.github.cellar{exe_arg}{args_str}"
        else:
            umu_bin = detect_umu(load_umu_path()) or "umu-run"
            umu_data = _umu_data_env()
            env_prefix = (
                f'env WINEPREFIX="{prefix}"'
                + (f' PROTONPATH="{proton}"' if proton else "")
                + f' GAMEID="{gameid}"'
                + f' UMU_FOLDERS_PATH="{umu_data["UMU_FOLDERS_PATH"]}"'
            )
            exec_line = f"{env_prefix} {umu_bin}{exe_arg}{args_str}"
        comment = (entry.summary or f"Launch {entry.name} via umu-launcher.").replace("\n", " ")

    # Categories
    xdg_cat = _CATEGORY_MAP.get(entry.category or "", "")
    categories = f"Application;{xdg_cat};" if xdg_cat else "Application;"

    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={desktop_name}",
        f"Comment={comment}",
        f"Icon={icon_ref}",
        f"Exec={exec_line}",
        "Terminal=false",
        f"Categories={categories}",
        f"StartupWMClass={entry.name}",
        f"X-Cellar-AppId={entry.id}",
        f"X-Cellar-Platform={platform}",
    ]

    path = desktop_entry_path(entry.id, target_idx)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    log.info("Created desktop entry: %s", path)
    _refresh_desktop_db()


def remove_desktop_entry(app_id: str, target_idx: int | None = None) -> None:
    """Remove .desktop file(s) and all installed icons for *app_id*.

    If *target_idx* is given, only that specific shortcut is removed.
    If *target_idx* is ``None`` (default), all shortcuts for the app are removed
    (primary + any numbered secondary files).
    """
    if target_idx is not None:
        path = desktop_entry_path(app_id, target_idx)
        if path.exists():
            path.unlink()
            log.info("Removed %s", path)
    else:
        # Remove primary and all secondary .desktop files.
        for p in _APPS_DIR.glob(f"cellar-{app_id}*.desktop"):
            p.unlink()
            log.info("Removed %s", p)
    _remove_icons(app_id)
    _refresh_desktop_db()

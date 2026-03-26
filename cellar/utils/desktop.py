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

def _sanitize(value: str) -> str:
    """Strip control characters that would corrupt a .desktop file.

    Newlines and carriage returns act as field terminators in the .desktop
    format — a crafted value could inject arbitrary keys like ``Exec=``.
    """
    return value.replace("\n", " ").replace("\r", " ")


_CATEGORY_MAP: dict[str, str] = {
    "Games": "Game",
    "Productivity": "Office",
    "Graphics": "Graphics",
    "Utility": "Utility",
}


def _desktop_quote(s: str) -> str:
    """Quote a string for use as a ``.desktop`` Exec argument.

    GLib's ``g_shell_parse_argv()`` parses the Exec value.  Arguments
    containing spaces or special characters must be enclosed in double
    quotes, with ``"``, ``$``, `` ` ``, and ``\\`` escaped inside.
    """
    special = frozenset('" $ ` \\ \t\n'.split() + [' '])
    if not any(ch in special for ch in s):
        return s
    escaped = s.replace('\\', '\\\\').replace('"', '\\"').replace('$', '\\$').replace('`', '\\`')
    return f'"{escaped}"'


def desktop_entry_path(app_id: str, target_idx: int = 0) -> Path:
    if target_idx == 0:
        return _APPS_DIR / f"cellar-{app_id}.desktop"
    return _APPS_DIR / f"cellar-{app_id}-{target_idx}.desktop"


def has_desktop_entry(app_id: str, target_idx: int = 0) -> bool:
    return desktop_entry_path(app_id, target_idx).exists()


def _remove_icons(app_id: str) -> None:
    """Delete all installed icon versions for *app_id*."""
    for p in _ICONS_DIR.glob(f"{app_id}_*.png"):
        p.unlink(missing_ok=True)
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
        tmp = dest.with_suffix(".tmp")
        tmp.write_bytes(png_bytes)
        tmp.replace(dest)
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
    icon_source: str | None = None,
    install_dir: str = "",
    target: dict | None = None,
    target_idx: int = 0,
    # Legacy alias — callers that still pass install_path= keep working.
    install_path: str = "",
) -> None:
    """Write a .desktop entry for *entry* to ~/.local/share/applications.

    *install_dir* is the resolved game directory (e.g. from ``_get_install_folder``).
    *target* overrides the entry's primary launch target (entry_point / launch_args).
    *target_idx* selects the .desktop filename suffix (0 = primary, no suffix).
    """
    # Accept legacy install_path (parent dir) but prefer install_dir (full path).
    _install_dir = install_dir or install_path
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

    # Resolve the actual game directory from the DB-backed install_path
    # (which reflects moves) or fall back to the default platform directory.
    platform = getattr(entry, "platform", "windows")

    def _resolve_game_dir() -> Path:
        if _install_dir:
            return Path(_install_dir)
        if platform == "dos":
            from cellar.backend.umu import dos_dir
            return dos_dir() / entry.id
        if platform == "linux":
            from cellar.backend.umu import native_dir
            return native_dir() / entry.id
        from cellar.backend.umu import prefixes_dir
        return prefixes_dir() / entry.id

    game_dir = _resolve_game_dir()

    # Exec — branch on platform
    if platform == "dos":
        from cellar.backend.dosbox import build_dos_exec_line
        exec_line = _desktop_quote(
            build_dos_exec_line(game_dir, exe_path, launch_args)
        )
        comment = (entry.summary or f"Launch {entry.name}.").replace("\n", " ")
    elif platform == "linux":
        if exe_path:
            exe = game_dir / exe_path
            exec_line = _desktop_quote(str(exe))
            if launch_args:
                exec_line += f" {launch_args}"
        else:
            exec_line = "true"  # placeholder; entry point unknown
        comment = (entry.summary or f"Launch {entry.name}.").replace("\n", " ")
    else:
        # umu-launcher launch: always via flatpak run so the bundled umu-run
        # is available regardless of how the shortcut is invoked.
        from cellar.backend.umu import (  # noqa: PLC0415
            _umu_data_env,
            runners_dir,
        )

        steam_appid = getattr(entry, "steam_appid", None)
        gameid = f"umu-{steam_appid}" if steam_appid else "0"

        # Use the runner from launch overrides if set, otherwise the installed runner.
        runner_name = ""
        try:
            from cellar.backend import database as _db  # noqa: PLC0415
            rec = _db.get_installed(entry.id) or {}
            overrides = _db.get_launch_overrides(entry.id)
            runner_name = overrides.get("runner") or rec.get("runner") or ""
        except Exception:  # noqa: BLE001
            pass

        prefix = str(game_dir)
        proton = str(runners_dir() / runner_name) if runner_name else ""

        exe_arg = f" {_desktop_quote(exe_path)}" if exe_path else ""
        args_str = f" {launch_args}" if launch_args else ""

        umu_data = _umu_data_env()
        env_parts = (
            f"--env=WINEPREFIX={_desktop_quote(prefix)}"
            + (f" --env=PROTONPATH={_desktop_quote(proton)}" if proton else "")
            + f" --env=GAMEID={_desktop_quote(gameid)}"
            + f" --env=UMU_FOLDERS_PATH={_desktop_quote(umu_data['UMU_FOLDERS_PATH'])}"
        )
        exec_line = (
            f"flatpak run --command=umu-run {env_parts} io.github.cellar{exe_arg}{args_str}"
        )
        comment = (entry.summary or f"Launch {entry.name} via umu-launcher.").replace("\n", " ")

    # Categories
    xdg_cat = _CATEGORY_MAP.get(entry.category or "", "")
    categories = f"{xdg_cat};" if xdg_cat else "Game;"

    # Working directory for Path=.
    work_dir = ""
    if platform == "windows" and exe_path:
        from cellar.backend.umu import _win_to_linux_path  # noqa: PLC0415
        linux_exe = _win_to_linux_path(exe_path, str(game_dir))
        work_dir = str(Path(linux_exe).parent)
    elif platform == "linux" and exe_path:
        work_dir = str((game_dir / exe_path).parent)
    elif platform == "dos":
        work_dir = str(game_dir)

    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={_sanitize(desktop_name)}",
        f"Comment={_sanitize(comment)}",
        f"Icon={_sanitize(icon_ref)}",
        f"Exec={_sanitize(exec_line)}",
        "Terminal=false",
        f"Categories={categories}",
        f"StartupWMClass={_sanitize(entry.name)}",
        f"X-Cellar-AppId={entry.id}",
        f"X-Cellar-Platform={platform}",
    ]
    if work_dir:
        lines.append(f"Path={_sanitize(work_dir)}")

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
            path.unlink(missing_ok=True)
            log.info("Removed %s", path)
    else:
        # Remove primary and all numbered secondary .desktop files.
        # The secondary glob anchors to a digit so "doom" can't match "doom-bar".
        for p in _APPS_DIR.glob(f"cellar-{app_id}.desktop"):
            p.unlink(missing_ok=True)
            log.info("Removed %s", p)
        for p in _APPS_DIR.glob(f"cellar-{app_id}-[0-9]*.desktop"):
            p.unlink(missing_ok=True)
            log.info("Removed %s", p)
    _remove_icons(app_id)
    _refresh_desktop_db()

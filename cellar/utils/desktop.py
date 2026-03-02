"""XDG .desktop entry helpers for Cellar-managed apps."""

from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)

_APPS_DIR = Path.home() / ".local/share/applications"
_ICONS_DIR = Path.home() / ".local/share/icons/hicolor/256x256/apps"

_CATEGORY_MAP: dict[str, str] = {
    "Games": "Game",
    "Productivity": "Office",
    "Graphics": "Graphics",
    "Utility": "Utility",
}


def desktop_entry_path(app_id: str) -> Path:
    return _APPS_DIR / f"cellar-{app_id}.desktop"


def icon_dest_path(app_id: str) -> Path:
    return _ICONS_DIR / f"cellar-{app_id}.png"


def has_desktop_entry(app_id: str) -> bool:
    return desktop_entry_path(app_id).exists()


def _install_icon(app_id: str, src: str) -> str:
    """Copy/convert *src* into the hicolor 256×256 theme directory.

    Returns the icon name to embed in the .desktop file, or the XDG
    fallback ``application-x-executable`` on failure.
    """
    _ICONS_DIR.mkdir(parents=True, exist_ok=True)
    dest = icon_dest_path(app_id)
    try:
        from PIL import Image

        img = Image.open(src)
        if img.mode not in ("RGB", "RGBA"):
            img = img.convert("RGBA")
        img.thumbnail((256, 256), Image.LANCZOS)
        canvas = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
        canvas.paste(img, ((256 - img.width) // 2, (256 - img.height) // 2))
        canvas.save(dest, "PNG")
        log.debug("Installed icon for %s → %s", app_id, dest)
        return f"cellar-{app_id}"
    except Exception as exc:
        log.warning("Could not install icon for %s: %s", app_id, exc)
        return "application-x-executable"


def create_desktop_entry(
    entry,               # AppEntry — avoid circular import at module level
    bottle_name: str,
    program_name: str | None,
    is_flatpak: bool,
    icon_source: str | None = None,
) -> None:
    """Write a .desktop entry for *entry* to ~/.local/share/applications."""
    _APPS_DIR.mkdir(parents=True, exist_ok=True)

    # Icon
    icon_ref = "application-x-executable"
    if icon_source and Path(icon_source).is_file():
        icon_ref = _install_icon(entry.id, icon_source)

    # Exec
    cli = (
        "flatpak run --command=bottles-cli com.usebottles.bottles"
        if is_flatpak
        else "bottles-cli"
    )
    if program_name:
        exec_line = f"{cli} run -p '{program_name}' -b '{bottle_name}'"
    elif entry.entry_point:
        exec_line = f"{cli} run -e '{entry.entry_point}' -b '{bottle_name}'"
    else:
        exec_line = f"{cli} run -b '{bottle_name}'"

    # Categories
    xdg_cat = _CATEGORY_MAP.get(entry.category or "", "")
    categories = f"Application;{xdg_cat};" if xdg_cat else "Application;"

    comment = (entry.summary or f"Launch {entry.name} using Bottles.").replace("\n", " ")

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
    ]

    path = desktop_entry_path(entry.id)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    log.info("Created desktop entry: %s", path)


def remove_desktop_entry(app_id: str) -> None:
    """Remove the .desktop file and installed icon for *app_id*."""
    for p in (desktop_entry_path(app_id), icon_dest_path(app_id)):
        if p.exists():
            p.unlink()
            log.info("Removed %s", p)

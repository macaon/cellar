"""GApplication entry point for Cellar."""

import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(name)s %(levelname)s: %(message)s",
)
# smbprotocol is very chatty at INFO level (logs every read/write response).
logging.getLogger("smbprotocol").setLevel(logging.WARNING)
logging.getLogger("smbclient").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Secret", "1")
from gi.repository import Adw, Gio  # noqa: E402


class CellarApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="io.github.cellar",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )

    def do_activate(self):
        from gi.repository import Gdk, Gtk

        from cellar.utils.paths import icons_dir
        from cellar.window import CellarWindow

        display = Gdk.Display.get_default()
        if display:
            Gtk.IconTheme.get_for_display(display).add_search_path(icons_dir())

            css = Gtk.CssProvider()
            _css_data = (
                "viewswitcher indicator {"
                "  background-color: @accent_bg_color;"
                "  color: @accent_fg_color;"
                "}"
                ".screenshot-nav {"
                "  transition: opacity 150ms ease-in-out;"
                "}"
                ".screenshots-band {"
                "  background-color: @view_bg_color;"
                "  padding-top: 20px;"
                "  padding-bottom: 20px;"
                "  border-top: 1px solid alpha(@window_fg_color, 0.1);"
                "  border-bottom: 1px solid alpha(@window_fg_color, 0.1);"
                "}"
                ".screenshot-pic {"
                "  border-radius: 8px;"
                "  box-shadow: 0 2px 8px rgba(0,0,0,0.15), 0 1px 3px rgba(0,0,0,0.1);"
                "}"
                ".info-card-sep {"
                "  background-color: @card_shade_color;"
                "}"
                ".info-cell {"
                "  padding: 14px;"
                "}"
                ".info-cell-first {"
                "  border-radius: 12px 0 0 12px;"
                "}"
                ".info-cell-last {"
                "  border-radius: 0 12px 12px 0;"
                "}"
                ".download-pill {"
                "  border-radius: 9999px;"
                "  background-color: alpha(@window_fg_color, 0.12);"
                "  padding: 4px 12px;"
                "  font-weight: bold;"
                "  min-width: 72px;"
                "}"
                ".download-pill-large {"
                "  font-size: 1.15em;"
                "  padding: 6px 18px;"
                "}"
                ".info-cell-interactive {"
                "  transition: background-color 150ms ease;"
                "}"
                ".info-cell-interactive.hovered {"
                "  background-color: alpha(@window_fg_color, 0.07);"
                "}"
                "flowboxchild.app-card-cell {"
                "  background: transparent;"
                "  padding: 0;"
                "}"
                "flowboxchild.app-card-cell:hover {"
                "  background: transparent;"
                "}"
                "flowboxchild.ss-tile-cell {"
                "  padding: 0;"
                "  background: transparent;"
                "}"
                "flowboxchild.ss-tile-cell:hover {"
                "  background: transparent;"
                "}"
                "flowboxchild.ss-divider-child {"
                "  padding: 0;"
                "  background: transparent;"
                "}"
                ".ss-tile {"
                "  border-radius: 8px;"
                "  background-color: black;"
                "}"
                ".ss-tile.selected {"
                "  box-shadow: 0 0 0 3px @accent_bg_color;"
                "}"
                ".ss-tile-pic {"
                "  border-radius: 8px;"
                "}"
                ".ss-check-badge {"
                "  background-color: @accent_bg_color;"
                "  color: @accent_fg_color;"
                "  border-radius: 4px;"
                "  padding: 3px;"
                "  margin: 4px;"
                "  -gtk-icon-size: 14px;"
                "}"
                ".ss-steam-badge {"
                "  background-color: alpha(@window_bg_color, 0.72);"
                "  color: @window_fg_color;"
                "  border-radius: 4px;"
                "  padding: 3px;"
                "  margin: 4px;"
                "  -gtk-icon-size: 14px;"
                "}"
                ".filter-active-dot {"
                "  background-color: @accent_bg_color;"
                "  min-width: 8px;"
                "  min-height: 8px;"
                "  border-radius: 4px;"
                "}"
                ".logo-pic {"
                "  filter: drop-shadow(0 2px 8px rgba(0,0,0,0.3));"
                "}"
                ".image-row-thumb {"
                "  border-radius: 4px;"
                "}"
            )
            # load_from_string was added in GTK 4.12; use load_from_data for
            # compatibility with older distros (e.g. Pop_OS / Ubuntu 22.04).
            if hasattr(css, "load_from_string"):
                css.load_from_string(_css_data)
            else:
                css.load_from_data(_css_data.encode())
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        win = self.props.active_window
        if not win:
            win = CellarWindow(application=self)
        win.present()


def _ensure_desktop_entry() -> None:
    """Create ~/.local/share/applications entry + user icon on first pip-install run.

    Idempotent: does nothing if the .desktop file already exists.  Skipped
    inside Flatpak — the manifest installs the desktop file and icons at
    build time.  Errors are silenced so a permissions quirk can never
    prevent the app from launching.
    """
    if os.environ.get("FLATPAK_ID"):
        return

    import shutil

    from cellar.utils.paths import icons_dir

    desktop_dir = _XDG_DATA_HOME / "applications"
    desktop_file = desktop_dir / "io.github.cellar.desktop"
    if desktop_file.exists():
        return

    try:
        # Copy the app SVG icon into the user icon theme so GNOME picks it up.
        icon_src = (
            Path(icons_dir()) / "hicolor" / "512x512" / "apps" / "io.github.cellar.svg"
        )
        icon_name = "io.github.cellar"
        if icon_src.exists():
            icon_dest_dir = (
                _XDG_DATA_HOME / "icons" / "hicolor" / "scalable" / "apps"
            )
            icon_dest_dir.mkdir(parents=True, exist_ok=True)
            dest = icon_dest_dir / "io.github.cellar.svg"
            if not dest.exists():
                shutil.copy2(icon_src, dest)

        # Prefer the absolute path to the cellar script: desktop sessions often
        # don't inherit ~/.local/bin on their PATH.
        cellar_bin = Path(sys.executable).parent / "cellar"
        exec_cmd = str(cellar_bin) if cellar_bin.exists() else "cellar"

        desktop_dir.mkdir(parents=True, exist_ok=True)
        desktop_file.write_text(
            "[Desktop Entry]\n"
            "Name=Cellar\n"
            "Comment=A GNOME storefront for Windows and Linux applications\n"
            f"Exec={exec_cmd}\n"
            f"Icon={icon_name}\n"
            "Terminal=false\n"
            "Type=Application\n"
            "Categories=GNOME;GTK;Utility;\n"
            "StartupWMClass=io.github.cellar\n"
        )
    except Exception as exc:
        log.debug("Desktop entry creation failed: %s", exc)


_XDG_DATA_HOME = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
)


def main():
    _ensure_desktop_entry()
    app = CellarApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())

"""GApplication entry point for Cellar."""

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(name)s %(levelname)s: %(message)s",
)
# smbprotocol is very chatty at INFO level (logs every read/write response).
logging.getLogger("smbprotocol").setLevel(logging.WARNING)
logging.getLogger("smbclient").setLevel(logging.WARNING)

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio  # noqa: E402


class CellarApplication(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="io.github.cellar",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self):
        from gi.repository import Gdk, Gtk

        from cellar.utils.paths import icons_dir
        from cellar.window import CellarWindow

        display = Gdk.Display.get_default()
        if display:
            Gtk.IconTheme.get_for_display(display).add_search_path(icons_dir())

            css = Gtk.CssProvider()
            css.load_from_string(
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
                "  background-color: alpha(@window_fg_color, 0.08);"
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
                ".app-card {"
                "  box-shadow: 0 0 0 1px alpha(@window_fg_color, 0.09),"
                "              0 1px 3px rgba(0,0,0,0.05),"
                "              0 2px 8px rgba(0,0,0,0.07);"
                "}"
                ".logo-pic {"
                "  filter: drop-shadow(0 2px 8px rgba(0,0,0,0.3));"
                "}"
            )
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        win = self.props.active_window
        if not win:
            win = CellarWindow(application=self)
        win.present()


def main():
    app = CellarApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())

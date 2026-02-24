"""GApplication entry point for Cellar."""

import sys

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
        from cellar.window import CellarWindow

        win = self.props.active_window
        if not win:
            win = CellarWindow(application=self)
        win.present()


def main():
    app = CellarApplication()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())

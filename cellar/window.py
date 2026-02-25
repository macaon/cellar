"""Main application window."""

from __future__ import annotations

import logging
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk

from cellar.utils.paths import ui_file

log = logging.getLogger(__name__)


@Gtk.Template(filename=ui_file("window.ui"))
class CellarWindow(Adw.ApplicationWindow):
    __gtype_name__ = "CellarWindow"

    search_button: Gtk.ToggleButton = Gtk.Template.Child()
    search_bar: Gtk.SearchBar = Gtk.Template.Child()
    search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    refresh_button: Gtk.Button = Gtk.Template.Child()
    menu_button: Gtk.MenuButton = Gtk.Template.Child()
    main_content: Gtk.Box = Gtk.Template.Child()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        self.search_bar.set_key_capture_widget(self)
        self.search_button.connect("toggled", self._on_search_toggled)
        self.search_bar.connect(
            "notify::search-mode-enabled", self._on_search_mode_changed
        )
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.refresh_button.connect("clicked", self._on_refresh_clicked)

        # Register win.preferences action (used by the hamburger menu item).
        prefs_action = Gio.SimpleAction.new("preferences", None)
        prefs_action.connect("activate", self._on_preferences_activated)
        self.add_action(prefs_action)

        # Register app.about action so the menu item doesn't crash.
        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about_activated)
        self.get_application().add_action(about_action)

        from cellar.views.browse import BrowseView

        self._browse = BrowseView()
        self._browse.set_vexpand(True)
        self.main_content.append(self._browse)

        self.connect("realize", lambda _w: self._load_catalogue())

    # ── Catalogue loading ─────────────────────────────────────────────────

    def _load_catalogue(self) -> None:
        from cellar.backend.config import load_repos
        from cellar.backend.repo import Repo, RepoError, RepoManager

        manager = RepoManager()

        # CELLAR_REPO env var acts as a dev/testing override on top of config.
        env_uri = os.environ.get("CELLAR_REPO", "")
        if env_uri:
            try:
                manager.add(Repo(env_uri))
            except RepoError as exc:
                log.warning("CELLAR_REPO %r is invalid: %s", env_uri, exc)

        for cfg in load_repos():
            try:
                manager.add(Repo(cfg["uri"], ssh_identity=cfg.get("ssh_identity")))
            except RepoError as exc:
                log.warning("Configured repo %r invalid: %s", cfg["uri"], exc)

        if not list(manager):
            self._browse.show_error(
                "No Repository Configured",
                "Open Preferences (the menu in the top-right corner) "
                "to add a repository source.",
            )
            return

        self.refresh_button.set_sensitive(False)
        try:
            entries = manager.fetch_all_catalogues()
            if entries:
                self._browse.load_entries(entries)
            else:
                self._browse.show_error(
                    "Empty Catalogue",
                    "No apps found in any configured repository.",
                )
        except Exception as exc:
            log.error("Failed to load catalogue: %s", exc)
            self._browse.show_error("Could Not Load Repository", str(exc))
        finally:
            self.refresh_button.set_sensitive(True)

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_preferences_activated(self, _action, _param) -> None:
        from cellar.views.settings import SettingsDialog

        dialog = SettingsDialog(on_repos_changed=self._load_catalogue)
        dialog.present(self)

    def _on_about_activated(self, _action, _param) -> None:
        dialog = Adw.AboutDialog(
            application_name="Cellar",
            application_icon="application-x-executable",
            version="0.4.0",
            comments="A GNOME storefront for Bottles-managed Windows apps.",
            license_type=Gtk.License.GPL_3_0,
        )
        dialog.present(self)

    def _on_search_toggled(self, button: Gtk.ToggleButton) -> None:
        self.search_bar.set_search_mode(button.get_active())

    def _on_search_mode_changed(self, bar: Gtk.SearchBar, _param) -> None:
        self.search_button.set_active(bar.get_search_mode())

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._browse.set_search_text(entry.get_text())

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self._load_catalogue()

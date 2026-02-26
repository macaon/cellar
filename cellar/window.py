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

    nav_view: Adw.NavigationView = Gtk.Template.Child()
    add_button: Gtk.Button = Gtk.Template.Child()
    search_button: Gtk.ToggleButton = Gtk.Template.Child()
    search_bar: Gtk.SearchBar = Gtk.Template.Child()
    search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    refresh_button: Gtk.Button = Gtk.Template.Child()
    menu_button: Gtk.MenuButton = Gtk.Template.Child()
    toast_overlay: Adw.ToastOverlay = Gtk.Template.Child()
    main_content: Gtk.Box = Gtk.Template.Child()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # The first successfully loaded Repo — used to resolve asset URIs in
        # the detail view.  Updated on every catalogue reload.
        self._first_repo = None

        self.search_bar.set_key_capture_widget(self)
        self.add_button.connect("clicked", self._on_add_app_clicked)
        self.search_button.connect("toggled", self._on_search_toggled)
        self.search_bar.connect(
            "notify::search-mode-enabled", self._on_search_mode_changed
        )
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.refresh_button.connect("clicked", self._on_refresh_clicked)

        prefs_action = Gio.SimpleAction.new("preferences", None)
        prefs_action.connect("activate", self._on_preferences_activated)
        self.add_action(prefs_action)

        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._on_about_activated)
        self.get_application().add_action(about_action)

        from cellar.views.browse import BrowseView

        self._browse = BrowseView()
        self._browse.set_vexpand(True)
        self._browse.connect("app-selected", self._on_app_selected)
        self.main_content.append(self._browse)

        self.connect("realize", lambda _w: self._load_catalogue())

    # ── Catalogue loading ─────────────────────────────────────────────────

    def _load_catalogue(self) -> None:
        from cellar.backend.config import load_repos
        from cellar.backend.repo import Repo, RepoError, RepoManager

        manager = RepoManager()
        self._first_repo = None

        env_uri = os.environ.get("CELLAR_REPO", "")
        if env_uri:
            try:
                r = Repo(env_uri)
                manager.add(r)
                self._first_repo = self._first_repo or r
            except RepoError as exc:
                log.warning("CELLAR_REPO %r is invalid: %s", env_uri, exc)

        for cfg in load_repos():
            try:
                r = Repo(cfg["uri"], ssh_identity=cfg.get("ssh_identity"))
                manager.add(r)
                self._first_repo = self._first_repo or r
            except RepoError as exc:
                log.warning("Configured repo %r invalid: %s", cfg["uri"], exc)

        can_add = self._first_repo is not None and self._first_repo.is_writable
        self.add_button.set_visible(can_add)

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

    def _on_app_selected(self, _browse, entry) -> None:
        from cellar.views.detail import DetailView

        resolver = self._first_repo.resolve_asset_uri if self._first_repo else None
        can_write = self._first_repo is not None and self._first_repo.is_writable

        def _on_edit(selected_entry):
            from cellar.views.edit_app import EditAppDialog

            EditAppDialog(
                entry=selected_entry,
                repo=self._first_repo,
                on_done=self._load_catalogue,
                on_deleted=self._on_entry_deleted,
            ).present(self)

        detail = DetailView(
            entry,
            resolve_asset=resolver,
            is_writable=can_write,
            on_edit=_on_edit if can_write else None,
        )
        page = Adw.NavigationPage(title=entry.name, child=detail)
        self.nav_view.push(page)

    def _on_entry_deleted(self) -> None:
        self.nav_view.pop()
        self._load_catalogue()

    def _on_add_app_clicked(self, _button) -> None:
        chooser = Gtk.FileChooserNative(
            title="Select Bottles Backup",
            transient_for=self,
            action=Gtk.FileChooserAction.OPEN,
        )
        f = Gtk.FileFilter()
        f.set_name("Bottles backup (*.tar.gz)")
        f.add_pattern("*.tar.gz")
        chooser.add_filter(f)
        chooser.connect("response", self._on_archive_chosen, chooser)
        chooser.show()

    def _on_archive_chosen(self, _chooser, response, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        archive_path = chooser.get_file().get_path()
        from cellar.views.add_app import AddAppDialog

        dialog = AddAppDialog(
            archive_path=archive_path,
            repo=self._first_repo,
            on_done=self._load_catalogue,
        )
        dialog.present(self)

    def _on_preferences_activated(self, _action, _param) -> None:
        from cellar.views.settings import SettingsDialog

        dialog = SettingsDialog(on_repos_changed=self._load_catalogue)
        dialog.present(self)

    def _on_about_activated(self, _action, _param) -> None:
        dialog = Adw.AboutDialog(
            application_name="Cellar",
            application_icon="application-x-executable",
            version="0.7.4",
            comments="A GNOME storefront for Bottles-managed Windows apps.",
            license_type=Gtk.License.GPL_3_0,
        )
        dialog.present(self)

    def _show_toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message))

    def _on_search_toggled(self, button: Gtk.ToggleButton) -> None:
        self.search_bar.set_search_mode(button.get_active())

    def _on_search_mode_changed(self, bar: Gtk.SearchBar, _param) -> None:
        self.search_button.set_active(bar.get_search_mode())

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._browse.set_search_text(entry.get_text())

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self._load_catalogue()

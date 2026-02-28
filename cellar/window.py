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
    explore_box: Gtk.Box = Gtk.Template.Child()
    installed_box: Gtk.Box = Gtk.Template.Child()
    updates_box: Gtk.Box = Gtk.Template.Child()
    updates_page: Adw.ViewStackPage = Gtk.Template.Child()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # The first successfully loaded Repo — used to resolve asset URIs in
        # the detail view.  Updated on every catalogue reload.
        self._first_repo = None
        # All writable repos from the last catalogue load — passed to AddAppDialog
        # so the user can choose which one to add a package to.
        self._writable_repos: list = []

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

        self._browse_explore = BrowseView(
            empty_title="No Repository Configured",
            empty_description=(
                "Open Preferences (the menu in the top-right corner) "
                "to add a repository source."
            ),
        )
        self._browse_explore.set_vexpand(True)
        self._browse_explore.connect("app-selected", self._on_app_selected)
        self.explore_box.append(self._browse_explore)

        self._browse_installed = BrowseView(
            empty_title="Nothing Installed",
            empty_description="Apps you install will appear here.",
        )
        self._browse_installed.set_vexpand(True)
        self._browse_installed.connect("app-selected", self._on_app_selected)
        self.installed_box.append(self._browse_installed)

        self._browse_updates = BrowseView(
            empty_title="Up to Date",
            empty_description="All installed apps are up to date.",
        )
        self._browse_updates.set_vexpand(True)
        self._browse_updates.connect("app-selected", self._on_app_selected)
        self.updates_box.append(self._browse_updates)

        self.connect("realize", lambda _w: self._load_catalogue())

    # ── Catalogue loading ─────────────────────────────────────────────────

    def _load_catalogue(self) -> None:
        from cellar.backend.config import load_repos
        from cellar.backend.repo import Repo, RepoError, RepoManager

        # A single MountOperation with this window as parent covers all repos
        # in this load pass.  Gtk.MountOperation shows credential dialogs and
        # saves accepted passwords to the GNOME Keyring automatically.
        mount_op = Gtk.MountOperation(parent=self)

        manager = RepoManager()
        self._first_repo = None
        self._writable_repos = []

        env_uri = os.environ.get("CELLAR_REPO", "")
        if env_uri:
            try:
                r = Repo(env_uri, mount_op=mount_op)
                manager.add(r)
                self._first_repo = self._first_repo or r
            except RepoError as exc:
                log.warning("CELLAR_REPO %r is invalid: %s", env_uri, exc)

        for cfg in load_repos():
            try:
                r = Repo(
                    cfg["uri"],
                    cfg.get("name", ""),
                    ssh_identity=cfg.get("ssh_identity"),
                    mount_op=mount_op,
                )
                manager.add(r)
                self._first_repo = self._first_repo or r
            except RepoError as exc:
                log.warning("Configured repo %r invalid: %s", cfg["uri"], exc)

        self._writable_repos = [r for r in manager if r.is_writable]
        self.add_button.set_visible(bool(self._writable_repos))

        if not list(manager):
            self._browse_explore.show_error(
                "No Repository Configured",
                "Open Preferences (the menu in the top-right corner) "
                "to add a repository source.",
            )
            self._browse_installed.load_entries([])
            self._browse_updates.load_entries([])
            self.updates_page.set_badge_number(0)
            return

        self.refresh_button.set_sensitive(False)
        try:
            entries = manager.fetch_all_catalogues()
            if entries:
                from cellar.backend import database
                resolver = self._first_repo.resolve_asset_uri if self._first_repo else None

                installed_entries = []
                update_entries = []
                for e in entries:
                    rec = database.get_installed(e.id)
                    if rec is not None:
                        installed_entries.append(e)
                        if rec.get("installed_version") != e.version and bool(e.archive):
                            update_entries.append(e)

                self._browse_explore.load_entries(entries, resolve_asset=resolver)
                self._browse_installed.load_entries(installed_entries, resolve_asset=resolver)
                self._browse_updates.load_entries(update_entries, resolve_asset=resolver)
                self.updates_page.set_badge_number(len(update_entries))
            else:
                self._browse_explore.show_error(
                    "Empty Catalogue",
                    "No apps found in any configured repository.",
                )
                self._browse_installed.load_entries([])
                self._browse_updates.load_entries([])
                self.updates_page.set_badge_number(0)
        except Exception as exc:
            log.error("Failed to load catalogue: %s", exc)
            self._browse_explore.show_error("Could Not Load Repository", str(exc))
            self._browse_installed.load_entries([])
            self._browse_updates.load_entries([])
            self.updates_page.set_badge_number(0)
        finally:
            self.refresh_button.set_sensitive(True)

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_app_selected(self, _browse, entry) -> None:
        from cellar.views.detail import DetailView
        from cellar.backend import database
        from cellar.backend.bottles import detect_all_bottles
        from cellar.backend.config import load_bottles_data_path

        resolver = self._first_repo.resolve_asset_uri if self._first_repo else None
        can_write = self._first_repo is not None and self._first_repo.is_writable

        all_bottles = detect_all_bottles(load_bottles_data_path())

        # Reconcile DB against disk: if the bottle directory no longer exists
        # in any detected Bottles installation (e.g. deleted from within
        # Bottles), remove the stale record so we show "Install" again.
        rec = database.get_installed(entry.id)
        if rec and all_bottles and not any(
            (b.data_path / rec["bottle_name"]).is_dir() for b in all_bottles
        ):
            log.info(
                "Bottle %r no longer exists on disk; removing stale DB record for %r",
                rec["bottle_name"], entry.id,
            )
            database.remove_installed(entry.id)
            rec = None
        is_installed = rec is not None

        def _on_edit(selected_entry):
            from cellar.views.edit_app import EditAppDialog

            def _on_edit_done():
                self.nav_view.pop()
                self._load_catalogue()

            EditAppDialog(
                entry=selected_entry,
                repo=self._first_repo,
                on_done=_on_edit_done,
                on_deleted=self._on_entry_deleted,
            ).present(self)

        def _on_install_done(bottle_name: str) -> None:
            repo_uri = str(self._first_repo.uri) if self._first_repo else ""
            database.mark_installed(entry.id, bottle_name, entry.version, repo_uri)
            self._show_toast(f"{entry.name} installed successfully")

        def _on_remove_done() -> None:
            self._show_toast(f"{entry.name} removed")

        def _on_update_done() -> None:
            repo_uri = str(self._first_repo.uri) if self._first_repo else ""
            database.mark_installed(entry.id, rec["bottle_name"], entry.version, repo_uri)
            self._show_toast(f"{entry.name} updated successfully")

        detail = DetailView(
            entry,
            resolve_asset=resolver,
            is_writable=can_write,
            on_edit=_on_edit if can_write else None,
            bottles_installs=all_bottles,
            is_installed=is_installed,
            installed_record=rec,
            on_install_done=_on_install_done,
            on_remove_done=_on_remove_done,
            on_update_done=_on_update_done,
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
            repos=self._writable_repos,
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
            version="0.12.4",
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
        text = entry.get_text()
        self._browse_explore.set_search_text(text)
        self._browse_installed.set_search_text(text)
        self._browse_updates.set_search_text(text)

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self._load_catalogue()

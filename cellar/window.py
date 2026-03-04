"""Main application window."""

from __future__ import annotations

import logging
import os
import threading

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
    filter_button: Gtk.MenuButton = Gtk.Template.Child()
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

        # Category toggle buttons in the filter popover — rebuilt after each catalogue load.
        self._category_btns: dict[str, Gtk.ToggleButton] = {}
        self._active_category: str = ""

        # The first successfully loaded Repo — used to resolve asset URIs in
        # the browse grid and callbacks.  Updated on every catalogue reload.
        self._first_repo = None
        # All writable repos from the last catalogue load — passed to AddAppDialog
        # so the user can choose which one to add a package to.
        self._writable_repos: list = []
        # All repos (writable + read-only) — passed to SettingsDialog so it can
        # list bases available in any repo, not just writable ones.
        self._all_repos: list = []
        # Maps entry.id → list of Repo objects that carry the entry, for the
        # source selector shown in the detail view.
        self._entry_repos: dict = {}

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

        # Sync the component index (runner download URLs) in the background.
        from cellar.backend import components
        threading.Thread(target=components.sync_index, daemon=True).start()

    # ── Catalogue loading ─────────────────────────────────────────────────

    def _load_catalogue(self) -> None:
        from cellar.backend.config import load_repos
        from cellar.backend.repo import Repo, RepoError, RepoManager  # noqa: F401

        # A single MountOperation with this window as parent covers all repos
        # in this load pass.  Gtk.MountOperation shows credential dialogs and
        # saves accepted passwords to the GNOME Keyring automatically.
        mount_op = Gtk.MountOperation(parent=self)

        manager = RepoManager()
        self._first_repo = None
        self._writable_repos = []
        self._category_btns = {}
        self._active_category = ""
        self.filter_button.remove_css_class("suggested-action")

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
                ca_cert_name = cfg.get("ca_cert") or None
                ca_cert_path: str | None = None
                if ca_cert_name:
                    from cellar.backend.config import certs_dir
                    resolved = certs_dir() / ca_cert_name
                    ca_cert_path = str(resolved) if resolved.exists() else None
                    if not ca_cert_path:
                        log.warning("CA cert %r not found in certs dir; ignoring", ca_cert_name)
                r = Repo(
                    cfg["uri"],
                    cfg.get("name", ""),
                    ssh_identity=cfg.get("ssh_identity"),
                    mount_op=mount_op,
                    ssl_verify=cfg.get("ssl_verify", True),
                    ca_cert=ca_cert_path,
                    token=cfg.get("token") or None,
                )
                manager.add(r)
                self._first_repo = self._first_repo or r
            except RepoError as exc:
                log.warning("Configured repo %r invalid: %s", cfg["uri"], exc)

        self._writable_repos = [r for r in manager if r.is_writable]
        self._all_repos = list(manager)
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
            self._rebuild_filter_popover([])
            return

        self.refresh_button.set_sensitive(False)

        # Fetch each repo individually so we can track which repo carries each
        # entry (for the source selector in the detail view).
        self._entry_repos = {}
        all_entries: dict = {}
        for repo in manager:
            try:
                for e in repo.fetch_catalogue():
                    all_entries[e.id] = e  # last-repo-wins
                    self._entry_repos.setdefault(e.id, []).append(repo)
            except RepoError as exc:
                log.warning("Failed to fetch from %s: %s", repo.uri, exc)
        entries = list(all_entries.values())

        try:
            if entries:
                from cellar.backend import database
                from cellar.backend.bottles import detect_all_bottles
                from cellar.backend.config import load_bottles_data_path
                resolver = self._first_repo.resolve_asset_uri if self._first_repo else None

                all_bottles = detect_all_bottles(load_bottles_data_path())

                installed_entries = []
                update_entries = []
                for e in entries:
                    rec = database.get_installed(e.id)
                    if rec is None:
                        continue
                    # Reconcile against disk: remove stale records where the
                    # installed directory has been deleted outside Cellar.
                    bottle_name = rec.get("bottle_name", "")
                    if e.platform == "linux":
                        install_path = rec.get("install_path", "")
                        if install_path and bottle_name:
                            from pathlib import Path as _Path  # noqa: PLC0415
                            if not (_Path(install_path) / bottle_name).is_dir():
                                log.info(
                                    "Linux app dir %r gone from disk; removing stale record for %r",
                                    bottle_name, e.id,
                                )
                                database.remove_installed(e.id)
                                continue
                    elif all_bottles and bottle_name and not any(
                        (b.data_path / bottle_name).is_dir() for b in all_bottles
                    ):
                        log.info(
                            "Bottle %r gone from disk; removing stale record for %r",
                            bottle_name, e.id,
                        )
                        database.remove_installed(e.id)
                        continue
                    installed_entries.append(e)
                    if rec.get("installed_version") != e.version and bool(e.archive):
                        update_entries.append(e)

                installed_ids = {e.id for e in installed_entries}
                self._browse_explore.load_entries(entries, resolve_asset=resolver, installed_ids=installed_ids)
                self._browse_installed.load_entries(installed_entries, resolve_asset=resolver, installed_ids=installed_ids)
                self._browse_updates.load_entries(update_entries, resolve_asset=resolver)
                self.updates_page.set_badge_number(len(update_entries))
                self._rebuild_filter_popover(entries)
            else:
                self._browse_explore.show_error(
                    "Empty Catalogue",
                    "No apps found in any configured repository.",
                )
                self._browse_installed.load_entries([])
                self._browse_updates.load_entries([])
                self.updates_page.set_badge_number(0)
                self._rebuild_filter_popover([])
        except Exception as exc:
            log.error("Failed to load catalogue: %s", exc)
            self._browse_explore.show_error("Could Not Load Repository", str(exc))
            self._browse_installed.load_entries([])
            self._browse_updates.load_entries([])
            self.updates_page.set_badge_number(0)
        finally:
            self.refresh_button.set_sensitive(True)

    # ── Filter popover ────────────────────────────────────────────────────

    def _rebuild_filter_popover(self, entries: list) -> None:
        """Build (or rebuild) the category filter popover from the current entry list."""
        categories = sorted({e.category for e in entries if e.category})
        self._category_btns = {}
        self._active_category = ""

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_top(4)
        outer.set_margin_bottom(4)
        outer.set_margin_start(4)
        outer.set_margin_end(4)

        if not categories:
            empty_lbl = Gtk.Label(label="No categories available")
            empty_lbl.add_css_class("dim-label")
            empty_lbl.set_margin_top(8)
            empty_lbl.set_margin_bottom(8)
            outer.append(empty_lbl)
        else:
            # "All" button clears the filter
            all_btn = Gtk.Button(label="All")
            all_btn.add_css_class("flat")
            all_btn.connect("clicked", self._on_filter_clear)
            outer.append(all_btn)

            outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

            for cat in categories:
                btn = Gtk.ToggleButton(label=cat)
                btn.add_css_class("flat")
                btn.connect("toggled", self._on_category_toggled, cat)
                self._category_btns[cat] = btn
                outer.append(btn)

        popover = Gtk.Popover()
        popover.set_child(outer)
        self.filter_button.set_popover(popover)

    def _on_category_toggled(self, btn: Gtk.ToggleButton, category: str) -> None:
        if not btn.get_active():
            # Ignore the de-activate signal when we programmatically untoggle
            return
        # Untoggle all others
        for cat, other in self._category_btns.items():
            if cat != category:
                other.handler_block_by_func(self._on_category_toggled)
                other.set_active(False)
                other.handler_unblock_by_func(self._on_category_toggled)
        self._active_category = category
        self.filter_button.add_css_class("suggested-action")
        self._browse_explore.set_active_category(category)
        self._browse_installed.set_active_category(category)
        self._browse_updates.set_active_category(category)
        self.filter_button.get_popover().popdown()

    def _on_filter_clear(self, _button: Gtk.Button) -> None:
        for btn in self._category_btns.values():
            btn.handler_block_by_func(self._on_category_toggled)
            btn.set_active(False)
            btn.handler_unblock_by_func(self._on_category_toggled)
        self._active_category = ""
        self.filter_button.remove_css_class("suggested-action")
        self._browse_explore.set_active_category("")
        self._browse_installed.set_active_category("")
        self._browse_updates.set_active_category("")
        self.filter_button.get_popover().popdown()

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_app_selected(self, _browse, entry) -> None:
        from cellar.views.detail import DetailView
        from cellar.backend import database
        from cellar.backend.bottles import detect_all_bottles
        from cellar.backend.config import load_bottles_data_path

        source_repos = self._entry_repos.get(entry.id, [])
        if not source_repos and self._first_repo:
            source_repos = [self._first_repo]
        can_write = self._first_repo is not None and self._first_repo.is_writable

        all_bottles = detect_all_bottles(load_bottles_data_path())

        # Reconcile DB against disk: remove stale records where the installed
        # directory has been deleted outside Cellar.
        rec = database.get_installed(entry.id)
        if rec:
            bottle_name = rec.get("bottle_name", "")
            if entry.platform == "linux":
                install_path = rec.get("install_path", "")
                if install_path and bottle_name:
                    from pathlib import Path as _Path  # noqa: PLC0415
                    if not (_Path(install_path) / bottle_name).is_dir():
                        log.info(
                            "Linux app dir %r gone from disk; removing stale DB record for %r",
                            bottle_name, entry.id,
                        )
                        database.remove_installed(entry.id)
                        rec = None
            elif all_bottles and bottle_name and not any(
                (b.data_path / bottle_name).is_dir() for b in all_bottles
            ):
                log.info(
                    "Bottle %r no longer exists on disk; removing stale DB record for %r",
                    bottle_name, entry.id,
                )
                database.remove_installed(entry.id)
                rec = None
        is_installed = rec is not None

        def _on_edit(selected_entry):
            from cellar.views.edit_app import EditAppDialog
            from cellar.views.detail import DetailView

            def _on_edit_done(updated_entry):
                self._load_catalogue()
                current_page = self.nav_view.get_visible_page()
                if current_page is not None:
                    new_detail = DetailView(
                        updated_entry,
                        source_repos=self._entry_repos.get(updated_entry.id) or source_repos,
                        is_writable=can_write,
                        on_edit=_on_edit if can_write else None,
                        bottles_installs=all_bottles,
                        is_installed=is_installed,
                        installed_record=rec,
                        on_install_done=_on_install_done,
                        on_remove_done=_on_remove_done,
                        on_update_done=_on_update_done,
                    )
                    current_page.set_child(new_detail)
                    current_page.set_title(updated_entry.name)
                self._show_toast("Entry updated")

            EditAppDialog(
                entry=selected_entry,
                repo=self._first_repo,
                on_done=_on_edit_done,
                on_deleted=self._on_entry_deleted,
            ).present(self)

        def _on_install_done(bottle_name: str, install_path: str = "") -> None:
            repo_uri = str(self._first_repo.uri) if self._first_repo else ""
            database.mark_installed(
                entry.id, bottle_name, entry.version, repo_uri,
                platform=entry.platform,
                install_path=install_path,
            )
            self._show_toast(f"{entry.name} installed successfully")
            self._load_catalogue()

        def _on_remove_done() -> None:
            self._show_toast(f"{entry.name} removed")
            self._load_catalogue()

        def _on_update_done() -> None:
            repo_uri = str(self._first_repo.uri) if self._first_repo else ""
            database.mark_installed(entry.id, rec["bottle_name"], entry.version, repo_uri)
            self._show_toast(f"{entry.name} updated successfully")
            self._load_catalogue()

        detail = DetailView(
            entry,
            source_repos=source_repos,
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
        dialog = Adw.AlertDialog(
            heading="Add App to Catalogue",
            body="Choose the type of app to add.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("bottles", "Bottles App")
        dialog.add_response("linux", "Linux Native App")
        dialog.set_default_response("bottles")
        dialog.connect("response", self._on_add_type_chosen)
        dialog.present(self)

    def _on_add_type_chosen(self, _dialog, response) -> None:
        if response == "bottles":
            self._open_bottles_chooser()
        elif response == "linux":
            self._open_linux_dir_chooser()

    def _open_bottles_chooser(self) -> None:
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

    def _open_linux_dir_chooser(self) -> None:
        chooser = Gtk.FileChooserNative(
            title="Select App Directory",
            transient_for=self,
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        chooser.connect("response", self._on_linux_dir_chosen, chooser)
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

    def _on_linux_dir_chosen(self, _chooser, response, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        dir_path = chooser.get_file().get_path()
        from cellar.views.add_app import AddAppDialog

        dialog = AddAppDialog(
            source_dir=dir_path,
            repos=self._writable_repos,
            on_done=self._load_catalogue,
        )
        dialog.present(self)

    def _on_preferences_activated(self, _action, _param) -> None:
        from cellar.views.settings import SettingsDialog

        dialog = SettingsDialog(
            on_repos_changed=self._load_catalogue,
            writable_repos=self._writable_repos,
            all_repos=self._all_repos,
        )
        dialog.present(self)

    def _on_about_activated(self, _action, _param) -> None:
        dialog = Adw.AboutDialog(
            application_name="Cellar",
            application_icon="application-x-executable",
            version="0.34.4",
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

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


def _has_update(installed_rec: dict, entry) -> bool:
    """Return True if the catalogue entry's archive differs from what is installed.

    Compares CRC32 checksums — content-addressed, so any change to the
    archive (DLC, patch, rebuild) triggers an update without a manual version
    bump.  Returns False when either side has no CRC (entry not yet packaged,
    or record pre-dates the archive_crc32 column).
    """
    cat_crc = entry.archive_crc32 or ""
    stored_crc = installed_rec.get("archive_crc32") or ""
    return bool(cat_crc and stored_crc and cat_crc != stored_crc)


def _reconcile_installed_record(entry) -> dict | None:
    """Return the DB record for *entry* if still valid on disk, else ``None``.

    Removes the record and returns ``None`` if the installed directory has
    been deleted outside Cellar (stale record).
    """
    from cellar.backend import database  # noqa: PLC0415
    from cellar.backend.umu import prefixes_dir, native_dir  # noqa: PLC0415

    rec = database.get_installed(entry.id)
    if rec is None:
        return None
    prefix_dir = rec.get("prefix_dir", "")
    if entry.platform == "linux":
        if not (native_dir() / entry.id).is_dir():
            log.info(
                "Linux app dir %r gone from disk; removing stale record for %r",
                entry.id, entry.id,
            )
            database.remove_installed(entry.id)
            return None
    elif prefix_dir and not (prefixes_dir() / prefix_dir).is_dir():
        log.info(
            "Prefix %r gone from disk; removing stale record for %r",
            prefix_dir, entry.id,
        )
        database.remove_installed(entry.id)
        return None
    return rec


@Gtk.Template(filename=ui_file("window.ui"))
class CellarWindow(Adw.ApplicationWindow):
    __gtype_name__ = "CellarWindow"

    nav_view: Adw.NavigationView = Gtk.Template.Child()
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
    builder_box: Gtk.Box = Gtk.Template.Child()
    builder_page: Adw.ViewStackPage = Gtk.Template.Child()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # Category check buttons in the filter popover — rebuilt after each catalogue load.
        self._category_btns: dict[str, Gtk.CheckButton] = {}
        self._active_category: str = ""

        # The first successfully loaded Repo — used to resolve asset URIs in
        # the browse grid and callbacks.  Updated on every catalogue reload.
        self._first_repo = None
        self._writable_repos: list = []
        self._all_repos: list = []
        # Maps entry.id → list of Repo objects that carry the entry, for the
        # source selector shown in the detail view.
        self._entry_repos: dict = {}

        self.search_bar.set_key_capture_widget(self)
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

        from cellar.views.package_builder import PackageBuilderView
        self._package_builder = PackageBuilderView(
            on_catalogue_changed=self._load_catalogue,
        )
        self._package_builder.set_vexpand(True)
        self.builder_box.append(self._package_builder)

        self.connect("realize", lambda _w: self._load_catalogue())

        # Pre-warm the GE-Proton release list in the background.
        from cellar.backend import runners
        threading.Thread(target=runners.fetch_releases, daemon=True).start()

    # ── Catalogue loading ─────────────────────────────────────────────────

    def _load_catalogue(self) -> None:
        from cellar.backend.config import load_repos, load_smb_password
        from cellar.backend.repo import Repo, RepoError, RepoManager  # noqa: F401

        manager = RepoManager()
        self._first_repo = None
        self._writable_repos = []
        self._category_btns = {}
        self._active_category = ""
        self.filter_button.remove_css_class("suggested-action")

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
                ca_cert_name = cfg.get("ca_cert") or None
                ca_cert_path: str | None = None
                if ca_cert_name:
                    from cellar.backend.config import certs_dir
                    resolved = certs_dir() / ca_cert_name
                    ca_cert_path = str(resolved) if resolved.exists() else None
                    if not ca_cert_path:
                        log.warning("CA cert %r not found in certs dir; ignoring", ca_cert_name)
                smb_username = cfg.get("smb_username") or None
                smb_password = load_smb_password(cfg["uri"]) if smb_username else None
                r = Repo(
                    cfg["uri"],
                    cfg.get("name", ""),
                    ssh_identity=cfg.get("ssh_identity"),
                    ssl_verify=cfg.get("ssl_verify", True),
                    ca_cert=ca_cert_path,
                    token=cfg.get("token") or None,
                    smb_username=smb_username,
                    smb_password=smb_password,
                )
                manager.add(r)
                self._first_repo = self._first_repo or r
            except RepoError as exc:
                log.warning("Configured repo %r invalid: %s", cfg["uri"], exc)

        self._writable_repos = [r for r in manager if r.is_writable]
        self._all_repos = list(manager)
        self.builder_page.set_visible(bool(self._writable_repos))
        self._package_builder.update_repos(self._writable_repos, all_repos=self._all_repos)

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
                resolver = self._first_repo.resolve_asset_uri if self._first_repo else None

                installed_entries = []
                update_entries = []
                for e in entries:
                    rec = _reconcile_installed_record(e)
                    if rec is None:
                        continue
                    installed_entries.append(e)
                    if bool(e.archive) and _has_update(rec, e):
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

            group_anchor: Gtk.CheckButton | None = None
            for cat in categories:
                btn = Gtk.CheckButton(label=cat)
                btn.add_css_class("flat")
                if group_anchor is None:
                    group_anchor = btn
                else:
                    btn.set_group(group_anchor)
                btn.connect("toggled", self._on_category_toggled, cat)
                self._category_btns[cat] = btn
                outer.append(btn)

        popover = Gtk.Popover()
        popover.set_child(outer)
        self.filter_button.set_popover(popover)

    def _on_category_toggled(self, btn: Gtk.CheckButton, category: str) -> None:
        if not btn.get_active():
            # Ignore the deactivation signal fired by the radio group.
            return
        self._active_category = category
        self.filter_button.add_css_class("suggested-action")
        self._browse_explore.set_active_category(category)
        self._browse_installed.set_active_category(category)
        self._browse_updates.set_active_category(category)
        self.filter_button.get_popover().popdown()

    def _on_filter_clear(self, _button: Gtk.Button) -> None:
        for btn in self._category_btns.values():
            btn.set_active(False)
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

        source_repos = self._entry_repos.get(entry.id, [])
        if not source_repos and self._first_repo:
            source_repos = [self._first_repo]
        can_write = self._first_repo is not None and self._first_repo.is_writable

        rec = _reconcile_installed_record(entry)
        is_installed = rec is not None

        def _on_edit(selected_entry):
            from cellar.views.edit_app import EditAppDialog
            from cellar.views.detail import DetailView

            def _on_edit_done(updated_entry):
                import dataclasses as _dc
                try:
                    icons = self._first_repo.fetch_category_icons() if self._first_repo else {}
                except Exception:
                    icons = {}
                icon = icons.get(updated_entry.category, "")
                if icon:
                    updated_entry = _dc.replace(updated_entry, category_icon=icon)
                current_page = self.nav_view.get_visible_page()
                if current_page is not None:
                    new_detail = DetailView(
                        updated_entry,
                        source_repos=self._entry_repos.get(updated_entry.id) or source_repos,
                        is_writable=can_write,
                        on_edit=_on_edit if can_write else None,
                        is_installed=is_installed,
                        installed_record=rec,
                        on_install_done=_on_install_done,
                        on_remove_done=_on_remove_done,
                        on_update_done=_on_update_done,
                    )
                    current_page.set_child(new_detail)
                    current_page.set_title(updated_entry.name)
                self._show_toast("Entry updated")
                self._load_catalogue()

            EditAppDialog(
                entry=selected_entry,
                repo=self._first_repo,
                on_done=_on_edit_done,
                on_deleted=self._on_entry_deleted,
            ).present(self)

        def _on_install_done(prefix_dir: str, install_path: str = "", runner: str = "", install_size: int = 0) -> None:
            repo_uri = str(self._first_repo.uri) if self._first_repo else ""
            database.mark_installed(
                entry.id, prefix_dir, entry.version, repo_uri,
                platform=entry.platform,
                install_path=install_path,
                runner=runner,
                steam_appid=entry.steam_appid,
                archive_crc32=entry.archive_crc32,
                install_size=install_size,
            )
            self._show_toast(f"{entry.name} installed successfully")
            self._load_catalogue()

        def _on_remove_done() -> None:
            self._show_toast(f"{entry.name} removed")
            self._load_catalogue()

        def _on_update_done(install_size: int = 0) -> None:
            repo_uri = str(self._first_repo.uri) if self._first_repo else ""
            existing_rec = database.get_installed(entry.id) or {}
            database.mark_installed(
                entry.id, existing_rec.get("prefix_dir", entry.id), entry.version, repo_uri,
                runner=existing_rec.get("runner", ""),
                steam_appid=entry.steam_appid,
                archive_crc32=entry.archive_crc32,
                install_size=install_size,
            )
            self._show_toast(f"{entry.name} updated successfully")
            self._load_catalogue()

        detail = DetailView(
            entry,
            source_repos=source_repos,
            is_writable=can_write,
            on_edit=_on_edit if can_write else None,
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

    def _on_preferences_activated(self, _action, _param) -> None:
        from cellar.views.settings import SettingsDialog

        dialog = SettingsDialog(on_repos_changed=self._load_catalogue)
        dialog.present(self)

    def _on_about_activated(self, _action, _param) -> None:
        dialog = Adw.AboutDialog(
            application_name="Cellar",
            application_icon="io.github.cellar",
            version="0.43.18",
            comments="A GNOME storefront for Windows and Linux apps.",
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

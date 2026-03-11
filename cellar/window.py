"""Main application window."""

from __future__ import annotations

import logging
import os
from cellar.utils.async_work import run_in_background
from pathlib import Path

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
    stored_install_path = rec.get("install_path") or ""
    if entry.platform == "linux":
        app_dir = (
            Path(stored_install_path) / entry.id
            if stored_install_path
            else native_dir() / entry.id
        )
        if not app_dir.is_dir():
            log.info(
                "Linux app dir %r gone from disk; removing stale record for %r",
                str(app_dir), entry.id,
            )
            database.remove_installed(entry.id)
            return None
    elif prefix_dir:
        install_dir = Path(stored_install_path) if stored_install_path else prefixes_dir()
        if not (install_dir / prefix_dir).is_dir():
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
    view_stack: Adw.ViewStack = Gtk.Template.Child()
    offline_banner: Adw.Banner = Gtk.Template.Child()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # Category / genre check buttons in the filter popover — rebuilt after each catalogue load.
        self._category_btns: dict[str, Gtk.CheckButton] = {}
        self._active_categories: set[str] = set()
        self._genre_btns: dict[str, Gtk.CheckButton] = {}
        self._active_genres: set[str] = set()

        # Builder filter state (type + repo).
        self._builder_type_btns: dict[str, Gtk.CheckButton] = {}
        self._builder_repo_btns: dict[str, Gtk.CheckButton] = {}
        self._builder_active_types: set[str] = set()
        self._builder_active_repos: set[str] = set()

        # Track which popover is active so we can rebuild on tab switch.
        self._browse_filter_entries: list = []
        self._browse_filter_repos: list | None = None

        # No custom child — let the GtkMenuButton use its icon-name from the
        # .ui file so it renders with standard headerbar flat styling.

        # The first successfully loaded Repo — used to resolve asset URIs in
        # the browse grid and callbacks.  Updated on every catalogue reload.
        self._first_repo = None
        self._writable_repos: list = []
        self._all_repos: list = []
        # Maps entry.id → list of Repo objects that carry the entry, for the
        # source selector shown in the detail view.
        self._entry_repos: dict = {}
        # Entry IDs that are only reachable via offline repos.
        self._offline_entry_ids: set[str] = set()

        self.search_bar.set_key_capture_widget(self)
        self.search_button.connect("toggled", self._on_search_toggled)
        self.search_bar.connect(
            "notify::search-mode-enabled", self._on_search_mode_changed
        )
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.refresh_button.connect("clicked", self._on_refresh_clicked)
        self.view_stack.connect("notify::visible-child", self._on_view_switched)

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

        from cellar.views.builder import PackageBuilderView
        self._package_builder = PackageBuilderView(
            on_catalogue_changed=self._load_catalogue,
        )
        self._package_builder.set_vexpand(True)
        self.builder_box.append(self._package_builder)

        self.connect("realize", lambda _w: self._load_catalogue())

        # Pre-warm the GE-Proton release list in the background.
        from cellar.backend import runners
        run_in_background(runners.fetch_releases, on_error=lambda msg: log.debug("Runner pre-warm failed: %s", msg))

    # ── Catalogue loading ─────────────────────────────────────────────────

    def _load_catalogue(self) -> None:
        from cellar.backend.config import load_repos, load_smb_password
        from cellar.backend.repo import Repo, RepoError, RepoManager  # noqa: F401

        manager = RepoManager()
        self._first_repo = None
        self._writable_repos = []
        self._category_btns = {}
        self._active_categories = set()

        env_uri = os.environ.get("CELLAR_REPO", "")
        if env_uri:
            try:
                r = Repo(env_uri)
                manager.add(r)
                self._first_repo = self._first_repo or r
            except RepoError as exc:
                log.warning("CELLAR_REPO %r is invalid: %s", env_uri, exc)

        for cfg in load_repos():
            if not cfg.get("enabled", True):
                continue
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
                ssh_username = cfg.get("ssh_username") or None
                ssh_password = cfg.get("ssh_password") or None
                r = Repo(
                    cfg["uri"],
                    cfg.get("name", ""),
                    ssh_identity=cfg.get("ssh_identity"),
                    ssh_username=ssh_username,
                    ssh_password=ssh_password,
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
        self._set_filter_active(False)

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

        # Build per-repo app-ID sets to detect distinct repos (vs mirrors).
        _repo_ids: dict[str, set[str]] = {}
        for app_id, repos in self._entry_repos.items():
            for repo in repos:
                _repo_ids.setdefault(repo.uri, set()).add(app_id)
        _seen: dict[frozenset[str], object] = {}
        for repo in manager:
            key = frozenset(_repo_ids.get(repo.uri, ()))
            if key not in _seen:
                _seen[key] = repo
        self._distinct_repos = list(_seen.values())

        # Map app_id → set of repo URIs for filter matching.
        entry_repo_uris: dict[str, set[str]] = {
            app_id: {r.uri for r in repos}
            for app_id, repos in self._entry_repos.items()
        }

        # Determine offline repos and show/hide the banner.
        # Exclude repos whose catalogue.json was removed (not a network issue).
        offline_repos = [r for r in manager if r.is_offline and not r.is_catalogue_missing]
        if offline_repos:
            names = ", ".join(r.name for r in offline_repos)
            noun = "Repository" if len(offline_repos) == 1 else "Repositories"
            self.offline_banner.set_title(f"{noun} offline — showing installed apps only: {names}")
            self.offline_banner.set_revealed(True)
        else:
            self.offline_banner.set_revealed(False)

        # Track entries reachable only via offline repos.
        self._offline_entry_ids = {
            e.id for e in entries
            if all(r.is_offline for r in self._entry_repos.get(e.id, []))
        }

        try:
            if entries:
                # Prefer an online repo for asset resolution.
                online_repo = next((r for r in manager if not r.is_offline), None)
                resolver_repo = online_repo or self._first_repo
                resolver = resolver_repo.resolve_asset_uri if resolver_repo else None

                installed_records: dict[str, dict] = {}
                for e in entries:
                    rec = _reconcile_installed_record(e)
                    if rec is not None:
                        installed_records[e.id] = rec

                installed_entries = [e for e in entries if e.id in installed_records]
                # Updates only for entries reachable online (can actually download).
                update_entries = [
                    e for e in installed_entries
                    if _has_update(installed_records[e.id], e)
                    and e.id not in self._offline_entry_ids
                ]

                # Explore: show all reachable entries + installed-from-offline entries.
                explore_entries = [
                    e for e in entries
                    if e.id not in self._offline_entry_ids or e.id in installed_records
                ]

                installed_ids = set(installed_records.keys())
                self._browse_explore.load_entries(explore_entries, resolve_asset=resolver, installed_ids=installed_ids, entry_repo_uris=entry_repo_uris)
                self._browse_installed.load_entries(installed_entries, resolve_asset=resolver, installed_ids=installed_ids, entry_repo_uris=entry_repo_uris)
                self._browse_updates.load_entries(update_entries, resolve_asset=resolver, installed_ids=installed_ids, entry_repo_uris=entry_repo_uris)
                self.updates_page.set_badge_number(len(update_entries))
                self._rebuild_filter_popover(explore_entries, self._distinct_repos)
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

    def _rebuild_filter_popover(self, entries: list, distinct_repos: list | None = None) -> None:
        """Build (or rebuild) the filter popover from the current entry list."""
        # Cache for restoring when switching back from builder tab.
        self._browse_filter_entries = entries
        self._browse_filter_repos = distinct_repos
        categories = sorted({e.category for e in entries if e.category})
        genres = sorted({g for e in entries for g in e.genres})
        self._category_btns = {}
        self._genre_btns = {}
        self._repo_btns: dict[str, Gtk.CheckButton] = {}
        self._active_categories = set()
        self._active_genres = set()
        self._active_repos: set[str] = set()
        show_repos = distinct_repos is not None and len(distinct_repos) >= 2

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_top(4)
        outer.set_margin_bottom(4)
        outer.set_margin_start(4)
        outer.set_margin_end(4)

        if not categories and not show_repos and not genres:
            empty_lbl = Gtk.Label(label="No categories available")
            empty_lbl.add_css_class("dim-label")
            empty_lbl.set_margin_top(8)
            empty_lbl.set_margin_bottom(8)
            outer.append(empty_lbl)
        else:
            # "All" button clears all filters.
            all_btn = Gtk.Button(label="All")
            all_btn.add_css_class("flat")
            all_btn.connect("clicked", self._on_filter_clear)
            outer.append(all_btn)

            outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

            # Repository section (only when 2+ distinct repos).
            if show_repos:
                repo_lbl = Gtk.Label(label="Repository")
                repo_lbl.add_css_class("heading")
                repo_lbl.set_halign(Gtk.Align.START)
                repo_lbl.set_margin_start(8)
                repo_lbl.set_margin_top(4)
                repo_lbl.set_margin_bottom(2)
                outer.append(repo_lbl)

                for repo in distinct_repos:
                    btn = Gtk.CheckButton(label=repo.name)
                    btn.add_css_class("flat")
                    btn.connect("toggled", self._on_repo_toggled, repo.uri)
                    self._repo_btns[repo.uri] = btn
                    outer.append(btn)

                outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

            # Category section.
            if show_repos and categories:
                cat_lbl = Gtk.Label(label="Category")
                cat_lbl.add_css_class("heading")
                cat_lbl.set_halign(Gtk.Align.START)
                cat_lbl.set_margin_start(8)
                cat_lbl.set_margin_top(4)
                cat_lbl.set_margin_bottom(2)
                outer.append(cat_lbl)

            for cat in categories:
                btn = Gtk.CheckButton(label=cat)
                btn.add_css_class("flat")
                btn.connect("toggled", self._on_category_toggled, cat)
                self._category_btns[cat] = btn
                outer.append(btn)

            # Genre section.
            if genres:
                if categories or show_repos:
                    outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
                genre_lbl = Gtk.Label(label="Genre")
                genre_lbl.add_css_class("heading")
                genre_lbl.set_halign(Gtk.Align.START)
                genre_lbl.set_margin_start(8)
                genre_lbl.set_margin_top(4)
                genre_lbl.set_margin_bottom(2)
                outer.append(genre_lbl)

                for genre in genres:
                    btn = Gtk.CheckButton(label=genre)
                    btn.add_css_class("flat")
                    btn.connect("toggled", self._on_genre_toggled, genre)
                    self._genre_btns[genre] = btn
                    outer.append(btn)

        popover = Gtk.Popover()
        popover.set_child(outer)
        self.filter_button.set_popover(popover)

    def _set_filter_active(self, active: bool) -> None:
        """Toggle accent styling on the filter button."""
        if active:
            self.filter_button.add_css_class("suggested-action")
        else:
            self.filter_button.remove_css_class("suggested-action")

    def _any_filter_active(self) -> bool:
        return bool(self._active_categories) or bool(self._active_repos) or bool(self._active_genres)

    def _on_category_toggled(self, btn: Gtk.CheckButton, category: str) -> None:
        if btn.get_active():
            self._active_categories.add(category)
        else:
            self._active_categories.discard(category)
        active = self._active_categories.copy()
        self._set_filter_active(self._any_filter_active())
        self._browse_explore.set_active_categories(active)
        self._browse_installed.set_active_categories(active)
        self._browse_updates.set_active_categories(active)

    def _on_repo_toggled(self, btn: Gtk.CheckButton, repo_uri: str) -> None:
        if btn.get_active():
            self._active_repos.add(repo_uri)
        else:
            self._active_repos.discard(repo_uri)
        active = self._active_repos.copy()
        self._set_filter_active(self._any_filter_active())
        self._browse_explore.set_active_repos(active)
        self._browse_installed.set_active_repos(active)
        self._browse_updates.set_active_repos(active)

    def _on_genre_toggled(self, btn: Gtk.CheckButton, genre: str) -> None:
        if btn.get_active():
            self._active_genres.add(genre)
        else:
            self._active_genres.discard(genre)
        active = self._active_genres.copy()
        self._set_filter_active(self._any_filter_active())
        self._browse_explore.set_active_genres(active)
        self._browse_installed.set_active_genres(active)
        self._browse_updates.set_active_genres(active)

    def _on_filter_clear(self, _button: Gtk.Button) -> None:
        for btn in self._category_btns.values():
            btn.set_active(False)
        for btn in self._repo_btns.values():
            btn.set_active(False)
        for btn in self._genre_btns.values():
            btn.set_active(False)
        self._active_categories = set()
        self._active_repos = set()
        self._active_genres = set()
        self._set_filter_active(False)
        self._browse_explore.set_active_categories(set())
        self._browse_installed.set_active_categories(set())
        self._browse_updates.set_active_categories(set())
        self._browse_explore.set_active_repos(set())
        self._browse_installed.set_active_repos(set())
        self._browse_updates.set_active_repos(set())
        self._browse_explore.set_active_genres(set())
        self._browse_installed.set_active_genres(set())
        self._browse_updates.set_active_genres(set())
        self.filter_button.get_popover().popdown()

    def apply_genre_filter(self, genre: str) -> None:
        """Activate a single genre filter from outside (e.g. detail view pill click)."""
        self._on_filter_clear(None)  # type: ignore[arg-type]
        btn = self._genre_btns.get(genre)
        if btn:
            btn.set_active(True)

    # ── Builder filter popover ─────────────────────────────────────────────

    def _rebuild_browse_filter_popover(self) -> None:
        """Restore the browse-tab filter popover."""
        self._rebuild_filter_popover(
            self._browse_filter_entries, self._browse_filter_repos
        )

    def _rebuild_builder_filter_popover(self) -> None:
        """Build the builder-tab filter popover (Type + Repo)."""
        self._builder_type_btns = {}
        self._builder_repo_btns = {}
        self._builder_active_types = set()
        self._builder_active_repos = set()
        self._package_builder.set_active_types(set())
        self._package_builder.set_active_repos(set())

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_top(4)
        outer.set_margin_bottom(4)
        outer.set_margin_start(4)
        outer.set_margin_end(4)

        # "All" button clears all filters.
        all_btn = Gtk.Button(label="All")
        all_btn.add_css_class("flat")
        all_btn.connect("clicked", self._on_builder_filter_clear)
        outer.append(all_btn)

        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Type section.
        type_lbl = Gtk.Label(label="Type")
        type_lbl.add_css_class("heading")
        type_lbl.set_halign(Gtk.Align.START)
        type_lbl.set_margin_start(8)
        type_lbl.set_margin_top(4)
        type_lbl.set_margin_bottom(2)
        outer.append(type_lbl)

        for key, label in (("proton", "Proton"), ("native", "Native"), ("base", "Base")):
            btn = Gtk.CheckButton(label=label)
            btn.add_css_class("flat")
            btn.connect("toggled", self._on_builder_type_toggled, key)
            self._builder_type_btns[key] = btn
            outer.append(btn)

        # Repo section (only when 2+ writable repos).
        if len(self._writable_repos) >= 2:
            outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            repo_lbl = Gtk.Label(label="Repository")
            repo_lbl.add_css_class("heading")
            repo_lbl.set_halign(Gtk.Align.START)
            repo_lbl.set_margin_start(8)
            repo_lbl.set_margin_top(4)
            repo_lbl.set_margin_bottom(2)
            outer.append(repo_lbl)

            for repo in self._writable_repos:
                btn = Gtk.CheckButton(label=repo.name or repo.uri)
                btn.add_css_class("flat")
                btn.connect("toggled", self._on_builder_repo_toggled, repo.uri)
                self._builder_repo_btns[repo.uri] = btn
                outer.append(btn)

        popover = Gtk.Popover()
        popover.set_child(outer)
        self.filter_button.set_popover(popover)
        self._set_filter_active(False)

    def _on_builder_type_toggled(self, btn: Gtk.CheckButton, type_key: str) -> None:
        if btn.get_active():
            self._builder_active_types.add(type_key)
        else:
            self._builder_active_types.discard(type_key)
        self._set_filter_active(
            bool(self._builder_active_types) or bool(self._builder_active_repos)
        )
        self._package_builder.set_active_types(self._builder_active_types.copy())

    def _on_builder_repo_toggled(self, btn: Gtk.CheckButton, repo_uri: str) -> None:
        if btn.get_active():
            self._builder_active_repos.add(repo_uri)
        else:
            self._builder_active_repos.discard(repo_uri)
        self._set_filter_active(
            bool(self._builder_active_types) or bool(self._builder_active_repos)
        )
        self._package_builder.set_active_repos(self._builder_active_repos.copy())

    def _on_builder_filter_clear(self, _button: Gtk.Button) -> None:
        for btn in self._builder_type_btns.values():
            btn.set_active(False)
        for btn in self._builder_repo_btns.values():
            btn.set_active(False)
        self._builder_active_types = set()
        self._builder_active_repos = set()
        self._set_filter_active(False)
        self._package_builder.set_active_types(set())
        self._package_builder.set_active_repos(set())
        self.filter_button.get_popover().popdown()

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_app_selected(self, _browse, entry) -> None:
        from cellar.views.detail import DetailView
        from cellar.backend import database

        source_repos = self._entry_repos.get(entry.id, [])
        if not source_repos and self._first_repo:
            source_repos = [self._first_repo]
        is_offline = entry.id in self._offline_entry_ids
        # Editable only if a writable AND online repo carries this entry.
        can_write = any(r.is_writable for r in source_repos) and not is_offline

        rec = _reconcile_installed_record(entry)
        is_installed = rec is not None

        def _on_edit(selected_entry):
            from cellar.views.edit_app import EditAppDialog
            from cellar.views.detail import DetailView

            def _on_edit_done(updated_entry):
                log.debug("_on_edit_done: updated_entry.id=%r screenshots=%s",
                          updated_entry.id, updated_entry.screenshots)
                import dataclasses as _dc
                try:
                    icons = self._first_repo.fetch_category_icons() if self._first_repo else {}
                except Exception:
                    icons = {}
                icon = icons.get(updated_entry.category, "")
                if icon:
                    updated_entry = _dc.replace(updated_entry, category_icon=icon)
                # Reload catalogue FIRST so _init_asset_cache runs with the new
                # generated_at before the DetailView's background screenshot
                # downloads begin.  If we did this after creating DetailView the
                # cache wipe would race with (and delete) the freshly downloaded
                # screenshot files.
                self._load_catalogue()
                current_page = self.nav_view.get_visible_page()
                log.debug("_on_edit_done: current_page=%r", current_page)
                if current_page is not None:
                    repos_for_detail = self._entry_repos.get(updated_entry.id) or source_repos
                    log.debug("_on_edit_done: source_repos for DetailView: %s",
                              [r.uri for r in repos_for_detail])
                    new_detail = DetailView(
                        updated_entry,
                        source_repos=repos_for_detail,
                        is_writable=can_write,
                        on_edit=_on_edit if can_write else None,
                        is_installed=is_installed,
                        installed_record=rec,
                        on_install_done=_on_install_done,
                        on_remove_done=_on_remove_done,
                        on_update_done=_on_update_done,
                        on_genre_filter=_on_genre_filter,
                        is_offline=is_offline,
                    )
                    current_page.set_child(new_detail)
                    current_page.set_title(updated_entry.name)
                self._show_toast("Entry updated")

            EditAppDialog(
                entry=selected_entry,
                repo=self._first_repo,
                on_done=_on_edit_done,
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

        def _on_genre_filter(genre: str) -> None:
            self.nav_view.pop()
            self.apply_genre_filter(genre)

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
            on_genre_filter=_on_genre_filter,
            is_offline=is_offline,
        )
        page = Adw.NavigationPage(title=entry.name, child=detail)
        self.nav_view.push(page)

    def _on_preferences_activated(self, _action, _param) -> None:
        from cellar.views.settings import SettingsDialog

        dialog = SettingsDialog(on_repos_changed=self._load_catalogue)
        dialog.present(self)

    def _on_about_activated(self, _action, _param) -> None:
        dialog = Adw.AboutDialog(
            application_name="Cellar",
            application_icon="io.github.cellar",
            version="0.57.0",
            comments="A GNOME storefront for Windows and Linux apps.",
            license_type=Gtk.License.GPL_3_0,
        )
        dialog.present(self)

    def _show_toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message))

    def _on_view_switched(self, stack: Adw.ViewStack, _param) -> None:
        in_builder = stack.get_visible_child_name() == "builder"
        if in_builder:
            self._rebuild_builder_filter_popover()
        else:
            self._rebuild_browse_filter_popover()

    def _on_search_toggled(self, button: Gtk.ToggleButton) -> None:
        self.search_bar.set_search_mode(button.get_active())

    def _on_search_mode_changed(self, bar: Gtk.SearchBar, _param) -> None:
        active = bar.get_search_mode()
        self.search_button.set_active(active)
        if active:
            self.search_button.add_css_class("suggested-action")
        else:
            self.search_button.remove_css_class("suggested-action")

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        text = entry.get_text()
        self._browse_explore.set_search_text(text)
        self._browse_installed.set_search_text(text)
        self._browse_updates.set_search_text(text)
        self._package_builder.set_search_text(text)

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self._load_catalogue()

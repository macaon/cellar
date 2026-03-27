"""Main application window."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import gi

from cellar.utils.async_work import run_in_background

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk

from cellar._version import __version__
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
    been deleted outside Cellar (stale record).  Supports per-app custom
    locations via the ``install_path`` / ``prefix_dir`` DB fields.
    """
    from cellar.backend import database  # noqa: PLC0415
    from cellar.backend.umu import dos_dir, native_dir, prefixes_dir  # noqa: PLC0415

    rec = database.get_installed(entry.id)
    if rec is None:
        return None
    prefix_dir = rec.get("prefix_dir") or entry.id
    stored_install_path = rec.get("install_path") or ""

    if stored_install_path:
        # Per-app custom location or global override — trust the DB.
        app_dir = Path(stored_install_path) / prefix_dir
    elif entry.platform == "dos":
        app_dir = dos_dir() / prefix_dir
    elif entry.platform == "linux":
        app_dir = native_dir() / prefix_dir
    else:
        app_dir = prefixes_dir() / prefix_dir

    if not app_dir.is_dir():
        log.info(
            "Install dir %r gone from disk; removing stale record for %r",
            str(app_dir), entry.id,
        )
        database.remove_installed(entry.id)
        return None
    return rec


@dataclass
class _CatalogueData:
    """Result of a background catalogue fetch — pure data, no GTK objects."""

    repos: list = field(default_factory=list)
    first_repo: object | None = None
    writable_repos: list = field(default_factory=list)
    entries: list = field(default_factory=list)
    entry_repos: dict = field(default_factory=dict)        # app_id → list[Repo]
    distinct_repos: list = field(default_factory=list)
    entry_repo_uris: dict = field(default_factory=dict)    # app_id → set[str]
    offline_repos: list = field(default_factory=list)
    offline_entry_ids: set = field(default_factory=set)
    installed_records: dict = field(default_factory=dict)   # app_id → dict
    error: str = ""


def _fetch_catalogue_data() -> _CatalogueData:
    """Build repos, fetch catalogues, reconcile installs — all off the UI thread."""
    from cellar.backend.config import load_repos, load_smb_password, load_ssh_password
    from cellar.backend.repo import Repo, RepoError, RepoManager

    manager = RepoManager()
    first_repo = None

    env_uri = os.environ.get("CELLAR_REPO", "")
    if env_uri:
        try:
            r = Repo(env_uri)
            manager.add(r)
            first_repo = first_repo or r
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
            ssh_password = load_ssh_password(cfg["uri"]) if ssh_username else None
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
            first_repo = first_repo or r
        except RepoError as exc:
            log.warning("Configured repo %r invalid: %s", cfg["uri"], exc)

    repos = list(manager)
    if not repos:
        return _CatalogueData()

    writable_repos = [r for r in manager if r.is_writable]

    # Fetch each repo individually to track which repo carries each entry.
    entry_repos: dict = {}
    all_entries: dict = {}
    for repo in manager:
        try:
            for e in repo.fetch_catalogue():
                all_entries[e.id] = e
                entry_repos.setdefault(e.id, []).append(repo)
        except RepoError as exc:
            log.warning("Failed to fetch from %s: %s", repo.uri, exc)
    entries = list(all_entries.values())

    # Detect distinct repos (vs mirrors).
    _repo_ids: dict[str, set[str]] = {}
    for app_id, app_repos in entry_repos.items():
        for repo in app_repos:
            _repo_ids.setdefault(repo.uri, set()).add(app_id)
    _seen: dict[frozenset[str], object] = {}
    for repo in manager:
        key = frozenset(_repo_ids.get(repo.uri, ()))
        if key not in _seen:
            _seen[key] = repo
    distinct_repos = list(_seen.values())

    entry_repo_uris: dict[str, set[str]] = {
        app_id: {r.uri for r in app_repos}
        for app_id, app_repos in entry_repos.items()
    }

    offline_repos = [r for r in manager if r.is_offline and not r.is_catalogue_missing]
    offline_entry_ids = {
        e.id for e in entries
        if all(r.is_offline for r in entry_repos.get(e.id, []))
    }

    installed_records: dict[str, dict] = {}
    for e in entries:
        rec = _reconcile_installed_record(e)
        if rec is not None:
            installed_records[e.id] = rec

    return _CatalogueData(
        repos=repos,
        first_repo=first_repo,
        writable_repos=writable_repos,
        entries=entries,
        entry_repos=entry_repos,
        distinct_repos=distinct_repos,
        entry_repo_uris=entry_repo_uris,
        offline_repos=offline_repos,
        offline_entry_ids=offline_entry_ids,
        installed_records=installed_records,
    )


@Gtk.Template(filename=ui_file("window.ui"))
class CellarWindow(Adw.ApplicationWindow):
    __gtype_name__ = "CellarWindow"

    nav_view: Adw.NavigationView = Gtk.Template.Child()
    search_button: Gtk.ToggleButton = Gtk.Template.Child()
    filter_button: Gtk.MenuButton = Gtk.Template.Child()
    view_toggle_button: Gtk.Button = Gtk.Template.Child()
    search_bar: Gtk.SearchBar = Gtk.Template.Child()
    search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    refresh_button: Gtk.Button = Gtk.Template.Child()
    transfers_button: Gtk.Button = Gtk.Template.Child()
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

        self._settings = Gio.Settings(schema_id="io.github.cellar")
        w = self._settings.get_int("window-width")
        h = self._settings.get_int("window-height")
        if w > 0 and h > 0:
            self.set_default_size(w, h)
        if self._settings.get_boolean("window-maximized"):
            self.maximize()
        self.connect("close-request", self._save_window_state)

        # Category/genre/platform check buttons in filter popover — rebuilt after catalogue load.
        self._category_btns: dict[str, Gtk.CheckButton] = {}
        self._active_categories: set[str] = set()
        self._genre_btns: dict[str, Gtk.CheckButton] = {}
        self._active_genres: set[str] = set()
        self._platform_btns: dict[str, Gtk.CheckButton] = {}
        self._active_platforms: set[str] = set()

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

        # Install queue — shared across all detail views.
        from cellar.backend.install_queue import InstallQueue
        self._install_queue = InstallQueue(
            on_complete=self._on_queue_install_complete,
            on_error=self._on_queue_install_error,
            on_cancelled=self._on_queue_install_cancelled,
            on_progress=self._on_queue_progress,
            on_download_stats=self._on_queue_download_stats,
            on_queue_changed=self._on_queue_changed,
        )
        # Map app_id → entry for DB writes on completion.
        self._pending_entries: dict = {}

        self.search_bar.set_key_capture_widget(self)
        self.search_button.connect("toggled", self._on_search_toggled)
        self.search_bar.connect(
            "notify::search-mode-enabled", self._on_search_mode_changed
        )
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.refresh_button.connect("clicked", self._on_refresh_clicked)
        self.view_stack.connect("notify::visible-child", self._on_view_switched)

        # Display mode toggle (card ↔ capsule).
        from cellar.backend.config import load_display_mode, save_display_mode
        self._save_display_mode = save_display_mode
        self._display_mode = load_display_mode()
        if self._display_mode == "capsule":
            self.view_toggle_button.set_icon_name("view-list-symbolic")
            self.view_toggle_button.set_tooltip_text("Card view")
        self.view_toggle_button.connect("clicked", self._on_view_toggle)

        # Publish queue — shared across builder and browse views.
        from cellar.backend.publish_queue import PublishQueue
        self._publish_queue = PublishQueue(
            on_complete=self._on_publish_complete,
            on_error=self._on_publish_error,
            on_cancelled=self._on_publish_cancelled,
            on_progress=self._on_publish_progress,
            on_bytes=self._on_publish_bytes,
            on_queue_changed=self._on_queue_changed,
        )

        self.transfers_button.connect("clicked", self._on_transfers_clicked)
        self._transfers_dialog = None

        search_action = Gio.SimpleAction.new("search", None)
        search_action.connect(
            "activate", lambda *_: self.search_button.set_active(
                not self.search_button.get_active()
            ),
        )
        self.add_action(search_action)

        close_action = Gio.SimpleAction.new("close", None)
        close_action.connect("activate", lambda *_: self.close())
        self.add_action(close_action)

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

        # Apply initial display mode to all browse views.
        if self._display_mode != "card":
            self._browse_explore.set_display_mode(self._display_mode)
            self._browse_installed.set_display_mode(self._display_mode)
            self._browse_updates.set_display_mode(self._display_mode)

        from cellar.views.builder import PackageBuilderView
        self._package_builder = PackageBuilderView(
            nav_view=self.nav_view,
            on_catalogue_changed=self._load_catalogue,
            publish_queue=self._publish_queue,
        )
        self.nav_view.connect("popped", self._package_builder._on_nav_popped)
        self._package_builder.set_vexpand(True)
        self.builder_box.append(self._package_builder)

        self.connect("realize", lambda _w: self._load_catalogue())

        # Pre-warm the GE-Proton release list in the background.
        from cellar.backend import runners
        run_in_background(
            runners.fetch_releases,
            on_error=lambda msg: log.debug("Runner pre-warm failed: %s", msg),
        )


    # ── Catalogue loading ─────────────────────────────────────────────────

    def _load_catalogue(self) -> None:
        """Kick off a background catalogue fetch and apply results on the UI thread."""
        self._category_btns = {}
        self._active_categories = set()
        self.refresh_button.set_sensitive(False)
        self._browse_explore.show_loading()

        run_in_background(
            work=_fetch_catalogue_data,
            on_done=self._apply_catalogue,
            on_error=self._on_catalogue_error,
        )

    def _on_catalogue_error(self, message: str) -> None:
        """Handle a background fetch that raised an exception."""
        log.error("Catalogue fetch failed: %s", message)
        self._browse_explore.show_error("Could Not Load Repository", message)
        self._browse_installed.load_entries([])
        self._browse_updates.load_entries([])
        self.updates_page.set_badge_number(0)
        self._rebuild_filter_popover([])
        self.refresh_button.set_sensitive(True)

    def _apply_catalogue(self, data: _CatalogueData) -> None:
        """Apply fetched catalogue data to the UI — runs on the main thread."""
        self._first_repo = data.first_repo
        self._writable_repos = data.writable_repos
        self._all_repos = data.repos
        self._entry_repos = data.entry_repos
        self._offline_entry_ids = data.offline_entry_ids

        self.builder_page.set_visible(bool(data.writable_repos))
        self._package_builder.update_repos(data.writable_repos, all_repos=data.repos)
        self._set_filter_active(False)

        if not data.repos:
            self._browse_explore.show_error(
                "No Repository Configured",
                "Open Preferences (the menu in the top-right corner) "
                "to add a repository source.",
            )
            self._browse_installed.load_entries([])
            self._browse_updates.load_entries([])
            self.updates_page.set_badge_number(0)
            self._rebuild_filter_popover([])
            self.refresh_button.set_sensitive(True)
            return

        self._distinct_repos = data.distinct_repos

        # Offline banner.
        if data.offline_repos:
            names = ", ".join(r.name for r in data.offline_repos)
            noun = "Repository" if len(data.offline_repos) == 1 else "Repositories"
            self.offline_banner.set_title(
                f"{noun} offline — showing installed apps only: {names}",
            )
            self.offline_banner.set_revealed(True)
        else:
            self.offline_banner.set_revealed(False)

        try:
            if data.entries:
                _fallback_repo = (
                    next((r for r in data.repos if not r.is_offline), None)
                    or data.first_repo
                )

                def resolver(
                    rel_path: str,
                    *,
                    _entry_repos=data.entry_repos,
                    _fb=_fallback_repo,
                ) -> str:
                    if not rel_path:
                        return ""
                    parts = rel_path.split("/")
                    if len(parts) >= 2 and parts[0] == "apps":
                        app_id = parts[1]
                        repos = _entry_repos.get(app_id, [])
                        repo = (
                            next((r for r in repos if not r.is_offline), None)
                            or (repos[0] if repos else _fb)
                        )
                    else:
                        repo = _fb
                    return repo.resolve_asset_uri(rel_path) if repo else ""

                installed_entries = [
                    e for e in data.entries if e.id in data.installed_records
                ]
                update_entries = [
                    e for e in installed_entries
                    if _has_update(data.installed_records[e.id], e)
                    and e.id not in data.offline_entry_ids
                ]
                explore_entries = [
                    e for e in data.entries
                    if e.id not in data.offline_entry_ids
                    or e.id in data.installed_records
                ]

                installed_ids = set(data.installed_records.keys())
                _load_kw = dict(
                    resolve_asset=resolver,
                    installed_ids=installed_ids,
                    entry_repo_uris=data.entry_repo_uris,
                )
                self._browse_explore.load_entries(explore_entries, **_load_kw)
                self._browse_installed.load_entries(installed_entries, **_load_kw)
                self._browse_updates.load_entries(update_entries, **_load_kw)
                self.updates_page.set_badge_number(len(update_entries))
                self._sync_publishing_overlays()
                self._rebuild_filter_popover(explore_entries, data.distinct_repos)
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
        platforms = sorted({e.platform for e in entries if e.platform})
        self._category_btns = {}
        self._genre_btns = {}
        self._platform_btns = {}
        self._repo_btns: dict[str, Gtk.CheckButton] = {}
        self._active_categories = set()
        self._active_genres = set()
        self._active_platforms = set()
        self._active_repos: set[str] = set()
        show_repos = distinct_repos is not None and len(distinct_repos) >= 2

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        outer.set_margin_top(4)
        outer.set_margin_bottom(4)
        outer.set_margin_start(4)
        outer.set_margin_end(4)

        show_platforms = len(platforms) >= 2
        if not categories and not show_repos and not genres and not show_platforms:
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

            # Platform section (only when 2+ distinct platforms).
            if show_platforms:
                if categories or show_repos or genres:
                    outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
                plat_lbl = Gtk.Label(label="Platform")
                plat_lbl.add_css_class("heading")
                plat_lbl.set_halign(Gtk.Align.START)
                plat_lbl.set_margin_start(8)
                plat_lbl.set_margin_top(4)
                plat_lbl.set_margin_bottom(2)
                outer.append(plat_lbl)

                _platform_labels = {"windows": "Windows", "linux": "Linux", "dos": "DOS"}
                for plat in platforms:
                    label = _platform_labels.get(plat, plat.title())
                    btn = Gtk.CheckButton(label=label)
                    btn.add_css_class("flat")
                    btn.connect("toggled", self._on_platform_toggled, plat)
                    self._platform_btns[plat] = btn
                    outer.append(btn)

        popover = Gtk.Popover()
        popover.set_child(outer)
        self.filter_button.set_popover(popover)

    def _set_filter_active(self, active: bool) -> None:
        """Toggle accent styling on the filter button."""
        if active:
            self.filter_button.add_css_class("accent")
        else:
            self.filter_button.remove_css_class("accent")

    def _any_filter_active(self) -> bool:
        return (
            bool(self._active_categories) or bool(self._active_repos)
            or bool(self._active_genres) or bool(self._active_platforms)
        )

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

    def _on_platform_toggled(self, btn: Gtk.CheckButton, platform: str) -> None:
        if btn.get_active():
            self._active_platforms.add(platform)
        else:
            self._active_platforms.discard(platform)
        active = self._active_platforms.copy()
        self._set_filter_active(self._any_filter_active())
        self._browse_explore.set_active_platforms(active)
        self._browse_installed.set_active_platforms(active)
        self._browse_updates.set_active_platforms(active)

    def _on_filter_clear(self, _button: Gtk.Button) -> None:
        for btn in self._category_btns.values():
            btn.set_active(False)
        for btn in self._repo_btns.values():
            btn.set_active(False)
        for btn in self._genre_btns.values():
            btn.set_active(False)
        for btn in self._platform_btns.values():
            btn.set_active(False)
        self._active_categories = set()
        self._active_repos = set()
        self._active_genres = set()
        self._active_platforms = set()
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
        self._browse_explore.set_active_platforms(set())
        self._browse_installed.set_active_platforms(set())
        self._browse_updates.set_active_platforms(set())
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
        from cellar.backend import database
        from cellar.views.detail import DetailView

        source_repos = self._entry_repos.get(entry.id, [])
        if not source_repos and self._first_repo:
            source_repos = [self._first_repo]
        is_offline = entry.id in self._offline_entry_ids

        rec = _reconcile_installed_record(entry)
        is_installed = rec is not None

        def _on_remove_done() -> None:
            self._show_toast(f"{entry.name} removed")
            self._load_catalogue()

        def _on_update_done(install_size: int = 0, delta_size: int = 0) -> None:
            repo_uri = str(self._first_repo.uri) if self._first_repo else ""
            existing_rec = database.get_installed(entry.id) or {}
            database.mark_installed(
                entry.id, existing_rec.get("prefix_dir", entry.id), entry.version, repo_uri,
                runner=existing_rec.get("runner", ""),
                steam_appid=entry.steam_appid,
                archive_crc32=entry.archive_crc32,
                install_size=install_size,
                delta_size=delta_size,
            )
            self._show_toast(f"{entry.name} updated successfully")
            self._load_catalogue()

        def _on_genre_filter(genre: str) -> None:
            self.nav_view.pop()
            self.apply_genre_filter(genre)

        detail = DetailView(
            entry,
            source_repos=source_repos,
            is_installed=is_installed,
            installed_record=rec,
            install_queue=self._install_queue,
            on_remove_done=_on_remove_done,
            on_update_done=_on_update_done,
            on_genre_filter=_on_genre_filter,
            is_offline=is_offline,
            on_catalogue_changed=lambda: self._load_catalogue(),
        )
        page = Adw.NavigationPage(title=entry.name, child=detail)
        self.nav_view.push(page)

    def _on_transfers_clicked(self, _btn) -> None:
        from cellar.views.transfer_dialog import TransferDialog

        dialog = TransferDialog(self._install_queue, self._publish_queue)
        self._transfers_dialog = dialog
        dialog.connect("closed", lambda _d: setattr(self, "_transfers_dialog", None))
        dialog.present(self)

    def _save_window_state(self, _widget) -> bool:
        self._settings.set_boolean("window-maximized", self.is_maximized())
        if not self.is_maximized():
            w, h = self.get_default_size()
            self._settings.set_int("window-width", w)
            self._settings.set_int("window-height", h)
        return False

    def _on_preferences_activated(self, _action, _param) -> None:
        from cellar.views.settings import SettingsDialog

        dialog = SettingsDialog(on_repos_changed=self._load_catalogue)
        dialog.present(self)

    def _on_about_activated(self, _action, _param) -> None:
        dialog = Adw.AboutDialog(
            application_name="Cellar",
            application_icon="io.github.cellar",
            version=__version__,
            comments="A GNOME storefront for Windows and Linux apps.",
            license_type=Gtk.License.GPL_3_0,
        )
        dialog.present(self)

    def _show_toast(self, message: str) -> None:
        self.toast_overlay.add_toast(Adw.Toast(title=message))

    # ── Install queue callbacks ───────────────────────────────────────────

    def _on_queue_install_complete(self, result) -> None:
        from cellar.backend import database

        entry = self._pending_entries.pop(result.app_id, None)
        if entry is not None:
            repo_uri = str(self._first_repo.uri) if self._first_repo else ""
            database.mark_installed(
                entry.id, result.prefix_dir, entry.version, repo_uri,
                platform=entry.platform,
                install_path=result.install_path,
                runner=result.runner,
                steam_appid=entry.steam_appid,
                archive_crc32=entry.archive_crc32,
                install_size=result.install_size,
                delta_size=result.delta_size,
            )
            self._show_toast(f"\u201c{entry.name}\u201d installed successfully")
        else:
            self._show_toast(f"\u201c{result.app_id}\u201d installed successfully")

        # Update the active detail view immediately so the button shows "Open".
        from cellar.views.detail import DetailView
        page = self.nav_view.get_visible_page()
        if page is not None:
            child = page.get_child()
            if isinstance(child, DetailView) and child._entry.id == result.app_id:
                child._is_installed = True
                child._installed_record = {
                    "prefix_dir": result.prefix_dir,
                    "install_path": result.install_path,
                    "install_size": result.install_size,
                    "delta_size": result.delta_size,
                    "runner": result.runner,
                }
                if result.runner and child._runner_label:
                    child._runner_label.set_label(result.runner)
                child._update_install_button()
                child._rebuild_info_cards()

        self._load_catalogue()

    def _on_queue_install_error(self, app_id: str, message: str) -> None:
        entry = self._pending_entries.pop(app_id, None)
        name = entry.name if entry else app_id
        self._show_toast(f"Installation of \u201c{name}\u201d failed")
        log.error("Install queue error for %s: %s", app_id, message)

    def _on_queue_install_cancelled(self, app_id: str) -> None:
        self._pending_entries.pop(app_id, None)

    def _on_queue_progress(self, app_id: str, phase: str, fraction: float) -> None:
        """Store progress and update the active detail view."""
        q = self._install_queue
        q._active_phase = phase or q._active_phase
        q._active_fraction = fraction
        # Reset download stats when phase changes (e.g. from download to extract).
        if phase:
            q._active_dl_done = 0
            q._active_dl_total = 0
            q._active_dl_speed = 0.0
        self._refresh_active_detail_progress()

    def _on_queue_download_stats(
        self, app_id: str, downloaded: int, total: int, speed: float,
    ) -> None:
        """Store byte-level download stats and update the active detail view."""
        q = self._install_queue
        q._active_dl_done = downloaded
        q._active_dl_total = total
        q._active_dl_speed = speed
        # Compute fraction from bytes — more reliable than the separate progress callback.
        if total > 0:
            q._active_fraction = min(downloaded / total, 1.0)
        self._refresh_active_detail_progress()

    def _refresh_active_detail_progress(self) -> None:
        """Push latest progress to the visible DetailView and downloads dialog."""
        from cellar.views.detail import DetailView
        page = self.nav_view.get_visible_page()
        if page is not None:
            child = page.get_child()
            if isinstance(child, DetailView):
                child._update_install_progress(self._install_queue)
        # Also update the transfers dialog if open.
        dlg = getattr(self, "_transfers_dialog", None)
        if dlg is not None:
            dlg.update_active_stats()

    def _on_queue_changed(self) -> None:
        """Update UI elements that depend on queue state."""
        has_dl = not self._install_queue.is_empty
        has_ul = not self._publish_queue.is_empty
        self.transfers_button.set_visible(has_dl or has_ul)
        if has_dl and has_ul:
            self.transfers_button.set_icon_name("network-transmit-receive-symbolic")
        elif has_ul:
            self.transfers_button.set_icon_name("network-transmit-symbolic")
        else:
            self.transfers_button.set_icon_name("network-receive-symbolic")
        # Refresh the active detail view's install button if visible.
        self._refresh_active_detail_button()
        self._sync_publishing_overlays()
        # Repopulate the transfer dialog so completed/queued rows update.
        dlg = getattr(self, "_transfers_dialog", None)
        if dlg is not None:
            dlg._populate()

    def _refresh_active_detail_button(self) -> None:
        """If a DetailView is currently visible, refresh its install button."""
        from cellar.views.detail import DetailView
        page = self.nav_view.get_visible_page()
        if page is not None:
            child = page.get_child()
            if isinstance(child, DetailView):
                child._update_install_button()

    # ── Publish queue callbacks ───────────────────────────────────────────

    def _on_publish_complete(self, result) -> None:
        self._show_toast(f"Published \u201c{result.app_name}\u201d to {result.repo_name}.")
        if result.delete_after:
            self._package_builder._reload_projects()
        self._load_catalogue()

    def _on_publish_error(self, app_id: str, message: str) -> None:
        self._show_toast(f"Publish of \u201c{app_id}\u201d failed: {message}")

    def _on_publish_cancelled(self, app_id: str) -> None:
        self._show_toast("Publish cancelled.")

    def _on_publish_progress(self, app_id: str, phase: str, fraction: float) -> None:
        dlg = getattr(self, "_transfers_dialog", None)
        if dlg is not None:
            dlg.update_active_stats()

    def _on_publish_bytes(self, app_id: str, total_bytes: int, stats_text: str) -> None:
        dlg = getattr(self, "_transfers_dialog", None)
        if dlg is not None:
            dlg.update_active_stats()

    def _sync_publishing_overlays(self) -> None:
        """Push current publish-queue IDs to all browse views as spinner overlays."""
        q = self._publish_queue
        active = q.active_job
        ids: set[str] = set()
        if active is not None:
            ids.add(active.app_id)
        for job in q.queued_jobs:
            ids.add(job.app_id)
        self._browse_explore.set_publishing_ids(ids)
        self._browse_installed.set_publishing_ids(ids)
        self._browse_updates.set_publishing_ids(ids)

    def _on_view_switched(self, stack: Adw.ViewStack, _param) -> None:
        in_builder = stack.get_visible_child_name() == "builder"
        self.view_toggle_button.set_visible(not in_builder)
        if in_builder:
            self._rebuild_builder_filter_popover()
        else:
            self._rebuild_browse_filter_popover()

    def _on_view_toggle(self, button: Gtk.Button) -> None:
        mode = "card" if self._display_mode == "capsule" else "capsule"
        self._display_mode = mode
        # Icon reflects the current display mode.
        if mode == "capsule":
            button.set_icon_name("view-list-symbolic")
            button.set_tooltip_text("Card view")
        else:
            button.set_icon_name("view-grid-symbolic")
            button.set_tooltip_text("Capsule view")
        self._browse_explore.set_display_mode(mode)
        self._browse_installed.set_display_mode(mode)
        self._browse_updates.set_display_mode(mode)
        self._save_display_mode(mode)

    def _on_search_toggled(self, button: Gtk.ToggleButton) -> None:
        self.search_bar.set_search_mode(button.get_active())

    def _on_search_mode_changed(self, bar: Gtk.SearchBar, _param) -> None:
        active = bar.get_search_mode()
        self.search_button.set_active(active)

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        text = entry.get_text()
        self._browse_explore.set_search_text(text)
        self._browse_installed.set_search_text(text)
        self._browse_updates.set_search_text(text)
        self._package_builder.set_search_text(text)

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        self._load_catalogue()

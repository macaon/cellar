"""Package Builder view — create and publish WINEPREFIX-based app packages.

Shown in the main window when at least one writable repo is configured.
Maintainers use this view to:

1. Create a project (App or Base) that owns a WINEPREFIX.
2. Select a GE-Proton runner and initialize the prefix.
3. Install dependencies (winetricks verbs) and run installers.
4. Set an entry point (App projects only).
5. Test-launch the app to verify it works.
6. Publish — stream-compress the prefix directly to the repo and update
   catalogue.json (App and Base alike).  No intermediate local archive.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import Callable

from cellar.utils.async_work import run_in_background

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from cellar.backend.project import (
    Project,
    ProjectType,
    create_project,
    delete_project,
    load_projects,
    package_project,
    save_project,
)
from cellar.views.builder.catalogue_import import CatalogueEntriesDialog
from cellar.views.builder.dependencies import DependencyPickerDialog
from cellar.views.builder.metadata import AppMetadataDialog
from cellar.views.builder.pickers import (
    AddLaunchTargetDialog,
    BasePickerDialog,
    RunnerPickerDialog,
)
from cellar.views.builder.progress import ProgressDialog

log = logging.getLogger(__name__)

class PackageBuilderView(Gtk.Box):
    """Two-panel package builder: project list on the left, detail on the right."""

    def __init__(
        self,
        *,
        writable_repos: list | None = None,
        all_repos: list | None = None,
        on_catalogue_changed: Callable | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self._writable_repos: list = writable_repos or []
        self._all_repos: list = all_repos or []
        self._on_catalogue_changed = on_catalogue_changed
        self._project: Project | None = None
        self._project_rows: list[_ProjectRow] = []

        self._build()
        self._reload_projects()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_repos(self, writable_repos: list, *, all_repos: list | None = None) -> None:
        self._writable_repos = writable_repos
        if all_repos is not None:
            self._all_repos = all_repos

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        # ── Left sidebar ──────────────────────────────────────────────────
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.set_size_request(260, -1)
        sidebar.add_css_class("sidebar")

        # Sidebar header
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        header.set_title_widget(Gtk.Label())  # no centred title — label is at start

        title_label = Gtk.Label(label="Packages")
        title_label.add_css_class("heading")
        title_label.set_margin_start(8)
        header.pack_start(title_label)

        btn_box = Gtk.Box(spacing=4)
        btn_box.set_margin_start(4)
        btn_box.set_margin_end(4)

        new_app_btn = Gtk.Button(icon_name="grid-large-symbolic")
        new_app_btn.set_tooltip_text("New Windows package")
        new_app_btn.add_css_class("flat")
        new_app_btn.connect("clicked", self._on_new_app_clicked)

        new_linux_btn = Gtk.Button(icon_name="penguin-alt-symbolic")
        new_linux_btn.set_tooltip_text("New Linux package")
        new_linux_btn.add_css_class("flat")
        new_linux_btn.connect("clicked", self._on_new_linux_clicked)

        new_base_btn = Gtk.Button(icon_name="package-x-generic-symbolic")
        new_base_btn.set_tooltip_text("New Base package")
        new_base_btn.add_css_class("flat")
        new_base_btn.connect("clicked", self._on_new_base_clicked)

        import_btn = Gtk.Button(icon_name="open-book-symbolic")
        import_btn.set_tooltip_text("Catalogue entries…")
        import_btn.add_css_class("flat")
        import_btn.connect("clicked", self._on_import_clicked)

        self._delete_btn = Gtk.Button(icon_name="edit-delete-symbolic")
        self._delete_btn.set_tooltip_text("Delete package")
        self._delete_btn.add_css_class("flat")
        self._delete_btn.add_css_class("destructive-action")
        self._delete_btn.set_sensitive(False)
        self._delete_btn.connect("clicked", self._on_delete_clicked)

        btn_box.append(new_app_btn)
        btn_box.append(new_linux_btn)
        btn_box.append(new_base_btn)
        btn_box.append(import_btn)
        btn_box.append(self._delete_btn)
        header.pack_end(btn_box)

        sidebar.append(header)
        sidebar.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Project list
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_vexpand(True)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list_box.add_css_class("navigation-sidebar")
        self._list_box.connect("row-selected", self._on_row_selected)
        scroll.set_child(self._list_box)
        sidebar.append(scroll)

        self.append(sidebar)
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # ── Right panel ───────────────────────────────────────────────────
        self._detail_stack = Gtk.Stack()
        self._detail_stack.set_hexpand(True)
        self._detail_stack.set_vexpand(True)

        # Empty state
        empty = Adw.StatusPage(
            description="Create a new package or select one from the list.",
            icon_name="package-x-generic-symbolic",
        )
        self._detail_stack.add_named(empty, "empty")

        # Detail container — scroll (populated by _show_project())
        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        detail_box.set_vexpand(True)

        self._detail_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._detail_scroll.set_vexpand(True)
        detail_box.append(self._detail_scroll)

        self._detail_stack.add_named(detail_box, "detail")

        self._detail_stack.set_visible_child_name("empty")
        self.append(self._detail_stack)

    # ------------------------------------------------------------------
    # Project list management
    # ------------------------------------------------------------------

    def _reload_projects(self) -> None:
        """Reload project list from disk and refresh the sidebar."""
        projects = load_projects()
        # Clear existing rows
        while True:
            row = self._list_box.get_row_at_index(0)
            if row is None:
                break
            self._list_box.remove(row)
        self._project_rows = []

        for p in projects:
            row = _ProjectRow(p)
            self._list_box.append(row)
            self._project_rows.append(row)

        if not projects:
            self._project = None
            self._delete_btn.set_sensitive(False)
            self._detail_stack.set_visible_child_name("empty")

    def _on_row_selected(self, _lb, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            self._project = None
            self._delete_btn.set_sensitive(False)
            self._detail_stack.set_visible_child_name("empty")
            return
        self._project = row.project  # type: ignore[attr-defined]
        self._delete_btn.set_sensitive(True)
        self._show_project(self._project)

    def _on_new_app_clicked(self, _btn) -> None:
        dialog = AppMetadataDialog(on_created=self._on_project_created)
        dialog.present(self)

    def _on_new_linux_clicked(self, _btn) -> None:
        dialog = AppMetadataDialog(project_type="linux", on_created=self._on_project_created)
        dialog.present(self)

    def _on_new_base_clicked(self, _btn) -> None:
        from cellar.backend import runners as _runners
        installed = _runners.installed_runners()
        runner = installed[0] if installed else ""
        project = create_project("", "base", runner=runner)
        self._on_project_created(project)

    def _on_import_clicked(self, _btn) -> None:
        if not self._all_repos:
            self._show_toast("No repositories configured.")
            return
        dialog = CatalogueEntriesDialog(
            repos=self._all_repos,
            on_imported=self._on_project_imported,
            on_catalogue_changed=self._on_catalogue_changed,
        )
        dialog.present(self)

    def _on_project_imported(self, project: Project) -> None:
        self._reload_projects()
        for i, row in enumerate(self._project_rows):
            if row.project.slug == project.slug:
                self._list_box.select_row(self._list_box.get_row_at_index(i))
                break

    def _on_delete_clicked(self, _btn) -> None:
        if self._project is None:
            return
        name = self._project.name
        slug = self._project.slug

        dialog = Adw.AlertDialog(
            heading=f"Delete '{name}'?",
            body="The project directory (including the prefix) will be permanently deleted.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(d, resp):
            if resp == "delete":
                delete_project(slug)
                self._project = None
                self._reload_projects()
                self._detail_stack.set_visible_child_name("empty")

        dialog.connect("response", _on_response)
        dialog.present(self)

    def _on_project_created(self, project: Project) -> None:
        self._reload_projects()
        # Select the newly created project
        for i, row in enumerate(self._project_rows):
            if row.project.slug == project.slug:
                self._list_box.select_row(self._list_box.get_row_at_index(i))
                break

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------

    def _show_project(self, project: Project, *, expand_sel: bool = False) -> None:
        """Build and display the detail panel for *project*."""
        page = Adw.PreferencesPage()
        clamp = Adw.Clamp(maximum_size=700)
        clamp.set_child(page)
        self._detail_scroll.set_child(clamp)

        # ── 1. Metadata section (App / Linux projects — first, to set title/slug) ──
        if project.project_type in ("app", "linux"):
            meta_group = Adw.PreferencesGroup(title="Metadata")

            # Title — always visible
            self._meta_name_row = Adw.EntryRow(title="Title")
            self._meta_name_row.set_text(project.name)
            _steam_btn = Gtk.Button(icon_name="system-search-symbolic")
            _steam_btn.add_css_class("flat")
            _steam_btn.set_valign(Gtk.Align.CENTER)
            _steam_btn.set_tooltip_text("Look up on Steam")
            _steam_btn.connect("clicked", self._on_meta_steam_lookup)
            self._meta_name_row.add_suffix(_steam_btn)

            def _on_name_changed(row):
                if self._project:
                    self._project.name = row.get_text()
                    save_project(self._project)
                    for r in self._project_rows:
                        if r.project.slug == self._project.slug:
                            r._label.set_text(self._project.name)
                            break

            self._meta_name_row.connect("changed", _on_name_changed)
            meta_group.add(self._meta_name_row)

            # App ID — always visible, read-only
            _slug_row = Adw.ActionRow(title="App ID", subtitle=project.slug)
            _slug_row.add_css_class("property")
            meta_group.add(_slug_row)

            # Details summary row — opens AppMetadataDialog
            _details_row = Adw.ActionRow(title="Details")
            _details_summary = self._make_metadata_summary(project)
            if _details_summary:
                _details_row.set_subtitle(_details_summary)
            _details_btn = Gtk.Button(label="Edit\u2026", valign=Gtk.Align.CENTER)
            _details_btn.connect("clicked", self._on_edit_metadata_clicked)
            _details_row.add_suffix(_details_btn)
            _details_row.set_activatable_widget(_details_btn)
            meta_group.add(_details_row)

            page.add(meta_group)

        # ── 2. Runner / Base Image (Windows packages only) ────────────────
        if project.project_type != "linux":
            sel_group = Adw.PreferencesGroup()
            if project.project_type == "base":
                sel_group_title = "Runner"
                sel_active_label = project.runner or "No runner selected"
            else:
                sel_group_title = "Base Image"
                sel_active_label = project.runner or "No base image selected"

            self._sel_active_row = Adw.ActionRow(title=sel_group_title)
            self._sel_active_row.set_subtitle(sel_active_label)

            if project.project_type == "base":
                # Flat layout: runner list + Download button, no expander
                self._sel_expander = sel_group
                dl_btn = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
                dl_btn.add_css_class("suggested-action")
                dl_btn.connect("clicked", self._on_download_runner_clicked)
                self._sel_active_row.add_suffix(dl_btn)
                sel_group.add(self._sel_active_row)
            else:
                self._sel_expander = Adw.ExpanderRow(title="Change\u2026")
                self._sel_expander.set_expanded(expand_sel)
                sel_group.add(self._sel_active_row)
                sel_group.add(self._sel_expander)

            page.add(sel_group)

            if project.project_type == "base":
                self._populate_runner_expander(project)
            else:
                self._populate_base_expander(project)

        # ── 3. Prefix (Windows / Base only) ───────────────────────────────
        if project.project_type != "linux":
            prefix_group = Adw.PreferencesGroup(title="Prefix")
            prefix_exists = project.prefix_path.is_dir()
            status_text = "Initialized" if (prefix_exists and project.initialized) else (
                "Directory exists (not initialized)" if prefix_exists else "Not initialized"
            )
            self._prefix_status_row = Adw.ActionRow(title="Status", subtitle=status_text)
            self._init_btn = Gtk.Button(label="Initialize")
            self._init_btn.set_valign(Gtk.Align.CENTER)
            self._init_btn.add_css_class("suggested-action")
            self._init_btn.set_sensitive(bool(project.runner) and not project.initialized)
            self._init_btn.connect("clicked", self._on_init_prefix_clicked)
            self._prefix_status_row.add_suffix(self._init_btn)
            prefix_group.add(self._prefix_status_row)
            page.add(prefix_group)

        # ── 4. Dependencies (Windows packages only) ───────────────────────
        if project.project_type != "linux":
            dep_group = Adw.PreferencesGroup(title="Dependencies")
            for verb in project.deps_installed:
                row = Adw.ActionRow(title=verb)
                rm_btn = Gtk.Button(icon_name="edit-delete-symbolic")
                rm_btn.set_valign(Gtk.Align.CENTER)
                rm_btn.add_css_class("flat")
                rm_btn.connect("clicked", self._on_remove_dep_clicked, verb)
                row.add_suffix(rm_btn)
                dep_group.add(row)

            add_dep_row = Adw.ActionRow(title="Add Dependencies\u2026")
            add_dep_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
            add_dep_btn.add_css_class("suggested-action")
            add_dep_btn.connect("clicked", self._on_add_dep_clicked)
            add_dep_row.add_suffix(add_dep_btn)
            add_dep_row.set_activatable_widget(add_dep_btn)
            add_dep_row.set_sensitive(project.initialized)
            dep_group.add(add_dep_row)
            page.add(dep_group)

        # ── 5. Files section (Windows app only) ───────────────────────────
        if project.project_type == "app":
            files_group = Adw.PreferencesGroup(title="Files")

            run_installer_row = Adw.ActionRow(
                title="Run Installer",
                subtitle="Run a .exe inside the prefix",
            )
            run_installer_row.set_sensitive(project.initialized)
            run_btn = Gtk.Button(label="Choose\u2026")
            run_btn.set_valign(Gtk.Align.CENTER)
            run_btn.connect("clicked", self._on_run_installer_clicked)
            run_installer_row.add_suffix(run_btn)
            files_group.add(run_installer_row)

            _browse_row = Adw.ActionRow(
                title="Browse Prefix",
                subtitle="Open drive_c in the file manager",
            )
            _browse_row.set_sensitive(project.initialized)
            _browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
            _browse_btn.set_valign(Gtk.Align.CENTER)
            _browse_btn.add_css_class("flat")
            _browse_btn.connect("clicked", self._on_browse_prefix_clicked)
            _browse_row.add_suffix(_browse_btn)
            files_group.add(_browse_row)

            _winecfg_row = Adw.ActionRow(
                title="Wine Configuration",
                subtitle="Open winecfg (DLL overrides, Windows version, …)",
            )
            _winecfg_row.set_sensitive(project.initialized)
            _winecfg_btn = Gtk.Button(label="Open")
            _winecfg_btn.set_valign(Gtk.Align.CENTER)
            _winecfg_btn.connect("clicked", self._on_winecfg_clicked)
            _winecfg_row.add_suffix(_winecfg_btn)
            files_group.add(_winecfg_row)

            page.add(files_group)

            # Launch Targets (Windows app)
            targets_group = Adw.PreferencesGroup(title="Launch Targets")
            for _ep in project.entry_points:
                _ep_subtitle = _ep.get("path", "")
                if _ep.get("args"):
                    _ep_subtitle += "  " + _ep["args"]
                _ep_row = Adw.ActionRow(
                    title=_ep.get("name", ""),
                    subtitle=_ep_subtitle,
                )
                _ep_row.set_subtitle_selectable(True)
                _rm_btn = Gtk.Button(icon_name="edit-delete-symbolic")
                _rm_btn.set_valign(Gtk.Align.CENTER)
                _rm_btn.add_css_class("flat")
                _rm_btn.connect("clicked", self._on_remove_entry_point_clicked, _ep)
                _ep_row.add_suffix(_rm_btn)
                targets_group.add(_ep_row)

            _add_ep_row = Adw.ActionRow(title="Add Launch Target\u2026")
            _add_ep_row.set_sensitive(project.initialized)
            _add_ep_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
            _add_ep_btn.add_css_class("suggested-action")
            _add_ep_btn.connect("clicked", self._on_add_entry_point_clicked)
            _add_ep_row.add_suffix(_add_ep_btn)
            _add_ep_row.set_activatable_widget(_add_ep_btn)
            targets_group.add(_add_ep_row)

            page.add(targets_group)

        # ── 5b. Source Folder + Launch Targets (Linux only) ───────────────
        elif project.project_type == "linux":
            _linux_ready = bool(project.source_dir) and Path(project.source_dir).is_dir()
            src_group = Adw.PreferencesGroup(title="Source Folder")

            src_row = Adw.ActionRow(title="Folder")
            src_row.set_subtitle(project.source_dir or "Not set")
            src_row.set_subtitle_selectable(True)

            if _linux_ready:
                _open_btn = Gtk.Button(icon_name="folder-open-symbolic")
                _open_btn.set_valign(Gtk.Align.CENTER)
                _open_btn.add_css_class("flat")
                _open_btn.connect("clicked", self._on_browse_prefix_clicked)
                src_row.add_suffix(_open_btn)

            _choose_btn = Gtk.Button(label="Choose\u2026")
            _choose_btn.set_valign(Gtk.Align.CENTER)
            _choose_btn.connect("clicked", self._on_choose_source_dir_clicked)
            src_row.add_suffix(_choose_btn)

            src_group.add(src_row)
            page.add(src_group)

            # Launch Targets (Linux)
            targets_group = Adw.PreferencesGroup(title="Launch Targets")
            for _ep in project.entry_points:
                _ep_subtitle = _ep.get("path", "")
                if _ep.get("args"):
                    _ep_subtitle += "  " + _ep["args"]
                _ep_row = Adw.ActionRow(
                    title=_ep.get("name", ""),
                    subtitle=_ep_subtitle,
                )
                _ep_row.set_subtitle_selectable(True)
                _rm_btn = Gtk.Button(icon_name="edit-delete-symbolic")
                _rm_btn.set_valign(Gtk.Align.CENTER)
                _rm_btn.add_css_class("flat")
                _rm_btn.connect("clicked", self._on_remove_entry_point_clicked, _ep)
                _ep_row.add_suffix(_rm_btn)
                targets_group.add(_ep_row)

            _add_ep_row = Adw.ActionRow(title="Add Launch Target\u2026")
            _add_ep_row.set_sensitive(_linux_ready)
            _add_ep_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
            _add_ep_btn.add_css_class("suggested-action")
            _add_ep_btn.connect("clicked", self._on_add_entry_point_clicked)
            _add_ep_row.add_suffix(_add_ep_btn)
            _add_ep_row.set_activatable_widget(_add_ep_btn)
            targets_group.add(_add_ep_row)

            page.add(targets_group)

        # ── 7. Publish section ────────────────────────────────────────────
        # Browse Prefix for base projects (app projects have it in the Files section)
        if project.project_type == "base":
            base_files_group = Adw.PreferencesGroup(title="Files")
            _browse_row = Adw.ActionRow(
                title="Browse Prefix",
                subtitle="Open drive_c in the file manager",
            )
            _browse_row.set_sensitive(project.initialized)
            _browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
            _browse_btn.set_valign(Gtk.Align.CENTER)
            _browse_btn.add_css_class("flat")
            _browse_btn.connect("clicked", self._on_browse_prefix_clicked)
            _browse_row.add_suffix(_browse_btn)
            base_files_group.add(_browse_row)

            _winecfg_row = Adw.ActionRow(
                title="Wine Configuration",
                subtitle="Open winecfg (DLL overrides, Windows version, …)",
            )
            _winecfg_row.set_sensitive(project.initialized)
            _winecfg_btn = Gtk.Button(label="Open")
            _winecfg_btn.set_valign(Gtk.Align.CENTER)
            _winecfg_btn.connect("clicked", self._on_winecfg_clicked)
            _winecfg_row.add_suffix(_winecfg_btn)
            base_files_group.add(_winecfg_row)

            page.add(base_files_group)

        pkg_group = Adw.PreferencesGroup(title="Publish")

        if project.project_type in ("app", "linux"):
            _ready = (
                bool(project.source_dir) and Path(project.source_dir).is_dir()
                if project.project_type == "linux"
                else project.initialized
            )

            # Test launch
            test_row = Adw.ActionRow(
                title="Test Launch",
                subtitle="Launch the app to verify it works",
            )
            test_row.set_sensitive(_ready)
            test_btn = Gtk.Button(label="Launch")
            test_btn.set_valign(Gtk.Align.CENTER)
            test_btn.connect("clicked", self._on_test_launch_clicked)
            test_row.add_suffix(test_btn)
            pkg_group.add(test_row)

            if project.origin_app_id:
                origin_row = Adw.ActionRow(
                    title="Origin",
                    subtitle=f"Updating catalogue entry: {project.origin_app_id}",
                )
                origin_row.add_css_class("property")
                pkg_group.add(origin_row)

                pub_row = Adw.ActionRow(
                    title="Publish Update",
                    subtitle="Re-archive and replace the catalogue entry",
                )
                pub_row.set_sensitive(_ready and bool(project.entry_points))
                pub_btn = Gtk.Button(label="Publish\u2026")
                pub_btn.set_valign(Gtk.Align.CENTER)
                pub_btn.add_css_class("suggested-action")
                pub_btn.connect("clicked", self._on_publish_update_clicked)
                pub_row.add_suffix(pub_btn)
                pkg_group.add(pub_row)
            else:
                publish_row = Adw.ActionRow(
                    title="Publish App",
                    subtitle="Archive and open Add to Catalogue dialog",
                )
                publish_row.set_sensitive(_ready and bool(project.entry_points))
                pub_btn = Gtk.Button(label="Publish\u2026")
                pub_btn.set_valign(Gtk.Align.CENTER)
                pub_btn.add_css_class("suggested-action")
                pub_btn.connect("clicked", self._on_publish_app_clicked)
                publish_row.add_suffix(pub_btn)
                pkg_group.add(publish_row)

        else:
            # Base: publish base
            publish_row = Adw.ActionRow(
                title="Publish Base",
                subtitle="Archive prefix and runner, and upload to repository",
            )
            publish_row.set_sensitive(project.initialized)
            pub_btn = Gtk.Button(label="Publish\u2026")
            pub_btn.set_valign(Gtk.Align.CENTER)
            pub_btn.add_css_class("suggested-action")
            pub_btn.connect("clicked", self._on_publish_base_clicked)
            publish_row.add_suffix(pub_btn)
            pkg_group.add(publish_row)

        page.add(pkg_group)

        self._detail_stack.set_visible_child_name("detail")

    # ------------------------------------------------------------------
    # Signal handlers — runners (base projects)
    # ------------------------------------------------------------------

    def _populate_runner_expander(self, project: Project) -> None:
        """Populate the Runner group with radio rows for installed runners."""
        from cellar.backend import runners as _runners

        first_check: Gtk.CheckButton | None = None
        for rname in _runners.installed_runners():
            row = Adw.ActionRow(title=rname)
            check = Gtk.CheckButton()
            check.set_valign(Gtk.Align.CENTER)
            if first_check is None:
                first_check = check
            else:
                check.set_group(first_check)
            check.set_active(rname == project.runner)
            check.connect("toggled", self._on_runner_radio_toggled, rname)
            row.add_prefix(check)
            row.set_activatable_widget(check)

            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.add_css_class("flat")
            del_btn.set_tooltip_text("Delete runner")
            del_btn.connect("clicked", self._on_delete_runner_clicked, rname)
            row.add_suffix(del_btn)

            self._sel_expander.add(row)

    def _on_runner_radio_toggled(self, check: Gtk.CheckButton, runner_name: str) -> None:
        """Select a runner for the current base project."""
        if not check.get_active() or self._project is None:
            return
        self._project.runner = runner_name
        self._project.name = runner_name
        for r in self._project_rows:
            if r.project is self._project:
                r.refresh_label()
                break
        save_project(self._project)
        if hasattr(self, "_init_btn"):
            self._init_btn.set_sensitive(
                bool(self._project.runner) and not self._project.initialized
            )
        if hasattr(self, "_sel_active_row"):
            self._sel_active_row.set_subtitle(runner_name)

    def _on_download_runner_clicked(self, _btn) -> None:
        """Open the runner picker to download a new GE-Proton release."""
        project = self._project
        dialog = RunnerPickerDialog(
            on_installed=lambda name: self._on_runner_installed(name, project),
        )
        dialog.present(self)

    def _on_runner_installed(self, runner_name: str, project: Project | None) -> None:
        """Called after a runner finishes installing — refresh the detail panel."""
        if project is not None and self._project is project:
            if not project.runner:
                project.runner = runner_name
                project.name = runner_name
                for r in self._project_rows:
                    if r.project is project:
                        r.refresh_label()
                        break
                save_project(project)
            self._show_project(project, expand_sel=True)

    def _on_delete_runner_clicked(self, _btn, runner_name: str) -> None:
        """Confirm and delete an installed runner."""
        projects = load_projects()
        using = [p.name for p in projects if p.runner == runner_name]

        body = f"Delete runner \u201c{runner_name}\u201d?"
        if using:
            names = ", ".join(using)
            body += f"\n\nUsed by: {names}"

        dialog = Adw.AlertDialog(heading="Delete Runner", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda d, r: self._do_delete_runner(runner_name) if r == "delete" else None,
        )
        dialog.present(self)

    def _do_delete_runner(self, runner_name: str) -> None:
        from cellar.backend.runners import remove_runner
        remove_runner(runner_name)
        if self._project is not None:
            if self._project.runner == runner_name:
                self._project.runner = ""
                self._project.name = "(no runner)"
                for r in self._project_rows:
                    if r.project is self._project:
                        r.refresh_label()
                        break
                save_project(self._project)
            self._show_project(self._project, expand_sel=True)

    # ------------------------------------------------------------------
    # Signal handlers — base images (app projects)
    # ------------------------------------------------------------------

    def _populate_base_expander(self, project: Project) -> None:
        """Populate the Base Image expander with radio rows for installed bases."""
        from cellar.backend.database import get_all_installed_bases

        bases = get_all_installed_bases()
        base_runners = [b["runner"] for b in bases]

        # If no runner set yet, default to the latest installed base (last by installed_at)
        effective_runner = project.runner or (base_runners[-1] if base_runners else "")
        if effective_runner and not project.runner:
            project.runner = effective_runner
            save_project(project)
            if hasattr(self, "_sel_active_row"):
                self._sel_active_row.set_subtitle(effective_runner)

        first_check: Gtk.CheckButton | None = None
        for runner in base_runners:
            row = Adw.ActionRow(title=runner)
            check = Gtk.CheckButton()
            check.set_valign(Gtk.Align.CENTER)
            if first_check is None:
                first_check = check
            else:
                check.set_group(first_check)
            check.set_active(runner == effective_runner)
            check.connect("toggled", self._on_base_radio_toggled, runner)
            row.add_prefix(check)
            row.set_activatable_widget(check)

            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.add_css_class("flat")
            del_btn.set_tooltip_text("Delete base image")
            del_btn.connect("clicked", self._on_delete_base_clicked, runner)
            row.add_suffix(del_btn)

            self._sel_expander.add_row(row)

        add_row = Adw.ActionRow(title="Download Base Image")
        add_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._on_download_base_clicked)
        add_row.add_suffix(add_btn)
        add_row.set_activatable_widget(add_btn)
        self._sel_expander.add_row(add_row)

    def _on_base_radio_toggled(self, check: Gtk.CheckButton, runner: str) -> None:
        """Select a base image for the current app project."""
        if not check.get_active() or self._project is None:
            return
        self._project.runner = runner
        save_project(self._project)
        if hasattr(self, "_init_btn"):
            self._init_btn.set_sensitive(
                bool(self._project.runner) and not self._project.initialized
            )
        if hasattr(self, "_sel_active_row"):
            self._sel_active_row.set_subtitle(runner)

    def _on_download_base_clicked(self, _btn) -> None:
        """Open the base picker to download a base image from a repo."""
        project = self._project
        dialog = BasePickerDialog(
            repos=self._all_repos,
            on_installed=lambda runner: self._on_base_installed(runner, project),
        )
        dialog.present(self)

    def _on_base_installed(self, runner: str, project: Project | None) -> None:
        """Called after a base finishes installing — refresh the detail panel."""
        if project is not None and self._project is project:
            if not project.runner:
                project.runner = runner
                save_project(project)
            self._show_project(project, expand_sel=True)

    def _on_delete_base_clicked(self, _btn, runner: str) -> None:
        """Confirm and delete an installed base image."""
        dialog = Adw.AlertDialog(
            heading="Delete Base Image",
            body=f"Delete base image \u201c{runner}\u201d from local storage?",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda d, r: self._do_delete_base(runner) if r == "delete" else None,
        )
        dialog.present(self)

    def _do_delete_base(self, runner: str) -> None:
        from cellar.backend.base_store import remove_base
        remove_base(runner)
        if self._project is not None:
            if self._project.runner == runner:
                self._project.runner = ""
                save_project(self._project)
            self._show_project(self._project, expand_sel=True)

    def _on_init_prefix_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if project.project_type == "linux":
            project.prefix_path.mkdir(parents=True, exist_ok=True)
            self._on_init_done(project, True)
            return
        if not project.runner:
            return
        project.prefix_path.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label="Initializing prefix…")
        progress.present(self)

        def _work():
            from cellar.backend.umu import init_prefix
            result = init_prefix(
                project.prefix_path,
                project.runner,
                steam_appid=project.steam_appid,
            )
            # umu-run "" initializes the prefix then tries to execute an
            # empty string, which Wine rejects with exit code 1.  Use the
            # presence of drive_c as the real success indicator.
            return result.returncode == 0 or (project.prefix_path / "drive_c").is_dir()

        def _finish(ok: bool) -> None:
            progress.force_close()
            self._on_init_done(project, ok)
            if not ok:
                self._show_toast("Prefix initialization failed. Check logs.")

        def _on_err(msg: str) -> None:
            log.error("init_prefix failed: %s", msg)
            _finish(False)

        run_in_background(_work, on_done=_finish, on_error=_on_err)

    def _on_init_done(self, project: Project, ok: bool) -> None:
        if ok:
            project.initialized = True
            save_project(project)
            self._show_project(project)

    # ------------------------------------------------------------------
    # Signal handlers — metadata
    # ------------------------------------------------------------------

    def _on_meta_steam_lookup(self, _btn) -> None:
        if self._project is None:
            return
        from cellar.views.steam_picker import SteamPickerDialog
        query = self._project.name
        if hasattr(self, "_meta_name_row"):
            query = self._meta_name_row.get_text().strip() or query
        picker = SteamPickerDialog(query=query, on_picked=self._apply_steam_to_meta)
        picker.present(self.get_root())

    def _apply_steam_to_meta(self, result: dict) -> None:
        if self._project is None:
            return
        p = self._project
        if result.get("name") and hasattr(self, "_meta_name_row"):
            self._meta_name_row.set_text(result["name"])
        if result.get("developer") and not p.developer:
            p.developer = result["developer"]
        if result.get("publisher") and not p.publisher:
            p.publisher = result["publisher"]
        if result.get("year") and not p.release_year:
            p.release_year = result["year"]
        if result.get("summary") and not p.summary:
            p.summary = result["summary"]
        if result.get("description") and not p.description:
            p.description = result["description"]
        if result.get("steam_appid") and p.steam_appid is None:
            p.steam_appid = result["steam_appid"]
        if result.get("website") and not p.website:
            p.website = result["website"]
        if result.get("genres") and not p.genres:
            p.genres = list(result["genres"])
        if result.get("category") and not p.category:
            from cellar.backend.packager import BASE_CATEGORIES as _BASE_CATS
            if result["category"] in _BASE_CATS:
                p.category = result["category"]
        save_project(p)
        self._show_project(p)

    def _on_edit_metadata_clicked(self, _btn) -> None:
        if self._project is None:
            return
        dialog = AppMetadataDialog(
            project=self._project,
            on_changed=lambda: self._show_project(self._project),
        )
        dialog.present(self)

    def _make_metadata_summary(self, project: Project) -> str:
        """One-line summary of filled optional metadata for the Details row subtitle."""
        parts: list[str] = []
        if project.category:
            parts.append(project.category)
        if project.developer:
            parts.append(project.developer)
        if project.release_year:
            parts.append(str(project.release_year))
        return "  ·  ".join(parts)

    # ------------------------------------------------------------------
    # Signal handlers — dependencies
    # ------------------------------------------------------------------

    def _on_add_dep_clicked(self, _btn) -> None:
        if self._project is None:
            return
        if not self._project.runner:
            self._show_toast("Select a runner before adding dependencies.")
            return
        dialog = DependencyPickerDialog(
            project=self._project,
            on_dep_changed=lambda: self._show_project(self._project),
        )
        dialog.present(self)

    def _on_remove_dep_clicked(self, _btn, verb: str) -> None:
        if self._project is None:
            return
        if verb in self._project.deps_installed:
            self._project.deps_installed.remove(verb)
            save_project(self._project)
            self._show_project(self._project)

    # ------------------------------------------------------------------
    # Signal handlers — files
    # ------------------------------------------------------------------

    def _on_run_installer_clicked(self, _btn) -> None:
        if self._project is None or not self._project.runner:
            self._show_toast("Select a runner before running an installer.")
            return
        project = self._project
        chooser = Gtk.FileChooserNative(
            title="Select Installer (.exe)",
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Run",
        )
        win = self.get_root()
        if isinstance(win, Gtk.Window):
            chooser.set_transient_for(win)
        f = Gtk.FileFilter()
        f.set_name("Windows executables")
        f.add_pattern("*.exe")
        f.add_pattern("*.msi")
        chooser.add_filter(f)
        chooser.connect(
            "response",
            lambda c, r: self._on_installer_chosen(c, r, project),
        )
        chooser.show()
        # Keep a reference
        self._installer_chooser = chooser

    def _on_installer_chosen(
        self, chooser: Gtk.FileChooserNative, response: int, project: Project
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        exe_path = chooser.get_file().get_path()
        self._run_in_prefix_with_progress(
            project,
            exe=exe_path,
            label=f"Running {Path(exe_path).name}…",
            on_done=lambda ok: log.info("Installer exited ok=%s", ok),
        )

    def _on_choose_source_dir_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        chooser = Gtk.FileChooserNative(
            title="Select Installation Folder",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="Select",
        )
        win = self.get_root()
        if isinstance(win, Gtk.Window):
            chooser.set_transient_for(win)
        if project.source_dir and Path(project.source_dir).parent.is_dir():
            chooser.set_current_folder(
                Gio.File.new_for_path(str(Path(project.source_dir).parent))
            )
        chooser.connect("response", lambda c, r: self._on_source_dir_chosen(c, r, project))
        chooser.show()
        self._source_dir_chooser = chooser

    def _on_source_dir_chosen(
        self, chooser: Gtk.FileChooserNative, response: int, project: Project
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        path = chooser.get_file().get_path()
        project.source_dir = path
        save_project(project)
        self._show_project(project)

    def _on_browse_prefix_clicked(self, _btn) -> None:
        if self._project is None:
            return
        if self._project.project_type == "linux":
            target = Path(self._project.source_dir) if self._project.source_dir else None
        else:
            target = self._project.prefix_path / "drive_c"
            if not target.is_dir():
                target = self._project.prefix_path
        if not target or not target.is_dir():
            self._show_toast("Directory not set yet.")
            return
        subprocess.Popen(["xdg-open", str(target)], start_new_session=True)

    def _on_winecfg_clicked(self, _btn) -> None:
        if self._project is None or not self._project.runner:
            self._show_toast("Select a runner first.")
            return
        from cellar.backend.umu import launch_app
        launch_app(
            app_id=f"project-{self._project.slug}",
            entry_point="winecfg",
            runner_name=self._project.runner,
            steam_appid=self._project.steam_appid,
            prefix_dir=self._project.prefix_path,
        )

    def _on_add_entry_point_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if project.project_type == "linux":
            if not project.source_dir:
                self._show_toast("Choose a source folder first.")
                return
            prefix_path = Path(project.source_dir)
            platform = "linux"
        else:
            prefix_path = project.prefix_path
            platform = "windows"
        dialog = AddLaunchTargetDialog(
            prefix_path=prefix_path,
            platform=platform,
            on_added=lambda ep: self._on_entry_point_added(project, ep),
        )
        dialog.present(self)

    def _on_entry_point_added(self, project: Project, ep: dict) -> None:
        project.entry_points.append(ep)
        save_project(project)
        self._show_project(project)

    def _on_remove_entry_point_clicked(self, _btn, ep: dict) -> None:
        if self._project is None:
            return
        try:
            self._project.entry_points.remove(ep)
        except ValueError:
            return
        save_project(self._project)
        self._show_project(self._project)

    # ------------------------------------------------------------------
    # Signal handlers — package
    # ------------------------------------------------------------------

    def _on_test_launch_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if not project.entry_point:
            self._show_toast("Add a launch target before test launching.")
            return
        if project.project_type == "linux":
            if not project.source_dir:
                self._show_toast("Set a source folder first.")
                return
            exe = Path(project.source_dir) / project.entry_point
            if not exe.exists():
                self._show_toast(f"Executable not found: {exe}")
                return
            import shlex
            cmd = [str(exe)]
            if project.entry_args:
                cmd += shlex.split(project.entry_args)
            subprocess.Popen(cmd, start_new_session=True)
            return
        if not project.runner:
            self._show_toast("Select a runner before test launching.")
            return
        from cellar.backend.umu import launch_app
        launch_app(
            app_id=f"project-{project.slug}",
            entry_point=project.entry_point,
            runner_name=project.runner,
            steam_appid=project.steam_appid,
            prefix_dir=project.prefix_path,
            launch_args=project.entry_args,
        )

    def _on_publish_app_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if not project.entry_point:
            self._show_toast("Add a launch target before publishing.")
            return
        if project.project_type == "linux" and not project.source_dir:
            self._show_toast("Choose a source folder before publishing.")
            return
        if project.project_type != "linux" and not project.runner:
            self._show_toast("Select a runner before publishing.")
            return
        if not project.category:
            self._show_toast("Set a category in Metadata before publishing.")
            return
        if not self._writable_repos:
            self._show_toast("No writable repository configured.")
            return

        repo = self._writable_repos[0]
        _src_path = Path(project.source_dir) if project.project_type == "linux" else project.prefix_path

        # Build AppEntry from project metadata.
        from cellar.models.app_entry import AppEntry, BuiltWith
        _slug = project.slug
        _icon_ext = Path(project.icon_path).suffix if project.icon_path else ".png"
        _cover_ext = Path(project.cover_path).suffix if project.cover_path else ".jpg"
        entry = AppEntry(
            id=_slug,
            name=project.name,
            version=project.version or "1.0",
            category=project.category,
            summary=project.summary,
            description=project.description,
            developer=project.developer,
            publisher=project.publisher,
            release_year=project.release_year,
            website=project.website,
            genres=tuple(project.genres),
            steam_appid=project.steam_appid,
            icon=f"apps/{_slug}/icon{_icon_ext}" if project.icon_path else "",
            cover=f"apps/{_slug}/cover{_cover_ext}" if project.cover_path else "",
            logo=f"apps/{_slug}/logo.png" if project.logo_path else "",
            hide_title=project.hide_title,
            screenshots=tuple(
                f"apps/{_slug}/screenshots/{i + 1:02d}{Path(p).suffix}"
                for i, p in enumerate(project.screenshot_paths)
            ),
            archive=f"apps/{_slug}/{_slug}.tar.zst",
            entry_point=project.entry_point,
            launch_args=project.entry_args,
            update_strategy="safe",
            platform="linux" if project.project_type == "linux" else "windows",
            built_with=None if project.project_type == "linux" else BuiltWith(runner=project.runner),
        )
        images: dict = {}
        if project.icon_path:
            images["icon"] = project.icon_path
        if project.cover_path:
            images["cover"] = project.cover_path
        if project.logo_path:
            images["logo"] = project.logo_path
        if project.screenshot_paths:
            images["screenshots"] = list(project.screenshot_paths)

        progress = ProgressDialog(label="Compressing…")
        progress.present(self)

        from cellar.utils.progress import fmt_size, trunc_middle as _trunc
        _current_file: list[str] = [""]

        def _file_cb(name: str) -> None:
            _current_file[0] = name
            GLib.idle_add(progress.set_stats, _trunc(name, 40))

        def _bytes_cb(n: int) -> None:
            name = _current_file[0]
            text = (_trunc(name, 28) + " \u2022 " if name else "") + fmt_size(n) + " written"
            GLib.idle_add(progress.set_stats, text)

        cancel_event = threading.Event()

        def _reset_phase(label: str) -> None:
            _current_file[0] = ""
            GLib.idle_add(progress.set_label, label)
            GLib.idle_add(progress.set_stats, "")
            GLib.idle_add(progress.set_fraction, 0.0)

        def _work():
            from cellar.backend.packager import (
                compress_prefix_zst, compress_prefix_delta_zst, import_to_repo,
            )
            repo_root = repo.writable_path()
            archive_dest = repo_root / entry.archive
            archive_dest.parent.mkdir(parents=True, exist_ok=True)

            if project.project_type == "linux":
                size, crc32 = compress_prefix_zst(
                    _src_path,
                    archive_dest,
                    cancel_event=cancel_event,
                    progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                    file_cb=_file_cb,
                    bytes_cb=_bytes_cb,
                )
                base_runner = ""
            else:
                from cellar.backend.base_store import is_base_installed, base_path
                _use_delta = is_base_installed(project.runner)
                if _use_delta:
                    _reset_phase("Scanning files…")
                    size, crc32 = compress_prefix_delta_zst(
                        _src_path,
                        base_path(project.runner),
                        archive_dest,
                        cancel_event=cancel_event,
                        phase_cb=_reset_phase,
                        progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                        file_cb=_file_cb,
                        bytes_cb=_bytes_cb,
                    )
                    base_runner = project.runner
                else:
                    size, crc32 = compress_prefix_zst(
                        project.prefix_path,
                        archive_dest,
                        cancel_event=cancel_event,
                        progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                        file_cb=_file_cb,
                        bytes_cb=_bytes_cb,
                    )
                    base_runner = ""

            GLib.idle_add(progress.set_label, "Finalizing…")
            GLib.idle_add(progress.set_stats, "")
            GLib.idle_add(progress.start_pulse)
            final_entry = _dc_replace(
                entry,
                archive_crc32=crc32,
                archive_size=size,
                base_runner=base_runner,
            )
            import_to_repo(
                repo_root,
                final_entry,
                "",
                images,
                archive_in_place=True,
                phase_cb=lambda s: GLib.idle_add(progress.set_label, s),
            )

        def _done(_result) -> None:
            progress.force_close()
            delete_project(project.slug)
            self._project = None
            self._reload_projects()
            self._detail_stack.set_visible_child_name("empty")
            self._show_toast(f"Published '{project.name}' to {repo.name or repo.uri}.")
            if self._on_catalogue_changed:
                self._on_catalogue_changed()

        def _error(msg: str) -> None:
            progress.force_close()
            err = Adw.AlertDialog(heading="Publish failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

    def _on_publish_update_clicked(self, _btn) -> None:
        """Re-publish: update an existing catalogue entry in-place."""
        if self._project is None:
            return
        project = self._project
        if not project.origin_app_id:
            return
        if not project.entry_point:
            self._show_toast("Add a launch target before publishing.")
            return
        if project.project_type == "linux" and not project.source_dir:
            self._show_toast("Choose a source folder before publishing.")
            return
        if project.project_type != "linux" and not project.runner:
            self._show_toast("Select a runner before publishing.")
            return
        if not self._writable_repos:
            self._show_toast("No writable repository configured.")
            return

        _src_path = Path(project.source_dir) if project.project_type == "linux" else project.prefix_path

        # Find a writable repo that has this entry.
        repo = None
        old_entry = None
        for r in self._writable_repos:
            try:
                old_entry = r.fetch_entry_by_id(project.origin_app_id)
                repo = r
                break
            except Exception:
                pass
        if repo is None or old_entry is None:
            self._show_toast(
                f"Could not find '{project.origin_app_id}' in any writable repository."
            )
            return

        progress = ProgressDialog(label="Compressing…")
        progress.present(self)

        from cellar.utils.progress import fmt_size, trunc_middle as _trunc
        _current_file: list[str] = [""]

        def _file_cb(name: str) -> None:
            _current_file[0] = name
            GLib.idle_add(progress.set_stats, _trunc(name, 40))

        def _bytes_cb(n: int) -> None:
            name = _current_file[0]
            text = (_trunc(name, 28) + " \u2022 " if name else "") + fmt_size(n) + " written"
            GLib.idle_add(progress.set_stats, text)

        cancel_event = threading.Event()

        def _reset_phase(label: str) -> None:
            _current_file[0] = ""
            GLib.idle_add(progress.set_label, label)
            GLib.idle_add(progress.set_stats, "")
            GLib.idle_add(progress.set_fraction, 0.0)

        def _work():
            from cellar.backend.packager import (
                compress_prefix_zst, compress_prefix_delta_zst, update_in_repo,
            )
            repo_root = repo.writable_path()
            archive_dest = repo_root / old_entry.archive
            archive_dest.parent.mkdir(parents=True, exist_ok=True)

            if project.project_type == "linux":
                size, crc32 = compress_prefix_zst(
                    _src_path,
                    archive_dest,
                    cancel_event=cancel_event,
                    progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                    file_cb=_file_cb,
                    bytes_cb=_bytes_cb,
                )
                base_runner = ""
            else:
                from cellar.backend.base_store import is_base_installed, base_path
                _use_delta = is_base_installed(project.runner)
                if _use_delta:
                    _reset_phase("Scanning files…")
                    size, crc32 = compress_prefix_delta_zst(
                        project.prefix_path,
                        base_path(project.runner),
                        archive_dest,
                        cancel_event=cancel_event,
                        phase_cb=_reset_phase,
                        progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                        file_cb=_file_cb,
                        bytes_cb=_bytes_cb,
                    )
                    base_runner = project.runner
                else:
                    size, crc32 = compress_prefix_zst(
                        project.prefix_path,
                        archive_dest,
                        cancel_event=cancel_event,
                        progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                        file_cb=_file_cb,
                        bytes_cb=_bytes_cb,
                    )
                    base_runner = old_entry.base_runner  # preserve existing delta setting

            GLib.idle_add(progress.set_label, "Finalizing…")
            GLib.idle_add(progress.set_stats, "")
            GLib.idle_add(progress.start_pulse)
            new_entry = _dc_replace(
                old_entry,
                archive_crc32=crc32,
                archive_size=size,
                base_runner=base_runner,
            )
            update_in_repo(
                repo_root,
                old_entry,
                new_entry,
                images={},
                new_archive_src=None,
                phase_cb=lambda s: GLib.idle_add(progress.set_label, s),
            )

        def _done(_result) -> None:
            progress.force_close()
            delete_project(project.slug)
            self._project = None
            self._reload_projects()
            self._detail_stack.set_visible_child_name("empty")
            self._show_toast(f"Update published for {project.name}.")
            if self._on_catalogue_changed:
                self._on_catalogue_changed()

        def _error(msg: str) -> None:
            progress.force_close()
            err = Adw.AlertDialog(heading="Publish failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

    def _on_publish_base_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if not project.runner:
            self._show_toast("Select a runner before publishing.")
            return
        if not self._writable_repos:
            self._show_toast("No writable repository configured.")
            return

        repo = self._writable_repos[0]
        progress = ProgressDialog(label="Compressing and uploading…")
        progress.present(self)

        from cellar.utils.progress import fmt_size, trunc_middle as _trunc
        _current_file: list[str] = [""]

        def _file_cb(name: str) -> None:
            _current_file[0] = name
            GLib.idle_add(progress.set_stats, _trunc(name, 40))

        def _bytes_cb(n: int) -> None:
            name = _current_file[0]
            text = (_trunc(name, 28) + " \u2022 " if name else "") + fmt_size(n) + " written"
            GLib.idle_add(progress.set_stats, text)

        def _work():
            from cellar.backend.packager import (
                compress_prefix_zst, compress_runner_zst, upsert_base,
            )
            from cellar.backend.base_store import install_base_from_dir
            from cellar.backend.umu import runners_dir

            runner = project.runner
            repo_root = repo.writable_path()

            # ── Compress and upload the runner ────────────────────────────
            runner_src = runners_dir() / runner
            runner_archive_rel = f"runners/{runner}.tar.zst"
            runner_archive_dest = repo_root / runner_archive_rel
            runner_archive_dest.parent.mkdir(parents=True, exist_ok=True)

            GLib.idle_add(progress.set_label, "Compressing and uploading runner…")
            GLib.idle_add(progress.set_fraction, 0.0)
            runner_size, runner_crc32 = compress_runner_zst(
                runner_src,
                runner_archive_dest,
                progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                file_cb=_file_cb,
                bytes_cb=_bytes_cb,
            )

            # ── Compress and upload the base image ────────────────────────
            GLib.idle_add(progress.set_label, "Compressing and uploading base image…")
            GLib.idle_add(progress.set_fraction, 0.0)
            archive_dest_rel = f"bases/{runner}-base.tar.zst"
            archive_dest = repo_root / archive_dest_rel
            archive_dest.parent.mkdir(parents=True, exist_ok=True)

            size, crc32 = compress_prefix_zst(
                project.prefix_path,
                archive_dest,
                progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                file_cb=_file_cb,
                bytes_cb=_bytes_cb,
            )

            GLib.idle_add(progress.set_label, "Finalizing…")
            GLib.idle_add(progress.set_stats, "")
            GLib.idle_add(progress.start_pulse)
            upsert_base(
                repo_root, runner, runner, archive_dest_rel, crc32, size,
                runner_archive=runner_archive_rel,
                runner_archive_crc32=runner_crc32,
                runner_archive_size=runner_size,
            )

            GLib.idle_add(progress.set_label, "Installing base locally…")
            GLib.idle_add(progress.set_fraction, 0.0)
            install_base_from_dir(
                project.prefix_path,
                runner,
                repo_source=repo.uri,
                progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
            )

        def _done(_result) -> None:
            progress.force_close()
            delete_project(project.slug)
            self._project = None
            self._reload_projects()
            self._detail_stack.set_visible_child_name("empty")
            self._show_toast(f"Base '{project.runner}' published.")
            if self._on_catalogue_changed:
                self._on_catalogue_changed()

        def _error(msg: str) -> None:
            progress.force_close()
            err = Adw.AlertDialog(heading="Failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_in_prefix_with_progress(
        self,
        project: Project,
        exe: str,
        label: str,
        on_done: Callable[[bool], None],
    ) -> None:
        """Run *exe* in *project*'s prefix on a background thread with a progress dialog."""
        if not project.runner:
            self._show_toast("Select a runner first.")
            return

        project.prefix_path.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label=label)
        progress.present(self)

        def _work():
            from cellar.backend.umu import run_in_prefix
            result = run_in_prefix(
                project.prefix_path,
                project.runner,
                exe,
                timeout=600,
            )
            return result.returncode == 0

        def _finish(ok: bool) -> None:
            progress.force_close()
            on_done(ok)
            if not ok:
                self._show_toast("Command exited with non-zero status. Check logs.")

        def _on_err(msg: str) -> None:
            log.error("run_in_prefix failed: %s", msg)
            _finish(False)

        run_in_background(_work, on_done=_finish, on_error=_on_err)

    def _show_toast(self, message: str) -> None:
        win = self.get_root()
        if hasattr(win, "toast_overlay"):
            win.toast_overlay.add_toast(Adw.Toast(title=message))


# ---------------------------------------------------------------------------
# Helper widgets
# ---------------------------------------------------------------------------

class _ProjectRow(Gtk.ListBoxRow):
    """A row in the project list sidebar."""

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(12)
        box.set_margin_end(12)

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        self._label = Gtk.Label(label=project.name, xalign=0)
        self._label.set_hexpand(True)
        self._label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        top.append(self._label)

        _badge_labels = {"base": "Base", "linux": "Linux"}
        badge = Gtk.Label(label=_badge_labels.get(project.project_type, "App"))
        badge.add_css_class("caption")
        badge.add_css_class("dim-label")
        top.append(badge)

        box.append(top)

        self.set_child(box)

    def refresh_label(self) -> None:
        """Update the displayed name (called when runner/winver changes)."""
        self._label.set_label(self.project.name)


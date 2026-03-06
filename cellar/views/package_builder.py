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

import html
import logging
import os
import re
import subprocess
import threading
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import Callable

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

log = logging.getLogger(__name__)

# Curated winetricks verbs grouped by category and sorted alphabetically.
# Format: (category_name, [(verb, description), ...])
_VERB_CATALOGUE: list[tuple[str, list[tuple[str, str]]]] = [
    ("Visual C++ Runtimes", [
        ("vcrun2003", "Visual C++ 2003 Redistributable"),
        ("vcrun2005", "Visual C++ 2005 Redistributable"),
        ("vcrun2008", "Visual C++ 2008 Redistributable"),
        ("vcrun2010", "Visual C++ 2010 Redistributable"),
        ("vcrun2012", "Visual C++ 2012 Redistributable"),
        ("vcrun2013", "Visual C++ 2013 Redistributable"),
        ("vcrun2015", "Visual C++ 2015 Redistributable"),
        ("vcrun2017", "Visual C++ 2017 Redistributable"),
        ("vcrun2019", "Visual C++ 2019 Redistributable"),
        ("vcrun2022", "Visual C++ 2022 Redistributable"),
        ("vcrun6",    "Visual C++ 6.0 SP6 runtime"),
    ]),
    (".NET Framework", [
        ("dotnet11",  ".NET Framework 1.1"),
        ("dotnet20",  ".NET Framework 2.0"),
        ("dotnet30",  ".NET Framework 3.0"),
        ("dotnet35",  ".NET Framework 3.5"),
        ("dotnet40",  ".NET Framework 4.0"),
        ("dotnet45",  ".NET Framework 4.5"),
        ("dotnet452", ".NET Framework 4.5.2"),
        ("dotnet46",  ".NET Framework 4.6"),
        ("dotnet461", ".NET Framework 4.6.1"),
        ("dotnet462", ".NET Framework 4.6.2"),
        ("dotnet471", ".NET Framework 4.7.1"),
        ("dotnet472", ".NET Framework 4.7.2"),
        ("dotnet48",  ".NET Framework 4.8"),
        ("dotnet6",   ".NET 6.0 desktop runtime"),
        ("dotnet7",   ".NET 7.0 desktop runtime"),
        ("dotnet8",   ".NET 8.0 desktop runtime"),
    ]),
    ("DirectX", [
        ("d3dcompiler_43", "D3DCompiler 43"),
        ("d3dcompiler_47", "D3DCompiler 47"),
        ("d3dx10",         "DirectX 10 DLLs"),
        ("d3dx11_42",      "DirectX 11 DLL (d3dx11_42)"),
        ("d3dx11_43",      "DirectX 11 DLL (d3dx11_43)"),
        ("d3dx9",          "DirectX 9 DLLs (all versions)"),
        ("dinput8",        "DirectInput 8"),
        ("xact",           "XACT Engine"),
        ("xactengine3_7",  "XACT Engine 3.7"),
    ]),
    ("Media & Codecs", [
        ("amstream",   "DirectShow amstream.dll"),
        ("devenum",    "DirectShow devenum.dll"),
        ("lavfilters", "LAV Filters (open-source media codecs)"),
        ("openal",     "OpenAL audio library"),
        ("quartz",     "DirectShow quartz.dll"),
        ("wmp10",      "Windows Media Player 10"),
        ("wmp11",      "Windows Media Player 11"),
        ("wmp9",       "Windows Media Player 9"),
        ("wmv9vcm",    "MS WMV9 Video Codec"),
    ]),
    ("Fonts", [
        ("allfonts",   "All winetricks fonts"),
        ("corefonts",  "Microsoft Core Fonts (Arial, Times New Roman…)"),
        ("liberation", "Liberation fonts (free Arial/Times/Courier)"),
        ("tahoma",     "MS Tahoma"),
    ]),
    ("System DLLs", [
        ("gdiplus",  "Microsoft GDI+"),
        ("mfc100",   "Microsoft Foundation Classes 10.0"),
        ("mfc110",   "Microsoft Foundation Classes 11.0"),
        ("mfc120",   "Microsoft Foundation Classes 12.0"),
        ("mfc140",   "Microsoft Foundation Classes 14.0"),
        ("mfc42",    "Microsoft Foundation Classes 4.2"),
        ("msvcirt",  "MS VC++ 6.0 C++ runtime (msvcirt.dll)"),
        ("msxml3",   "MS XML 3.0"),
        ("msxml4",   "MS XML 4.0"),
        ("msxml6",   "MS XML 6.0 SP1"),
    ]),
    ("Game Runtimes", [
        ("gfw",   "Games for Windows LIVE"),
        ("physx", "NVIDIA PhysX"),
        ("xna31", "Microsoft XNA Framework 3.1"),
        ("xna40", "Microsoft XNA Framework 4.0"),
    ]),
]


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
        header.set_title_widget(Gtk.Label(label="Projects"))

        btn_box = Gtk.Box(spacing=4)
        btn_box.set_margin_start(4)
        btn_box.set_margin_end(4)

        new_app_btn = Gtk.Button(icon_name="list-add-symbolic")
        new_app_btn.set_tooltip_text("New App Project")
        new_app_btn.add_css_class("flat")
        new_app_btn.connect("clicked", self._on_new_app_clicked)

        new_base_btn = Gtk.Button(icon_name="package-x-generic-symbolic")
        new_base_btn.set_tooltip_text("New Base Project")
        new_base_btn.add_css_class("flat")
        new_base_btn.connect("clicked", self._on_new_base_clicked)

        import_btn = Gtk.Button(icon_name="document-save-symbolic")
        import_btn.set_tooltip_text("Import from catalogue…")
        import_btn.add_css_class("flat")
        import_btn.connect("clicked", self._on_import_clicked)

        self._delete_btn = Gtk.Button(icon_name="edit-delete-symbolic")
        self._delete_btn.set_tooltip_text("Delete project")
        self._delete_btn.add_css_class("flat")
        self._delete_btn.add_css_class("destructive-action")
        self._delete_btn.set_sensitive(False)
        self._delete_btn.connect("clicked", self._on_delete_clicked)

        btn_box.append(new_app_btn)
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
            title="No Project Selected",
            description="Create a new project or select one from the list.",
            icon_name="package-x-generic-symbolic",
        )
        self._detail_stack.add_named(empty, "empty")

        # Detail container — hint banner (pinned) + scroll (populated by _show_project())
        detail_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        detail_box.set_vexpand(True)

        self._hint_banner = Adw.Banner(title="", revealed=False)
        detail_box.append(self._hint_banner)

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
        dialog = _AppMetadataDialog(on_created=self._on_project_created)
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
        dialog = _ImportFromCatalogueDialog(
            repos=self._all_repos,
            on_imported=self._on_project_imported,
        )
        dialog.present(self)

    def _on_project_imported(self, project: Project | None) -> None:
        if project is None:
            # A base image was downloaded — no new project, just show a toast.
            self._show_toast("Base image installed.")
            return
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

        # Update the pinned hint banner
        hint = self._get_next_step_hint(project)
        self._hint_banner.set_title(hint)
        self._hint_banner.set_revealed(bool(hint))

        # ── 1. Metadata section (App projects only — first, to set title/slug) ──
        if project.project_type == "app":
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

            # Details summary row — opens _AppMetadataDialog
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

        # ── 2. Runner / Base Image ────────────────────────────────────────
        sel_group = Adw.PreferencesGroup()
        if project.project_type == "base":
            expander_title = "Runner"
            expander_subtitle = project.runner or "No runner selected"
        else:
            expander_title = "Base Image"
            expander_subtitle = project.runner or "No base image selected"
        self._sel_expander = Adw.ExpanderRow(
            title=expander_title,
            subtitle=expander_subtitle,
        )
        self._sel_expander.set_expanded(expand_sel)
        sel_group.add(self._sel_expander)
        page.add(sel_group)

        if project.project_type == "base":
            self._populate_runner_expander(project)
        else:
            self._populate_base_expander(project)

        # ── 3. Prefix ─────────────────────────────────────────────────────
        prefix_group = Adw.PreferencesGroup(title="Prefix")
        prefix_exists = project.prefix_path.is_dir()
        status_text = "Initialized" if (prefix_exists and project.initialized) else (
            "Directory exists (not initialized)" if prefix_exists else "Not initialized"
        )
        self._prefix_status_row = Adw.ActionRow(
            title="Status",
            subtitle=status_text,
        )
        self._init_btn = Gtk.Button(label="Initialize")
        self._init_btn.set_valign(Gtk.Align.CENTER)
        self._init_btn.add_css_class("suggested-action")
        self._init_btn.set_sensitive(bool(project.runner) and not project.initialized)
        self._init_btn.connect("clicked", self._on_init_prefix_clicked)
        self._prefix_status_row.add_suffix(self._init_btn)
        prefix_group.add(self._prefix_status_row)
        page.add(prefix_group)

        # ── 4. Dependencies ───────────────────────────────────────────────
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

        # ── 6. Files section (App projects only) ──────────────────────────
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

            page.add(files_group)

            # Launch Targets
            targets_group = Adw.PreferencesGroup(title="Launch Targets")
            for _ep in project.entry_points:
                _ep_row = Adw.ActionRow(
                    title=_ep.get("name", ""),
                    subtitle=_ep.get("path", ""),
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
            page.add(base_files_group)

        pkg_group = Adw.PreferencesGroup(title="Publish")

        if project.project_type == "app":
            # Test launch
            test_row = Adw.ActionRow(
                title="Test Launch",
                subtitle="Launch the app to verify it works",
            )
            test_row.set_sensitive(project.initialized)
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
                    subtitle="Re-archive prefix and replace the catalogue entry",
                )
                pub_row.set_sensitive(project.initialized)
                pub_btn = Gtk.Button(label="Publish\u2026")
                pub_btn.set_valign(Gtk.Align.CENTER)
                pub_btn.add_css_class("suggested-action")
                pub_btn.connect("clicked", self._on_publish_update_clicked)
                pub_row.add_suffix(pub_btn)
                pkg_group.add(pub_row)
            else:
                publish_row = Adw.ActionRow(
                    title="Publish App",
                    subtitle="Archive prefix and open Add to Catalogue dialog",
                )
                publish_row.set_sensitive(project.initialized)
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
                subtitle="Archive prefix and upload to repository",
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
        """Populate the Runner expander with radio rows for installed runners."""
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

            self._sel_expander.add_row(row)

        add_row = Adw.ActionRow(title="Download Runner")
        add_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._on_download_runner_clicked)
        add_row.add_suffix(add_btn)
        add_row.set_activatable_widget(add_btn)
        self._sel_expander.add_row(add_row)

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
        if hasattr(self, "_sel_expander"):
            self._sel_expander.set_subtitle(runner_name)

    def _on_download_runner_clicked(self, _btn) -> None:
        """Open the runner picker to download a new GE-Proton release."""
        project = self._project
        dialog = _RunnerPickerDialog(
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
            if hasattr(self, "_sel_expander"):
                self._sel_expander.set_subtitle(effective_runner)

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
        if hasattr(self, "_sel_expander"):
            self._sel_expander.set_subtitle(runner)

    def _on_download_base_clicked(self, _btn) -> None:
        """Open the base picker to download a base image from a repo."""
        project = self._project
        dialog = _BasePickerDialog(
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
        if self._project is None or not self._project.runner:
            return
        project = self._project
        project.prefix_path.mkdir(parents=True, exist_ok=True)

        progress = _ProgressDialog(label="Initializing prefix…")
        progress.present(self)

        def _bg():
            try:
                from cellar.backend.umu import init_prefix
                result = init_prefix(
                    project.prefix_path,
                    project.runner,
                    steam_appid=project.steam_appid,
                )
                # umu-run "" initializes the prefix then tries to execute an
                # empty string, which Wine rejects with exit code 1.  Use the
                # presence of drive_c as the real success indicator.
                ok = result.returncode == 0 or (project.prefix_path / "drive_c").is_dir()
            except Exception as exc:
                log.error("init_prefix failed: %s", exc)
                ok = False
            GLib.idle_add(_finish, ok)

        def _finish(ok: bool) -> None:
            progress.force_close()
            self._on_init_done(project, ok)
            if not ok:
                self._show_toast("Prefix initialization failed. Check logs.")

        threading.Thread(target=_bg, daemon=True).start()

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
        dialog = _AppMetadataDialog(
            project=self._project,
            on_changed=lambda: self._show_project(self._project),
        )
        dialog.present(self)

    def _get_next_step_hint(self, project: Project) -> str:
        """Return a short hint string for the banner, or '' if no guidance needed."""
        if project.project_type == "app":
            if not project.name or project.name == project.slug:
                return "Enter the app title to get started."
            if not project.runner:
                return "Select a base image to continue."
            if not project.initialized:
                return "Initialize the prefix to set up the Wine environment."
            if not project.entry_points:
                return "Run the installer, then add a launch target."
            if not project.category:
                return "Set a category in Details before publishing."
            return ""
        else:  # base
            if not project.runner:
                return "Select or download a runner."
            if not project.initialized:
                return "Initialize the prefix."
            return ""

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
        dialog = _DependencyPickerDialog(
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

    def _on_browse_prefix_clicked(self, _btn) -> None:
        if self._project is None:
            return
        target = self._project.prefix_path / "drive_c"
        if not target.is_dir():
            target = self._project.prefix_path
            if not target.is_dir():
                self._show_toast("Prefix not initialized yet.")
                return
        subprocess.Popen(["xdg-open", str(target)], start_new_session=True)

    def _on_add_entry_point_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        dialog = _AddLaunchTargetDialog(
            prefix_path=project.prefix_path,
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
        )

    def _on_publish_app_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if not project.entry_point:
            self._show_toast("Add a launch target before publishing.")
            return
        if not project.runner:
            self._show_toast("Select a runner before publishing.")
            return
        if not project.category:
            self._show_toast("Set a category in Metadata before publishing.")
            return
        if not self._writable_repos:
            self._show_toast("No writable repository configured.")
            return

        repo = self._writable_repos[0]

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
            screenshots=tuple(
                f"apps/{_slug}/screenshots/{i + 1:02d}{Path(p).suffix}"
                for i, p in enumerate(project.screenshot_paths)
            ),
            archive=f"apps/{_slug}/{_slug}.tar.zst",
            entry_point=project.entry_point,
            update_strategy="safe",
            built_with=BuiltWith(runner=project.runner),
        )
        images: dict = {}
        if project.icon_path:
            images["icon"] = project.icon_path
        if project.cover_path:
            images["cover"] = project.cover_path
        if project.screenshot_paths:
            images["screenshots"] = list(project.screenshot_paths)

        progress = _ProgressDialog(label="Compressing and uploading…")
        progress.present(self)

        cancel_event = threading.Event()

        def _bg():
            try:
                from cellar.backend.packager import (
                    compress_prefix_zst, compress_prefix_delta_zst, import_to_repo,
                )
                from cellar.backend.base_store import is_base_installed, base_path
                from cellar.utils.progress import fmt_file_count
                repo_root = repo.writable_path()
                archive_dest = repo_root / entry.archive
                archive_dest.parent.mkdir(parents=True, exist_ok=True)

                _use_delta = is_base_installed(project.runner)
                if _use_delta:
                    GLib.idle_add(progress.set_label, "Scanning files…")
                    size, crc32 = compress_prefix_delta_zst(
                        project.prefix_path,
                        base_path(project.runner),
                        archive_dest,
                        cancel_event=cancel_event,
                        phase_cb=lambda s: GLib.idle_add(progress.set_label, s),
                        progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                        stats_cb=lambda done, total, _speed: GLib.idle_add(
                            progress.set_stats, fmt_file_count(done, total)
                        ),
                    )
                    base_runner = project.runner
                else:
                    size, crc32 = compress_prefix_zst(
                        project.prefix_path,
                        archive_dest,
                        cancel_event=cancel_event,
                        progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                        stats_cb=lambda done, total, _speed: GLib.idle_add(
                            progress.set_stats, fmt_file_count(done, total)
                        ),
                    )
                    base_runner = ""

                # Images + catalogue.
                GLib.idle_add(progress.set_stats, "")
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
                GLib.idle_add(_done)
            except Exception as exc:
                GLib.idle_add(_error, str(exc))

        def _done() -> None:
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

        threading.Thread(target=_bg, daemon=True).start()

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
        if not project.runner:
            self._show_toast("Select a runner before publishing.")
            return
        if not self._writable_repos:
            self._show_toast("No writable repository configured.")
            return

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

        progress = _ProgressDialog(label="Compressing and uploading…")
        progress.present(self)

        cancel_event = threading.Event()

        def _bg():
            try:
                from cellar.backend.packager import (
                    compress_prefix_zst, compress_prefix_delta_zst, update_in_repo,
                )
                from cellar.backend.base_store import is_base_installed, base_path
                from cellar.utils.progress import fmt_file_count
                repo_root = repo.writable_path()
                archive_dest = repo_root / old_entry.archive
                archive_dest.parent.mkdir(parents=True, exist_ok=True)

                _use_delta = is_base_installed(project.runner)
                if _use_delta:
                    GLib.idle_add(progress.set_label, "Scanning files…")
                    size, crc32 = compress_prefix_delta_zst(
                        project.prefix_path,
                        base_path(project.runner),
                        archive_dest,
                        cancel_event=cancel_event,
                        phase_cb=lambda s: GLib.idle_add(progress.set_label, s),
                        progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                        stats_cb=lambda done, total, _speed: GLib.idle_add(
                            progress.set_stats, fmt_file_count(done, total)
                        ),
                    )
                    base_runner = project.runner
                else:
                    size, crc32 = compress_prefix_zst(
                        project.prefix_path,
                        archive_dest,
                        cancel_event=cancel_event,
                        progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                        stats_cb=lambda done, total, _speed: GLib.idle_add(
                            progress.set_stats, fmt_file_count(done, total)
                        ),
                    )
                    base_runner = old_entry.base_runner  # preserve existing delta setting

                # Update entry CRC + base_runner, then write catalogue.
                GLib.idle_add(progress.set_stats, "")
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
                GLib.idle_add(_done)
            except Exception as exc:
                GLib.idle_add(_error, str(exc))

        def _done() -> None:
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

        threading.Thread(target=_bg, daemon=True).start()

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
        progress = _ProgressDialog(label="Compressing and uploading…")
        progress.present(self)

        def _bg():
            try:
                from cellar.backend.packager import compress_prefix_zst, upsert_base
                from cellar.backend.base_store import install_base_from_dir

                runner = project.runner
                repo_root = repo.writable_path()
                archive_dest_rel = f"bases/{runner}-base.tar.zst"
                archive_dest = repo_root / archive_dest_rel
                archive_dest.parent.mkdir(parents=True, exist_ok=True)

                # Compress prefix and stream directly to the repo destination —
                # one pass, no intermediate local copy, no shutil.copy2 metadata ops.
                size, crc32 = compress_prefix_zst(
                    project.prefix_path,
                    archive_dest,
                    progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                )

                # Update catalogue.json
                upsert_base(repo_root, runner, archive_dest_rel, crc32, size)

                # Install base locally from the project prefix — no need to
                # read back the archive we just uploaded.
                GLib.idle_add(progress.set_label, "Installing base locally…")
                GLib.idle_add(progress.set_fraction, 0.0)
                install_base_from_dir(
                    project.prefix_path,
                    runner,
                    repo_source=repo.uri,
                    progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                )

                GLib.idle_add(_done)
            except Exception as exc:
                GLib.idle_add(_error, str(exc))

        def _done() -> None:
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

        threading.Thread(target=_bg, daemon=True).start()

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

        progress = _ProgressDialog(label=label)
        progress.present(self)

        def _bg():
            try:
                from cellar.backend.umu import run_in_prefix
                result = run_in_prefix(
                    project.prefix_path,
                    project.runner,
                    exe,
                    timeout=600,
                )
                ok = result.returncode == 0
            except Exception as exc:
                log.error("run_in_prefix failed: %s", exc)
                ok = False
            GLib.idle_add(_finish, ok)

        def _finish(ok: bool) -> None:
            progress.force_close()
            on_done(ok)
            if not ok:
                self._show_toast(f"Command exited with non-zero status. Check logs.")

        threading.Thread(target=_bg, daemon=True).start()

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

        badge = Gtk.Label(label="Base" if project.project_type == "base" else "App")
        badge.add_css_class("caption")
        badge.add_css_class("dim-label")
        top.append(badge)

        box.append(top)

        self.set_child(box)

    def refresh_label(self) -> None:
        """Update the displayed name (called when runner/winver changes)."""
        self._label.set_label(self.project.name)


class _AppMetadataDialog(Adw.Dialog):
    """Unified dialog for creating or editing an App project's metadata.

    Pass ``project=None`` for create mode (shows Cancel + Create buttons).
    Pass an existing ``project`` for edit mode (shows Done button, auto-saves).
    In edit mode the title is locked once the prefix has been initialized.
    """

    def __init__(
        self,
        *,
        project: Project | None = None,
        on_created: Callable | None = None,
        on_changed: Callable | None = None,
    ) -> None:
        self._is_edit = project is not None
        super().__init__(
            title="Details" if self._is_edit else "New App",
            content_width=480,
        )
        self._project = project
        self._on_created = on_created
        self._on_changed = on_changed
        # Temp image paths used in create mode until the project exists
        self._tmp_icon: str = ""
        self._tmp_cover: str = ""
        self._tmp_screenshots: list[str] = []
        self._chooser = None
        self._steam_screenshots_data: list[dict] = []

        self._build_ui()

    def _build_ui(self) -> None:
        from cellar.backend.packager import BASE_CATEGORIES as _BASE_CATS

        p = self._project
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        if self._is_edit:
            done_btn = Gtk.Button(label="Done")
            done_btn.add_css_class("suggested-action")
            done_btn.connect("clicked", lambda _: self.close())
            header.pack_end(done_btn)
        else:
            cancel_btn = Gtk.Button(label="Cancel")
            cancel_btn.connect("clicked", lambda _: self.close())
            header.pack_start(cancel_btn)
            self._create_btn = Gtk.Button(label="Create")
            self._create_btn.add_css_class("suggested-action")
            self._create_btn.set_sensitive(False)
            self._create_btn.connect("clicked", self._on_create_clicked)
            header.pack_end(self._create_btn)

        toolbar.add_top_bar(header)
        page = Adw.PreferencesPage()

        # ── Identity ──────────────────────────────────────────────────────
        id_group = Adw.PreferencesGroup()

        # Title — editable unless prefix is already initialized
        title_locked = self._is_edit and bool(p and p.initialized)
        if title_locked:
            self._title_row = Adw.ActionRow(title="Title")
            self._title_row.set_subtitle(p.name if p else "")
            self._title_row.add_css_class("property")
        else:
            self._title_row = Adw.EntryRow(title="Title")
            self._title_row.set_text(p.name if p else "")
            self._title_row.connect("changed", self._on_title_changed)
            steam_btn = Gtk.Button(icon_name="system-search-symbolic")
            steam_btn.add_css_class("flat")
            steam_btn.set_valign(Gtk.Align.CENTER)
            steam_btn.set_tooltip_text("Look up on Steam")
            steam_btn.connect("clicked", self._on_steam_lookup)
            self._title_row.add_suffix(steam_btn)
        id_group.add(self._title_row)

        # App ID
        slug_subtitle = p.slug if p else ""
        self._slug_row = Adw.ActionRow(title="App ID", subtitle=slug_subtitle)
        self._slug_row.add_css_class("property")
        id_group.add(self._slug_row)

        page.add(id_group)

        # ── Details ───────────────────────────────────────────────────────
        det_group = Adw.PreferencesGroup(title="Details")

        self._version_row = Adw.EntryRow(title="Version")
        self._version_row.set_text(p.version if p else "1.0")
        if self._is_edit:
            self._version_row.connect("changed", lambda r: self._save("version", r.get_text()))
        det_group.add(self._version_row)

        self._cats = list(_BASE_CATS)
        self._cat_row = Adw.ComboRow(title="Category")
        self._cat_row.set_model(Gtk.StringList.new(["(none)"] + self._cats))
        cat_val = p.category if p else ""
        if cat_val in self._cats:
            self._cat_row.set_selected(self._cats.index(cat_val) + 1)
        if self._is_edit:
            self._cat_row.connect("notify::selected", self._on_cat_changed)
        det_group.add(self._cat_row)

        self._dev_row = Adw.EntryRow(title="Developer")
        self._dev_row.set_text(p.developer if p else "")
        if self._is_edit:
            self._dev_row.connect("changed", lambda r: self._save("developer", r.get_text()))
        det_group.add(self._dev_row)

        self._pub_row = Adw.EntryRow(title="Publisher")
        self._pub_row.set_text(p.publisher if p else "")
        if self._is_edit:
            self._pub_row.connect("changed", lambda r: self._save("publisher", r.get_text()))
        det_group.add(self._pub_row)

        self._year_row = Adw.EntryRow(title="Release Year")
        if p and p.release_year:
            self._year_row.set_text(str(p.release_year))
        if self._is_edit:
            self._year_row.connect("changed", self._on_year_changed)
        det_group.add(self._year_row)

        self._steam_row = Adw.EntryRow(title="Steam App ID")
        self._steam_row.set_tooltip_text("Used for protonfixes. Leave empty for GAMEID=0.")
        if p and p.steam_appid is not None:
            self._steam_row.set_text(str(p.steam_appid))
        if self._is_edit:
            self._steam_row.connect("changed", self._on_steam_changed)
        det_group.add(self._steam_row)

        self._website_row = Adw.EntryRow(title="Website")
        self._website_row.set_text(p.website if p else "")
        if self._is_edit:
            self._website_row.connect("changed", lambda r: self._save("website", r.get_text()))
        det_group.add(self._website_row)

        self._genres_row = Adw.EntryRow(title="Genres")
        self._genres_row.set_tooltip_text("Comma-separated list, e.g. Action, RPG")
        if p and p.genres:
            self._genres_row.set_text(", ".join(p.genres))
        if self._is_edit:
            self._genres_row.connect("changed", self._on_genres_changed)
        det_group.add(self._genres_row)

        page.add(det_group)

        # ── Description ───────────────────────────────────────────────────
        desc_group = Adw.PreferencesGroup(title="Description")

        self._summary_row = Adw.EntryRow(title="Summary")
        self._summary_row.set_text(p.summary if p else "")
        if self._is_edit:
            self._summary_row.connect("changed", lambda r: self._save("summary", r.get_text()))
        desc_group.add(self._summary_row)

        self._desc_row = Adw.EntryRow(title="Description")
        self._desc_row.set_text(p.description if p else "")
        if self._is_edit:
            self._desc_row.connect("changed", lambda r: self._save("description", r.get_text()))
        desc_group.add(self._desc_row)

        page.add(desc_group)

        # ── Images ────────────────────────────────────────────────────────
        img_group = Adw.PreferencesGroup(title="Images")

        self._icon_row = Adw.ActionRow(title="Icon")
        icon_sub = Path(p.icon_path).name if (p and p.icon_path) else "Not set"
        self._icon_row.set_subtitle(icon_sub)
        icon_btn = Gtk.Button(label="Choose\u2026", valign=Gtk.Align.CENTER)
        icon_btn.connect("clicked", self._on_pick_icon)
        self._icon_row.add_suffix(icon_btn)
        img_group.add(self._icon_row)

        self._cover_row = Adw.ActionRow(title="Cover")
        cover_sub = Path(p.cover_path).name if (p and p.cover_path) else "Not set"
        self._cover_row.set_subtitle(cover_sub)
        cover_btn = Gtk.Button(label="Choose\u2026", valign=Gtk.Align.CENTER)
        cover_btn.connect("clicked", self._on_pick_cover)
        self._cover_row.add_suffix(cover_btn)
        img_group.add(self._cover_row)

        ss_count = len(p.screenshot_paths) if p else 0
        self._ss_row = Adw.ActionRow(title="Screenshots")
        self._ss_row.set_subtitle(
            f"{ss_count} file{'s' if ss_count != 1 else ''} selected"
            if ss_count else "None selected"
        )
        ss_btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        ss_btns.set_valign(Gtk.Align.CENTER)
        self._steam_ss_btn = Gtk.Button(label="From Steam\u2026")
        self._steam_ss_btn.set_visible(False)
        self._steam_ss_btn.connect("clicked", self._on_steam_screenshots_clicked)
        ss_btns.append(self._steam_ss_btn)
        browse_btn = Gtk.Button(label="Browse\u2026")
        browse_btn.connect("clicked", self._on_pick_screenshots)
        ss_btns.append(browse_btn)
        self._ss_row.add_suffix(ss_btns)
        img_group.add(self._ss_row)

        page.add(img_group)
        toolbar.set_content(page)
        self.set_child(toolbar)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _save(self, attr: str, value) -> None:
        if self._project:
            setattr(self._project, attr, value)
            save_project(self._project)
            if self._on_changed:
                self._on_changed()

    def _pick_image(self, title: str, multi: bool, callback) -> None:
        chooser = Gtk.FileChooserNative(
            title=title,
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            select_multiple=multi,
        )
        img_filter = Gtk.FileFilter()
        img_filter.set_name("Images (PNG, JPG, ICO, SVG)")
        img_filter.add_mime_type("image/png")
        img_filter.add_mime_type("image/jpeg")
        img_filter.add_mime_type("image/x-icon")
        img_filter.add_mime_type("image/vnd.microsoft.icon")
        img_filter.add_mime_type("image/svg+xml")
        chooser.add_filter(img_filter)
        chooser.connect("response", callback, chooser)
        chooser.show()
        self._chooser = chooser

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_title_changed(self, row) -> None:
        from cellar.backend.packager import slugify
        title = row.get_text().strip()
        if self._is_edit:
            self._save("name", title)
        else:
            slug = slugify(title) if title else ""
            self._slug_row.set_subtitle(slug)
            self._create_btn.set_sensitive(bool(title))

    def _on_cat_changed(self, row, _param) -> None:
        if self._project:
            idx = row.get_selected()
            self._project.category = self._cats[idx - 1] if idx > 0 else ""
            save_project(self._project)
            if self._on_changed:
                self._on_changed()

    def _on_year_changed(self, row) -> None:
        txt = row.get_text().strip()
        try:
            val = int(txt) if txt else None
        except ValueError:
            return
        self._save("release_year", val)

    def _on_steam_changed(self, row) -> None:
        txt = row.get_text().strip()
        self._save("steam_appid", int(txt) if txt.isdigit() else None)

    def _on_genres_changed(self, row) -> None:
        txt = row.get_text().strip()
        genres = [g.strip() for g in txt.split(",") if g.strip()] if txt else []
        self._save("genres", genres)

    def _on_steam_lookup(self, _btn) -> None:
        from cellar.views.steam_picker import SteamPickerDialog
        if isinstance(self._title_row, Adw.EntryRow):
            query = self._title_row.get_text().strip()
        elif self._project:
            query = self._project.name
        else:
            query = ""
        picker = SteamPickerDialog(query=query, on_picked=self._apply_steam)
        picker.present(self.get_root())

    def _apply_steam(self, result: dict) -> None:
        if result.get("name") and isinstance(self._title_row, Adw.EntryRow):
            self._title_row.set_text(result["name"])
        if result.get("developer") and not self._dev_row.get_text().strip():
            self._dev_row.set_text(result["developer"])
        if result.get("publisher") and not self._pub_row.get_text().strip():
            self._pub_row.set_text(result["publisher"])
        if result.get("year") and not self._year_row.get_text().strip():
            self._year_row.set_text(str(result["year"]))
        if result.get("summary") and not self._summary_row.get_text().strip():
            self._summary_row.set_text(result["summary"])
        if result.get("description") and not self._desc_row.get_text().strip():
            self._desc_row.set_text(result["description"])
        if result.get("steam_appid") and not self._steam_row.get_text().strip():
            self._steam_row.set_text(str(result["steam_appid"]))
        if result.get("website") and not self._website_row.get_text().strip():
            self._website_row.set_text(result["website"])
        if result.get("genres") and not self._genres_row.get_text().strip():
            self._genres_row.set_text(", ".join(result["genres"]))
        if result.get("category") and result["category"] in self._cats:
            self._cat_row.set_selected(self._cats.index(result["category"]) + 1)
        if result.get("screenshots"):
            self._steam_screenshots_data = result["screenshots"]
            self._steam_ss_btn.set_visible(True)

    def _on_steam_screenshots_clicked(self, _btn) -> None:
        if not self._steam_screenshots_data:
            return
        from cellar.views.steam_screenshot_picker import SteamScreenshotPickerDialog
        picker = SteamScreenshotPickerDialog(
            screenshots_data=self._steam_screenshots_data,
            on_confirmed=self._on_steam_screenshots_confirmed,
        )
        picker.present(self.get_root())

    def _on_steam_screenshots_confirmed(
        self, selected_urls: list[str], local_paths: list[str]
    ) -> None:
        if not selected_urls:
            all_paths = list(local_paths)
            self._apply_screenshot_paths(all_paths)
            return

        self._ss_row.set_subtitle("Downloading\u2026")

        if self._project:
            dl_dir = self._project.project_dir / "screenshots"
            dl_dir.mkdir(parents=True, exist_ok=True)
        else:
            import tempfile as _tmp
            dl_dir = Path(_tmp.mkdtemp(prefix="cellar-ss-"))

        def _download() -> None:
            from cellar.utils.http import make_session
            session = make_session()
            downloaded: list[str] = []
            for i, url in enumerate(selected_urls):
                try:
                    resp = session.get(url, timeout=30)
                    if resp.ok:
                        suffix = ".jpg" if url.lower().endswith(".jpg") else ".png"
                        dest = dl_dir / f"steam_{i:02d}{suffix}"
                        dest.write_bytes(resp.content)
                        downloaded.append(str(dest))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Screenshot download failed: %s", exc)
            GLib.idle_add(self._on_screenshots_downloaded, downloaded + list(local_paths))

        threading.Thread(target=_download, daemon=True).start()

    def _on_screenshots_downloaded(self, paths: list[str]) -> None:
        self._apply_screenshot_paths(paths)

    def _apply_screenshot_paths(self, paths: list[str]) -> None:
        count = len(paths)
        self._ss_row.set_subtitle(
            f"{count} file{'s' if count != 1 else ''} selected" if count else "None selected"
        )
        if self._project:
            self._project.screenshot_paths = paths
            save_project(self._project)
        else:
            self._tmp_screenshots = paths

    def _on_pick_icon(self, _btn) -> None:
        self._pick_image("Select Icon", False, self._on_icon_chosen)

    def _on_icon_chosen(self, _c, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = chooser.get_file().get_path()
            self._icon_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
            if self._project:
                self._project.icon_path = path
                save_project(self._project)
            else:
                self._tmp_icon = path

    def _on_pick_cover(self, _btn) -> None:
        self._pick_image("Select Cover", False, self._on_cover_chosen)

    def _on_cover_chosen(self, _c, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = chooser.get_file().get_path()
            self._cover_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
            if self._project:
                self._project.cover_path = path
                save_project(self._project)
            else:
                self._tmp_cover = path

    def _on_pick_screenshots(self, _btn) -> None:
        self._pick_image("Select Screenshots", True, self._on_screenshots_chosen)

    def _on_screenshots_chosen(self, _c, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            files = chooser.get_files()
            paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            count = len(paths)
            self._ss_row.set_subtitle(
                f"{count} file{'s' if count != 1 else ''} selected"
                if count else "None selected"
            )
            if self._project:
                self._project.screenshot_paths = paths
                save_project(self._project)
            else:
                self._tmp_screenshots = paths

    def _on_create_clicked(self, _btn) -> None:
        if isinstance(self._title_row, Adw.EntryRow):
            name = self._title_row.get_text().strip()
        else:
            return
        if not name:
            return

        cat_idx = self._cat_row.get_selected()
        year_txt = self._year_row.get_text().strip()
        steam_txt = self._steam_row.get_text().strip()
        summary = self._summary_row.get_text().strip()

        project = create_project(name, "app")
        project.category = self._cats[cat_idx - 1] if cat_idx > 0 else ""
        project.developer = self._dev_row.get_text().strip()
        project.publisher = self._pub_row.get_text().strip()
        project.version = self._version_row.get_text().strip() or "1.0"
        try:
            project.release_year = int(year_txt) if year_txt else None
        except ValueError:
            project.release_year = None
        project.summary = summary
        project.description = self._desc_row.get_text().strip() or summary
        project.steam_appid = int(steam_txt) if steam_txt.isdigit() else None
        project.website = self._website_row.get_text().strip()
        genres_txt = self._genres_row.get_text().strip()
        project.genres = [g.strip() for g in genres_txt.split(",") if g.strip()] if genres_txt else []
        if self._tmp_icon:
            project.icon_path = self._tmp_icon
        if self._tmp_cover:
            project.cover_path = self._tmp_cover
        if self._tmp_screenshots:
            project.screenshot_paths = self._tmp_screenshots
        save_project(project)

        self.close()
        if self._on_created:
            self._on_created(project)


class _AddLaunchTargetDialog(Adw.Dialog):
    """Dialog for adding a new launch target (name + executable path) to a project."""

    def __init__(self, prefix_path: Path, on_added: Callable) -> None:
        super().__init__(title="Add Launch Target", content_width=480)
        self._prefix_path = prefix_path
        self._on_added = on_added
        self._chosen_path: str = ""
        self._build_ui()

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        self._add_btn = Gtk.Button(label="Add")
        self._add_btn.add_css_class("suggested-action")
        self._add_btn.set_sensitive(False)
        self._add_btn.connect("clicked", self._on_add_clicked)
        header.pack_end(self._add_btn)

        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()

        self._name_row = Adw.EntryRow(title="Name")
        self._name_row.set_tooltip_text('E.g. "Main Game", "Config Tool"')
        self._name_row.connect("changed", self._validate)
        group.add(self._name_row)

        self._exe_row = Adw.ActionRow(title="Executable")
        self._exe_row.set_subtitle("Not selected")
        browse_btn = Gtk.Button(label="Browse\u2026", valign=Gtk.Align.CENTER)
        browse_btn.connect("clicked", self._on_browse_clicked)
        self._exe_row.add_suffix(browse_btn)
        self._exe_row.set_activatable_widget(browse_btn)
        group.add(self._exe_row)

        page.add(group)
        toolbar.set_content(page)
        self.set_child(toolbar)

    def _validate(self, *_) -> None:
        self._add_btn.set_sensitive(
            bool(self._name_row.get_text().strip()) and bool(self._chosen_path)
        )

    def _on_browse_clicked(self, _btn) -> None:
        drive_c = self._prefix_path / "drive_c"
        chooser = Gtk.FileChooserNative(
            title="Select Executable (.exe)",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Select",
        )
        if drive_c.is_dir():
            chooser.set_current_folder(Gio.File.new_for_path(str(drive_c)))
        exe_filter = Gtk.FileFilter()
        exe_filter.set_name("Windows executables (*.exe)")
        exe_filter.add_pattern("*.exe")
        chooser.add_filter(exe_filter)
        chooser.connect("response", self._on_exe_chosen, chooser)
        chooser.show()
        self._chooser = chooser

    def _on_exe_chosen(self, _c, response, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        abs_path = chooser.get_file().get_path()
        drive_c = self._prefix_path / "drive_c"
        try:
            rel = os.path.relpath(abs_path, str(drive_c))
            win_path = "C:\\" + rel.replace("/", "\\")
        except ValueError:
            win_path = abs_path
        self._chosen_path = win_path
        self._exe_row.set_subtitle(GLib.markup_escape_text(win_path))
        # Auto-fill name from filename if empty
        if not self._name_row.get_text().strip():
            stem = Path(abs_path).stem
            self._name_row.set_text(stem)
        self._validate()

    def _on_add_clicked(self, _btn) -> None:
        name = self._name_row.get_text().strip()
        if not name or not self._chosen_path:
            return
        self.close()
        self._on_added({"name": name, "path": self._chosen_path})


class _RunnerPickerDialog(Adw.Dialog):
    """Lists available GE-Proton releases and lets the user download one.

    Fetches the release list from the GitHub API in a background thread
    (showing a spinner), then presents a selectable list.  Selecting a
    release and clicking Install opens ``InstallRunnerDialog``.
    """

    def __init__(self, on_installed: Callable[[str], None]) -> None:
        super().__init__(title="Download Runner", content_width=440)
        self._on_installed = on_installed
        self._releases: list[dict] = []
        self._selected_idx: int = -1

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        self._install_btn = Gtk.Button(label="Install")
        self._install_btn.add_css_class("suggested-action")
        self._install_btn.set_sensitive(False)
        self._install_btn.connect("clicked", self._on_install_clicked)
        header.pack_end(self._install_btn)

        toolbar.add_top_bar(header)

        # Stack: loading spinner → release list
        self._stack = Gtk.Stack()

        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        spinner_box.set_valign(Gtk.Align.CENTER)
        spinner_box.set_vexpand(True)
        spinner = Gtk.Spinner(spinning=True)
        spinner.set_size_request(32, 32)
        spinner_box.append(spinner)
        loading_lbl = Gtk.Label(label="Fetching releases…")
        loading_lbl.add_css_class("dim-label")
        spinner_box.append(loading_lbl)
        self._stack.add_named(spinner_box, "loading")

        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_min_content_height(300)
        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_top(12)
        self._list_box.set_margin_bottom(12)
        self._list_box.set_margin_start(12)
        self._list_box.set_margin_end(12)
        self._list_box.connect("row-selected", self._on_row_selected)
        scroll.set_child(self._list_box)
        self._stack.add_named(scroll, "list")

        self._stack.set_visible_child_name("loading")
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        threading.Thread(target=self._fetch_releases, daemon=True).start()

    def _fetch_releases(self) -> None:
        from cellar.backend import runners as _runners
        try:
            releases = _runners.fetch_releases(limit=20)
        except Exception:
            releases = []
        GLib.idle_add(self._populate_list, releases)

    def _populate_list(self, releases: list[dict]) -> None:
        self._releases = releases

        from cellar.backend import runners as _runners
        for rel in releases:
            already = _runners.is_installed(rel["tag"])

            row = Adw.ActionRow(title=rel["name"])
            size_mb = rel["size"] / 1_000_000 if rel.get("size") else 0
            subtitle = f"{size_mb:.0f} MB" if size_mb else ""
            if already:
                subtitle = ("  ·  " if subtitle else "") + "Installed"
            row.set_subtitle(subtitle)

            if already:
                icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
                row.add_suffix(icon)

            self._list_box.append(row)

        if not releases:
            err_row = Adw.ActionRow(
                title="Could not fetch releases",
                subtitle="Check your network connection.",
            )
            self._list_box.append(err_row)

        self._stack.set_visible_child_name("list")

    def _on_row_selected(self, _lb, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            self._selected_idx = -1
            self._install_btn.set_sensitive(False)
            return
        self._selected_idx = row.get_index()
        # Don't enable Install if the release is already installed
        if 0 <= self._selected_idx < len(self._releases):
            from cellar.backend import runners as _runners
            tag = self._releases[self._selected_idx]["tag"]
            self._install_btn.set_sensitive(not _runners.is_installed(tag))
        else:
            self._install_btn.set_sensitive(False)

    def _on_install_clicked(self, _btn) -> None:
        if not (0 <= self._selected_idx < len(self._releases)):
            return
        rel = self._releases[self._selected_idx]
        from cellar.backend.umu import runners_dir
        from cellar.views.install_runner import InstallRunnerDialog
        target_dir = runners_dir() / rel["tag"]
        parent_win = self.get_root()
        dlg = InstallRunnerDialog(
            runner_name=rel["name"],
            url=rel["url"],
            checksum=rel.get("checksum", ""),
            target_dir=target_dir,
            on_done=self._on_runner_installed,
        )
        self.close()
        dlg.present(parent_win)

    def _on_runner_installed(self, runner_name: str) -> None:
        self._on_installed(runner_name)


class _BasePickerDialog(Adw.Dialog):
    """Lists base images available on configured repos and lets the user download one.

    Shows locally installed bases with a checkmark.  Selecting an uninstalled
    base and clicking Install downloads the archive and installs it via
    ``base_store.install_base``.
    """

    def __init__(self, repos: list, on_installed: Callable[[str], None]) -> None:
        super().__init__(title="Download Base Image", content_width=440)
        self._repos = repos
        self._on_installed = on_installed
        self._bases: list[tuple] = []  # (BaseEntry, repo)
        self._selected_idx: int = -1

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        self._install_btn = Gtk.Button(label="Install")
        self._install_btn.add_css_class("suggested-action")
        self._install_btn.set_sensitive(False)
        self._install_btn.connect("clicked", self._on_install_clicked)
        header.pack_end(self._install_btn)

        toolbar.add_top_bar(header)

        # Stack: loading spinner → base list
        self._stack = Gtk.Stack()

        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        spinner_box.set_valign(Gtk.Align.CENTER)
        spinner_box.set_vexpand(True)
        spinner = Gtk.Spinner(spinning=True)
        spinner.set_size_request(32, 32)
        spinner_box.append(spinner)
        loading_lbl = Gtk.Label(label="Fetching bases…")
        loading_lbl.add_css_class("dim-label")
        spinner_box.append(loading_lbl)
        self._stack.add_named(spinner_box, "loading")

        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_min_content_height(300)
        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_top(12)
        self._list_box.set_margin_bottom(12)
        self._list_box.set_margin_start(12)
        self._list_box.set_margin_end(12)
        self._list_box.connect("row-selected", self._on_row_selected)
        scroll.set_child(self._list_box)
        self._stack.add_named(scroll, "list")

        self._stack.set_visible_child_name("loading")
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        threading.Thread(target=self._fetch_bases, daemon=True).start()

    def _fetch_bases(self) -> None:
        from cellar.backend.base_store import is_base_installed
        results: list[tuple] = []
        seen: set[str] = set()
        for repo in self._repos:
            try:
                for runner, base_entry in repo.fetch_bases().items():
                    if runner not in seen:
                        seen.add(runner)
                        results.append((base_entry, repo))
            except Exception as exc:
                log.warning("Could not fetch bases from %s: %s", repo.uri, exc)
        results.sort(key=lambda t: t[0].runner, reverse=True)
        GLib.idle_add(self._populate_list, results)

    def _populate_list(self, results: list[tuple]) -> None:
        from cellar.backend.base_store import is_base_installed
        self._bases = results

        for base_entry, repo in results:
            already = is_base_installed(base_entry.runner)
            row = Adw.ActionRow(title=base_entry.runner)
            size_mb = base_entry.archive_size / 1_000_000 if base_entry.archive_size else 0
            subtitle = f"{size_mb:.0f} MB" if size_mb else ""
            repo_name = repo.name or repo.uri
            if subtitle:
                subtitle += f"  ·  {repo_name}"
            else:
                subtitle = repo_name
            if already:
                subtitle += "  ·  Installed"
            row.set_subtitle(subtitle)

            if already:
                icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
                row.add_suffix(icon)

            self._list_box.append(row)

        if not results:
            err_row = Adw.ActionRow(
                title="No bases found",
                subtitle="Publish a base image to a repository first.",
            )
            self._list_box.append(err_row)

        self._stack.set_visible_child_name("list")

    def _on_row_selected(self, _lb, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            self._selected_idx = -1
            self._install_btn.set_sensitive(False)
            return
        self._selected_idx = row.get_index()
        if 0 <= self._selected_idx < len(self._bases):
            from cellar.backend.base_store import is_base_installed
            runner = self._bases[self._selected_idx][0].runner
            self._install_btn.set_sensitive(not is_base_installed(runner))
        else:
            self._install_btn.set_sensitive(False)

    def _on_install_clicked(self, _btn) -> None:
        if not (0 <= self._selected_idx < len(self._bases)):
            return
        base_entry, repo = self._bases[self._selected_idx]
        parent_win = self.get_root()
        self.close()

        progress = _ProgressDialog(label=f"Downloading {base_entry.runner}…")
        progress.present(parent_win)

        def _bg():
            import tempfile
            try:
                from cellar.backend.base_store import install_base
                from cellar.backend.installer import (
                    _build_source,  # noqa: PLC2701
                )

                archive_uri = repo.resolve_asset_uri(base_entry.archive)
                chunks, total = _build_source(
                    archive_uri,
                    expected_size=base_entry.archive_size,
                    token=repo.token,
                    ssl_verify=repo.ssl_verify,
                    ca_cert=repo.ca_cert,
                )

                # Stream to a temp file, then install from that.
                with tempfile.NamedTemporaryFile(
                    prefix="cellar-base-", suffix=".tar.zst", delete=False,
                ) as tmp:
                    tmp_path = Path(tmp.name)
                    received = 0
                    for chunk in chunks:
                        tmp.write(chunk)
                        received += len(chunk)
                        if total:
                            GLib.idle_add(progress.set_fraction, received / total)

                GLib.idle_add(progress.set_label, "Installing base…")
                GLib.idle_add(progress.set_fraction, 0.0)
                install_base(
                    tmp_path,
                    base_entry.runner,
                    repo_source=repo.uri,
                    progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                )
                tmp_path.unlink(missing_ok=True)
                GLib.idle_add(_done)
            except Exception as exc:
                log.error("Base install failed: %s", exc)
                GLib.idle_add(_error, str(exc))

        def _done() -> None:
            progress.force_close()
            self._on_installed(base_entry.runner)

        def _error(msg: str) -> None:
            progress.force_close()
            err = Adw.AlertDialog(heading="Install failed", body=msg)
            err.add_response("ok", "OK")
            err.present(parent_win)

        threading.Thread(target=_bg, daemon=True).start()


class _ImportFromCatalogueDialog(Adw.Dialog):
    """List catalogue entries from all repos and import one as a new project.

    Downloads the archive, extracts it to the project prefix directory, and
    pre-fills all metadata from the catalogue entry.  The resulting project
    has ``origin_app_id`` set so re-publishing updates the existing entry.
    """

    def __init__(self, repos: list, on_imported: Callable) -> None:
        super().__init__(title="Import from Catalogue", content_width=520)
        self._repos = repos
        self._on_imported = on_imported
        self._entries: list[tuple] = []   # (item, repo, kind) — kind = "app" | "base"
        self._selected_idx: int = -1

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        self._import_btn = Gtk.Button(label="Import")
        self._import_btn.add_css_class("suggested-action")
        self._import_btn.set_sensitive(False)
        self._import_btn.connect("clicked", self._on_import_clicked)
        header.pack_end(self._import_btn)

        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()

        # Loading state
        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        spinner_box.set_valign(Gtk.Align.CENTER)
        spinner_box.set_vexpand(True)
        spinner = Gtk.Spinner(spinning=True)
        spinner.set_size_request(32, 32)
        spinner_box.append(spinner)
        lbl = Gtk.Label(label="Fetching catalogue…")
        lbl.add_css_class("dim-label")
        spinner_box.append(lbl)
        self._stack.add_named(spinner_box, "loading")

        # List state
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_min_content_height(360)
        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_top(12)
        self._list_box.set_margin_bottom(12)
        self._list_box.set_margin_start(12)
        self._list_box.set_margin_end(12)
        self._list_box.connect("row-selected", self._on_row_selected)
        scroll.set_child(self._list_box)
        self._stack.add_named(scroll, "list")

        self._stack.set_visible_child_name("loading")
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        threading.Thread(target=self._fetch_entries, daemon=True).start()

    def _fetch_entries(self) -> None:
        apps: list[tuple] = []
        bases: list[tuple] = []
        seen_bases: set[str] = set()
        for repo in self._repos:
            try:
                for entry in repo.fetch_catalogue():
                    if entry.archive:
                        apps.append((entry, repo, "app"))
            except Exception as exc:
                log.warning("Could not fetch catalogue from %s: %s", repo.uri, exc)
            try:
                for runner, base_entry in repo.fetch_bases().items():
                    if runner not in seen_bases:
                        seen_bases.add(runner)
                        bases.append((base_entry, repo, "base"))
            except Exception as exc:
                log.warning("Could not fetch bases from %s: %s", repo.uri, exc)
        apps.sort(key=lambda t: t[0].name.lower())
        bases.sort(key=lambda t: t[0].runner.lower())
        GLib.idle_add(self._populate, apps + bases)

    def _populate(self, results: list[tuple]) -> None:
        self._entries = results
        for item, repo, kind in results:
            size_mb = (item.archive_size or 0) / 1_000_000
            size_str = f"{size_mb:.0f} MB" if size_mb else ""
            repo_name = repo.name or repo.uri
            if kind == "base":
                title = item.runner
                subtitle_parts = [size_str, repo_name]
            else:
                title = item.name
                subtitle_parts = [item.version or "", size_str, repo_name]
            subtitle = "  ·  ".join(p for p in subtitle_parts if p)
            row = Adw.ActionRow(title=html.escape(title), subtitle=html.escape(subtitle))
            if kind == "base":
                chip = Gtk.Label(label="Base Image")
                chip.add_css_class("tag")
                chip.set_valign(Gtk.Align.CENTER)
                row.add_suffix(chip)
            self._list_box.append(row)

        if not results:
            empty_row = Adw.ActionRow(
                title="No entries found",
                subtitle="Add a repository with published apps.",
            )
            self._list_box.append(empty_row)

        self._stack.set_visible_child_name("list")

    def _on_row_selected(self, _lb, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            self._selected_idx = -1
            self._import_btn.set_sensitive(False)
            return
        self._selected_idx = row.get_index()
        self._import_btn.set_sensitive(0 <= self._selected_idx < len(self._entries))

    def _on_import_clicked(self, _btn) -> None:
        if not (0 <= self._selected_idx < len(self._entries)):
            return
        item, repo, kind = self._entries[self._selected_idx]

        if kind == "base":
            self._start_import_base(item, repo)
        else:
            self._start_import(item, repo)

    def _start_import_base(self, base_entry, repo) -> None:
        root = self.get_root()
        self.close()

        progress = _ProgressDialog(label=f"Downloading {base_entry.runner}…")
        progress.present(root)

        def _bg():
            import tempfile
            import time
            try:
                from cellar.backend.base_store import install_base
                from cellar.backend.installer import _build_source  # noqa: PLC2701
                from cellar.utils.progress import fmt_stats

                archive_uri = repo.resolve_asset_uri(base_entry.archive)
                chunks, total = _build_source(
                    archive_uri,
                    expected_size=base_entry.archive_size,
                    token=repo.token,
                    ssl_verify=repo.ssl_verify,
                    ca_cert=repo.ca_cert,
                )

                with tempfile.NamedTemporaryFile(
                    prefix="cellar-base-", suffix=".tar.zst", delete=False,
                ) as tmp:
                    tmp_path = Path(tmp.name)
                    received = 0
                    t0 = time.monotonic()
                    for chunk in chunks:
                        tmp.write(chunk)
                        received += len(chunk)
                        elapsed = time.monotonic() - t0
                        speed = received / elapsed if elapsed > 0 else 0.0
                        if total:
                            GLib.idle_add(progress.set_fraction, received / total)
                        GLib.idle_add(progress.set_stats, fmt_stats(received, total or 0, speed))

                GLib.idle_add(progress.set_stats, "")
                GLib.idle_add(progress.set_label, "Installing base…")
                GLib.idle_add(progress.set_fraction, 0.0)
                install_base(
                    tmp_path,
                    base_entry.runner,
                    repo_source=repo.uri,
                    progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                )
                tmp_path.unlink(missing_ok=True)
                GLib.idle_add(_done)

            except Exception as exc:
                log.error("Base download failed: %s", exc)
                GLib.idle_add(_error, str(exc))

        def _done() -> None:
            progress.force_close()
            self._on_imported(None)

        def _error(msg: str) -> None:
            progress.force_close()
            err = Adw.AlertDialog(heading="Download failed", body=msg)
            err.add_response("ok", "OK")
            err.present(root)

        threading.Thread(target=_bg, daemon=True).start()

    def _start_import(self, entry, repo) -> None:
        root = self.get_root()
        self.close()

        progress = _ProgressDialog(label="Downloading…")
        progress.present(root)

        def _bg():
            import shutil
            import tempfile
            try:
                from cellar.backend.installer import (
                    _build_source,        # noqa: PLC2701
                    _find_bottle_dir,     # noqa: PLC2701
                    _stream_and_extract,  # noqa: PLC2701
                )
                archive_uri = repo.resolve_asset_uri(entry.archive)
                chunks, total = _build_source(
                    archive_uri,
                    expected_size=entry.archive_size,
                    token=repo.token,
                    ssl_verify=repo.ssl_verify,
                    ca_cert=repo.ca_cert,
                )

                with tempfile.TemporaryDirectory(prefix="cellar-import-") as tmp_str:
                    tmp = Path(tmp_str)
                    extract_dir = tmp / "extracted"
                    extract_dir.mkdir()

                    _stream_and_extract(
                        chunks, total,
                        is_zst=archive_uri.endswith(".tar.zst"),
                        dest=extract_dir,
                        expected_crc32=entry.archive_crc32,
                        cancel_event=None,
                        progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                        stats_cb=None,
                        name_cb=None,
                    )

                    bottle_src = _find_bottle_dir(extract_dir)

                    from cellar.backend.packager import slugify
                    from cellar.backend.project import (
                        Project,
                        load_projects,
                        save_project,
                    )
                    slug = slugify(entry.id)
                    existing = {p.slug for p in load_projects()}
                    base_slug, i = slug, 2
                    while slug in existing:
                        slug = f"{base_slug}-{i}"
                        i += 1

                    _ep = entry.entry_point or ""
                    project = Project(
                        name=entry.name,
                        slug=slug,
                        project_type="app",
                        runner=entry.built_with.runner if entry.built_with else "",
                        entry_points=[{"name": "Main", "path": _ep}] if _ep else [],
                        steam_appid=entry.steam_appid,
                        initialized=True,
                        origin_app_id=entry.id,
                    )

                    GLib.idle_add(progress.set_label, "Copying prefix…")
                    project.prefix_path.mkdir(parents=True, exist_ok=True)
                    for src in bottle_src.rglob("*"):
                        rel = src.relative_to(bottle_src)
                        dst = project.prefix_path / rel
                        if src.is_dir():
                            dst.mkdir(parents=True, exist_ok=True)
                        elif src.is_file():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dst)

                    save_project(project)
                    GLib.idle_add(_done, project)

            except Exception as exc:
                log.error("Import failed: %s", exc)
                GLib.idle_add(_error, str(exc))

        def _done(project) -> None:
            progress.force_close()
            self._on_imported(project)

        def _error(msg: str) -> None:
            progress.force_close()
            err = Adw.AlertDialog(heading="Import failed", body=msg)
            err.add_response("ok", "OK")
            err.present(root)

        threading.Thread(target=_bg, daemon=True).start()


class _DependencyPickerDialog(Adw.Dialog):
    """Browse and install winetricks dependencies.

    Presents verbs grouped in collapsible Adw.ExpanderRow sections (one per
    category).  Each verb row has a per-row install (download icon) or remove
    (trash icon) button; a spinner replaces the button while installing.
    The search entry in the header bar auto-expands matching sections and hides
    non-matching verbs.
    """

    def __init__(self, project: Project, on_dep_changed: Callable) -> None:
        super().__init__(title="Dependencies", content_width=500)
        self._project = project
        self._on_dep_changed = on_dep_changed
        # list of (ExpanderRow, [ActionRow, ...]) for search visibility control
        self._category_rows: list[tuple[Adw.ExpanderRow, list[Adw.ActionRow]]] = []

        toolbar = Adw.ToolbarView()

        # Header with search entry as title widget
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda _: self.close())
        header.pack_start(close_btn)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search…")
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("search-changed", self._on_search_changed)
        header.set_title_widget(self._search_entry)
        toolbar.add_top_bar(header)

        # Main scroll area
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_min_content_height(420)
        scroll.set_vexpand(True)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_top(12)
        self._list_box.set_margin_bottom(12)
        self._list_box.set_margin_start(12)
        self._list_box.set_margin_end(12)

        for category, verbs in _VERB_CATALOGUE:
            exp_row = Adw.ExpanderRow(title=html.escape(category))
            verb_rows: list[Adw.ActionRow] = []

            for verb, description in verbs:
                verb_row = Adw.ActionRow(title=verb, subtitle=html.escape(description))
                verb_row._verb = verb  # type: ignore[attr-defined]
                verb_row._search_key = f"{verb} {description} {category}".lower()  # type: ignore[attr-defined]
                suffix = self._make_suffix(verb)
                verb_row._suffix_stack = suffix  # type: ignore[attr-defined]
                verb_row.add_suffix(suffix)
                exp_row.add_row(verb_row)
                verb_rows.append(verb_row)

            self._list_box.append(exp_row)
            self._category_rows.append((exp_row, verb_rows))

        scroll.set_child(self._list_box)
        toolbar.set_content(scroll)
        self.set_child(toolbar)

    # ── Suffix stack: idle / installing / installed ─────────────────────────

    def _make_suffix(self, verb: str) -> Gtk.Stack:
        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        # idle: download button
        install_btn = Gtk.Button(icon_name="folder-download-symbolic")
        install_btn.set_valign(Gtk.Align.CENTER)
        install_btn.set_tooltip_text(f"Install {verb}")
        install_btn.add_css_class("flat")
        install_btn.connect("clicked", self._on_install_clicked, verb, stack)
        stack.add_named(install_btn, "idle")

        # installing: spinner
        spinner = Gtk.Spinner(spinning=True)
        spinner.set_valign(Gtk.Align.CENTER)
        spinner.set_size_request(16, 16)
        stack.add_named(spinner, "installing")

        # installed: check icon + remove button
        installed_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        installed_box.set_valign(Gtk.Align.CENTER)
        check = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        check.add_css_class("success")
        remove_btn = Gtk.Button(icon_name="edit-delete-symbolic")
        remove_btn.set_valign(Gtk.Align.CENTER)
        remove_btn.set_tooltip_text(f"Remove {verb} from tracking")
        remove_btn.add_css_class("flat")
        remove_btn.connect("clicked", self._on_remove_clicked, verb, stack)
        installed_box.append(check)
        installed_box.append(remove_btn)
        stack.add_named(installed_box, "installed")

        state = "installed" if verb in self._project.deps_installed else "idle"
        stack.set_visible_child_name(state)
        return stack

    # ── Search ──────────────────────────────────────────────────────────────

    def _on_search_changed(self, _entry) -> None:
        query = self._search_entry.get_text().lower().strip()
        if not query:
            for exp_row, verb_rows in self._category_rows:
                exp_row.set_visible(True)
                exp_row.set_expanded(False)
                for vr in verb_rows:
                    vr.set_visible(True)
            return

        for exp_row, verb_rows in self._category_rows:
            has_match = False
            for vr in verb_rows:
                match = query in vr._search_key  # type: ignore[attr-defined]
                vr.set_visible(match)
                if match:
                    has_match = True
            exp_row.set_visible(has_match)
            if has_match:
                exp_row.set_expanded(True)

    # ── Install handlers ────────────────────────────────────────────────────

    def _on_install_clicked(self, _btn, verb: str, stack: Gtk.Stack) -> None:
        self._install_verbs([verb], stack)

    def _install_verbs(self, verbs: list[str], stack: Gtk.Stack) -> None:
        dlg = _WinetricksProgressDialog(verbs)
        dlg.present(self)

        def _bg():
            from cellar.backend.umu import run_winetricks
            try:
                result = run_winetricks(
                    self._project.prefix_path,
                    self._project.runner,
                    verbs,
                    line_cb=lambda line: GLib.idle_add(dlg.push_line, line),
                )
                ok = result.returncode == 0
            except Exception as exc:
                log.error("run_winetricks failed: %s", exc)
                ok = False
            GLib.idle_add(_finish, ok)

        def _finish(ok: bool) -> None:
            dlg.force_close()
            if ok:
                for v in verbs:
                    if v not in self._project.deps_installed:
                        self._project.deps_installed.append(v)
                save_project(self._project)
                stack.set_visible_child_name("installed")
                self._on_dep_changed()
            else:
                stack.set_visible_child_name("idle")
                log.warning("winetricks install failed for: %s", verbs)

        threading.Thread(target=_bg, daemon=True).start()

    # ── Remove handler ──────────────────────────────────────────────────────

    def _on_remove_clicked(self, _btn, verb: str, stack: Gtk.Stack) -> None:
        if verb in self._project.deps_installed:
            self._project.deps_installed.remove(verb)
        save_project(self._project)
        stack.set_visible_child_name("idle")
        self._on_dep_changed()


class _ProgressDialog(Adw.Dialog):
    """Simple blocking progress dialog for long-running operations."""

    def __init__(self, label: str) -> None:
        super().__init__(content_width=340)
        self.set_can_close(False)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(24)
        box.set_margin_bottom(24)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.set_valign(Gtk.Align.CENTER)

        self._label = Gtk.Label(label=label, xalign=0.5)
        self._label.add_css_class("dim-label")

        self._bar = Gtk.ProgressBar()
        self._bar.set_show_text(True)
        self._bar.set_text("")
        self._pulse_id = GLib.timeout_add(80, self._pulse)

        box.append(self._label)
        box.append(self._bar)
        toolbar.set_content(box)
        self.set_child(toolbar)

    def _pulse(self) -> bool:
        self._bar.pulse()
        return True

    def set_label(self, text: str) -> None:
        self._label.set_text(text)

    def set_fraction(self, fraction: float) -> None:
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self._bar.set_fraction(fraction)

    def set_stats(self, text: str) -> None:
        self._bar.set_text(text)

    def force_close(self) -> None:
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self.set_can_close(True)
        self.close()


class _WinetricksProgressDialog(Adw.Dialog):
    """Progress dialog for winetricks verb installation.

    Streams output from the winetricks subprocess line-by-line via
    :meth:`push_line`.  Parses key lines to show a human-readable
    "current operation" label and appends all output to a scrollable log.
    """

    # Regex patterns for line classification
    _RE_DOWNLOADING = re.compile(
        r"Downloading https?://\S+\s+to\s+.*/winetricks/(\w+)", re.IGNORECASE
    )
    _RE_SKIP = re.compile(r"^(\S+) already installed, skipping")
    _RE_RUNNING = re.compile(
        r"Running winetricks verbs[^:]*:\s*(.+)$", re.IGNORECASE
    )
    # curl progress lines: start with whitespace+digits or the header row
    _RE_CURL = re.compile(r"^\s*(%\s+Total|[\d\s]+%)")

    _MAX_LOG_LINES = 200

    def __init__(self, verbs: list[str]) -> None:
        super().__init__(content_width=480)
        self.set_can_close(False)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.set_title_widget(Gtk.Label(label="Installing Dependencies"))
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)

        verb_label = Gtk.Label(label="Running: " + ", ".join(verbs))
        verb_label.set_xalign(0)
        verb_label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        verb_label.add_css_class("caption")
        verb_label.add_css_class("dim-label")
        box.append(verb_label)

        self._bar = Gtk.ProgressBar()
        self._bar.set_show_text(True)
        self._bar.set_text("")
        self._pulse_id = GLib.timeout_add(80, self._pulse)
        box.append(self._bar)

        self._status_label = Gtk.Label(label="Starting…")
        self._status_label.set_xalign(0)
        self._status_label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        box.append(self._status_label)

        # Scrollable log view
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_min_content_height(200)
        scroll.set_vexpand(True)
        scroll.add_css_class("card")

        self._text_buffer = Gtk.TextBuffer()
        text_view = Gtk.TextView(buffer=self._text_buffer, editable=False, cursor_visible=False)
        text_view.set_monospace(True)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_margin_top(6)
        text_view.set_margin_bottom(6)
        text_view.set_margin_start(8)
        text_view.set_margin_end(8)
        scroll.set_child(text_view)
        box.append(scroll)

        self._log_lines: list[str] = []
        self._scroll = scroll

        toolbar.set_content(box)
        self.set_child(toolbar)

    def _pulse(self) -> bool:
        self._bar.pulse()
        return True

    def push_line(self, line: str) -> None:
        """Called from GLib.idle_add with each output line from winetricks."""
        # Update status label from meaningful lines
        m = self._RE_DOWNLOADING.search(line)
        if m:
            self._status_label.set_text(f"Downloading {m.group(1)}…")
        elif self._RE_RUNNING.search(line):
            self._status_label.set_text("Running winetricks…")
        elif self._RE_SKIP.match(line):
            verb = self._RE_SKIP.match(line).group(1)
            self._status_label.set_text(f"{verb} already installed, skipping")

        # Append to log (skip noisy curl progress header rows)
        if not self._RE_CURL.match(line):
            self._log_lines.append(line)
            if len(self._log_lines) > self._MAX_LOG_LINES:
                self._log_lines.pop(0)
            self._text_buffer.set_text("\n".join(self._log_lines))
            # Auto-scroll to bottom
            end_iter = self._text_buffer.get_end_iter()
            self._text_buffer.place_cursor(end_iter)
            adj = self._scroll.get_vadjustment()
            adj.set_value(adj.get_upper())

    def force_close(self) -> None:
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self.set_can_close(True)
        self.close()

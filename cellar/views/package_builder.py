"""Package Builder view — create and publish WINEPREFIX-based app packages.

Shown in the main window when at least one writable repo is configured.
Maintainers use this view to:

1. Create a project (App or Base) that owns a WINEPREFIX.
2. Select a GE-Proton runner and initialize the prefix.
3. Install dependencies (winetricks verbs) and run installers.
4. Set an entry point (App projects only).
5. Test-launch the app to verify it works.
6. Publish — archive the prefix and open AddAppDialog pre-filled with
   project metadata (App), or upload the base directly to the repo (Base).
"""

from __future__ import annotations

import html
import logging
import os
import subprocess
import threading
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

        # Detail container — populated by _show_project()
        self._detail_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._detail_scroll.set_vexpand(True)
        self._detail_stack.add_named(self._detail_scroll, "detail")

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
        self._show_create_dialog("app")

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

    def _show_create_dialog(self, project_type: ProjectType) -> None:
        """Show dialog to create a new App or Base project."""
        dialog = _CreateProjectDialog(
            project_type=project_type,
            on_created=self._on_project_created,
        )
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

        # ── 1. Runner / Base Image (expandable, at top) ───────────────────
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

        # ── 2. Prefix section ─────────────────────────────────────────────
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

        # ── 3. Dependencies section ───────────────────────────────────────
        dep_group = Adw.PreferencesGroup(title="Dependencies")
        dep_group.set_description(
            "Winetricks verbs installed in this prefix. "
            "Requires winetricks on PATH."
        )
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
        dep_group.add(add_dep_row)
        page.add(dep_group)

        # ── 4. Files section (App projects only) ──────────────────────────
        if project.project_type == "app":
            files_group = Adw.PreferencesGroup(title="Files")

            run_installer_row = Adw.ActionRow(
                title="Run Installer",
                subtitle="Run a .exe inside the prefix",
            )
            run_btn = Gtk.Button(label="Choose\u2026")
            run_btn.set_valign(Gtk.Align.CENTER)
            run_btn.connect("clicked", self._on_run_installer_clicked)
            run_installer_row.add_suffix(run_btn)
            files_group.add(run_installer_row)

            ep_subtitle = project.entry_point or "Not set"
            self._ep_row = Adw.ActionRow(
                title="Entry Point",
                subtitle=ep_subtitle,
            )
            self._ep_row.set_subtitle_selectable(True)
            ep_btn = Gtk.Button(label="Set\u2026")
            ep_btn.set_valign(Gtk.Align.CENTER)
            ep_btn.connect("clicked", self._on_set_entry_point_clicked)
            self._ep_row.add_suffix(ep_btn)
            files_group.add(self._ep_row)

            page.add(files_group)

        # ── 5. Publish section ────────────────────────────────────────────
        pkg_group = Adw.PreferencesGroup(title="Publish")

        # Browse Prefix (both project types)
        browse_row = Adw.ActionRow(
            title="Browse Prefix",
            subtitle="Open drive_c in the file manager",
        )
        browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
        browse_btn.set_valign(Gtk.Align.CENTER)
        browse_btn.add_css_class("flat")
        browse_btn.connect("clicked", self._on_browse_prefix_clicked)
        browse_row.add_suffix(browse_btn)
        pkg_group.add(browse_row)

        if project.project_type == "app":
            # Steam App ID
            self._steam_id_entry = Adw.EntryRow(title="Steam App ID (optional)")
            self._steam_id_entry.set_tooltip_text(
                "Used to set GAMEID for protonfixes. Leave empty for GAMEID=0."
            )
            if project.steam_appid is not None:
                self._steam_id_entry.set_text(str(project.steam_appid))
            self._steam_id_entry.connect("changed", self._on_steam_id_changed)
            pkg_group.add(self._steam_id_entry)

            # Test launch
            test_row = Adw.ActionRow(
                title="Test Launch",
                subtitle="Launch the app to verify it works",
            )
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

        first_check: Gtk.CheckButton | None = None
        for runner in base_runners:
            row = Adw.ActionRow(title=runner)
            check = Gtk.CheckButton()
            check.set_valign(Gtk.Align.CENTER)
            if first_check is None:
                first_check = check
            else:
                check.set_group(first_check)
            check.set_active(runner == project.runner)
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
                result = init_prefix(project.prefix_path, project.runner)
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
            if hasattr(self, "_prefix_status_row"):
                self._prefix_status_row.set_subtitle("Initialized")
            if hasattr(self, "_init_btn"):
                self._init_btn.set_sensitive(False)

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

    def _on_set_entry_point_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        drive_c = project.prefix_path / "drive_c"
        chooser = Gtk.FileChooserNative(
            title="Select Entry Point (.exe)",
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Set",
        )
        win = self.get_root()
        if isinstance(win, Gtk.Window):
            chooser.set_transient_for(win)
        if drive_c.is_dir():
            chooser.set_current_folder(Gio.File.new_for_path(str(drive_c)))
        f = Gtk.FileFilter()
        f.set_name("Windows executables")
        f.add_pattern("*.exe")
        chooser.add_filter(f)
        chooser.connect(
            "response",
            lambda c, r: self._on_entry_point_chosen(c, r, project, drive_c),
        )
        chooser.show()
        self._ep_chooser = chooser

    def _on_entry_point_chosen(
        self,
        chooser: Gtk.FileChooserNative,
        response: int,
        project: Project,
        drive_c: Path,
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        path = chooser.get_file().get_path()
        try:
            rel = os.path.relpath(path, str(drive_c))
        except ValueError:
            rel = path
        project.entry_point = rel
        save_project(project)
        if hasattr(self, "_ep_row"):
            self._ep_row.set_subtitle(rel)

    # ------------------------------------------------------------------
    # Signal handlers — package
    # ------------------------------------------------------------------

    def _on_steam_id_changed(self, entry) -> None:
        if self._project is None:
            return
        text = entry.get_text().strip()
        self._project.steam_appid = int(text) if text.isdigit() else None
        save_project(self._project)

    def _on_test_launch_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if not project.entry_point:
            self._show_toast("Set an entry point before test launching.")
            return
        if not project.runner:
            self._show_toast("Select a runner before test launching.")
            return
        from cellar.backend.umu import launch_app
        launch_app(
            app_id=f"project-{project.slug}",
            entry_point=str(project.prefix_path / "drive_c" / project.entry_point),
            runner_name=project.runner,
            steam_appid=project.steam_appid,
        )

    def _on_publish_app_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if not project.entry_point:
            self._show_toast("Set an entry point before publishing.")
            return
        if not project.runner:
            self._show_toast("Select a runner before publishing.")
            return
        if not self._writable_repos:
            self._show_toast("No writable repository configured.")
            return

        progress = _ProgressDialog(label="Packaging prefix…")
        progress.present(self)

        cancel_event = threading.Event()

        def _bg():
            try:
                archive_path, size, crc32 = package_project(
                    project,
                    cancel_event=cancel_event,
                    progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                )
                GLib.idle_add(_done, archive_path, size, crc32)
            except Exception as exc:
                GLib.idle_add(_error, str(exc))

        def _done(archive_path: Path, size: int, crc32: str) -> None:
            progress.force_close()
            from cellar.views.add_app import AddAppDialog
            prefill = {
                "name": project.name,
                "runner": project.runner,
                "entry_point": project.entry_point,
            }
            if project.steam_appid is not None:
                prefill["steam_appid"] = project.steam_appid
            win = self.get_root()
            dlg = AddAppDialog(
                archive_path=str(archive_path),
                repos=self._writable_repos,
                on_done=self._on_catalogue_changed or (lambda: None),
                prefill=prefill,
            )
            dlg.present(win)

        def _error(msg: str) -> None:
            progress.force_close()
            err = Adw.AlertDialog(heading="Packaging failed", body=msg)
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
            self._show_toast("Set an entry point before publishing.")
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

        progress = _ProgressDialog(label="Packaging prefix…")
        progress.present(self)

        def _bg():
            try:
                archive_path, size, _crc = package_project(
                    project,
                    progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                )
                GLib.idle_add(_upload, archive_path, size)
            except Exception as exc:
                GLib.idle_add(_error, str(exc))

        def _upload(archive_path: Path, size: int) -> None:
            GLib.idle_add(progress.set_label, "Uploading to repository…")
            GLib.idle_add(progress.set_fraction, 0.0)

            def _do_upload():
                try:
                    from cellar.backend.packager import update_in_repo
                    repo_root = repo.writable_path()
                    update_in_repo(
                        repo_root,
                        old_entry,
                        old_entry,          # keep same ID/name/images; CRC updated inside
                        images={},
                        new_archive_src=str(archive_path),
                        progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                        phase_cb=lambda s: GLib.idle_add(progress.set_label, s),
                    )
                    GLib.idle_add(_done)
                except Exception as exc:
                    GLib.idle_add(_error, str(exc))

            threading.Thread(target=_do_upload, daemon=True).start()

        def _done() -> None:
            progress.force_close()
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


class _CreateProjectDialog(Adw.Dialog):
    """Dialog for creating a new App project (name input only)."""

    def __init__(self, project_type: ProjectType, on_created: Callable) -> None:
        super().__init__(title="New App Project", content_width=440)
        self._on_created = on_created

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

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
        group = Adw.PreferencesGroup()

        self._name_entry = Adw.EntryRow(title="Project Name")
        self._name_entry.connect("changed", self._validate)
        group.add(self._name_entry)

        page.add(group)
        toolbar.set_content(page)
        self.set_child(toolbar)

    def _validate(self, *_args) -> None:
        self._create_btn.set_sensitive(bool(self._name_entry.get_text().strip()))

    def _on_create_clicked(self, _btn) -> None:
        name = self._name_entry.get_text().strip()
        if not name:
            return
        project = create_project(name, "app")
        self.close()
        self._on_created(project)


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
            size_mb = rel["size"] / (1024 * 1024) if rel.get("size") else 0
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
            size_mb = base_entry.archive_size / (1024 * 1024) if base_entry.archive_size else 0
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
        self._entries: list[tuple] = []   # (entry, repo)
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
        results: list[tuple] = []
        for repo in self._repos:
            try:
                for entry in repo.fetch_catalogue():
                    if entry.archive:
                        results.append((entry, repo))
            except Exception as exc:
                log.warning("Could not fetch catalogue from %s: %s", repo.uri, exc)
        results.sort(key=lambda t: t[0].name.lower())
        GLib.idle_add(self._populate, results)

    def _populate(self, results: list[tuple]) -> None:
        self._entries = results
        for entry, repo in results:
            size_mb = (entry.archive_size or 0) / (1024 * 1024)
            size_str = f"{size_mb:.0f} MB" if size_mb else ""
            repo_name = repo.name or repo.uri
            subtitle_parts = [entry.version or "", size_str, repo_name]
            subtitle = "  ·  ".join(p for p in subtitle_parts if p)
            row = Adw.ActionRow(title=html.escape(entry.name), subtitle=html.escape(subtitle))
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
        entry, repo = self._entries[self._selected_idx]

        size_mb = (entry.archive_size or 0) / (1024 * 1024)
        size_str = f"{size_mb:.0f} MB" if size_mb else "unknown size"
        dialog = Adw.AlertDialog(
            heading=f'Import \u201c{entry.name}\u201d?',
            body=(
                f"The archive ({size_str}) will be downloaded and extracted "
                f"into a new project directory. This may take a while."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("import", "Import")
        dialog.set_response_appearance("import", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("import")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda d, r: self._start_import(entry, repo) if r == "import" else None)
        dialog.present(self)

    def _start_import(self, entry, repo) -> None:
        self.close()

        progress = _ProgressDialog(label="Downloading…")
        root = self.get_root()
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

                    project = Project(
                        name=entry.name,
                        slug=slug,
                        project_type="app",
                        runner=entry.runner or "",
                        entry_point=entry.entry_point or "",
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
        stack.set_visible_child_name("installing")

        def _bg():
            from cellar.backend.umu import run_winetricks
            try:
                result = run_winetricks(
                    self._project.prefix_path,
                    self._project.runner,
                    verbs,
                )
                ok = result.returncode == 0
            except Exception as exc:
                log.error("run_winetricks failed: %s", exc)
                ok = False
            GLib.idle_add(_finish, ok)

        def _finish(ok: bool) -> None:
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
        self._bar.set_show_text(False)
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

    def force_close(self) -> None:
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self.set_can_close(True)
        self.close()

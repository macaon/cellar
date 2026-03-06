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


class PackageBuilderView(Gtk.Box):
    """Two-panel package builder: project list on the left, detail on the right."""

    def __init__(
        self,
        *,
        writable_repos: list | None = None,
        on_catalogue_changed: Callable | None = None,
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self._writable_repos: list = writable_repos or []
        self._on_catalogue_changed = on_catalogue_changed
        self._project: Project | None = None
        self._project_rows: list[_ProjectRow] = []

        self._build()
        self._reload_projects()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_repos(self, writable_repos: list) -> None:
        self._writable_repos = writable_repos

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

        self._delete_btn = Gtk.Button(icon_name="edit-delete-symbolic")
        self._delete_btn.set_tooltip_text("Delete project")
        self._delete_btn.add_css_class("flat")
        self._delete_btn.add_css_class("destructive-action")
        self._delete_btn.set_sensitive(False)
        self._delete_btn.connect("clicked", self._on_delete_clicked)

        btn_box.append(new_app_btn)
        btn_box.append(new_base_btn)
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
        self._show_create_dialog("base")

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

    def _show_project(self, project: Project) -> None:
        """Build and display the detail panel for *project*."""
        # Rebuild the detail content
        page = Adw.PreferencesPage()
        clamp = Adw.Clamp(maximum_size=700)
        clamp.set_child(page)
        self._detail_scroll.set_child(clamp)

        # ── Prefix section ────────────────────────────────────────────────
        prefix_group = Adw.PreferencesGroup(title="Prefix")

        # Runner selector
        from cellar.backend import runners as _runners
        runners = _runners.installed_runners()

        runner_model = Gtk.StringList.new(runners if runners else ["(no runners installed)"])
        self._runner_row = Adw.ComboRow(title="Runner")
        self._runner_row.set_subtitle_lines(1)
        self._runner_row.set_model(runner_model)
        if project.runner and project.runner in runners:
            self._runner_row.set_selected(runners.index(project.runner))
        self._runner_row.connect("notify::selected", self._on_runner_changed)

        dl_btn = Gtk.Button(label="Download…")
        dl_btn.set_valign(Gtk.Align.CENTER)
        dl_btn.add_css_class("flat")
        dl_btn.connect("clicked", self._on_download_runner_clicked)
        self._runner_row.add_suffix(dl_btn)

        prefix_group.add(self._runner_row)

        # Prefix status row
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

        # ── Dependencies section ───────────────────────────────────────────
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

        add_dep_btn = Gtk.Button(label="Add dependency…")
        add_dep_btn.add_css_class("flat")
        add_dep_btn.connect("clicked", self._on_add_dep_clicked)
        dep_group.set_header_suffix(add_dep_btn)

        page.add(dep_group)

        # ── Files section (App projects only) ─────────────────────────────
        if project.project_type == "app":
            files_group = Adw.PreferencesGroup(title="Files")

            run_installer_row = Adw.ActionRow(
                title="Run Installer",
                subtitle="Run a .exe inside the prefix",
            )
            run_btn = Gtk.Button(label="Choose…")
            run_btn.set_valign(Gtk.Align.CENTER)
            run_btn.connect("clicked", self._on_run_installer_clicked)
            run_installer_row.add_suffix(run_btn)
            files_group.add(run_installer_row)

            browse_row = Adw.ActionRow(
                title="Browse Prefix",
                subtitle="Open drive_c in the file manager",
            )
            browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
            browse_btn.set_valign(Gtk.Align.CENTER)
            browse_btn.add_css_class("flat")
            browse_btn.connect("clicked", self._on_browse_prefix_clicked)
            browse_row.add_suffix(browse_btn)
            files_group.add(browse_row)

            ep_subtitle = project.entry_point or "Not set"
            self._ep_row = Adw.ActionRow(
                title="Entry Point",
                subtitle=ep_subtitle,
            )
            self._ep_row.set_subtitle_selectable(True)
            ep_btn = Gtk.Button(label="Set…")
            ep_btn.set_valign(Gtk.Align.CENTER)
            ep_btn.connect("clicked", self._on_set_entry_point_clicked)
            self._ep_row.add_suffix(ep_btn)
            files_group.add(self._ep_row)

            page.add(files_group)

        # ── Package section ────────────────────────────────────────────────
        pkg_group = Adw.PreferencesGroup(title="Package")

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

            # Publish App
            publish_row = Adw.ActionRow(
                title="Publish App",
                subtitle="Archive prefix and open Add to Catalogue dialog",
            )
            pub_btn = Gtk.Button(label="Publish…")
            pub_btn.set_valign(Gtk.Align.CENTER)
            pub_btn.add_css_class("suggested-action")
            pub_btn.connect("clicked", self._on_publish_app_clicked)
            publish_row.add_suffix(pub_btn)
            pkg_group.add(publish_row)

        else:
            # Base: browse only
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

            # Publish Base
            publish_row = Adw.ActionRow(
                title="Publish Base",
                subtitle="Archive prefix and upload to repository",
            )
            pub_btn = Gtk.Button(label="Publish…")
            pub_btn.set_valign(Gtk.Align.CENTER)
            pub_btn.add_css_class("suggested-action")
            pub_btn.connect("clicked", self._on_publish_base_clicked)
            publish_row.add_suffix(pub_btn)
            pkg_group.add(publish_row)

        page.add(pkg_group)

        self._detail_stack.set_visible_child_name("detail")

    # ------------------------------------------------------------------
    # Signal handlers — prefix
    # ------------------------------------------------------------------

    def _on_runner_changed(self, row, _param) -> None:
        if self._project is None:
            return
        from cellar.backend import runners as _runners
        runners = _runners.installed_runners()
        idx = row.get_selected()
        if 0 <= idx < len(runners):
            self._project.runner = runners[idx]
            # For base projects the name IS the runner name.
            if self._project.project_type == "base":
                self._project.name = runners[idx]
                for r in self._project_rows:
                    if r.project is self._project:
                        r.refresh_label()
                        break
            save_project(self._project)
            if hasattr(self, "_init_btn"):
                self._init_btn.set_sensitive(
                    bool(self._project.runner) and not self._project.initialized
                )

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
            # Auto-select the newly installed runner if no runner was set
            if not project.runner:
                project.runner = runner_name
                save_project(project)
            self._show_project(project)

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
        dialog = _AddDepDialog(on_add=self._on_dep_chosen)
        dialog.present(self)

    def _on_dep_chosen(self, verbs: str) -> None:
        if self._project is None or not verbs.strip():
            return
        project = self._project
        verb_list = verbs.strip().split()
        if not project.runner:
            self._show_toast("Select a runner first.")
            return

        progress = _ProgressDialog(label=f"Installing {verbs.strip()}…")
        progress.present(self)

        def _bg():
            try:
                from cellar.backend.umu import run_winetricks
                result = run_winetricks(project.prefix_path, project.runner, verb_list)
                ok = result.returncode == 0
            except Exception as exc:
                log.error("run_winetricks failed: %s", exc)
                ok = False
            GLib.idle_add(_finish, ok)

        def _finish(ok: bool) -> None:
            progress.force_close()
            self._on_dep_done(project, verbs.strip(), ok)
            if not ok:
                self._show_toast("Dependency install failed. Check logs.")

        threading.Thread(target=_bg, daemon=True).start()

    def _on_dep_done(self, project: Project, verbs: str, ok: bool) -> None:
        if ok:
            for verb in verbs.split():
                if verb not in project.deps_installed:
                    project.deps_installed.append(verb)
            save_project(project)
            self._show_project(project)

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
        progress = _ProgressDialog(label="Packaging base…")
        progress.present(self)

        def _bg():
            try:
                archive_path, size, crc32 = package_project(
                    project,
                    progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                )
                GLib.idle_add(_done, archive_path, size, crc32)
            except Exception as exc:
                GLib.idle_add(_error, str(exc))

        def _done(archive_path: Path, size: int, crc32: str) -> None:
            progress.set_label("Uploading to repository…")
            GLib.idle_add(progress.set_fraction, 0.0)

            def _upload():
                try:
                    runner = project.runner
                    repo_root = repo.writable_path()
                    archive_dest_rel = f"bases/{runner}-base.tar.zst"
                    archive_dest = repo_root / archive_dest_rel
                    archive_dest.parent.mkdir(parents=True, exist_ok=True)

                    # Copy archive to repo
                    import shutil
                    shutil.copy2(str(archive_path), str(archive_dest))

                    # Update catalogue.json
                    from cellar.backend.packager import upsert_base
                    upsert_base(repo_root, runner, archive_dest_rel, crc32, size)

                    # Install base locally for delta creation
                    from cellar.backend.base_store import install_base
                    install_base(archive_path, runner, repo_source=repo.uri)

                    GLib.idle_add(_upload_done)
                except Exception as exc:
                    GLib.idle_add(_error, str(exc))

            threading.Thread(target=_upload, daemon=True).start()

        def _upload_done() -> None:
            progress.force_close()
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
    """Dialog for creating a new App or Base project.

    App projects: user supplies a name (the only required input).
    Base projects: user picks a runner + Windows version — the name and slug
    are auto-generated from those two choices, so no free-text name is needed.
    """

    def __init__(self, project_type: ProjectType, on_created: Callable) -> None:
        title = "New Base Image" if project_type == "base" else "New App Project"
        super().__init__(title=title, content_width=440)
        self._project_type = project_type
        self._on_created = on_created

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        self._create_btn = Gtk.Button(label="Create")
        self._create_btn.add_css_class("suggested-action")
        self._create_btn.connect("clicked", self._on_create_clicked)
        header.pack_end(self._create_btn)

        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()

        self._name_entry: Adw.EntryRow | None = None
        self._runner_row: Adw.ComboRow | None = None
        self._runners: list[str] = []

        if project_type == "app":
            # App projects need a user-supplied name.
            self._name_entry = Adw.EntryRow(title="Project Name")
            self._name_entry.connect("changed", self._validate)
            group.add(self._name_entry)
        else:
            # Base projects: identity = runner.  Name is auto-generated.
            from cellar.backend import runners as _runners
            self._runners = _runners.installed_runners()

            runner_model = Gtk.StringList.new(self._runners or ["(no runners installed)"])
            self._runner_row = Adw.ComboRow(title="Runner")
            self._runner_row.set_model(runner_model)
            self._runner_row.connect("notify::selected", self._validate)

            dl_btn = Gtk.Button(label="Download…")
            dl_btn.set_valign(Gtk.Align.CENTER)
            dl_btn.add_css_class("flat")
            dl_btn.connect("clicked", self._on_download_runner_clicked)
            self._runner_row.add_suffix(dl_btn)

            group.add(self._runner_row)
            group.set_description(
                "One base per runner. Windows version is set per-app via winetricks."
            )

        page.add(group)
        toolbar.set_content(page)
        self.set_child(toolbar)
        self._validate()

    def _validate(self, *_args) -> None:
        ok = True
        if self._name_entry is not None:
            ok = bool(self._name_entry.get_text().strip())
        elif self._runner_row is not None:
            ok = bool(self._runners) and self._runner_row.get_selected() < len(self._runners)
        self._create_btn.set_sensitive(ok)

    def _on_download_runner_clicked(self, _btn) -> None:
        def _on_installed(name: str) -> None:
            from cellar.backend import runners as _runners
            self._runners = _runners.installed_runners()
            model = Gtk.StringList.new(self._runners)
            self._runner_row.set_model(model)
            if name in self._runners:
                self._runner_row.set_selected(self._runners.index(name))
            self._validate()

        dialog = _RunnerPickerDialog(on_installed=_on_installed)
        dialog.present(self)

    def _on_create_clicked(self, _btn) -> None:
        if self._project_type == "app":
            name = self._name_entry.get_text().strip()
            if not name:
                return
            project = create_project(name, "app")
        else:
            runner = ""
            if self._runners and self._runner_row is not None:
                idx = self._runner_row.get_selected()
                if 0 <= idx < len(self._runners):
                    runner = self._runners[idx]
            project = create_project("", "base", runner=runner)
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


class _AddDepDialog(Adw.Dialog):
    """Simple dialog to enter winetricks verbs."""

    def __init__(self, on_add: Callable[[str], None]) -> None:
        super().__init__(title="Add Dependency", content_width=380)
        self._on_add = on_add

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        self._add_btn = Gtk.Button(label="Install")
        self._add_btn.add_css_class("suggested-action")
        self._add_btn.set_sensitive(False)
        self._add_btn.connect("clicked", self._on_add_clicked)
        header.pack_end(self._add_btn)

        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup()
        group.set_description(
            "Enter one or more winetricks verbs separated by spaces, "
            "e.g. dotnet48 vcrun2022 corefonts"
        )
        self._verb_entry = Adw.EntryRow(title="Winetricks verbs")
        self._verb_entry.connect("changed", self._on_verb_changed)
        self._verb_entry.connect("entry-activated", lambda _: self._add_btn.emit("clicked"))
        group.add(self._verb_entry)
        page.add(group)
        toolbar.set_content(page)
        self.set_child(toolbar)

    def _on_verb_changed(self, entry) -> None:
        self._add_btn.set_sensitive(bool(entry.get_text().strip()))

    def _on_add_clicked(self, _btn) -> None:
        verbs = self._verb_entry.get_text().strip()
        if verbs:
            self.close()
            self._on_add(verbs)


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

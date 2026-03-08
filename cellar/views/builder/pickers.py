"""Picker dialogs: launch target, runner, and base image selection."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from cellar.utils.async_work import run_in_background
from cellar.views.builder.progress import ProgressDialog
from cellar.views.widgets import make_loading_stack

log = logging.getLogger(__name__)


class AddLaunchTargetDialog(Adw.Dialog):
    """Dialog for adding a new launch target (name + executable path) to a project."""

    def __init__(self, content_path: Path, on_added: Callable, platform: str = "windows") -> None:
        super().__init__(title="Add Launch Target", content_width=480)
        self._content_path = content_path
        self._platform = platform
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

        self._exe_row = Adw.ActionRow(title="Executable")
        self._exe_row.set_subtitle("Not selected")
        browse_btn = Gtk.Button(label="Browse\u2026", valign=Gtk.Align.CENTER)
        browse_btn.add_css_class("suggested-action")
        browse_btn.connect("clicked", self._on_browse_clicked)
        self._exe_row.add_suffix(browse_btn)
        self._exe_row.set_activatable_widget(browse_btn)
        group.add(self._exe_row)

        self._name_row = Adw.EntryRow(title="Name")
        self._name_row.set_tooltip_text('E.g. "Main Game", "Config Tool"')
        self._name_row.connect("changed", self._validate)
        group.add(self._name_row)

        self._args_row = Adw.EntryRow(title="Arguments (optional)")
        self._args_row.set_tooltip_text('E.g. "-windowedmode -nosplash"')
        group.add(self._args_row)

        page.add(group)
        toolbar.set_content(page)
        self.set_child(toolbar)

    def _validate(self, *_) -> None:
        self._add_btn.set_sensitive(
            bool(self._name_row.get_text().strip()) and bool(self._chosen_path)
        )

    def _on_browse_clicked(self, _btn) -> None:
        if self._platform == "linux":
            browse_root = self._content_path
            title = "Select Executable"
        else:
            browse_root = self._content_path / "drive_c"
            title = "Select Executable (.exe)"
        chooser = Gtk.FileChooserNative(
            title=title,
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Select",
        )
        if browse_root.is_dir():
            chooser.set_current_folder(Gio.File.new_for_path(str(browse_root)))
        if self._platform != "linux":
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
        if self._platform == "linux":
            try:
                rel = os.path.relpath(abs_path, str(self._content_path))
            except ValueError:
                rel = abs_path
            display_path = rel
        else:
            drive_c = self._content_path / "drive_c"
            try:
                rel = os.path.relpath(abs_path, str(drive_c))
                display_path = "C:\\" + rel.replace("/", "\\")
            except ValueError:
                display_path = abs_path
        self._chosen_path = display_path
        self._exe_row.set_subtitle(GLib.markup_escape_text(display_path))
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
        ep: dict = {"name": name, "path": self._chosen_path}
        args = self._args_row.get_text().strip()
        if args:
            ep["args"] = args
        self._on_added(ep)


class RunnerPickerDialog(Adw.Dialog):
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

        self._stack, self._list_box = make_loading_stack("Fetching releases\u2026")
        self._list_box.connect("row-selected", self._on_row_selected)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        run_in_background(self._fetch_releases)

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
                subtitle = ("  \u00b7  " if subtitle else "") + "Installed"
            row.set_subtitle(subtitle)

            if already:
                icon = Gtk.Image.new_from_icon_name("check-round-outline2-symbolic")
                icon.add_css_class("success")
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


class RepoPickerDialog(Adw.Dialog):
    """Lets the user choose which writable repository to publish to.

    If there is only one writable repo, callers should skip this dialog
    and use it directly.
    """

    def __init__(self, repos: list, on_selected: Callable) -> None:
        super().__init__(title="Choose Repository", content_width=440)
        self._repos = repos
        self._on_selected = on_selected
        self._selected_idx: int = -1

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        self._select_btn = Gtk.Button(label="Select")
        self._select_btn.add_css_class("suggested-action")
        self._select_btn.set_sensitive(False)
        self._select_btn.connect("clicked", self._on_select_clicked)
        header.pack_end(self._select_btn)

        toolbar.add_top_bar(header)

        list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        list_box.add_css_class("boxed-list")
        list_box.set_margin_top(12)
        list_box.set_margin_bottom(12)
        list_box.set_margin_start(12)
        list_box.set_margin_end(12)
        list_box.connect("row-selected", self._on_row_selected)

        for repo in repos:
            row = Adw.ActionRow(title=repo.name or repo.uri)
            if repo.name:
                row.set_subtitle(repo.uri)
            list_box.append(row)

        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        sw.set_child(list_box)
        toolbar.set_content(sw)
        self.set_child(toolbar)

    def _on_row_selected(self, _lb, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            self._selected_idx = -1
            self._select_btn.set_sensitive(False)
            return
        self._selected_idx = row.get_index()
        self._select_btn.set_sensitive(True)

    def _on_select_clicked(self, _btn) -> None:
        if not (0 <= self._selected_idx < len(self._repos)):
            return
        repo = self._repos[self._selected_idx]
        self.close()
        self._on_selected(repo)


class BasePickerDialog(Adw.Dialog):
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

        self._stack, self._list_box = make_loading_stack("Fetching bases\u2026")
        self._list_box.connect("row-selected", self._on_row_selected)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        run_in_background(self._fetch_bases)

    def _fetch_bases(self) -> None:
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
            already = is_base_installed(base_entry.name)
            row = Adw.ActionRow(title=base_entry.name)
            size_mb = base_entry.archive_size / 1_000_000 if base_entry.archive_size else 0
            subtitle = f"{size_mb:.0f} MB" if size_mb else ""
            repo_name = repo.name or repo.uri
            if subtitle:
                subtitle += f"  \u00b7  {repo_name}"
            else:
                subtitle = repo_name
            if already:
                subtitle += "  \u00b7  Installed"
            row.set_subtitle(subtitle)

            if already:
                icon = Gtk.Image.new_from_icon_name("check-round-outline2-symbolic")
                icon.add_css_class("success")
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
            name = self._bases[self._selected_idx][0].name
            self._install_btn.set_sensitive(not is_base_installed(name))
        else:
            self._install_btn.set_sensitive(False)

    def _on_install_clicked(self, _btn) -> None:
        if not (0 <= self._selected_idx < len(self._bases)):
            return
        base_entry, repo = self._bases[self._selected_idx]
        parent_win = self.get_root()
        self.close()

        progress = ProgressDialog(label=f"Downloading {base_entry.name}\u2026")
        progress.present(parent_win)

        def _work():
            import tempfile
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

            GLib.idle_add(progress.set_label, "Installing base\u2026")
            GLib.idle_add(progress.set_fraction, 0.0)
            install_base(
                tmp_path,
                base_entry.name,
                repo_source=repo.uri,
                progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
            )
            tmp_path.unlink(missing_ok=True)

        def _done(_result) -> None:
            progress.force_close()
            self._on_installed(base_entry.name)

        def _error(msg: str) -> None:
            progress.force_close()
            log.error("Base install failed: %s", msg)
            err = Adw.AlertDialog(heading="Install failed", body=msg)
            err.add_response("ok", "OK")
            err.present(parent_win)

        run_in_background(_work, on_done=_done, on_error=_error)

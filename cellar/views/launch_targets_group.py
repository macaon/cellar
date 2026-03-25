"""Reusable Launch Targets preferences group.

Provides a self-contained ``Adw.PreferencesGroup`` for editing launch
targets (executable path, arguments, environment, run-as-admin).  Used
by the DOSBox, ScummVM, and generic App Configuration dialogs.

Usage::

    group = LaunchTargetsGroup(entry, overrides)
    page.add(group.widget)
    ...
    targets = group.get_targets()   # list[dict]
"""

from __future__ import annotations

import logging
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.models.app_entry import AppEntry

log = logging.getLogger(__name__)


class LaunchTargetsGroup:
    """Manages a Launch Targets ``Adw.PreferencesGroup``."""

    def __init__(self, entry: AppEntry, overrides: dict | None = None) -> None:
        self._entry = entry
        self._overrides = overrides or {}
        self._launch_targets: list[dict] = []
        self._target_rows: list[Adw.ExpanderRow] = []
        self._chooser = None  # prevent GC of file choosers

        self._group = Adw.PreferencesGroup(title="Launch Targets")
        self._group.set_description(
            "Overrides the executables and arguments from the catalogue"
        )

        self._add_target_row_widget = Adw.ActionRow(title="Add Launch Target\u2026")
        add_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", self._on_add_target_clicked)
        self._add_target_row_widget.add_suffix(add_btn)
        self._add_target_row_widget.set_activatable_widget(add_btn)
        self._group.add(self._add_target_row_widget)

        # Populate from overrides if present, otherwise from catalogue
        targets_source = (
            self._overrides.get("launch_targets")
            or list(self._entry.launch_targets)
        )
        for t in targets_source:
            self._add_target_row_ui(dict(t))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def widget(self) -> Adw.PreferencesGroup:
        """The ``Adw.PreferencesGroup`` to add to a preferences page."""
        return self._group

    def get_targets(self) -> list[dict]:
        """Return the current launch targets as a list of dicts."""
        return list(self._launch_targets)

    def has_changes(self, catalogue_targets: list[dict]) -> bool:
        """Return True if targets differ from catalogue defaults."""
        return list(self._launch_targets) != catalogue_targets

    # ------------------------------------------------------------------
    # Row management
    # ------------------------------------------------------------------

    def _add_target_row_ui(self, target: dict) -> None:
        self._launch_targets.append(target)
        idx = len(self._launch_targets) - 1

        name = target.get("name", "Main")
        path = target.get("path", "")
        args = target.get("args", "")
        env = target.get("env", "")
        run_as_admin = target.get("run_as_admin", False)

        row = Adw.ExpanderRow(title=GLib.markup_escape_text(name))
        row.set_subtitle(GLib.markup_escape_text(path) if path else "Not set")
        row.set_subtitle_lines(1)

        browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
        browse_btn.add_css_class("flat")
        browse_btn.set_valign(Gtk.Align.CENTER)
        browse_btn.set_tooltip_text("Browse for executable\u2026")
        browse_btn.connect("clicked", self._on_browse_target, idx)
        row.add_suffix(browse_btn)

        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.connect("clicked", self._on_remove_target, idx)
        row.add_suffix(del_btn)

        name_entry = Adw.EntryRow(title="Name")
        name_entry.set_text(name)
        name_entry.connect("changed", self._on_target_name_changed, idx, row)
        row.add_row(name_entry)

        path_entry = Adw.EntryRow(title="Path")
        path_entry.set_text(path)
        path_entry.connect("changed", self._on_target_path_changed, idx, row)
        row.add_row(path_entry)

        args_entry = Adw.EntryRow(title="Arguments")
        args_entry.set_text(args)
        args_entry.connect("changed", self._on_target_args_changed, idx)
        row.add_row(args_entry)

        env_entry = Adw.EntryRow(title="Environment")
        env_entry.set_text(env)
        env_entry.set_tooltip_text(
            "Environment variables. Paste Steam launch options directly, e.g. "
            "PROTON_USE_WINED3D=1 PROTON_NO_ESYNC=1 %command% \u2014 "
            "%command% and unrecognised tokens are ignored automatically."
        )
        env_entry.connect("changed", self._on_target_env_changed, idx)
        row.add_row(env_entry)

        is_proton = getattr(self._entry, "platform", "windows") == "windows"
        if is_proton:
            admin_row = Adw.SwitchRow(
                title="Run as Administrator",
                subtitle="Set Wine to run this executable with admin privileges",
            )
            admin_row.set_active(run_as_admin)
            admin_row.connect(
                "notify::active", self._on_target_admin_changed, idx,
            )
            row.add_row(admin_row)

        self._target_rows.append(row)
        self._group.remove(self._add_target_row_widget)
        self._group.add(row)
        self._group.add(self._add_target_row_widget)

    def _on_add_target_clicked(self, _btn) -> None:
        self._add_target_row_ui({"name": "New Target", "path": ""})

    def _on_remove_target(self, _btn, idx: int) -> None:
        if idx >= len(self._launch_targets):
            return
        self._group.remove(self._target_rows[idx])
        self._launch_targets.pop(idx)
        self._target_rows.pop(idx)
        self._rebind_target_indices()

    def _rebind_target_indices(self) -> None:
        old_targets = list(self._launch_targets)
        for row in self._target_rows:
            self._group.remove(row)
        self._launch_targets.clear()
        self._target_rows.clear()
        for t in old_targets:
            self._add_target_row_ui(t)

    # ------------------------------------------------------------------
    # Field change handlers
    # ------------------------------------------------------------------

    def _on_target_name_changed(
        self, entry: Adw.EntryRow, idx: int, row: Adw.ExpanderRow,
    ) -> None:
        if idx < len(self._launch_targets):
            self._launch_targets[idx]["name"] = entry.get_text().strip()
            row.set_title(GLib.markup_escape_text(entry.get_text().strip()))

    def _on_target_path_changed(
        self, entry: Adw.EntryRow, idx: int, row: Adw.ExpanderRow,
    ) -> None:
        if idx < len(self._launch_targets):
            text = entry.get_text().strip()
            self._launch_targets[idx]["path"] = text
            row.set_subtitle(
                GLib.markup_escape_text(text) if text else "Not set",
            )

    def _on_target_args_changed(self, entry: Adw.EntryRow, idx: int) -> None:
        if idx < len(self._launch_targets):
            text = entry.get_text().strip()
            if text:
                self._launch_targets[idx]["args"] = text
            else:
                self._launch_targets[idx].pop("args", None)

    def _on_target_env_changed(self, entry: Adw.EntryRow, idx: int) -> None:
        if idx < len(self._launch_targets):
            text = entry.get_text().strip()
            if text:
                self._launch_targets[idx]["env"] = text
            else:
                self._launch_targets[idx].pop("env", None)

    def _on_target_admin_changed(
        self, switch: Adw.SwitchRow, _pspec, idx: int,
    ) -> None:
        if idx < len(self._launch_targets):
            if switch.get_active():
                self._launch_targets[idx]["run_as_admin"] = True
            else:
                self._launch_targets[idx].pop("run_as_admin", None)

    # ------------------------------------------------------------------
    # Browse for executable
    # ------------------------------------------------------------------

    def _get_browse_root(self) -> Path:
        """Determine the root directory for the file chooser."""
        entry = self._entry
        if entry.platform in ("linux", "dos"):
            from cellar.backend.database import get_installed

            rec = get_installed(entry.id)
            has_path = rec and rec.get("install_path")
            if has_path:
                browse_root = Path(rec["install_path"]) / entry.id
            elif entry.platform == "dos":
                from cellar.backend.umu import dos_dir

                browse_root = dos_dir() / entry.id
            else:
                from cellar.backend.umu import native_dir

                browse_root = native_dir() / entry.id
            if not browse_root.is_dir():
                browse_root = Path.home()
        else:
            from cellar.backend.umu import prefixes_dir

            prefix = prefixes_dir() / entry.id / "drive_c"
            browse_root = prefix if prefix.is_dir() else Path.home()
        return browse_root

    def _on_browse_target(self, _btn, idx: int) -> None:
        from gi.repository import Gio  # noqa: PLC0415

        browse_root = self._get_browse_root()
        entry = self._entry

        chooser = Gtk.FileChooserNative(
            title="Select Executable",
            transient_for=self._group.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Select",
        )
        chooser.set_current_folder(Gio.File.new_for_path(str(browse_root)))
        if entry.platform not in ("linux", "dos"):
            exe_filter = Gtk.FileFilter()
            exe_filter.set_name("Windows executables")
            for ext in ("exe", "msi", "bat", "cmd", "com", "lnk"):
                exe_filter.add_pattern(f"*.{ext}")
                exe_filter.add_pattern(f"*.{ext.upper()}")
            chooser.add_filter(exe_filter)
            all_filter = Gtk.FileFilter()
            all_filter.set_name("All files")
            all_filter.add_pattern("*")
            chooser.add_filter(all_filter)
        chooser.connect(
            "response",
            self._on_browse_response,
            chooser,
            browse_root,
            entry.platform,
            idx,
        )
        chooser.show()
        self._chooser = chooser  # prevent GC

    def _on_browse_response(
        self,
        _c,
        response: int,
        chooser,
        browse_root: Path,
        platform: str,
        idx: int,
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        abs_path = chooser.get_file().get_path()
        if platform in ("linux", "dos"):
            import os

            try:
                formatted = os.path.relpath(abs_path, str(browse_root))
            except ValueError:
                formatted = abs_path
        else:
            from cellar.utils.paths import to_win32_path

            formatted = to_win32_path(abs_path, str(browse_root))
        if idx < len(self._launch_targets):
            self._launch_targets[idx]["path"] = formatted
            self._target_rows[idx].set_subtitle(
                GLib.markup_escape_text(formatted),
            )
            # Update the path entry inside the row
            row = self._target_rows[idx]
            child = row.get_first_child()
            while child:
                if isinstance(child, Adw.EntryRow) and child.get_title() == "Path":
                    child.set_text(formatted)
                    break
                child = child.get_next_sibling()

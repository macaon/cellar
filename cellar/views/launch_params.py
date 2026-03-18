"""Launch Parameters dialog — per-installation overrides for launch settings."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.models.app_entry import AppEntry

log = logging.getLogger(__name__)

_AUDIO_LABELS = ["Auto", "PulseAudio", "ALSA", "OSS"]
_AUDIO_VALUES = ["auto", "pulseaudio", "alsa", "oss"]


class LaunchParamsDialog(Adw.Dialog):
    """Per-installation launch parameter overrides.

    Shows catalogue defaults merged with any saved local overrides.  Changes
    are written to the ``launch_overrides`` DB table.  Resetting clears the
    row entirely, reverting all effective values to the catalogue defaults.
    """

    def __init__(
        self,
        entry: AppEntry,
        *,
        on_saved: Callable | None = None,
    ) -> None:
        super().__init__(
            title=f"Launch Parameters — {entry.name}",
            content_width=560,
            content_height=520,
        )
        self._entry = entry
        self._on_saved = on_saved

        # Launch-target editing state
        self._launch_targets: list[dict] = []
        self._target_rows: list[Adw.ExpanderRow] = []
        self._targets_group: Adw.PreferencesGroup | None = None
        self._add_target_row_widget: Adw.ActionRow | None = None

        self._build_ui()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        from cellar.backend import database

        overrides = database.get_launch_overrides(self._entry.id)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        reset_btn = Gtk.Button(label="Reset")
        reset_btn.add_css_class("destructive-action")
        reset_btn.set_tooltip_text("Reset all overrides to catalogue defaults")
        reset_btn.connect("clicked", self._on_reset_clicked)
        header.pack_start(reset_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_clicked)
        header.pack_end(save_btn)

        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        self.set_child(toolbar)

        # ── Launch Targets group ──────────────────────────────────────────
        targets_group = Adw.PreferencesGroup(title="Launch Targets")
        targets_group.set_description(
            "Overrides the executables and arguments from the catalogue"
        )

        add_row = Adw.ActionRow(title="Add Launch Target\u2026")
        add_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", self._on_add_target_clicked)
        add_row.add_suffix(add_btn)
        add_row.set_activatable_widget(add_btn)
        self._add_target_row_widget = add_row
        self._targets_group = targets_group
        targets_group.add(add_row)

        page.add(targets_group)

        # Populate from overrides if present, otherwise from catalogue
        targets_source = overrides.get("launch_targets") or list(self._entry.launch_targets)
        for t in targets_source:
            self._add_target_row_ui(dict(t))

        # ── Wine/Proton groups (Windows apps only) ────────────────────────
        is_proton = getattr(self._entry, "platform", "windows") == "windows"
        self._runner_row = None
        self._dxvk_row = None
        self._vkd3d_row = None
        self._audio_row = None
        self._debug_row = None
        self._direct_proton_row = None

        if is_proton:
            runner_group = Adw.PreferencesGroup(title="Runner")
            runner_group.set_description(
                "Override the Wine/Proton runner for this app"
            )

            installed_runners = self._get_installed_runners()
            catalogue_runner = self._get_catalogue_runner()

            options = ["Catalogue default"]
            if catalogue_runner:
                options[0] = f"Catalogue default ({catalogue_runner})"
            options.extend(installed_runners)

            self._runner_row = Adw.ComboRow(title="Runner")
            self._runner_row.set_model(Gtk.StringList.new(options))

            override_runner = overrides.get("runner")
            if override_runner and override_runner in installed_runners:
                self._runner_row.set_selected(installed_runners.index(override_runner) + 1)
            else:
                self._runner_row.set_selected(0)

            runner_group.add(self._runner_row)
            page.add(runner_group)

            # ── Compatibility group ───────────────────────────────────────
            compat_group = Adw.PreferencesGroup(title="Compatibility")

            entry = self._entry

            self._dxvk_row = Adw.SwitchRow(
                title="DXVK",
                subtitle=f"Catalogue default: {'On' if entry.dxvk else 'Off'}",
            )
            self._dxvk_row.set_active(
                overrides["dxvk"] if "dxvk" in overrides else entry.dxvk
            )
            compat_group.add(self._dxvk_row)

            self._vkd3d_row = Adw.SwitchRow(
                title="VKD3D",
                subtitle=f"Catalogue default: {'On' if entry.vkd3d else 'Off'}",
            )
            self._vkd3d_row.set_active(
                overrides["vkd3d"] if "vkd3d" in overrides else entry.vkd3d
            )
            compat_group.add(self._vkd3d_row)

            self._audio_row = Adw.ComboRow(
                title="Audio Driver",
                subtitle=f"Catalogue default: {entry.audio_driver.capitalize()}",
            )
            self._audio_row.set_model(Gtk.StringList.new(_AUDIO_LABELS))
            audio_val = overrides.get("audio_driver") or entry.audio_driver
            if audio_val in _AUDIO_VALUES:
                self._audio_row.set_selected(_AUDIO_VALUES.index(audio_val))
            compat_group.add(self._audio_row)

            self._debug_row = Adw.SwitchRow(
                title="Proton Debug Logging",
                subtitle=f"Catalogue default: {'On' if entry.debug else 'Off'}",
            )
            self._debug_row.set_active(
                overrides["debug"] if "debug" in overrides else entry.debug
            )
            compat_group.add(self._debug_row)

            self._direct_proton_row = Adw.SwitchRow(
                title="Direct Proton Launch",
                subtitle=f"Catalogue default: {'On' if entry.direct_proton else 'Off'}",
            )
            self._direct_proton_row.set_active(
                overrides["direct_proton"] if "direct_proton" in overrides
                else entry.direct_proton
            )
            compat_group.add(self._direct_proton_row)

            page.add(compat_group)

    # ------------------------------------------------------------------
    # Launch target row management (adapted from metadata_editor.py)
    # ------------------------------------------------------------------

    def _add_target_row_ui(self, target: dict) -> None:
        self._launch_targets.append(target)
        idx = len(self._launch_targets) - 1

        name = target.get("name", "Main")
        path = target.get("path", "")
        args = target.get("args", "")

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

        self._target_rows.append(row)
        grp = self._targets_group
        grp.remove(self._add_target_row_widget)
        grp.add(row)
        grp.add(self._add_target_row_widget)

    def _on_add_target_clicked(self, _btn) -> None:
        self._add_target_row_ui({"name": "New Target", "path": ""})

    def _on_remove_target(self, _btn, idx: int) -> None:
        if idx >= len(self._launch_targets):
            return
        self._targets_group.remove(self._target_rows[idx])
        self._launch_targets.pop(idx)
        self._target_rows.pop(idx)
        self._rebind_target_indices()

    def _rebind_target_indices(self) -> None:
        old_targets = list(self._launch_targets)
        for row in self._target_rows:
            self._targets_group.remove(row)
        self._launch_targets.clear()
        self._target_rows.clear()
        for t in old_targets:
            self._add_target_row_ui(t)

    def _on_target_name_changed(self, entry: Adw.EntryRow, idx: int,
                                row: Adw.ExpanderRow) -> None:
        if idx < len(self._launch_targets):
            self._launch_targets[idx]["name"] = entry.get_text().strip()
            row.set_title(GLib.markup_escape_text(entry.get_text().strip()))

    def _on_target_path_changed(self, entry: Adw.EntryRow, idx: int,
                                row: Adw.ExpanderRow) -> None:
        if idx < len(self._launch_targets):
            text = entry.get_text().strip()
            self._launch_targets[idx]["path"] = text
            row.set_subtitle(GLib.markup_escape_text(text) if text else "Not set")

    def _on_target_args_changed(self, entry: Adw.EntryRow, idx: int) -> None:
        if idx < len(self._launch_targets):
            text = entry.get_text().strip()
            if text:
                self._launch_targets[idx]["args"] = text
            else:
                self._launch_targets[idx].pop("args", None)

    def _on_browse_target(self, _btn, idx: int) -> None:
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

        from gi.repository import Gio
        chooser = Gtk.FileChooserNative(
            title="Select Executable",
            transient_for=self.get_root(),
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
            "response", self._on_browse_response, chooser, browse_root, entry.platform, idx
        )
        chooser.show()
        self._chooser = chooser  # keep alive

    def _on_browse_response(self, _c, response: int, chooser,
                            browse_root: Path, platform: str, idx: int) -> None:
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
            self._target_rows[idx].set_subtitle(GLib.markup_escape_text(formatted))
            # Update the path entry inside the row
            row = self._target_rows[idx]
            for child in self._iter_row_children(row):
                if isinstance(child, Adw.EntryRow) and child.get_title() == "Path":
                    child.set_text(formatted)
                    break

    @staticmethod
    def _iter_row_children(row: Adw.ExpanderRow):
        child = row.get_first_child()
        while child:
            yield child
            child = child.get_next_sibling()

    # ------------------------------------------------------------------
    # Runner / catalogue helpers
    # ------------------------------------------------------------------

    def _get_installed_runners(self) -> list[str]:
        try:
            from cellar.backend.umu import runners_dir
            return sorted(
                d.name for d in runners_dir().iterdir()
                if d.is_dir() and not d.name.startswith(".")
            )
        except Exception:
            return []

    def _get_catalogue_runner(self) -> str:
        """Return the runner name from the catalogue's base image, if known."""
        try:
            from cellar.backend import database
            rec = database.get_installed(self._entry.id)
            return rec.get("runner", "") if rec else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Save / Reset
    # ------------------------------------------------------------------

    def _collect_overrides(self) -> dict:
        """Build the overrides dict from the current UI state."""
        entry = self._entry
        overrides: dict = {}

        # Launch targets: only save if they differ from catalogue defaults
        catalogue_targets = [dict(t) for t in entry.launch_targets]
        current_targets = list(self._launch_targets)
        if current_targets != catalogue_targets:
            overrides["launch_targets"] = current_targets

        # Runner (Windows apps only)
        if self._runner_row is not None:
            selected_idx = self._runner_row.get_selected()
            if selected_idx > 0:
                installed = self._get_installed_runners()
                if selected_idx - 1 < len(installed):
                    overrides["runner"] = installed[selected_idx - 1]

        # Booleans — only save if different from catalogue default
        if self._dxvk_row is not None and self._dxvk_row.get_active() != entry.dxvk:
            overrides["dxvk"] = self._dxvk_row.get_active()
        if self._vkd3d_row is not None and self._vkd3d_row.get_active() != entry.vkd3d:
            overrides["vkd3d"] = self._vkd3d_row.get_active()
        if self._debug_row is not None and self._debug_row.get_active() != entry.debug:
            overrides["debug"] = self._debug_row.get_active()
        if self._direct_proton_row is not None and self._direct_proton_row.get_active() != entry.direct_proton:
            overrides["direct_proton"] = self._direct_proton_row.get_active()

        # Audio driver
        if self._audio_row is not None:
            audio_val = _AUDIO_VALUES[self._audio_row.get_selected()]
            if audio_val != entry.audio_driver:
                overrides["audio_driver"] = audio_val

        return overrides

    def _on_save_clicked(self, _btn) -> None:
        from cellar.backend import database
        overrides = self._collect_overrides()
        if overrides:
            database.set_launch_overrides(self._entry.id, overrides)
        else:
            database.clear_launch_overrides(self._entry.id)
        self.close()
        if self._on_saved:
            self._on_saved()

    def _on_reset_clicked(self, _btn) -> None:
        dialog = Adw.AlertDialog(
            heading="Reset Launch Parameters?",
            body="This will remove all local overrides and restore the catalogue defaults.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("reset", "Reset")
        dialog.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_reset_confirmed)
        dialog.present(self)

    def _on_reset_confirmed(self, _dialog, response: str) -> None:
        if response != "reset":
            return
        from cellar.backend import database
        database.clear_launch_overrides(self._entry.id)
        self.close()
        if self._on_saved:
            self._on_saved()

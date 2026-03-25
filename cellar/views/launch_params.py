"""App Configuration dialog — per-installation overrides for launch settings.

Used for Windows (Proton) and Linux (native) apps.  DOS apps use the
DOSBox or ScummVM settings dialogs instead, which embed the same
reusable launch-targets group.
"""

from __future__ import annotations

import logging
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from cellar.models.app_entry import AppEntry

log = logging.getLogger(__name__)

_AUDIO_LABELS = ["Auto", "PulseAudio", "ALSA", "OSS"]
_AUDIO_VALUES = ["auto", "pulseaudio", "alsa", "oss"]


class AppConfigDialog(Adw.Dialog):
    """Per-installation app configuration overrides.

    Shows catalogue defaults merged with any saved local overrides.  Changes
    are written to the ``launch_overrides`` DB table.  Resetting clears the
    row entirely, reverting all effective values to the catalogue defaults.
    """

    def __init__(
        self,
        entry: AppEntry,
        *,
        install_folder: str = "",
        user_data_callbacks: dict[str, Callable] | None = None,
        on_saved: Callable | None = None,
    ) -> None:
        super().__init__(
            title=f"App Configuration — {entry.name}",
            content_width=560,
            content_height=520,
        )
        self._entry = entry
        self._install_folder = install_folder
        self._user_data_cbs = user_data_callbacks or {}
        self._on_saved = on_saved
        self._targets_group = None

        self._build_ui()

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        from cellar.backend import database
        from cellar.views.launch_targets_group import LaunchTargetsGroup
        from cellar.views.widgets import make_dialog_header

        self._overrides = database.get_launch_overrides(self._entry.id)

        toolbar, header, _save_btn = make_dialog_header(
            self, action_label="Save", action_cb=self._on_save_clicked,
        )

        reset_btn = Gtk.Button(label="Reset")
        reset_btn.add_css_class("destructive-action")
        reset_btn.set_tooltip_text("Reset all overrides to catalogue defaults")
        reset_btn.connect("clicked", self._on_reset_clicked)
        header.pack_start(reset_btn)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        self.set_child(toolbar)

        # ── Launch Targets (reusable group) ─────────────────────────────
        self._targets_group = LaunchTargetsGroup(self._entry, self._overrides)
        page.add(self._targets_group.widget)

        # ── User Data (install location, backup/import) ─────────────────
        self._build_user_data_group(page)

        # ── Wine/Proton groups (Windows apps only) ──────────────────────
        is_proton = getattr(self._entry, "platform", "windows") == "windows"
        self._runner_row = None
        self._dxvk_row = None
        self._vkd3d_row = None
        self._audio_row = None
        self._debug_row = None
        self._direct_proton_row = None
        self._no_lsteamclient_row = None

        if is_proton:
            self._build_runner_group(page)
            self._build_compat_group(page)

    def _build_user_data_group(self, page: Adw.PreferencesPage) -> None:
        if not self._install_folder and not self._user_data_cbs:
            return
        from cellar.views.user_data_group import UserDataGroup

        group = UserDataGroup(
            self._install_folder,
            on_open_folder=self._user_data_cbs.get("open_folder"),
            on_change_location=self._user_data_cbs.get("change_location"),
            on_backup=self._user_data_cbs.get("backup"),
            on_import=self._user_data_cbs.get("import"),
        )
        page.add(group.widget)

    def _build_runner_group(self, page: Adw.PreferencesPage) -> None:
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

        override_runner = self._overrides.get("runner")
        if override_runner and override_runner in installed_runners:
            self._runner_row.set_selected(
                installed_runners.index(override_runner) + 1,
            )
        else:
            self._runner_row.set_selected(0)

        runner_group.add(self._runner_row)
        page.add(runner_group)

    def _build_compat_group(self, page: Adw.PreferencesPage) -> None:
        compat_group = Adw.PreferencesGroup(title="Compatibility")
        entry = self._entry
        overrides = self._overrides

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

        self._no_lsteamclient_row = Adw.SwitchRow(
            title="Disable Steam Client Shim",
            subtitle=f"Catalogue default: {'On' if entry.no_lsteamclient else 'Off'}",
        )
        self._no_lsteamclient_row.set_active(
            overrides["no_lsteamclient"] if "no_lsteamclient" in overrides
            else entry.no_lsteamclient
        )
        compat_group.add(self._no_lsteamclient_row)

        page.add(compat_group)

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
        entry = self._entry
        overrides: dict = {}

        # Launch targets via reusable group
        catalogue_targets = [dict(t) for t in entry.launch_targets]
        if self._targets_group and self._targets_group.has_changes(catalogue_targets):
            overrides["launch_targets"] = self._targets_group.get_targets()

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
        if (self._direct_proton_row is not None
                and self._direct_proton_row.get_active() != entry.direct_proton):
            overrides["direct_proton"] = self._direct_proton_row.get_active()
        if (self._no_lsteamclient_row is not None
                and self._no_lsteamclient_row.get_active() != entry.no_lsteamclient):
            overrides["no_lsteamclient"] = self._no_lsteamclient_row.get_active()

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
            heading="Reset App Configuration?",
            body="This will remove all local overrides and restore the catalogue defaults.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("reset", "Reset")
        dialog.set_response_appearance(
            "reset", Adw.ResponseAppearance.DESTRUCTIVE,
        )
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

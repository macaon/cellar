"""Reusable DOSBox Staging settings dialog.

Used by the Package Builder (maintainer) and the detail view (end user)
to configure DOSBox Staging options for DOS game packages.  Reads and
writes ``dosbox-overrides.conf`` in the game's ``config/`` directory.

Usage::

    DosboxSettingsDialog(
        config_dir=Path(".../<game>/config"),
        assets_dir=Path(".../<game>/assets"),
        on_saved=lambda: ...,
    ).present(parent)
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk

from cellar.backend.dosbox import (
    ASPECT_OPTIONS,
    CHORUS_OPTIONS,
    CPU_PRESETS,
    CROSSFEED_OPTIONS,
    MACHINE_OPTIONS,
    MEMSIZE_OPTIONS,
    MIDIDEVICE_OPTIONS,
    OPLMODE_OPTIONS,
    RENDERER_OPTIONS,
    REVERB_OPTIONS,
    SBTYPE_OPTIONS,
    SHADER_OPTIONS,
    read_override,
    write_overrides_batch,
)

log = logging.getLogger(__name__)


class DosboxSettingsDialog(Adw.Dialog):
    """DOSBox Staging settings dialog with grouped preferences."""

    def __init__(
        self,
        config_dir: Path,
        assets_dir: Path | None = None,
        *,
        on_saved: Callable | None = None,
        allow_assets: bool = True,
    ) -> None:
        super().__init__(title="DOSBox Settings", content_width=500, content_height=700)
        self._config_dir = config_dir
        self._assets_dir = assets_dir
        self._on_saved = on_saved
        self._allow_assets = allow_assets
        self._conf = config_dir / "dosbox-overrides.conf"
        self._chooser = None  # prevent GC of file choosers

        # Collect all changes to write on Save
        self._pending: dict[tuple[str, str], str] = {}

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        from cellar.views.widgets import make_dialog_header

        toolbar, _header, _save_btn = make_dialog_header(
            self, action_label="Save", action_cb=self._on_save_clicked,
        )

        # Content
        scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        page = Adw.PreferencesPage()

        self._build_profile_group(page)
        self._build_display_group(page)
        self._build_performance_group(page)
        self._build_sound_group(page)
        self._build_midi_group(page)
        self._build_mixer_group(page)
        self._build_files_group(page)

        scroll.set_child(page)
        toolbar.set_content(scroll)
        self.set_child(toolbar)

    # ── Game Profile ──────────────────────────────────────────────────

    def _build_profile_group(self, page: Adw.PreferencesPage) -> None:
        from cellar.backend.dosbox_profiles import (
            apply_profile,
            read_profile_name,
            remove_profile_conf,
        )

        game_dir = self._config_dir.parent
        profile_name = read_profile_name(game_dir)

        group = Adw.PreferencesGroup(title="Game Profile")
        self._profile_row = Adw.ActionRow(
            title=profile_name or "No profile detected",
            subtitle=(
                "Recommended settings applied automatically"
                if profile_name
                else "Install a known game to auto-detect its profile"
            ),
        )

        redetect_btn = Gtk.Button(
            icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER,
        )
        redetect_btn.add_css_class("flat")
        redetect_btn.set_tooltip_text("Re-detect game profile")

        def _on_redetect(_btn):
            slug = apply_profile(game_dir)
            name = read_profile_name(game_dir)
            self._profile_row.set_title(name or "No profile detected")
            self._profile_row.set_subtitle(
                "Recommended settings applied automatically"
                if name
                else "No matching profile found"
            )
            self._remove_profile_btn.set_visible(name is not None)

        redetect_btn.connect("clicked", _on_redetect)
        self._profile_row.add_suffix(redetect_btn)

        remove_btn = Gtk.Button(
            icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER,
        )
        remove_btn.add_css_class("flat")
        remove_btn.set_tooltip_text("Remove auto-detected profile")
        remove_btn.set_visible(profile_name is not None)
        self._remove_profile_btn = remove_btn

        def _on_remove(_btn):
            remove_profile_conf(game_dir)
            self._profile_row.set_title("No profile detected")
            self._profile_row.set_subtitle("Profile removed")
            remove_btn.set_visible(False)

        remove_btn.connect("clicked", _on_remove)
        self._profile_row.add_suffix(remove_btn)

        group.add(self._profile_row)
        page.add(group)

    # ── Display ────────────────────────────────────────────────────────

    def _build_display_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Display")

        # Fullscreen
        fs = Adw.SwitchRow(title="Fullscreen", subtitle="Start in fullscreen mode")
        fs.set_active(self._read("sdl", "fullscreen") == "true")
        fs.connect("notify::active", lambda r, _: self._set(
            "sdl", "fullscreen", "true" if r.get_active() else "false",
        ))
        group.add(fs)

        # Renderer
        group.add(self._make_combo(
            "Renderer", "Graphics output backend",
            RENDERER_OPTIONS, self._read("sdl", "output"), "sdl", "output",
        ))

        # Video card
        group.add(self._make_combo(
            "Video Card", "Emulated display adapter",
            MACHINE_OPTIONS, self._read("dosbox", "machine"), "dosbox", "machine",
        ))

        # Shader
        group.add(self._make_combo(
            "Shader", "GLSL shader for post-processing",
            SHADER_OPTIONS, self._read("render", "glshader"), "render", "glshader",
        ))

        # Aspect ratio
        group.add(self._make_combo(
            "Aspect Ratio", "Aspect ratio correction",
            ASPECT_OPTIONS, self._read("render", "aspect"), "render", "aspect",
        ))

        # Integer scaling
        int_scale = Adw.SwitchRow(
            title="Integer Scaling",
            subtitle="Constrain scaling to whole-number multiples",
        )
        current_int = self._read("render", "integer_scaling")
        int_scale.set_active(current_int not in ("", "off"))
        int_scale.connect("notify::active", lambda r, _: self._set(
            "render", "integer_scaling", "auto" if r.get_active() else "off",
        ))
        group.add(int_scale)

        page.add(group)

    # ── Performance ───────────────────────────────────────────────────

    def _build_performance_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Performance")

        # CPU speed
        # Read both old 'cycles' and new 'cpu_cycles' keys
        current_cpu = self._read("cpu", "cpu_cycles") or self._read("cpu", "cycles")
        group.add(self._make_combo(
            "CPU Speed", "Emulated processor speed",
            CPU_PRESETS, current_cpu, "cpu", "cpu_cycles",
        ))

        # Memory
        group.add(self._make_combo(
            "Memory", "RAM available to DOS programs",
            MEMSIZE_OPTIONS, self._read("dosbox", "memsize"), "dosbox", "memsize",
        ))

        page.add(group)

    # ── Sound ─────────────────────────────────────────────────────────

    def _build_sound_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Sound Card")

        # Sound Blaster type
        group.add(self._make_combo(
            "Sound Blaster", "Sound Blaster model",
            SBTYPE_OPTIONS, self._read("sblaster", "sbtype"), "sblaster", "sbtype",
        ))

        # OPL mode
        group.add(self._make_combo(
            "OPL Synth", "FM synthesis chip model",
            OPLMODE_OPTIONS, self._read("sblaster", "oplmode"), "sblaster", "oplmode",
        ))

        # GUS
        gus = Adw.SwitchRow(
            title="Gravis UltraSound",
            subtitle="Requires UltraSound patches in the game directory",
        )
        gus.set_active(self._read("gus", "gus") == "true")
        gus.connect("notify::active", lambda r, _: self._set(
            "gus", "gus", "true" if r.get_active() else "false",
        ))
        group.add(gus)

        page.add(group)

    # ── MIDI & Music ──────────────────────────────────────────────────

    def _build_midi_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="MIDI &amp; Music")

        # MIDI device
        midi_combo = self._make_combo(
            "MIDI Device", "MIDI output synthesizer",
            MIDIDEVICE_OPTIONS, self._read("midi", "mididevice"), "midi", "mididevice",
        )
        group.add(midi_combo)

        # SoundFont row — uses shared central storage, selectable per game.
        self._sf_row = Adw.ActionRow(title="SoundFont")
        current_sf_path = self._read("fluidsynth", "soundfont")
        current_sf_name = Path(current_sf_path).name if current_sf_path else ""
        self._sf_row.set_subtitle(current_sf_name or "No SoundFont selected")

        if self._allow_assets:
            # "Select" button — pick from shared library or browse for new file
            sf_btn = Gtk.Button(label="Select\u2026", valign=Gtk.Align.CENTER)
            sf_btn.connect("clicked", self._on_select_soundfont)
            self._sf_row.add_suffix(sf_btn)

            if current_sf_name:
                sf_rm = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER)
                sf_rm.add_css_class("flat")
                sf_rm.set_tooltip_text("Clear SoundFont selection")
                sf_rm.connect("clicked", self._on_clear_soundfont)
                self._sf_row.add_suffix(sf_rm)

        group.add(self._sf_row)

        # MT-32 ROMs row — uses shared central storage.
        self._rom_row = Adw.ActionRow(title="MT-32 ROMs")
        shared_rom_dir = self._shared_mt32_dir()
        shared_rom_count = (len(list(shared_rom_dir.glob("*.rom")))
                            if shared_rom_dir.is_dir() else 0)
        if shared_rom_count:
            self._rom_row.set_subtitle(f"Installed ({shared_rom_count} ROM files)")
        else:
            self._rom_row.set_subtitle("Not installed")

        if self._allow_assets:
            rom_btn = Gtk.Button(label="Import\u2026", valign=Gtk.Align.CENTER)
            rom_btn.connect("clicked", self._on_browse_roms)
            rom_btn.set_sensitive(shared_rom_count == 0)
            self._rom_row.add_suffix(rom_btn)

        group.add(self._rom_row)

        # Show/hide based on MIDI device
        self._update_midi_rows(self._read("midi", "mididevice"))

        page.add(group)

    # ── Mixer Effects ─────────────────────────────────────────────────

    def _build_mixer_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Mixer Effects")

        group.add(self._make_combo(
            "Crossfeed", "Stereo crossfeed for OPL/CMS",
            CROSSFEED_OPTIONS, self._read("mixer", "crossfeed"), "mixer", "crossfeed",
        ))

        group.add(self._make_combo(
            "Reverb", "Reverb effect",
            REVERB_OPTIONS, self._read("mixer", "reverb"), "mixer", "reverb",
        ))

        group.add(self._make_combo(
            "Chorus", "Chorus effect",
            CHORUS_OPTIONS, self._read("mixer", "chorus"), "mixer", "chorus",
        ))

        page.add(group)

    # ── Config Files ──────────────────────────────────────────────────

    def _build_files_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(
            title="Advanced",
            description="Edit config files directly for settings not exposed above.",
        )

        base_conf = self._config_dir / "dosbox-staging.conf"
        if base_conf.is_file():
            row = Adw.ActionRow(
                title="dosbox-staging.conf",
                subtitle="Base DOSBox Staging defaults",
            )
            btn = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER)
            btn.add_css_class("flat")
            btn.set_tooltip_text("Open in text editor")
            btn.connect("clicked", lambda _: Gio.AppInfo.launch_default_for_uri(
                base_conf.as_uri(), None,
            ))
            row.add_suffix(btn)
            group.add(row)

        if self._conf.is_file():
            row = Adw.ActionRow(
                title="dosbox-overrides.conf",
                subtitle="Game-specific overrides",
            )
            btn = Gtk.Button(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER)
            btn.add_css_class("flat")
            btn.set_tooltip_text("Open in text editor")
            btn.connect("clicked", self._on_edit_overrides_clicked)
            row.add_suffix(btn)
            group.add(row)

        folder_row = Adw.ActionRow(
            title="Open Config Folder",
            subtitle=str(self._config_dir),
        )
        folder_btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        folder_btn.add_css_class("flat")
        folder_btn.connect("clicked", lambda _: Gio.AppInfo.launch_default_for_uri(
            self._config_dir.as_uri(), None,
        ))
        folder_row.add_suffix(folder_btn)
        group.add(folder_row)

        page.add(group)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _read(self, section: str, key: str) -> str:
        """Read current value from overrides conf."""
        return read_override(self._conf, section, key)

    def _set(self, section: str, key: str, value: str) -> None:
        """Stage a change to be written on Save."""
        self._pending[(section, key)] = value

    def _make_combo(
        self,
        title: str,
        subtitle: str,
        options: tuple[tuple[str, str], ...],
        current_value: str,
        section: str,
        key: str,
    ) -> Adw.ComboRow:
        """Create a ComboRow backed by a tuple of (label, value) pairs."""
        labels = Gtk.StringList()
        selected_idx = 0
        default_idx = 0
        for i, (label, value) in enumerate(options):
            labels.append(label)
            if value == current_value:
                selected_idx = i
            if "(default)" in label.lower() or "(recommended)" in label.lower():
                default_idx = i

        # No override set — select the default-labeled option, not the first.
        if not current_value:
            selected_idx = default_idx

        row = Adw.ComboRow(title=title, subtitle=subtitle, model=labels)
        row.set_selected(selected_idx)

        def _on_changed(r, _pspec, opts=options, sec=section, k=key):
            idx = r.get_selected()
            if 0 <= idx < len(opts):
                self._set(sec, k, opts[idx][1])
                # Special handling: MIDI device visibility
                if sec == "midi" and k == "mididevice":
                    self._update_midi_rows(opts[idx][1])

        row.connect("notify::selected", _on_changed)
        return row

    def _update_midi_rows(self, device: str) -> None:
        """Show/hide SoundFont and MT-32 rows based on MIDI device."""
        if not hasattr(self, "_sf_row"):
            return
        # Empty string = no override = DOSBox defaults to "auto"
        effective = device if device else "auto"
        show_sf = effective in ("auto", "fluidsynth")
        show_rom = effective in ("auto", "mt32")
        self._sf_row.set_visible(show_sf)
        self._rom_row.set_visible(show_rom)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _flush_pending(self) -> None:
        """Write any staged changes to disk immediately."""
        if self._pending:
            write_overrides_batch(self._conf, self._pending)
            self._pending.clear()

    def _on_save_clicked(self, _btn) -> None:
        self._flush_pending()
        self.close()
        if self._on_saved:
            self._on_saved()

    def _on_edit_overrides_clicked(self, _btn) -> None:
        """Flush pending changes, then open overrides conf in text editor.

        This ensures the file reflects the current dialog state before the
        user edits it manually.  After this point the dialog's Save button
        becomes a no-op (nothing pending), and any further GUI changes will
        layer on top of the manual edits.
        """
        self._flush_pending()
        Gio.AppInfo.launch_default_for_uri(self._conf.as_uri(), None)

    # ------------------------------------------------------------------
    # Asset management
    # ------------------------------------------------------------------

    @staticmethod
    def _shared_soundfonts_dir() -> Path:
        """Return the shared central SoundFont directory."""
        from cellar.backend.dosbox import dosbox_staging_dir
        d = dosbox_staging_dir() / "soundfonts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _shared_mt32_dir() -> Path:
        """Return the shared central MT-32 ROM directory."""
        from cellar.backend.dosbox import dosbox_staging_dir
        d = dosbox_staging_dir() / "mt32-roms"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _list_shared_soundfonts(self) -> list[Path]:
        """Return all SoundFont files in the shared library."""
        sf_dir = self._shared_soundfonts_dir()
        return sorted(
            p for p in sf_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".sf2", ".sf3"}
        )

    def _on_select_soundfont(self, _btn) -> None:
        """Show a dialog to pick from shared library or import a new SoundFont."""
        available = self._list_shared_soundfonts()

        if not available:
            # No shared soundfonts yet — go straight to file browser.
            self._on_browse_new_soundfont()
            return

        # Build a selection dialog with existing soundfonts + "Import new" option.
        dlg = Adw.AlertDialog(
            heading="Select SoundFont",
            body="Choose from your SoundFont library or import a new one.",
        )
        for sf in available:
            dlg.add_response(sf.name, sf.name)
        dlg.add_response("_import", "Import new\u2026")
        dlg.add_response("_cancel", "Cancel")
        dlg.set_close_response("_cancel")

        def _on_response(_dlg, response):
            if response == "_cancel":
                return
            if response == "_import":
                self._on_browse_new_soundfont()
                return
            # User picked an existing soundfont
            self._apply_soundfont(response)

        dlg.connect("response", _on_response)
        dlg.present(self.get_root())

    def _on_browse_new_soundfont(self) -> None:
        chooser = Gtk.FileChooserNative(
            title="Import SoundFont",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Import",
        )
        f = Gtk.FileFilter()
        f.set_name("SoundFonts (*.sf2, *.sf3)")
        f.add_pattern("*.sf2")
        f.add_pattern("*.SF2")
        f.add_pattern("*.sf3")
        f.add_pattern("*.SF3")
        chooser.add_filter(f)
        chooser.connect("response", self._on_new_sf_chosen, chooser)
        chooser.show()
        self._chooser = chooser

    def _on_new_sf_chosen(self, _c, response, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        src = Path(chooser.get_file().get_path())
        # Copy to shared central library
        shared_dir = self._shared_soundfonts_dir()
        dest = shared_dir / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
        self._apply_soundfont(src.name)

    def _apply_soundfont(self, sf_name: str) -> None:
        """Set the given SoundFont for this game — points at shared central dir."""
        shared_dir = self._shared_soundfonts_dir()
        self._set("midi", "mididevice", "fluidsynth")
        self._set("fluidsynth", "soundfont", str(shared_dir / sf_name))
        self._sf_row.set_subtitle(sf_name)

    def _on_clear_soundfont(self, _btn) -> None:
        self._set("fluidsynth", "soundfont", "")
        self._sf_row.set_subtitle("No SoundFont selected")

    def _on_browse_roms(self, _btn) -> None:
        chooser = Gtk.FileChooserNative(
            title="Select MT-32 ROM Files",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Add",
        )
        chooser.set_select_multiple(True)
        f = Gtk.FileFilter()
        f.set_name("ROM files (*.rom)")
        f.add_pattern("*.rom")
        f.add_pattern("*.ROM")
        chooser.add_filter(f)
        chooser.connect("response", self._on_roms_chosen, chooser)
        chooser.show()
        self._chooser = chooser

    def _on_roms_chosen(self, _c, response, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        # Copy ROMs to shared central library
        shared_dir = self._shared_mt32_dir()
        shared_dir.mkdir(parents=True, exist_ok=True)
        files = chooser.get_files()
        for i in range(files.get_n_items()):
            src = Path(files.get_item(i).get_path())
            dest = shared_dir / src.name
            if not dest.exists():
                shutil.copy2(src, dest)
        # Point config at shared central dir
        self._set("midi", "mididevice", "mt32")
        self._set("mt32", "romdir", str(shared_dir))
        # Update UI
        count = len(list(shared_dir.glob("*.rom")))
        self._rom_row.set_subtitle(f"Installed ({count} ROM files)")

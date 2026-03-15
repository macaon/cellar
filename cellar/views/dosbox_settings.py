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
from gi.repository import Adw, Gio, GLib, Gtk

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
        toolbar = Adw.ToolbarView()

        # Header
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.connect("clicked", self._on_save_clicked)
        header.pack_end(save_btn)

        toolbar.add_top_bar(header)

        # Content
        scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        page = Adw.PreferencesPage()

        self._build_display_group(page)
        self._build_performance_group(page)
        self._build_sound_group(page)
        self._build_midi_group(page)
        self._build_mixer_group(page)
        self._build_files_group(page)

        scroll.set_child(page)
        toolbar.set_content(scroll)
        self.set_child(toolbar)

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
        group = Adw.PreferencesGroup(title="MIDI & Music")

        # MIDI device
        midi_combo = self._make_combo(
            "MIDI Device", "MIDI output synthesizer",
            MIDIDEVICE_OPTIONS, self._read("midi", "mididevice"), "midi", "mididevice",
        )
        group.add(midi_combo)

        # SoundFont row
        self._sf_row = Adw.ActionRow(title="SoundFont")
        sf_dir = self._assets_dir / "soundfonts" if self._assets_dir else None
        current_sf = ""
        if sf_dir and sf_dir.is_dir():
            sfs = sorted(sf_dir.glob("*.sf[23]"))
            if sfs:
                current_sf = sfs[0].name
        self._sf_row.set_subtitle(current_sf or "No SoundFont installed")

        if self._allow_assets:
            sf_btn = Gtk.Button(label="Browse\u2026", valign=Gtk.Align.CENTER)
            sf_btn.connect("clicked", self._on_browse_soundfont)
            self._sf_row.add_suffix(sf_btn)

            if current_sf:
                sf_rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
                sf_rm.add_css_class("flat")
                sf_rm.connect("clicked", self._on_remove_soundfont)
                self._sf_row.add_suffix(sf_rm)

        group.add(self._sf_row)

        # MT-32 ROMs row
        self._rom_row = Adw.ActionRow(title="MT-32 ROMs")
        rom_dir = self._assets_dir / "mt32-roms" if self._assets_dir else None
        rom_count = 0
        if rom_dir and rom_dir.is_dir():
            rom_count = len(list(rom_dir.glob("*.rom")))
        self._rom_row.set_subtitle(
            f"{rom_count} ROM files installed" if rom_count
            else "No ROMs installed"
        )

        if self._allow_assets:
            rom_btn = Gtk.Button(label="Browse\u2026", valign=Gtk.Align.CENTER)
            rom_btn.connect("clicked", self._on_browse_roms)
            self._rom_row.add_suffix(rom_btn)

            if rom_count:
                rom_rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER)
                rom_rm.add_css_class("flat")
                rom_rm.connect("clicked", self._on_remove_roms)
                self._rom_row.add_suffix(rom_rm)

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
        for i, (label, value) in enumerate(options):
            labels.append(label)
            if value == current_value:
                selected_idx = i

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

    def _on_browse_soundfont(self, _btn) -> None:
        chooser = Gtk.FileChooserNative(
            title="Select SoundFont",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Add",
        )
        f = Gtk.FileFilter()
        f.set_name("SoundFonts (*.sf2, *.sf3)")
        f.add_pattern("*.sf2")
        f.add_pattern("*.SF2")
        f.add_pattern("*.sf3")
        f.add_pattern("*.SF3")
        chooser.add_filter(f)
        chooser.connect("response", self._on_sf_chosen, chooser)
        chooser.show()
        self._chooser = chooser

    def _on_sf_chosen(self, _c, response, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT or self._assets_dir is None:
            return
        src = Path(chooser.get_file().get_path())
        dest_dir = self._assets_dir / "soundfonts"
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest_dir / src.name)
        # Update config
        self._set("midi", "mididevice", "fluidsynth")
        self._set("fluidsynth", "soundfont", f"assets/soundfonts/{src.name}")
        # Update UI
        self._sf_row.set_subtitle(src.name)

    def _on_remove_soundfont(self, _btn) -> None:
        if self._assets_dir is None:
            return
        sf_dir = self._assets_dir / "soundfonts"
        if sf_dir.is_dir():
            shutil.rmtree(sf_dir)
        self._sf_row.set_subtitle("No SoundFont installed")

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
        if response != Gtk.ResponseType.ACCEPT or self._assets_dir is None:
            return
        dest_dir = self._assets_dir / "mt32-roms"
        dest_dir.mkdir(parents=True, exist_ok=True)
        for gfile in chooser.get_files():
            src = Path(gfile.get_path())
            shutil.copy2(src, dest_dir / src.name)
        # Update config
        self._set("midi", "mididevice", "mt32")
        self._set("mt32", "romdir", "assets/mt32-roms")
        # Update UI
        count = len(list(dest_dir.glob("*.rom")))
        self._rom_row.set_subtitle(f"{count} ROM files installed")

    def _on_remove_roms(self, _btn) -> None:
        if self._assets_dir is None:
            return
        rom_dir = self._assets_dir / "mt32-roms"
        if rom_dir.is_dir():
            shutil.rmtree(rom_dir)
        self._rom_row.set_subtitle("No ROMs installed")

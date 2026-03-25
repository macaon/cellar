"""ScummVM settings dialog.

Per-game configuration for ScummVM titles.  Reads and writes the
``config/scummvm.ini`` file in the game's install directory.

Usage::

    ScummvmSettingsDialog(
        config_dir=Path(".../<game>/config"),
        entry=app_entry,
        on_saved=lambda: ...,
    ).present(parent)
"""

from __future__ import annotations

import configparser
import logging
import shutil
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk

from cellar.models.app_entry import AppEntry

log = logging.getLogger(__name__)

# ScummVM render modes
_RENDER_MODES = (
    ("Default", ""),
    ("EGA", "ega"),
    ("VGA", "vga"),
    ("Hercules", "hercules"),
    ("CGA", "cga"),
    ("Amiga", "amiga"),
    ("Atari ST", "atari"),
    ("Macintosh", "macintosh"),
)

# ScummVM music drivers
_MUSIC_DRIVERS = (
    ("Auto-detect (default)", "auto"),
    ("AdLib / OPL", "adlib"),
    ("FluidSynth (SoundFont)", "fluidsynth"),
    ("MT-32 emulation", "mt32"),
    ("PC Speaker", "pcspk"),
    ("MIDI", "midi"),
    ("No music", "null"),
)

# Graphics scalers (written to "scaler" key, not "gfx_mode")
_SCALERS = (
    ("Default", ""),
    ("Normal", "normal"),
    ("HQ", "hq"),
    ("Edge", "edge"),
    ("AdvMAME", "advmame"),
    ("SAI", "sai"),
    ("SuperSAI", "supersai"),
    ("SuperEagle", "supereagle"),
    ("PM", "pm"),
    ("DotMatrix", "dotmatrix"),
    ("TV", "tv"),
)

# Scale factors
_SCALE_FACTORS = (
    ("Auto", ""),
    ("1x", "1"),
    ("2x", "2"),
    ("3x", "3"),
    ("4x", "4"),
)

# Stretch modes
_STRETCH_MODES = (
    ("Fit to window (default)", "fit"),
    ("Pixel-perfect stretch", "pixel-perfect"),
    ("Stretch to fill", "stretch"),
    ("Center (no scaling)", "center"),
    ("Fit, force aspect ratio", "fit_force_aspect"),
)

# Languages
_LANGUAGES = (
    ("Default", ""),
    ("English", "en"),
    ("German", "de"),
    ("French", "fr"),
    ("Italian", "it"),
    ("Portuguese", "pt"),
    ("Spanish", "es"),
    ("Japanese", "ja"),
    ("Chinese", "zh"),
    ("Korean", "ko"),
    ("Swedish", "sv"),
    ("Hebrew", "he"),
    ("Russian", "ru"),
    ("Czech", "cz"),
    ("Dutch", "nl"),
    ("Norwegian", "nb"),
    ("Polish", "pl"),
)


class ScummvmSettingsDialog(Adw.Dialog):
    """ScummVM settings dialog with grouped preferences."""

    def __init__(
        self,
        config_dir: Path,
        *,
        entry: AppEntry | None = None,
        install_folder: str = "",
        user_data_callbacks: dict[str, Callable] | None = None,
        on_saved: Callable | None = None,
    ) -> None:
        super().__init__(
            title="ScummVM Settings",
            content_width=500,
            content_height=600,
        )
        self._config_dir = config_dir
        self._entry = entry
        self._install_folder = install_folder
        self._user_data_cbs = user_data_callbacks or {}
        self._on_saved = on_saved
        self._conf_path = config_dir / "scummvm.ini"
        self._targets_group = None

        # Load current config
        self._config = configparser.ConfigParser()
        self._config.optionxform = str
        if self._conf_path.is_file():
            self._config.read(str(self._conf_path), encoding="utf-8")

        # Find the game section (first section that isn't scummvm/DEFAULT)
        self._game_section = ""
        for section in self._config.sections():
            if section.lower() not in ("scummvm", "default"):
                self._game_section = section
                break

        # Ensure gameid is set (required for ScummVM to recognize the target)
        if self._game_section and not self._config.has_option(self._game_section, "gameid"):
            self._config.set(self._game_section, "gameid", self._game_section)
            self._flush()

        # Migrate legacy gfx_mode key → scaler + scale_factor
        self._migrate_gfx_mode()

        self._build_ui()

    # ------------------------------------------------------------------
    # Config read/write helpers
    # ------------------------------------------------------------------

    def _read(self, key: str) -> str:
        """Read a key from the game section, falling back to scummvm section."""
        if self._game_section and self._config.has_option(self._game_section, key):
            return self._config.get(self._game_section, key)
        if self._config.has_section("scummvm") and self._config.has_option("scummvm", key):
            return self._config.get("scummvm", key)
        return ""

    def _set(self, key: str, value: str) -> None:
        """Set a key in the game section and flush to disk immediately."""
        section = self._game_section or "scummvm"
        if not self._config.has_section(section):
            self._config.add_section(section)
        if value:
            self._config.set(section, key, value)
        elif self._config.has_option(section, key):
            self._config.remove_option(section, key)
        self._flush()

    def _migrate_gfx_mode(self) -> None:
        """Migrate legacy ``gfx_mode`` values to ``scaler`` + ``scale_factor``.

        Earlier versions wrote combined values like ``tv2x`` to ``gfx_mode``.
        ScummVM expects ``scaler=tv`` and ``scale_factor=2`` as separate keys.
        """
        old = self._read("gfx_mode")
        if not old or self._read("scaler"):
            return  # nothing to migrate, or already migrated

        import re
        m = re.match(r"^([a-z]+?)(\d)x?$", old, re.IGNORECASE)
        if m:
            self._set("scaler", m.group(1))
            self._set("scale_factor", m.group(2))
        else:
            # Not a combined value — might be a plain scaler name
            self._set("scaler", old)

        # Remove the stale key
        section = self._game_section or "scummvm"
        if self._config.has_option(section, "gfx_mode"):
            self._config.remove_option(section, "gfx_mode")
            self._flush()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        from cellar.views.widgets import make_dialog_header

        toolbar, _header, _save_btn = make_dialog_header(
            self, action_label="Save", action_cb=self._on_save_clicked,
        )

        scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        page = Adw.PreferencesPage()

        self._build_launch_targets_group(page)
        self._build_user_data_group(page)
        self._build_game_group(page)
        self._build_graphics_group(page)
        self._build_audio_group(page)
        self._build_midi_group(page)
        self._build_advanced_group(page)

        scroll.set_child(page)
        toolbar.set_content(scroll)
        self.set_child(toolbar)

    # ── Launch Targets ─────────────────────────────────────────────────

    def _build_launch_targets_group(self, page: Adw.PreferencesPage) -> None:
        if self._entry is None:
            return
        from cellar.backend import database
        from cellar.views.launch_targets_group import LaunchTargetsGroup

        overrides = database.get_launch_overrides(self._entry.id)
        self._targets_group = LaunchTargetsGroup(self._entry, overrides)
        page.add(self._targets_group.widget)

    # ── Game ──────────────────────────────────────────────────────────

    def _build_game_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Game")

        # Language
        group.add(self._make_combo(
            "Language", "Override game language",
            _LANGUAGES, self._read("language"),
            "language",
        ))

        # Talk speed (0–255)
        talk_speed = self._make_scale(
            "Talk Speed", 0, 255,
            int(self._read("talkspeed") or "60"),
            "talkspeed",
        )
        group.add(talk_speed)

        # Copy protection
        copy_prot = Adw.SwitchRow(
            title="Copy Protection",
            subtitle="Enable original copy protection prompts",
        )
        copy_prot.set_active(self._read("copy_protection") == "true")
        copy_prot.connect("notify::active", lambda r, _: self._set(
            "copy_protection", "true" if r.get_active() else "false",
        ))
        group.add(copy_prot)

        page.add(group)

    # ── Graphics ───────────────────────────────────────────────────────

    def _build_graphics_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Graphics")

        # Fullscreen
        fs = Adw.SwitchRow(
            title="Fullscreen",
            subtitle="Start in fullscreen mode",
        )
        fs.set_active(self._read("fullscreen") == "true")
        fs.connect("notify::active", lambda r, _: self._set(
            "fullscreen", "true" if r.get_active() else "false",
        ))
        group.add(fs)

        # Aspect ratio correction
        aspect = Adw.SwitchRow(
            title="Aspect Ratio Correction",
            subtitle="Correct 320x200 to 4:3 display ratio",
        )
        aspect.set_active(self._read("aspect_ratio") != "false")
        aspect.connect("notify::active", lambda r, _: self._set(
            "aspect_ratio", "true" if r.get_active() else "false",
        ))
        group.add(aspect)

        # Filtering
        filtering = Adw.SwitchRow(
            title="Bilinear Filtering",
            subtitle="Smooth pixel edges (disable for sharp pixels)",
        )
        filtering.set_active(self._read("filtering") == "true")
        filtering.connect("notify::active", lambda r, _: self._set(
            "filtering", "true" if r.get_active() else "false",
        ))
        group.add(filtering)

        # Scaler
        group.add(self._make_combo(
            "Scaler", "Pixel-art scaling filter",
            _SCALERS, self._read("scaler"),
            "scaler",
        ))

        # Scale factor
        group.add(self._make_combo(
            "Scale Factor", "Scaling multiplier",
            _SCALE_FACTORS, self._read("scale_factor"),
            "scale_factor",
        ))

        # Stretch mode
        group.add(self._make_combo(
            "Stretch Mode", "How the game fits the window",
            _STRETCH_MODES, self._read("stretch_mode"),
            "stretch_mode",
        ))

        # Render mode
        group.add(self._make_combo(
            "Render Mode", "Graphics rendering style",
            _RENDER_MODES, self._read("render_mode"),
            "render_mode",
        ))

        page.add(group)

    # ── Audio ──────────────────────────────────────────────────────────

    def _build_audio_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(title="Audio")

        # Music driver
        music_combo = self._make_combo(
            "Music Device", "Music/MIDI output",
            _MUSIC_DRIVERS, self._read("music_driver"),
            "music_driver",
        )
        group.add(music_combo)

        # Music volume (0–255)
        music_vol = self._make_scale(
            "Music Volume", 0, 255,
            int(self._read("music_volume") or "192"),
            "music_volume",
        )
        group.add(music_vol)

        # SFX volume (0–255)
        sfx_vol = self._make_scale(
            "Sound Effects Volume", 0, 255,
            int(self._read("sfx_volume") or "192"),
            "sfx_volume",
        )
        group.add(sfx_vol)

        # Speech volume (0–255)
        speech_vol = self._make_scale(
            "Speech Volume", 0, 255,
            int(self._read("speech_volume") or "192"),
            "speech_volume",
        )
        group.add(speech_vol)

        # Subtitles
        subtitles = Adw.SwitchRow(
            title="Subtitles",
            subtitle="Show subtitles for speech",
        )
        subtitles.set_active(self._read("subtitles") == "true")
        subtitles.connect("notify::active", lambda r, _: self._set(
            "subtitles", "true" if r.get_active() else "false",
        ))
        group.add(subtitles)

        # Speech mute
        speech = Adw.SwitchRow(
            title="Speech",
            subtitle="Enable voice acting (when available)",
        )
        speech.set_active(self._read("speech_mute") != "true")
        speech.connect("notify::active", lambda r, _: self._set(
            "speech_mute", "false" if r.get_active() else "true",
        ))
        group.add(speech)

        page.add(group)

    # ── MIDI Assets ───────────────────────────────────────────────────

    def _build_midi_group(self, page: Adw.PreferencesPage) -> None:
        from cellar.backend.config import ensure_mt32_symlinks
        ensure_mt32_symlinks(self._shared_mt32_dir())

        group = Adw.PreferencesGroup(title="MIDI")

        # SoundFont row
        self._sf_row = Adw.ActionRow(title="SoundFont")
        current_sf = self._read("soundfont")
        current_sf_name = Path(current_sf).name if current_sf else ""
        self._sf_row.set_subtitle(current_sf_name or "No SoundFont selected")

        sf_btn = Gtk.Button(label="Select\u2026", valign=Gtk.Align.CENTER)
        sf_btn.connect("clicked", self._on_select_soundfont)
        self._sf_row.add_suffix(sf_btn)

        if current_sf_name:
            sf_rm = Gtk.Button(
                icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER,
            )
            sf_rm.add_css_class("flat")
            sf_rm.set_tooltip_text("Clear SoundFont selection")
            sf_rm.connect("clicked", self._on_clear_soundfont)
            self._sf_row.add_suffix(sf_rm)

        group.add(self._sf_row)

        # MT-32 ROMs row
        self._rom_row = Adw.ActionRow(title="MT-32 ROMs")
        shared_rom_dir = self._shared_mt32_dir()
        rom_count = (
            len(list(shared_rom_dir.glob("*.rom")))
            if shared_rom_dir.is_dir() else 0
        )
        self._rom_row.set_subtitle(
            f"Installed ({rom_count} ROM files)" if rom_count
            else "Not installed"
        )

        rom_btn = Gtk.Button(label="Import\u2026", valign=Gtk.Align.CENTER)
        rom_btn.connect("clicked", self._on_browse_roms)
        rom_btn.set_sensitive(rom_count == 0)
        self._rom_row.add_suffix(rom_btn)
        group.add(self._rom_row)

        # Show/hide based on music driver
        self._update_midi_rows(self._read("music_driver"))

        page.add(group)

    def _update_midi_rows(self, driver: str) -> None:
        """Show/hide SoundFont and MT-32 rows based on music driver."""
        if not hasattr(self, "_sf_row"):
            return
        effective = driver if driver else "auto"
        self._sf_row.set_visible(effective in ("auto", "fluidsynth"))
        self._rom_row.set_visible(effective in ("auto", "mt32"))

    # ── Advanced ───────────────────────────────────────────────────────

    def _build_advanced_group(self, page: Adw.PreferencesPage) -> None:
        group = Adw.PreferencesGroup(
            title="Advanced",
            description="Edit the config file directly for settings not exposed above.",
        )

        if self._conf_path.is_file():
            row = Adw.ActionRow(
                title="scummvm.ini",
                subtitle="Per-game ScummVM configuration",
            )
            btn = Gtk.Button(
                icon_name="document-edit-symbolic",
                valign=Gtk.Align.CENTER,
            )
            btn.add_css_class("flat")
            btn.set_tooltip_text("Open in text editor")
            btn.connect("clicked", lambda _: Gio.AppInfo.launch_default_for_uri(
                self._conf_path.as_uri(), None,
            ))
            row.add_suffix(btn)
            group.add(row)

        folder_row = Adw.ActionRow(
            title="Open Config Folder",
            subtitle=str(self._config_dir),
        )
        folder_btn = Gtk.Button(
            icon_name="folder-open-symbolic",
            valign=Gtk.Align.CENTER,
        )
        folder_btn.add_css_class("flat")
        folder_btn.connect("clicked", lambda _: Gio.AppInfo.launch_default_for_uri(
            self._config_dir.as_uri(), None,
        ))
        folder_row.add_suffix(folder_btn)
        group.add(folder_row)

        page.add(group)

    # ── User Data ──────────────────────────────────────────────────

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

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_combo(
        self,
        title: str,
        subtitle: str,
        options: tuple[tuple[str, str], ...],
        current_value: str,
        key: str,
    ) -> Adw.ComboRow:
        labels = Gtk.StringList()
        selected_idx = 0
        for i, (label, value) in enumerate(options):
            labels.append(label)
            if value == current_value:
                selected_idx = i

        row = Adw.ComboRow(title=title, subtitle=subtitle, model=labels)
        row.set_selected(selected_idx)

        def _on_changed(r, _pspec, opts=options, k=key):
            idx = r.get_selected()
            if 0 <= idx < len(opts):
                self._set(k, opts[idx][1])
                if k == "music_driver":
                    self._update_midi_rows(opts[idx][1])

        row.connect("notify::selected", _on_changed)
        return row

    def _make_scale(
        self,
        title: str,
        min_val: int,
        max_val: int,
        current: int,
        key: str,
    ) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title)
        scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, min_val, max_val, 1,
        )
        scale.set_value(current)
        scale.set_draw_value(True)
        scale.set_hexpand(False)
        scale.set_size_request(200, -1)
        scale.set_valign(Gtk.Align.CENTER)

        def _on_changed(s, k=key):
            self._set(k, str(int(s.get_value())))

        scale.connect("value-changed", _on_changed)
        row.add_suffix(scale)
        return row

    # ------------------------------------------------------------------
    # SoundFont / MT-32 helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _shared_soundfonts_dir() -> Path:
        from cellar.backend.config import soundfonts_dir
        return soundfonts_dir()

    @staticmethod
    def _shared_mt32_dir() -> Path:
        from cellar.backend.config import mt32_roms_dir
        return mt32_roms_dir()

    def _list_shared_soundfonts(self) -> list[Path]:
        sf_dir = self._shared_soundfonts_dir()
        return sorted(
            p for p in sf_dir.iterdir()
            if p.is_file() and p.suffix.lower() in {".sf2", ".sf3"}
        ) if sf_dir.is_dir() else []

    def _on_select_soundfont(self, _btn) -> None:
        available = self._list_shared_soundfonts()
        if not available:
            self._on_browse_new_soundfont()
            return

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
        shared_dir = self._shared_soundfonts_dir()
        dest = shared_dir / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
        self._apply_soundfont(src.name)

    def _apply_soundfont(self, sf_name: str) -> None:
        shared_dir = self._shared_soundfonts_dir()
        self._set("music_driver", "fluidsynth")
        self._set("soundfont", str(shared_dir / sf_name))
        self._sf_row.set_subtitle(sf_name)

    def _on_clear_soundfont(self, _btn) -> None:
        self._set("soundfont", "")
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
        from cellar.backend.config import ensure_mt32_symlinks

        shared_dir = self._shared_mt32_dir()
        shared_dir.mkdir(parents=True, exist_ok=True)
        files = chooser.get_files()
        for i in range(files.get_n_items()):
            src = Path(files.get_item(i).get_path())
            dest = shared_dir / src.name
            if not dest.exists():
                shutil.copy2(src, dest)
        ensure_mt32_symlinks(shared_dir)
        self._set("music_driver", "mt32")
        self._set("native_mt32", "true")
        self._set("extrapath", str(shared_dir))
        count = len(list(shared_dir.glob("*.rom"))) - len(list(shared_dir.glob("*.ROM")))
        self._rom_row.set_subtitle(f"Installed ({count} ROM files)")

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _flush(self) -> None:
        """Write the current config to disk."""
        self._conf_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._conf_path, "w", encoding="utf-8") as f:
            self._config.write(f)

    def _on_save_clicked(self, _btn) -> None:
        # Config is already on disk (flushed on each change).
        # Save launch target overrides (DB-backed, not INI).
        self._save_launch_targets()

        self.close()
        if self._on_saved:
            self._on_saved()

    def _save_launch_targets(self) -> None:
        if self._entry is None or self._targets_group is None:
            return
        from cellar.backend import database

        catalogue_targets = [dict(t) for t in self._entry.launch_targets]
        if self._targets_group.has_changes(catalogue_targets):
            overrides = database.get_launch_overrides(self._entry.id)
            overrides["launch_targets"] = self._targets_group.get_targets()
            database.set_launch_overrides(self._entry.id, overrides)
        else:
            overrides = database.get_launch_overrides(self._entry.id)
            overrides.pop("launch_targets", None)
            if overrides:
                database.set_launch_overrides(self._entry.id, overrides)
            else:
                database.clear_launch_overrides(self._entry.id)

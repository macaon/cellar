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

# Graphics scaling modes
_GFX_MODES = (
    ("Default", ""),
    ("Normal (no scaling)", "normal"),
    ("HQ 2x", "hq2x"),
    ("HQ 3x", "hq3x"),
    ("2x SAI", "2xsai"),
    ("Super 2x SAI", "super2xsai"),
    ("SuperEagle", "supereagle"),
    ("AdvMAME 2x", "advmame2x"),
    ("AdvMAME 3x", "advmame3x"),
    ("TV 2x", "tv2x"),
    ("DotMatrix", "dotmatrix"),
)

# Stretch modes
_STRETCH_MODES = (
    ("Fit to window (default)", "fit"),
    ("Pixel-perfect stretch", "pixel-perfect"),
    ("Stretch to fill", "stretch"),
    ("Center (no scaling)", "center"),
    ("Fit, force aspect ratio", "fit_force_aspect"),
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
        """Set a key in the game section."""
        section = self._game_section or "scummvm"
        if not self._config.has_section(section):
            self._config.add_section(section)
        if value:
            self._config.set(section, key, value)
        elif self._config.has_option(section, key):
            self._config.remove_option(section, key)

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
        self._build_graphics_group(page)
        self._build_audio_group(page)
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

        # Graphics scaler
        group.add(self._make_combo(
            "Graphics Mode", "Scaling algorithm",
            _GFX_MODES, self._read("gfx_mode"),
            "gfx_mode",
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
        group.add(self._make_combo(
            "Music Device", "Music/MIDI output",
            _MUSIC_DRIVERS, self._read("music_driver"),
            "music_driver",
        ))

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

        row.connect("notify::selected", _on_changed)
        return row

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def _on_save_clicked(self, _btn) -> None:
        # Write ScummVM config
        self._conf_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._conf_path, "w", encoding="utf-8") as f:
            self._config.write(f)
        log.info("Saved ScummVM config: %s", self._conf_path)

        # Save launch target overrides
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

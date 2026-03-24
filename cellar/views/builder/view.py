"""Package Builder view — create and publish WINEPREFIX-based app packages.

Shown in the main window when at least one writable repo is configured.
Maintainers use this view to:

1. Create a project (App or Base) that owns a WINEPREFIX.
2. Select a GE-Proton runner and initialize the prefix.
3. Install dependencies (winetricks verbs) and run installers.
4. Set an entry point (App projects only).
5. Test-launch the app to verify it works.
6. Publish — stream-compress the prefix directly to the repo and update
   catalogue.json (App and Base alike).  No intermediate local archive.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import Callable

import gi

from cellar.utils import natural_sort_key
from cellar.utils.async_work import run_in_background

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from cellar.backend.project import (
    Project,
    create_project,
    delete_project,
    load_projects,
    save_project,
)
from cellar.views.builder.dependencies import DependencyPickerDialog
from cellar.views.builder.pickers import (
    AddLaunchTargetDialog,
    BasePickerDialog,
    RunnerPickerDialog,
    pick_repo,
)
from cellar.views.builder.progress import ProgressDialog
from cellar.views.metadata_editor import MetadataEditorDialog, ProjectContext, RepoContext
from cellar.views.widgets import (
    BaseCard,
    make_app_grid,
    make_card_icon_from_name,
    make_gear_button,
    set_margins,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ScummVM compatibility prompt (shared by all DOS install paths)
# ---------------------------------------------------------------------------

def _check_scummvm_compat(
    content_dir: Path,
    project,
    presenter,
    *,
    on_refreshed: Callable | None = None,
) -> None:
    """Detect ScummVM compatibility and prompt the user to switch.

    Called after any DOS install/conversion completes (disc installer,
    GOG conversion, smart import).  If the user accepts, converts the
    DOSBox prefix to ScummVM in the background.
    """
    from cellar.backend.scummvm_profiles import detect_scummvm_profile, get_profile
    from cellar.backend.project import save_project
    from cellar.utils.async_work import run_in_background

    slug = detect_scummvm_profile(content_dir)
    if not slug:
        return

    profile = get_profile(slug)
    if profile is None:
        return

    compat = profile.get("compatibility", "Unknown")
    log.info("ScummVM-compatible game detected: %r (%s)", slug, compat)

    dlg = Adw.AlertDialog(
        heading=f"ScummVM Compatible ({compat})",
        body=(
            "This game is compatible with ScummVM, which provides better "
            "graphics scaling, input handling, and save management.\n\n"
            "Reverting to DOSBox requires a full reinstallation."
        ),
    )
    dlg.add_response("keep", "Keep DOSBox")
    dlg.add_response("switch", "Switch to ScummVM")
    dlg.set_response_appearance("switch", Adw.ResponseAppearance.SUGGESTED)
    dlg.set_default_response("switch")
    dlg.set_close_response("keep")

    def _do_convert():
        def _convert():
            from cellar.backend.scummvm_convert import convert_to_scummvm
            convert_to_scummvm(content_dir, profile)

        def _done(_r):
            project.engine = "scummvm"
            save_project(project)
            if on_refreshed:
                on_refreshed()
            elif hasattr(presenter, "_show_project"):
                presenter._show_project(project)

        def _error(msg):
            log.error("ScummVM conversion failed: %s", msg)

        run_in_background(_convert, on_done=_done, on_error=_error)

    def _check_and_convert():
        from cellar.backend.scummvm import is_scummvm_available
        if is_scummvm_available():
            _do_convert()
            return

        install_dlg = Adw.AlertDialog(
            heading="ScummVM Not Found",
            body=(
                "ScummVM must be installed before switching.\n\n"
                "Install it via your package manager or run:\n"
                "flatpak install org.scummvm.ScummVM"
            ),
        )
        install_dlg.add_response("cancel", "Cancel")
        install_dlg.add_response("check", "Check Again")
        install_dlg.set_default_response("check")
        install_dlg.set_close_response("cancel")

        def _on_check(_d, resp):
            if resp != "check":
                return
            from cellar.backend.scummvm import is_scummvm_available
            if is_scummvm_available():
                _do_convert()
            else:
                log.warning("ScummVM still not found")

        install_dlg.connect("response", _on_check)
        install_dlg.present(presenter)

    def _on_resp(_d, response):
        if response != "switch":
            return
        _check_and_convert()

    dlg.connect("response", _on_resp)
    dlg.present(presenter)


# ---------------------------------------------------------------------------
# DOS installer launch helper (shared by PackageBuilderView and
# _NewProjectDialog so it lives at module level).
# ---------------------------------------------------------------------------

def _launch_dos_installer(
    presenter,
    project,
    content_dir: Path,
    disc_image_paths: list[Path],
    floppy_paths: list[Path],
) -> None:
    """Show the DOSBox session dialog and run DOSBox.

    *presenter* is the GTK widget used as parent for the dialog and for
    calling ``_show_project()`` when done.
    """
    import re

    from cellar.backend.dosbox import run_dos_installer
    from cellar.backend.project import save_project
    from cellar.utils.async_work import run_in_background

    has_multi_disc = len(disc_image_paths) > 1
    has_multi_floppy = len(floppy_paths) > 1

    # ── Build dialog ──────────────────────────────────────────────────
    info_dialog = Adw.Dialog(content_width=520, content_height=620)
    info_dialog.set_can_close(False)

    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar(
        show_start_title_buttons=False,
        show_end_title_buttons=False,
    )
    header.set_title_widget(Gtk.Label(label="DOSBox Staging"))
    toolbar.add_top_bar(header)

    page = Adw.PreferencesPage()

    # ── Mounted media ────────────────────────────────────────────────
    media_group = Adw.PreferencesGroup(title="Mounted Drives")

    drive_c_row = Adw.ActionRow(title="C:", subtitle="Hard Drive (hdd/)")
    drive_c_row.add_prefix(Gtk.Image.new_from_icon_name("drive-harddisk-symbolic"))
    media_group.add(drive_c_row)

    if disc_image_paths:
        n = len(disc_image_paths)
        first_name = disc_image_paths[0].name
        disc_sub = first_name if n == 1 else f"{first_name} (1 of {n})"
        disc_row = Adw.ActionRow(title="D:", subtitle=disc_sub)
        disc_row.add_prefix(Gtk.Image.new_from_icon_name("media-optical-symbolic"))
        media_group.add(disc_row)

    if floppy_paths:
        n = len(floppy_paths)
        first_name = floppy_paths[0].name
        floppy_sub = first_name if n == 1 else f"{first_name} (1 of {n})"
        floppy_row = Adw.ActionRow(title="A:", subtitle=floppy_sub)
        floppy_row.add_prefix(Gtk.Image.new_from_icon_name("media-floppy-symbolic"))
        media_group.add(floppy_row)

    page.add(media_group)

    # ── Audio (populated from stderr) ────────────────────────────────
    audio_group = Adw.PreferencesGroup(title="Audio")

    sb_row = Adw.ActionRow(title="Sound Blaster", subtitle="Detecting\u2026")
    sb_row.add_prefix(Gtk.Image.new_from_icon_name("audio-card-symbolic"))
    audio_group.add(sb_row)

    midi_row = Adw.ActionRow(title="MIDI", subtitle="Detecting\u2026")
    midi_row.add_prefix(Gtk.Image.new_from_icon_name("audio-x-midi-symbolic"))
    audio_group.add(midi_row)

    page.add(audio_group)

    # ── Keyboard shortcuts ───────────────────────────────────────────
    keys_group = Adw.PreferencesGroup(title="Keyboard Shortcuts")

    _shortcuts = [
        ("Ctrl+F11", "Decrease CPU cycles (slower)"),
        ("Ctrl+F12", "Increase CPU cycles (faster)"),
    ]
    if has_multi_disc or has_multi_floppy:
        media = "disc" if has_multi_disc else "floppy"
        count = len(disc_image_paths) if has_multi_disc else len(floppy_paths)
        _shortcuts.insert(0, ("Ctrl+F4", f"Swap {media} ({count} mounted)"))
    _shortcuts += [
        ("Ctrl+F10", "Release mouse capture"),
        ("Alt+Enter", "Toggle fullscreen"),
        ("Alt+Pause", "Pause emulation"),
    ]

    for key, desc in _shortcuts:
        row = Adw.ActionRow(title=desc)
        badge = Gtk.Label(label=key)
        badge.add_css_class("dim-label")
        badge.add_css_class("caption")
        badge.add_css_class("monospace")
        row.add_suffix(badge)
        keys_group.add(row)

    page.add(keys_group)

    # ── DOSBox Output (collapsible log) ──────────────────────────────
    log_group = Adw.PreferencesGroup()

    expander = Gtk.Expander(label="DOSBox Output")
    expander.set_expanded(False)

    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
    scroll.set_min_content_height(180)
    scroll.set_max_content_height(300)
    scroll.add_css_class("card")

    # Smaller monospace font for log output
    _log_css = Gtk.CssProvider()
    _log_css.load_from_string("textview { font-size: 0.85em; }")

    log_buffer = Gtk.TextBuffer()
    log_view = Gtk.TextView(
        buffer=log_buffer, editable=False, cursor_visible=False,
    )
    log_view.set_monospace(True)
    log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    log_view.set_margin_top(6)
    log_view.set_margin_bottom(6)
    log_view.set_margin_start(8)
    log_view.set_margin_end(8)
    log_view.get_style_context().add_provider(
        _log_css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )
    scroll.set_child(log_view)
    expander.set_child(scroll)
    log_group.add(expander)

    page.add(log_group)

    # ── Stderr parsing ───────────────────────────────────────────────
    log_lines: list[str] = []
    _MAX_LOG = 300

    # Audio detail parsers
    _RE_BLASTER = re.compile(
        r"Setting 'BLASTER' environment variable to '([^']+)'",
    )
    _RE_MIDI = re.compile(r"MIDI: Opened device (.+)", re.IGNORECASE)
    _RE_MIDI_FAIL = re.compile(r"MIDI: Can't find device", re.IGNORECASE)
    _RE_FSYNTH = re.compile(r"FSYNTH: Using SoundFont (.+)", re.IGNORECASE)
    _RE_MT32 = re.compile(r"MT32: Initialised", re.IGNORECASE)
    _RE_OPL_PORTS = re.compile(r"OPL: Running \S+ on ports? \S+ and (\S+)", re.IGNORECASE)
    _RE_DISC_SWAP = re.compile(r"Drive ([A-Z]): disk (\d+) of (\d+) now active")

    def _on_stderr(line: str):
        stripped = line
        if " | " in line:
            stripped = line.split(" | ", 1)[1]

        # Parse BLASTER env var (e.g. "A220 I7 D1 T4")
        blaster_m = _RE_BLASTER.search(stripped)
        if blaster_m:
            blaster = blaster_m.group(1)
            # Parse BLASTER string: A=address, I=IRQ, D=DMA, H=HDMA, T=type
            parts: list[str] = []
            for token in blaster.split():
                if token.startswith("A"):
                    parts.append(f"Port {token[1:]}h")
                elif token.startswith("I"):
                    parts.append(f"IRQ {token[1:]}")
                elif token.startswith("D"):
                    parts.append(f"DMA {token[1:]}")
                elif token.startswith("H"):
                    parts.append(f"High DMA {token[1:]}")
            sb_row.set_subtitle(", ".join(parts) if parts else blaster)

        # Parse MIDI device
        midi_m = _RE_MIDI.search(stripped)
        if midi_m:
            midi_row.set_subtitle(midi_m.group(1).strip())

        # MIDI not available — OPL is the fallback for FM MIDI
        if _RE_MIDI_FAIL.search(stripped):
            midi_row.set_subtitle("OPL FM synthesis (Sound Blaster)")

        # Parse OPL ports — update MIDI subtitle if MIDI fell back to OPL
        opl_m = _RE_OPL_PORTS.search(stripped)
        if opl_m and "OPL FM" in midi_row.get_subtitle():
            midi_row.set_subtitle(
                f"OPL FM synthesis (port {opl_m.group(1)})"
            )

        # Parse FluidSynth SoundFont
        fsynth_m = _RE_FSYNTH.search(stripped)
        if fsynth_m:
            sf_name = Path(fsynth_m.group(1).strip()).name
            midi_row.set_subtitle(f"FluidSynth \u2014 {sf_name}")

        # Parse MT-32
        if _RE_MT32.search(stripped):
            midi_row.set_subtitle("MT-32 Emulation")

        # Parse disc swap
        swap_m = _RE_DISC_SWAP.search(stripped)
        if swap_m:
            drive, cur, total = swap_m.group(1), swap_m.group(2), swap_m.group(3)
            if drive == "D" and disc_image_paths:
                idx = int(cur) - 1
                name = (disc_image_paths[idx].name
                        if 0 <= idx < len(disc_image_paths)
                        else f"Disc {cur}")
                disc_row.set_subtitle(f"{name} ({cur} of {total})")
            elif drive == "A" and floppy_paths:
                idx = int(cur) - 1
                name = floppy_paths[idx].name if 0 <= idx < len(floppy_paths) else f"Disk {cur}"
                floppy_row.set_subtitle(f"{name} ({cur} of {total})")

        # Append to log view
        log_lines.append(line)
        end_iter = log_buffer.get_end_iter()
        prefix = "\n" if log_buffer.get_char_count() > 0 else ""
        log_buffer.insert(end_iter, prefix + line)
        while len(log_lines) > _MAX_LOG:
            log_lines.pop(0)
            start = log_buffer.get_start_iter()
            first_nl = log_buffer.get_iter_at_line(1)
            log_buffer.delete(start, first_nl)
        adj = scroll.get_vadjustment()
        adj.set_value(adj.get_upper())

    toolbar.set_content(page)
    info_dialog.set_child(toolbar)
    info_dialog.present(presenter)

    def _close_dialog():
        info_dialog.set_can_close(True)
        info_dialog.close()

    def _work():
        return run_dos_installer(
            content_dir=content_dir,
            disc_images=disc_image_paths,
            floppy_images=floppy_paths,
            stderr_cb=lambda line: GLib.idle_add(_on_stderr, line),
        )

    def _done(entry_points):
        _close_dialog()

        # Clean up floppies after successful install — they were only
        # needed for the installer and shouldn't be shipped.
        if entry_points and floppy_paths:
            floppy_dir = content_dir / "floppy"
            if floppy_dir.is_dir():
                shutil.rmtree(floppy_dir, ignore_errors=True)
            project.floppy_images = []

        if entry_points:
            project.entry_points = entry_points

            # Now that the game is installed on the HDD, detect a profile.
            from cellar.backend.dosbox_profiles import apply_profile
            apply_profile(content_dir)

        save_project(project)
        if hasattr(presenter, "_show_project"):
            presenter._show_project(project)

        # Check for ScummVM compatibility (after save so project is up to date).
        if entry_points:
            GLib.idle_add(
                _check_scummvm_compat, content_dir, project, presenter,
            )

    def _error(msg):
        _close_dialog()
        log.error("DOSBox installer failed: %s", msg)
        err = Adw.AlertDialog(
            heading="Installation failed",
            body=f"DOSBox installer encountered an error:\n{msg}",
        )
        err.add_response("ok", "OK")
        err.present(presenter)

    run_in_background(work=_work, on_done=_done, on_error=_error)


class PackageBuilderView(Adw.Bin):
    """Package builder: project list → detail page via stack navigation."""

    def __init__(
        self,
        *,
        nav_view: Adw.NavigationView | None = None,
        writable_repos: list | None = None,
        all_repos: list | None = None,
        on_catalogue_changed: Callable | None = None,
        publish_queue=None,
    ) -> None:
        super().__init__()
        self._nav_view: Adw.NavigationView | None = nav_view
        self._writable_repos: list = writable_repos or []
        self._all_repos: list = all_repos or []
        self._on_catalogue_changed = on_catalogue_changed
        self._publish_queue = publish_queue
        self._project: Project | None = None
        self._project_cards: list[_ProjectCard] = []
        self._replacing_detail = False
        self._search_text: str = ""
        self._active_types: set[str] = set()
        self._active_repos: set[str] = set()

        self._setup_actions()
        self._build()
        self._reload_projects()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_repos(self, writable_repos: list, *, all_repos: list | None = None) -> None:
        self._writable_repos = writable_repos
        if all_repos is not None:
            self._all_repos = all_repos
        self._reload_projects()

    def set_search_text(self, text: str) -> None:
        self._search_text = text
        self._apply_filter()

    def set_active_types(self, types: set[str]) -> None:
        self._active_types = types
        self._apply_filter()

    def set_active_repos(self, repos: set[str]) -> None:
        self._active_repos = repos
        self._apply_filter()

    def _apply_filter(self) -> None:
        """Show/hide cards based on current search text and active filters."""
        child = self._flow_box.get_child_at_index(0)
        i = 0
        while child is not None:
            if isinstance(child, _NewProjectCard):
                child.set_visible(True)
            elif isinstance(child, (_ProjectCard, _CatalogueCard)):
                child.set_visible(
                    child.matches(self._search_text, self._active_types, self._active_repos)
                )
            i += 1
            child = self._flow_box.get_child_at_index(i)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    @staticmethod
    def _find_scrolled_window(widget: Gtk.Widget) -> Gtk.ScrolledWindow | None:
        """Walk the widget tree to find the first GtkScrolledWindow child."""
        if isinstance(widget, Gtk.ScrolledWindow):
            return widget
        child = widget.get_first_child()
        while child is not None:
            result = PackageBuilderView._find_scrolled_window(child)
            if result is not None:
                return result
            child = child.get_next_sibling()
        return None

    def _get_detail_scroll_position(self) -> float:
        """Get current scroll position of the detail page, or 0.0."""
        if self._nav_view is None:
            return 0.0
        visible = self._nav_view.get_visible_page()
        if visible is not None and visible.get_tag() == "builder-detail":
            sw = self._find_scrolled_window(visible)
            if sw is not None:
                return sw.get_vadjustment().get_value()
        return 0.0

    def _restore_scroll_position(self, pos: float) -> None:
        """Restore scroll position on the current detail page after it's laid out."""
        if pos <= 0.0 or self._nav_view is None:
            return
        visible = self._nav_view.get_visible_page()
        if visible is None:
            return
        sw = self._find_scrolled_window(visible)
        if sw is not None:
            def _apply(*_args):
                sw.get_vadjustment().set_value(pos)
                return False  # run once
            # Defer until after layout so upper bound is correct
            GLib.idle_add(_apply)

    def _setup_actions(self) -> None:
        """Register Gio actions for the builder view."""
        self._action_group = Gio.SimpleActionGroup()

        delete_act = Gio.SimpleAction.new("delete", None)
        delete_act.connect(
            "activate",
            lambda *_: self._on_delete_clicked(self._project) if self._project else None,
        )
        self._action_group.add_action(delete_act)

        self.insert_action_group("builder", self._action_group)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        # Register CSS for the dashed new-project card
        css = Gtk.CssProvider()
        css.load_from_string(
            ".new-project-card {"
            "  border: 2px dashed alpha(@card_shade_color, 0.5);"
            "  border-radius: 12px;"
            "  background: none;"
            "}"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self._flow_box = make_app_grid(on_activated=self._on_card_activated)

        self._list_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        self._list_scroll.set_vexpand(True)
        self._list_scroll.set_child(self._flow_box)

        self.set_child(self._list_scroll)

    # ------------------------------------------------------------------
    # Project list management
    # ------------------------------------------------------------------

    def _reload_projects(self) -> None:
        """Reload project list from disk and refresh the card grid."""
        # Preserve scroll position across rebuilds.
        vadj = self._list_scroll.get_vadjustment()
        saved_scroll = vadj.get_value()

        projects = load_projects()
        # Clear existing cards
        while True:
            child = self._flow_box.get_child_at_index(0)
            if child is None:
                break
            self._flow_box.remove(child)
        self._project_cards: list[_ProjectCard] = []

        # Always-visible "New Project" card at the start
        self._flow_box.append(_NewProjectCard())

        for p in sorted(projects, key=lambda x: natural_sort_key(x.name)):
            card = _ProjectCard(p)
            self._project_cards.append(card)
            self._flow_box.append(card)

        # Add catalogue entry cards from writable repos
        imported_ids = {p.origin_app_id for p in projects if p.origin_app_id}
        catalogue_entries, used_bases = self._fetch_writable_catalogue_entries(imported_ids)
        for entry, repo, kind in catalogue_entries:
            has_dependants = kind == "base" and entry.name in used_bases
            is_windows_app = kind == "app" and getattr(entry, "platform", "windows") == "windows"
            card = _CatalogueCard(
                entry, repo, kind,
                on_download=self._on_catalogue_download,
                on_delete=self._on_catalogue_delete,
                on_edit=self._on_catalogue_edit if kind == "app" else None,
                on_change_base=self._on_change_base_image if is_windows_app else None,
                has_dependants=has_dependants,
                show_repo=len(self._writable_repos) > 1,
            )
            self._flow_box.append(card)

        if not projects:
            self._project = None

        # Restore scroll position after GTK lays out the new children.
        if saved_scroll > 0:
            GLib.idle_add(lambda: vadj.set_value(saved_scroll) or False)

    def _fetch_writable_catalogue_entries(
        self, imported_ids: set[str],
    ) -> tuple[list[tuple], set[str]]:
        """Return catalogue entries from writable repos not already imported.

        Also returns the set of base names referenced by at least one app
        (via ``base_image`` in the slim index), so callers can prevent
        deletion of bases that still have dependants.
        """
        results: list[tuple] = []
        used_bases: set[str] = set()
        for repo in self._writable_repos:
            try:
                for entry in repo.fetch_catalogue():
                    if entry.id not in imported_ids:
                        results.append((entry, repo, "app"))
                    if entry.base_image:
                        used_bases.add(entry.base_image)
            except Exception as exc:
                log.warning("Could not fetch catalogue from %s: %s", repo.uri, exc)
            try:
                for name, base_entry in repo.fetch_bases().items():
                    if base_entry.name not in imported_ids:
                        results.append((base_entry, repo, "base"))
            except Exception as exc:
                log.warning("Could not fetch bases from %s: %s", repo.uri, exc)
        results.sort(key=lambda t: natural_sort_key(t[0].name))
        return results, used_bases

    def _on_card_activated(self, _fb, child: Gtk.FlowBoxChild) -> None:
        if isinstance(child, _ProjectCard):
            # Reload from disk to pick up any changes saved since the list was built.
            from cellar.backend.project import load_project
            fresh = load_project(child.project.slug)
            self._project = fresh if fresh else child.project
            self._show_project(self._project)
        elif isinstance(child, _NewProjectCard):
            self._on_new_project_clicked(None)

    def _pop_detail(self) -> None:
        """Pop the builder detail page from the main navigation view."""
        if self._nav_view is None:
            return
        visible = self._nav_view.get_visible_page()
        if visible is not None and visible.get_tag() == "builder-detail":
            self._nav_view.pop()

    def _on_nav_popped(self, _nav, page) -> None:
        """Called by the main nav view when a page is popped."""
        if page is not None and page.get_tag() == "builder-detail":
            if not self._replacing_detail:
                self._project = None

    def _on_new_project_clicked(self, _btn) -> None:
        """Open the guided New Project chooser dialog."""
        dialog = _NewProjectDialog(
            on_windows=self._on_new_windows,
            on_linux=lambda: self._on_new_linux_clicked(None),
            on_dos=lambda: self._on_new_dos_clicked(None),
            on_base=lambda: self._on_new_base_clicked(None),
            on_import=self._on_project_created,
            parent_view=self,
        )
        dialog.present(self)

    def _on_new_windows(self) -> None:
        """Create a new Windows project — base image is selected in detail view."""
        dialog = MetadataEditorDialog(
            context=ProjectContext(), on_created=self._on_project_created,
        )
        dialog.present(self)

    def _on_new_linux_clicked(self, _btn) -> None:
        dialog = MetadataEditorDialog(
            context=ProjectContext(project_type="linux"), on_created=self._on_project_created,
        )
        dialog.present(self)

    def _on_new_dos_clicked(self, _btn) -> None:
        dialog = MetadataEditorDialog(
            context=ProjectContext(project_type="dos"), on_created=self._on_project_created,
        )
        dialog.present(self)

    def _on_new_base_clicked(self, _btn) -> None:
        from cellar.backend import runners as _runners
        installed = _runners.installed_runners()
        runner = installed[0] if installed else ""
        project = create_project("", "base", runner=runner)
        self._on_project_created(project)

    def _on_project_imported(self, project: Project) -> None:
        self._reload_projects()
        self._project = project
        self._show_project(project)

    def _on_delete_clicked(self, project: Project) -> None:
        name = project.name
        slug = project.slug

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
                def _after_delete():
                    self._project = None
                    self._reload_projects()
                    self._pop_detail()

                run_in_background(
                    lambda: delete_project(slug),
                    on_done=lambda _r: _after_delete(),
                )

        dialog.connect("response", _on_response)
        dialog.present(self)

    def _on_project_created(self, project: Project) -> None:
        if project.project_type == "dos":
            from cellar.backend.dosbox import prepare_dos_layout
            from cellar.backend.project import save_project
            content = project.content_path
            content.mkdir(parents=True, exist_ok=True)
            prepare_dos_layout(content)
            project.source_dir = str(content)
            project.initialized = True
            save_project(project)
        self._reload_projects()
        self._project = project
        self._show_project(project)

    # ------------------------------------------------------------------
    # Catalogue card actions
    # ------------------------------------------------------------------

    def _on_catalogue_download(self, card: "_CatalogueCard") -> None:
        """Confirm and import a published catalogue entry for editing."""
        entry, repo, kind = card.entry, card.repo, card.kind
        dialog = Adw.AlertDialog(
            heading="Download for Editing?",
            body=f"\u201c{entry.name}\u201d will be downloaded from the repository for editing.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("download", "Download")
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_download_confirmed, entry, repo, kind)
        dialog.present(self)

    def _on_download_confirmed(self, _d, response: str, entry, repo, kind: str) -> None:
        if response != "download":
            return
        if kind == "base":
            self._import_base_entry(entry, repo)
        else:
            self._import_app_entry(entry, repo)

    def _import_app_entry(self, entry, repo) -> None:
        """Download and import an app catalogue entry as a builder project."""
        cancel = threading.Event()
        progress = ProgressDialog(label="Downloading\u2026", cancel_event=cancel)
        progress.present(self)

        def _work():
            import tempfile

            from cellar.backend.installer import (
                InstallCancelled,
                _build_source,
                _find_top_dir,
                _install_chunks,
                _stream_and_extract,
            )
            from cellar.backend.packager import slugify
            from cellar.utils.progress import fmt_stats

            nonlocal entry
            if entry.is_partial:
                GLib.idle_add(progress.set_label, "Fetching metadata\u2026")
                entry = repo.fetch_app_metadata(entry.id)
                GLib.idle_add(progress.set_label, "Downloading\u2026")

            archive_uri = repo.resolve_asset_uri(entry.archive)

            from cellar.backend.config import install_data_dir
            try:
                with tempfile.TemporaryDirectory(prefix="cellar-import-",
                                                 dir=install_data_dir()) as tmp_str:
                    tmp = Path(tmp_str)
                    extract_dir = tmp / "extracted"
                    extract_dir.mkdir()

                    if entry.archive_chunks:
                        _install_chunks(
                            archive_uri, entry.archive_chunks, extract_dir,
                            strip_top_dir=True,
                            cancel_event=cancel,
                            progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                            stats_cb=lambda d, t, s: GLib.idle_add(
                                progress.set_stats, fmt_stats(d, t, s)),
                            token=repo.token,
                            ssl_verify=repo.ssl_verify,
                            ca_cert=repo.ca_cert,
                        )
                    else:
                        chunks, total = _build_source(
                            archive_uri,
                            expected_size=entry.archive_size,
                            token=repo.token,
                            ssl_verify=repo.ssl_verify,
                            ca_cert=repo.ca_cert,
                        )
                        _stream_and_extract(
                            chunks, total,
                            is_zst=archive_uri.endswith(".tar.zst"),
                            dest=extract_dir,
                            expected_crc32=entry.archive_crc32,
                            cancel_event=cancel,
                            progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                            stats_cb=lambda d, t, s: GLib.idle_add(
                                progress.set_stats, fmt_stats(d, t, s)),
                            name_cb=None,
                        )

                    if entry.archive_chunks:
                        content_src = extract_dir  # strip_top_dir already applied
                    else:
                        content_src = _find_top_dir(extract_dir)

                    slug = slugify(entry.id)
                    existing = {p.slug for p in load_projects()}
                    base_slug, i = slug, 2
                    while slug in existing:
                        slug = f"{base_slug}-{i}"
                        i += 1

                    project = Project(
                        name=entry.name,
                        slug=slug,
                        project_type={"windows": "app", "linux": "linux", "dos": "dos"}.get(
                            entry.platform, "app"),
                        runner=entry.base_image,
                        entry_points=[dict(t) for t in entry.launch_targets],
                        steam_appid=entry.steam_appid,
                        initialized=True,
                        origin_app_id=entry.id,
                        version=entry.version or "1.0",
                        category=entry.category or "",
                        developer=entry.developer or "",
                        publisher=entry.publisher or "",
                        release_year=entry.release_year,
                        website=entry.website or "",
                        genres=list(entry.genres) if entry.genres else [],
                        summary=entry.summary or "",
                        description=entry.description or "",
                        hide_title=entry.hide_title,
                        dxvk=entry.dxvk,
                        vkd3d=entry.vkd3d,
                        audio_driver=entry.audio_driver,
                        debug=entry.debug,
                        direct_proton=entry.direct_proton,
                        no_lsteamclient=entry.no_lsteamclient,
                        lock_runner=entry.lock_runner,
                    )

                    GLib.idle_add(progress.set_label, "Copying content\u2026")
                    project.content_path.mkdir(parents=True, exist_ok=True)
                    for src in content_src.rglob("*"):
                        rel = src.relative_to(content_src)
                        dst = project.content_path / rel
                        if src.is_dir():
                            dst.mkdir(parents=True, exist_ok=True)
                        elif src.is_file():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dst)

                    # Download image assets
                    GLib.idle_add(progress.set_label, "Downloading images\u2026")
                    project.project_dir.mkdir(parents=True, exist_ok=True)

                    for slot, rel_path in [
                        ("icon", entry.icon), ("cover", entry.cover), ("logo", entry.logo)
                    ]:
                        if not rel_path:
                            continue
                        try:
                            local = repo.resolve_asset_uri(rel_path)
                            if local and Path(local).is_file():
                                ext = Path(local).suffix or Path(rel_path).suffix
                                dest = project.project_dir / f"{slot}{ext}"
                                shutil.copy2(local, dest)
                                setattr(project, f"{slot}_path", str(dest))
                        except Exception:
                            log.warning("Could not download %s for import", slot)

                    screenshot_paths: list[str] = []
                    for idx, ss_rel in enumerate(entry.screenshots):
                        try:
                            local = repo.resolve_asset_uri(ss_rel)
                            if local and Path(local).is_file():
                                ext = Path(local).suffix or Path(ss_rel).suffix
                                dest = project.project_dir / f"screenshot_{idx}{ext}"
                                shutil.copy2(local, dest)
                                screenshot_paths.append(str(dest))
                        except Exception:
                            log.warning("Could not download screenshot %s", ss_rel)
                    project.screenshot_paths = screenshot_paths

                    if project.project_type in ("linux", "dos"):
                        project.source_dir = str(project.content_path)

                    save_project(project)
                    return project
            except InstallCancelled:
                return None

        def _done(project) -> None:
            progress.force_close()
            if project is None:
                return
            self._on_project_imported(project)

        def _error(msg: str) -> None:
            progress.force_close()
            log.error("Import failed: %s", msg)
            err = Adw.AlertDialog(heading="Import failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

    def _import_base_entry(self, base_entry, repo) -> None:
        """Download and import a base catalogue entry as a builder project."""
        cancel = threading.Event()
        progress = ProgressDialog(label=f"Downloading {base_entry.name}\u2026",
                                  cancel_event=cancel)
        progress.present(self)

        def _work():
            import tempfile

            from cellar.backend.installer import (
                InstallCancelled,
                _build_source,
                _find_top_dir,
                _install_chunks,
                _stream_and_extract,
            )
            from cellar.backend.packager import slugify
            from cellar.utils.progress import fmt_stats

            archive_uri = repo.resolve_asset_uri(base_entry.archive)

            from cellar.backend.config import install_data_dir
            try:
                with tempfile.TemporaryDirectory(prefix="cellar-base-import-",
                                                 dir=install_data_dir()) as tmp_str:
                    tmp = Path(tmp_str)
                    extract_dir = tmp / "extracted"
                    extract_dir.mkdir()

                    if base_entry.archive_chunks:
                        _install_chunks(
                            archive_uri, base_entry.archive_chunks, extract_dir,
                            strip_top_dir=True,
                            cancel_event=cancel,
                            progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                            stats_cb=lambda d, t, s: GLib.idle_add(
                                progress.set_stats, fmt_stats(d, t, s)),
                            token=repo.token,
                            ssl_verify=repo.ssl_verify,
                            ca_cert=repo.ca_cert,
                        )
                    else:
                        chunks, total = _build_source(
                            archive_uri,
                            expected_size=base_entry.archive_size,
                            token=repo.token,
                            ssl_verify=repo.ssl_verify,
                            ca_cert=repo.ca_cert,
                        )
                        _stream_and_extract(
                            chunks, total,
                            is_zst=archive_uri.endswith(".tar.zst"),
                            dest=extract_dir,
                            expected_crc32=base_entry.archive_crc32,
                            cancel_event=cancel,
                            progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                            stats_cb=lambda d, t, s: GLib.idle_add(
                                progress.set_stats, fmt_stats(d, t, s)),
                            name_cb=None,
                        )

                    if base_entry.archive_chunks:
                        content_src = extract_dir
                    else:
                        content_src = _find_top_dir(extract_dir)

                    slug = slugify(base_entry.name)
                    existing = {p.slug for p in load_projects()}
                    base_slug, i = slug, 2
                    while slug in existing:
                        slug = f"{base_slug}-{i}"
                        i += 1

                    project = Project(
                        name=base_entry.name,
                        slug=slug,
                        project_type="base",
                        runner=base_entry.runner,
                        initialized=True,
                    )

                    GLib.idle_add(progress.set_label, "Copying content\u2026")
                    GLib.idle_add(progress.set_fraction, 0.0)
                    project.content_path.mkdir(parents=True, exist_ok=True)
                    for src in content_src.rglob("*"):
                        rel = src.relative_to(content_src)
                        dst = project.content_path / rel
                        if src.is_dir():
                            dst.mkdir(parents=True, exist_ok=True)
                        elif src.is_file():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dst)

                    save_project(project)
                    return project
            except InstallCancelled:
                return None

        def _done(project) -> None:
            progress.force_close()
            if project is None:
                return
            self._on_project_imported(project)

        def _error(msg: str) -> None:
            progress.force_close()
            log.error("Base import failed: %s", msg)
            err = Adw.AlertDialog(heading="Import failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

    def _on_catalogue_delete(self, card: "_CatalogueCard") -> None:
        """Confirm and remove a catalogue entry from its repo."""
        entry, repo, kind = card.entry, card.repo, card.kind
        dialog = Adw.AlertDialog(
            heading="Remove from Repository?",
            body=f"\u201c{entry.name}\u201d will be permanently deleted from the repository.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_confirmed, entry, repo, kind)
        dialog.present(self)

    def _on_delete_confirmed(self, _d, response: str, entry, repo, kind: str) -> None:
        if response != "remove":
            return

        def _work():
            from cellar.backend import packager
            repo_root = repo.writable_path()
            if kind == "base":
                packager.remove_base(repo_root, entry.name)
            else:
                packager.remove_from_repo(repo_root, entry)

        def _done(_result) -> None:
            self._reload_projects()
            if self._on_catalogue_changed:
                self._on_catalogue_changed()

        def _error(msg: str) -> None:
            log.error("Delete failed: %s", msg)
            err = Adw.AlertDialog(heading="Delete failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

    def _on_catalogue_edit(self, card: "_CatalogueCard") -> None:
        """Fetch full metadata for the selected card then open MetadataEditorDialog."""
        entry, repo = card.entry, card.repo

        def _fetch():
            return repo.fetch_app_metadata(entry.id)

        def _open(full_entry) -> None:
            def _on_done(_updated_entry) -> None:
                self._reload_projects()
                if self._on_catalogue_changed:
                    self._on_catalogue_changed()

            MetadataEditorDialog(
                context=RepoContext(entry=full_entry, repo=repo),
                on_done=_on_done,
            ).present(self)

        def _error(msg: str) -> None:
            log.error("Failed to load metadata for %s: %s", entry.id, msg)
            err = Adw.AlertDialog(heading="Could not load metadata", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_fetch, on_done=_open, on_error=_error)

    def _on_change_base_image(self, card: "_CatalogueCard") -> None:
        """Let the user reassign a base image without re-uploading the archive."""
        entry, repo = card.entry, card.repo

        def _fetch():
            full = repo.fetch_app_metadata(entry.id)
            bases = repo.fetch_bases()
            return full, bases

        def _show(result) -> None:
            full_entry, bases = result
            if not bases:
                err = Adw.AlertDialog(
                    heading="No base images",
                    body="This repository has no base images published.",
                )
                err.add_response("ok", "OK")
                err.present(self)
                return
            self._show_base_picker(full_entry, repo, bases)

        def _error(msg: str) -> None:
            log.error("Failed to load bases for %s: %s", entry.id, msg)
            err = Adw.AlertDialog(heading="Could not load bases", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_fetch, on_done=_show, on_error=_error)

    def _show_base_picker(self, entry, repo, bases: dict) -> None:
        """Present a dialog to pick a base image for *entry*."""
        from cellar.views.widgets import make_dialog_header

        dlg = Adw.Dialog(title="Change Base Image", content_width=440)
        toolbar, _hdr, apply_btn = make_dialog_header(
            dlg,
            action_label="Apply",
            action_sensitive=False,
        )

        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_min_content_height(300)
        list_box = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        list_box.add_css_class("boxed-list")
        set_margins(list_box, 12)
        scroll.set_child(list_box)

        current = entry.base_image
        selected: list[str] = [current]  # mutable container for closure
        sorted_names = sorted(bases.keys())

        # "None" row — clear base image
        list_box.append(Adw.ActionRow(title="None"))

        for name in sorted_names:
            row = Adw.ActionRow(title=name, subtitle=f"Runner: {bases[name].runner}")
            list_box.append(row)

        # Pre-select the current base (index 0 = None, 1+ = sorted names)
        if current and current in bases:
            pre_idx = sorted_names.index(current) + 1
        else:
            pre_idx = 0
        pre_row = list_box.get_row_at_index(pre_idx)
        if pre_row:
            list_box.select_row(pre_row)

        def _on_row_selected(_lb, row):
            if row is None:
                return
            idx = row.get_index()
            selected[0] = "" if idx == 0 else sorted_names[idx - 1]
            apply_btn.set_sensitive(selected[0] != current)

        list_box.connect("row-selected", _on_row_selected)

        def _on_apply(_btn) -> None:
            new_base = selected[0]
            updated = _dc_replace(entry, base_image=new_base)

            def _save():
                from cellar.backend.packager import update_app_metadata
                update_app_metadata(repo.writable_path(), updated)

            def _done(_r) -> None:
                dlg.close()
                toast = Adw.Toast(title=f"Base image updated to \u201c{new_base or 'None'}\u201d")
                toast_overlay = self.get_ancestor(Adw.ToastOverlay)
                if toast_overlay:
                    toast_overlay.add_toast(toast)
                self._reload_projects()
                if self._on_catalogue_changed:
                    self._on_catalogue_changed()

            def _error(msg: str) -> None:
                log.error("Failed to update base image: %s", msg)
                apply_btn.set_sensitive(True)
                err = Adw.AlertDialog(heading="Update failed", body=msg)
                err.add_response("ok", "OK")
                err.present(dlg)

            apply_btn.set_sensitive(False)
            run_in_background(_save, on_done=_done, on_error=_error)

        apply_btn.connect("clicked", _on_apply)
        toolbar.set_content(scroll)
        dlg.set_child(toolbar)
        dlg.present(self)

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------

    def _show_project(self, project: Project, *, expand_sel: bool = False) -> None:
        """Build a detail page for *project* and push it onto the nav stack."""
        _type_labels = {
            "app": "Windows App", "linux": "Linux App",
            "dos": "DOS App", "base": "Base Image",
        }

        # Build toolbar with header
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self._content_title = Adw.WindowTitle(
            title=project.name,
            subtitle=_type_labels.get(project.project_type, ""),
        )
        header.set_title_widget(self._content_title)

        # Gear menu
        self._detail_gear_btn = make_gear_button()
        self._refresh_detail_menu(project)
        header.pack_end(self._detail_gear_btn)

        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        detail_page = Adw.NavigationPage(title=project.name, child=toolbar)
        detail_page.set_tag("builder-detail")
        # Re-insert action group so menu actions resolve from the detail page.
        detail_page.insert_action_group("builder", self._action_group)

        # ── 1. Metadata section (App / Linux / DOS projects — first, to set title/slug) ──
        if project.project_type in ("app", "linux", "dos"):
            meta_group = Adw.PreferencesGroup(title="Metadata")

            # Title — always visible
            self._meta_name_row = Adw.EntryRow(title="Title")
            self._meta_name_row.set_text(project.name)
            _steam_btn = Gtk.Button(icon_name="system-search-symbolic")
            _steam_btn.add_css_class("flat")
            _steam_btn.set_valign(Gtk.Align.CENTER)
            _steam_btn.set_tooltip_text("Look up on Steam")
            _steam_btn.connect("clicked", self._on_meta_steam_lookup)
            self._meta_name_row.add_suffix(_steam_btn)

            def _on_name_changed(row):
                if self._project:
                    self._project.name = row.get_text()
                    save_project(self._project)
                    self._content_title.set_title(self._project.name)
                    for r in self._project_cards:
                        if r.project.slug == self._project.slug:
                            r._name_label.set_text(self._project.name)
                            break

            self._meta_name_row.connect("changed", _on_name_changed)
            meta_group.add(self._meta_name_row)

            # App ID — always visible, read-only
            _slug_row = Adw.ActionRow(title="App ID", subtitle=project.slug)
            _slug_row.add_css_class("property")
            meta_group.add(_slug_row)

            # Category — visible inline so users don't miss it
            from cellar.backend.packager import BASE_CATEGORIES
            _cat_strings = Gtk.StringList.new(BASE_CATEGORIES)
            _cat_row = Adw.ComboRow(title="Category", model=_cat_strings)
            try:
                _cat_idx = BASE_CATEGORIES.index(project.category) if project.category else -1
            except ValueError:
                _cat_idx = -1
            if _cat_idx >= 0:
                _cat_row.set_selected(_cat_idx)
            else:
                _cat_row.set_selected(Gtk.INVALID_LIST_POSITION)

            def _on_category_selected(row, _pspec):
                idx = row.get_selected()
                if self._project and idx != Gtk.INVALID_LIST_POSITION:
                    self._project.category = BASE_CATEGORIES[idx]
                    save_project(self._project)

            _cat_row.connect("notify::selected", _on_category_selected)
            meta_group.add(_cat_row)

            # Details summary row — opens MetadataEditorDialog
            _details_row = Adw.ActionRow(title="Details")
            _details_summary = self._make_metadata_summary(project)
            if _details_summary:
                _details_row.set_subtitle(_details_summary)
            _details_btn = Gtk.Button(label="Edit\u2026", valign=Gtk.Align.CENTER)
            _details_btn.connect("clicked", self._on_edit_metadata_clicked)
            _details_row.add_suffix(_details_btn)
            _details_row.set_activatable_widget(_details_btn)
            meta_group.add(_details_row)

            page.add(meta_group)

        # ── 2. Runner / Base Image (Windows packages only) ────────────────
        if project.project_type not in ("linux", "dos"):
            sel_group = Adw.PreferencesGroup()
            if project.project_type == "base":
                sel_group_title = "Runner"
                sel_active_label = project.runner or "No runner selected"
            else:
                sel_group_title = "Base Image"
                sel_active_label = project.runner or "No base image selected"

            self._sel_active_row = Adw.ActionRow(title=sel_group_title)
            self._sel_active_row.set_subtitle(sel_active_label)

            if project.project_type == "base":
                # Flat layout: runner list + Download button, no expander
                self._sel_expander = sel_group
                dl_btn = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
                dl_btn.add_css_class("suggested-action")
                dl_btn.connect("clicked", self._on_download_runner_clicked)
                self._sel_active_row.add_suffix(dl_btn)
                sel_group.add(self._sel_active_row)
            else:
                # Flat layout: base list + Download button, no expander
                self._sel_expander = sel_group
                self._dl_base_btn = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
                self._dl_base_btn.connect("clicked", self._on_download_base_clicked)
                self._sel_active_row.add_suffix(self._dl_base_btn)
                sel_group.add(self._sel_active_row)

            page.add(sel_group)

            if project.project_type == "base":
                self._populate_runner_expander(project)
            else:
                self._populate_base_expander(project)

        # ── 2b. Base Name (base projects only) ───────────────────────────
        if project.project_type == "base":
            name_group = Adw.PreferencesGroup(title="Base Name")
            self._base_name_row = Adw.EntryRow(title="Name")
            self._base_name_row.set_text(project.name)

            def _on_base_name_changed(row):
                if self._project:
                    self._project.name = row.get_text()
                    save_project(self._project)
                    self._content_title.set_title(self._project.name)
                    for r in self._project_cards:
                        if r.project.slug == self._project.slug:
                            r._name_label.set_text(self._project.name)
                            break

            self._base_name_row.connect("changed", _on_base_name_changed)
            name_group.add(self._base_name_row)
            page.add(name_group)

        # ── 3. Prefix (Windows / Base only) ───────────────────────────────
        if project.project_type not in ("linux", "dos"):
            prefix_group = Adw.PreferencesGroup(title="Prefix")
            prefix_exists = project.content_path.is_dir()
            status_text = "Initialized" if (prefix_exists and project.initialized) else (
                "Directory exists (not initialized)" if prefix_exists else "Not initialized"
            )
            self._prefix_status_row = Adw.ActionRow(title="Status", subtitle=status_text)
            if project.initialized:
                _browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
                _browse_btn.set_valign(Gtk.Align.CENTER)
                _browse_btn.add_css_class("flat")
                _browse_btn.connect("clicked", self._on_browse_prefix_clicked)
                self._prefix_status_row.add_suffix(_browse_btn)
            else:
                self._init_btn = Gtk.Button(label="Initialize")
                self._init_btn.set_valign(Gtk.Align.CENTER)
                self._init_btn.add_css_class("suggested-action")
                self._init_btn.set_sensitive(bool(project.runner))
                self._init_btn.connect("clicked", self._on_init_prefix_clicked)
                self._prefix_status_row.add_suffix(self._init_btn)
            prefix_group.add(self._prefix_status_row)

            _winecfg_row = Adw.ActionRow(
                title="Wine Configuration",
                subtitle="Open winecfg (DLL overrides, Windows version, …)",
            )
            _winecfg_row.set_sensitive(project.initialized)
            _winecfg_btn = Gtk.Button(label="Open")
            _winecfg_btn.set_valign(Gtk.Align.CENTER)
            _winecfg_btn.connect("clicked", self._on_winecfg_clicked)
            _winecfg_row.add_suffix(_winecfg_btn)
            prefix_group.add(_winecfg_row)

            page.add(prefix_group)

        # ── 4. Files section (Windows app only) ───────────────────────────
        if project.project_type == "app":
            files_group = Adw.PreferencesGroup(title="Files")

            # Import Data row — shown when a Windows folder was dropped via smart import
            if project.source_dir and not project.installer_path:
                _import_row = Adw.ActionRow(
                    title="Import Folder",
                    subtitle=Path(project.source_dir).name,
                )
                _import_btn = Gtk.Button(label="Copy to Prefix")
                _import_btn.set_valign(Gtk.Align.CENTER)
                _import_btn.add_css_class("suggested-action")
                _import_btn.connect("clicked", self._on_import_folder_to_prefix)
                _import_row.add_suffix(_import_btn)
                _import_row.set_sensitive(project.initialized)
                files_group.add(_import_row)
            else:
                # Run Installer — only shown when no source_dir (not a folder import)
                if project.installer_path:
                    # First-time: prefilled installer from smart import
                    run_installer_row = Adw.ActionRow(
                        title="Run Installer",
                        subtitle=Path(project.installer_path).name,
                    )
                    run_btn = Gtk.Button(label="Launch")
                    run_btn.set_valign(Gtk.Align.CENTER)
                    run_btn.add_css_class("suggested-action")
                    run_btn.connect("clicked", self._on_launch_prefilled_installer)
                    run_installer_row.add_suffix(run_btn)
                    run_installer_row.set_sensitive(project.initialized)
                    files_group.add(run_installer_row)
                else:
                    # Post-first-install: drop zone for DLC / patches
                    drop_zone = self._build_installer_drop_zone(
                        hint="Drop installers or folders here",
                        on_browse_file=self._on_run_installer_clicked,
                        on_browse_folder=self._on_browse_dlc_folder_clicked,
                        show_browse_content=project.initialized,
                        on_browse_content=self._on_browse_prefix_clicked,
                    )
                    drop_zone.set_sensitive(project.initialized)
                    files_group.add(drop_zone)

            page.add(files_group)

            # Launch Targets (Windows app)
            targets_group = Adw.PreferencesGroup(title="Launch Targets")
            for _ep in project.entry_points:
                _ep_row = self._build_target_expander_row(_ep, is_proton=True)
                targets_group.add(_ep_row)

            _add_ep_row = Adw.ActionRow(title="Add Launch Target\u2026")
            _add_ep_row.set_sensitive(project.initialized)
            _add_ep_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
            _add_ep_btn.add_css_class("suggested-action")
            _add_ep_btn.connect("clicked", self._on_add_entry_point_clicked)
            _add_ep_row.add_suffix(_add_ep_btn)
            _add_ep_row.set_activatable_widget(_add_ep_btn)
            targets_group.add(_add_ep_row)

            page.add(targets_group)

        # ── 5b. Files + Launch Targets (Linux / DOS) ──────────────────────
        elif project.project_type in ("linux", "dos"):
            _linux_ready = bool(project.source_dir) and Path(project.source_dir).is_dir()
            _is_installer_project = bool(project.installer_type)
            _is_dos = project.project_type == "dos"

            _is_scummvm = _is_dos and project.engine == "scummvm"

            files_group = Adw.PreferencesGroup(title="Files")

            if _is_scummvm:
                # ScummVM: browse content only
                browse_row = Adw.ActionRow(
                    title="Browse Content",
                    subtitle=str(project.content_path),
                )
                browse_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
                browse_btn.connect("clicked",
                                   lambda *_: self._on_browse_prefix_clicked(None))
                browse_row.add_suffix(browse_btn)
                files_group.add(browse_row)
                page.add(files_group)
            elif _linux_ready and project.source_dir:
                if _is_dos:
                    # DOS: drop zone + browse content (matches Proton layout)
                    drop_zone = self._build_installer_drop_zone(
                        hint="Drop disc images or folders here",
                        subtitle="Images will be mounted in DOSBox",
                        on_browse_file=self._on_dos_browse_file,
                        on_browse_folder=self._on_browse_dlc_folder_clicked,
                        show_browse_content=True,
                        on_browse_content=self._on_browse_prefix_clicked,
                    )
                    files_group.add(drop_zone)
                else:
                    # Linux: drop zone + browse content (matches Proton/DOS layout)
                    drop_zone = self._build_installer_drop_zone(
                        hint="Drop executables or folders here",
                        on_browse_file=self._on_run_linux_installer_clicked,
                        on_browse_folder=self._on_browse_dlc_folder_clicked,
                        show_browse_content=True,
                        on_browse_content=self._on_browse_prefix_clicked,
                    )
                    files_group.add(drop_zone)

                page.add(files_group)

            elif _is_installer_project and project.installer_path:
                # First-time: prefilled installer from smart import
                run_installer_row = Adw.ActionRow(
                    title="Run Installer",
                    subtitle=Path(project.installer_path).name,
                )
                run_btn = Gtk.Button(label="Launch")
                run_btn.set_valign(Gtk.Align.CENTER)
                run_btn.add_css_class("suggested-action")
                run_btn.connect("clicked", self._on_rerun_isolated_installer)
                run_installer_row.add_suffix(run_btn)
                files_group.add(run_installer_row)
                page.add(files_group)

            else:
                # Not initialized — drop zone for initial import
                if _is_dos:
                    hint = "Drop CD/floppy images or folders here"
                    subtitle = ""
                    on_file = self._on_dos_browse_file
                else:
                    hint = "Drop executables or folders here"
                    subtitle = "Installers are not supported"
                    on_file = self._on_linux_browse_file
                drop_zone = self._build_installer_drop_zone(
                    hint=hint,
                    subtitle=subtitle,
                    on_browse_file=on_file,
                    on_browse_folder=self._on_choose_source_dir_clicked,
                )
                files_group.add(drop_zone)
                page.add(files_group)

            # DOSBox Staging section (DOS only, hidden after ScummVM conversion)
            if project.project_type == "dos" and project.engine != "scummvm":
                dos_group = Adw.PreferencesGroup(title="DOSBox Staging")

                # DOSBox Settings — only when source_dir is set
                if project.source_dir:
                    _settings_row = Adw.ActionRow(
                        title="DOSBox Settings",
                        subtitle="Display, CPU, sound, MIDI, mixer effects, and config files",
                        activatable=True,
                    )
                    _settings_btn = Gtk.Button(label="Open", valign=Gtk.Align.CENTER)
                    _settings_btn.connect("clicked", self._on_dosbox_settings_clicked)
                    _settings_row.add_suffix(_settings_btn)
                    _settings_row.set_activatable_widget(_settings_btn)
                    dos_group.add(_settings_row)

                # DOSBox Prompt — always available
                _has_discs = bool(
                    project.disc_images or project.floppy_images
                )
                _needs_install = _has_discs and not project.entry_points

                prompt_row = Adw.ActionRow(
                    title="Open DOSBox Prompt",
                    subtitle="Launch DOSBox with drives mounted (C: and any disc images)",
                )
                prompt_btn = Gtk.Button(label="Open")
                prompt_btn.set_valign(Gtk.Align.CENTER)
                if _needs_install:
                    prompt_btn.add_css_class("suggested-action")
                prompt_btn.connect("clicked", self._on_open_dosbox_prompt_clicked)
                prompt_row.add_suffix(prompt_btn)
                dos_group.add(prompt_row)

                page.add(dos_group)

            # Launch Targets (Linux / DOS — hidden for ScummVM)
            if _is_scummvm:
                scummvm_group = Adw.PreferencesGroup(title="ScummVM")
                scummvm_row = Adw.ActionRow(
                    title="Engine: ScummVM",
                    subtitle=f"Game ID: {project.scummvm_id or 'detected at runtime'}",
                )
                scummvm_group.add(scummvm_row)
                page.add(scummvm_group)
            else:
                targets_group = Adw.PreferencesGroup(title="Launch Targets")
                for _ep in project.entry_points:
                    _ep_row = self._build_target_expander_row(_ep, is_proton=False)
                    targets_group.add(_ep_row)

                _add_ep_row = Adw.ActionRow(title="Add Launch Target\u2026")
                _add_ep_row.set_sensitive(_linux_ready)
                _add_ep_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
                _add_ep_btn.add_css_class("suggested-action")
                _add_ep_btn.connect("clicked", self._on_add_entry_point_clicked)
                _add_ep_row.add_suffix(_add_ep_btn)
                _add_ep_row.set_activatable_widget(_add_ep_btn)
                targets_group.add(_add_ep_row)

                page.add(targets_group)

        # ── 6. Dependencies (Windows / Base only) ─────────────────────────
        if project.project_type not in ("linux", "dos"):
            dep_group = Adw.PreferencesGroup(title="Dependencies")
            for verb in project.deps_installed:
                row = Adw.ActionRow(title=verb)
                dep_group.add(row)

            add_dep_row = Adw.ActionRow(title="Add Dependencies\u2026")
            add_dep_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
            add_dep_btn.add_css_class("suggested-action")
            add_dep_btn.connect("clicked", self._on_add_dep_clicked)
            add_dep_row.add_suffix(add_dep_btn)
            add_dep_row.set_activatable_widget(add_dep_btn)
            add_dep_row.set_sensitive(project.initialized)
            dep_group.add(add_dep_row)
            page.add(dep_group)

        # ── 7. Publish section ────────────────────────────────────────────
        # Browse Prefix for base projects (app projects use the drop-zone browse button)
        if project.project_type == "base":
            base_files_group = Adw.PreferencesGroup(title="Files")
            _browse_row = Adw.ActionRow(
                title="Browse Prefix",
                subtitle="Open drive_c in the file manager",
            )
            _browse_row.set_sensitive(project.initialized)
            _browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
            _browse_btn.set_valign(Gtk.Align.CENTER)
            _browse_btn.add_css_class("flat")
            _browse_btn.connect("clicked", self._on_browse_prefix_clicked)
            _browse_row.add_suffix(_browse_btn)
            base_files_group.add(_browse_row)

            page.add(base_files_group)

        pkg_group = Adw.PreferencesGroup(title="Publish")

        if project.project_type in ("app", "linux", "dos"):
            _ready = (
                bool(project.source_dir) and Path(project.source_dir).is_dir()
                if project.project_type in ("linux", "dos")
                else project.initialized
            )

            # Build a list of missing prerequisites for informative subtitles
            _missing: list[str] = []
            if not _ready:
                _missing.append("initialize prefix" if project.project_type not in ("linux", "dos")
                                else "set source folder")
            if not project.entry_points:
                _missing.append("add a launch target")
            if not project.category:
                _missing.append("set a category")

            # ── Launch options (Windows apps only, saved to project) ──
            if project.project_type not in ("linux", "dos"):
                launch_group = Adw.PreferencesGroup(
                    title="Launch Options",
                    description="Saved to the package as recommended defaults",
                )

                self._dxvk_row = Adw.SwitchRow(
                    title="DXVK",
                    subtitle="Translate D3D9/10/11 via Vulkan (disable for WineD3D/OpenGL)",
                    active=project.dxvk,
                )
                self._dxvk_row.connect("notify::active", self._on_launch_opt_toggled, "dxvk")
                launch_group.add(self._dxvk_row)

                self._vkd3d_row = Adw.SwitchRow(
                    title="VKD3D",
                    subtitle="Translate D3D12 via Vulkan",
                    active=project.vkd3d,
                )
                self._vkd3d_row.connect("notify::active", self._on_launch_opt_toggled, "vkd3d")
                launch_group.add(self._vkd3d_row)

                _audio_choices = ["auto", "pulseaudio", "alsa", "oss"]
                _audio_model = Gtk.StringList.new(_audio_choices)
                self._audio_row = Adw.ComboRow(
                    title="Audio Driver",
                    subtitle="Wine audio backend",
                    model=_audio_model,
                )
                _cur_audio = (project.audio_driver
                              if project.audio_driver in _audio_choices
                              else "auto")
                self._audio_row.set_selected(_audio_choices.index(_cur_audio))
                self._audio_row.connect("notify::selected", self._on_audio_driver_changed)
                launch_group.add(self._audio_row)

                self._debug_row = Adw.SwitchRow(
                    title="Proton Debug Logging",
                    subtitle="Enable PROTON_LOG=1 when launching",
                    active=project.debug,
                )
                self._debug_row.connect("notify::active", self._on_launch_opt_toggled, "debug")
                launch_group.add(self._debug_row)

                self._direct_proton_row = Adw.SwitchRow(
                    title="Direct Proton Launch",
                    subtitle="Bypass umu-run and call Proton directly",
                    active=project.direct_proton,
                )
                self._direct_proton_row.connect(
                    "notify::active", self._on_launch_opt_toggled, "direct_proton",
                )
                launch_group.add(self._direct_proton_row)

                self._no_lsteamclient_row = Adw.SwitchRow(
                    title="Disable Steam Client Shim",
                    subtitle="Disable Proton's built-in lsteamclient.dll",
                    active=project.no_lsteamclient,
                )
                self._no_lsteamclient_row.connect(
                    "notify::active", self._on_launch_opt_toggled, "no_lsteamclient",
                )
                launch_group.add(self._no_lsteamclient_row)

                self._lock_runner_row = Adw.SwitchRow(
                    title="Lock Runner",
                    subtitle="Prevent end-users from overriding the runner",
                    active=project.lock_runner,
                )
                self._lock_runner_row.connect(
                    "notify::active", self._on_launch_opt_toggled, "lock_runner",
                )
                launch_group.add(self._lock_runner_row)

                page.add(launch_group)
            else:
                self._dxvk_row = None
                self._vkd3d_row = None
                self._audio_row = None
                self._debug_row = None
                self._direct_proton_row = None
                self._no_lsteamclient_row = None
                self._lock_runner_row = None

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

                _pub_subtitle = (
                    "Needs: " + ", ".join(_missing) if _missing
                    else "Re-archive and replace the catalogue entry"
                )
                pub_row = Adw.ActionRow(
                    title="Publish Update",
                    subtitle=_pub_subtitle,
                )
                pub_btn = Gtk.Button(label="Publish\u2026")
                pub_btn.set_valign(Gtk.Align.CENTER)
                pub_btn.add_css_class("suggested-action")
                pub_btn.connect("clicked", self._on_publish_app_clicked)
                pub_row.add_suffix(pub_btn)
                pkg_group.add(pub_row)
            else:
                _pub_subtitle = (
                    "Needs: " + ", ".join(_missing) if _missing
                    else "Archive and upload to repository"
                )
                publish_row = Adw.ActionRow(
                    title="Publish App",
                    subtitle=_pub_subtitle,
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
                subtitle="Archive prefix and runner, and upload to repository",
            )
            publish_row.set_sensitive(project.initialized)
            pub_btn = Gtk.Button(label="Publish\u2026")
            pub_btn.set_valign(Gtk.Align.CENTER)
            pub_btn.add_css_class("suggested-action")
            pub_btn.connect("clicked", self._on_publish_base_clicked)
            publish_row.add_suffix(pub_btn)
            pkg_group.add(publish_row)

        page.add(pkg_group)

        # Push detail page onto the main navigation view.
        # Guard against use-after-destroy: if the user navigated away during a
        # long-running installer the nav view may already be finalized.
        if not self.get_realized() or self._nav_view is None:
            return
        # Save scroll position so refreshes don't jump to top.
        saved_scroll = self._get_detail_scroll_position()
        # Guard against _on_nav_popped clearing self._project during the swap.
        self._replacing_detail = True
        # Pop any existing builder detail page before pushing the new one.
        visible = self._nav_view.get_visible_page()
        if visible is not None and visible.get_tag() == "builder-detail":
            self._nav_view.pop()
        self._replacing_detail = False
        self._nav_view.push(detail_page)
        self._restore_scroll_position(saved_scroll)

    def _refresh_detail_menu(self, project: Project) -> None:
        """Build/update the gear menu for the content header bar."""
        danger_section = Gio.Menu()
        danger_section.append("Delete Project\u2026", "builder.delete")
        menu = Gio.Menu()
        menu.append_section(None, danger_section)
        self._detail_gear_btn.set_menu_model(menu)

    # ------------------------------------------------------------------
    # Signal handlers — runners (base projects)
    # ------------------------------------------------------------------

    def _populate_runner_expander(self, project: Project) -> None:
        """Populate the Runner group with radio rows for installed runners."""
        from cellar.backend import runners as _runners

        # Runners referenced by at least one published base (in the base's
        # source repo) cannot be deleted.  We scope the check per-repo so
        # that publishing the same base to a second repo doesn't lock the
        # runner on the first repo indefinitely.
        from cellar.backend.database import get_all_installed_bases

        repo_by_uri = {repo.uri: repo for repo in self._all_repos}
        runners_in_use: set[str] = set()
        for rec in get_all_installed_bases():
            base_runner = rec["runner"]
            repo_source = rec.get("repo_source") or ""
            target_repo = repo_by_uri.get(repo_source)
            if target_repo is None:
                continue
            base_entry = target_repo._bases.get(base_runner)
            if base_entry and base_entry.runner:
                runners_in_use.add(base_entry.runner)

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
            in_use = rname in runners_in_use
            del_btn.set_sensitive(not in_use)
            del_btn.set_tooltip_text(
                "Runner is used by a published base image" if in_use else "Delete runner"
            )
            del_btn.connect("clicked", self._on_delete_runner_clicked, rname)
            row.add_suffix(del_btn)

            self._sel_expander.add(row)

    def _on_runner_radio_toggled(self, check: Gtk.CheckButton, runner_name: str) -> None:
        """Select a runner for the current base project."""
        if not check.get_active() or self._project is None:
            return
        # Pre-fill the base name with the runner name if it's empty or
        # still matches the previous runner (i.e. the user hasn't customised it).
        old_runner = self._project.runner
        self._project.runner = runner_name
        if (
            not self._project.name
            or self._project.name == old_runner
            or self._project.name == "(no runner)"
        ):
            self._project.name = runner_name
            if hasattr(self, "_base_name_row"):
                self._base_name_row.set_text(runner_name)
        for r in self._project_cards:
            if r.project is self._project:
                r.refresh_label()
                break
        save_project(self._project)
        if hasattr(self, "_init_btn"):
            self._init_btn.set_sensitive(
                bool(self._project.runner) and not self._project.initialized
            )
        if hasattr(self, "_sel_active_row"):
            self._sel_active_row.set_subtitle(runner_name)

    def _on_download_runner_clicked(self, _btn) -> None:
        """Open the runner picker to download a new GE-Proton release."""
        project = self._project
        dialog = RunnerPickerDialog(
            on_installed=lambda name: self._on_runner_installed(name, project),
        )
        dialog.present(self)

    def _on_runner_installed(self, runner_name: str, project: Project | None) -> None:
        """Called after a runner finishes installing — refresh the detail panel."""
        if project is not None and self._project is project:
            if not project.runner:
                project.runner = runner_name
                if not project.name or project.name == "(no runner)":
                    project.name = runner_name
                for r in self._project_cards:
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
                for r in self._project_cards:
                    if r.project is self._project:
                        r.refresh_label()
                        break
                save_project(self._project)
            self._show_project(self._project, expand_sel=True)

    # ------------------------------------------------------------------
    # Signal handlers — base images (app projects)
    # ------------------------------------------------------------------

    def _populate_base_expander(self, project: Project) -> None:
        """Populate the Base Image expander with radio rows for installed bases.

        Sources available bases from the repos' catalogues (not the DB) and
        shows only those that are also present on disk.
        """
        from cellar.backend.base_store import is_base_installed
        from cellar.backend.database import get_all_installed_bases

        # Build install-date index so we can sort by newest last
        all_base_recs = get_all_installed_bases()  # ordered by installed_at
        install_order = {rec["runner"]: i for i, rec in enumerate(all_base_recs)}

        seen: set[str] = set()
        base_images: list[str] = []
        for repo in self._all_repos:
            for name in repo._bases:
                if name not in seen and is_base_installed(name):
                    seen.add(name)
                    base_images.append(name)
        # Sort by install date (newest last); fall back to alpha for unknowns
        base_images.sort(key=lambda n: (install_order.get(n, -1), n))

        # Toggle Download button accent: only highlight when no bases installed
        if hasattr(self, "_dl_base_btn"):
            if base_images:
                self._dl_base_btn.remove_css_class("suggested-action")
            else:
                self._dl_base_btn.add_css_class("suggested-action")

        # Base images referenced by at least one published app (in the base's
        # source repo) cannot be deleted.  Scoped per-repo so that mirroring a
        # base to a second repo doesn't prevent cleanup on the first.
        repo_by_uri = {repo.uri: repo for repo in self._all_repos}
        bases_in_use: set[str] = set()
        for rec in all_base_recs:
            base_runner = rec["runner"]
            repo_source = rec.get("repo_source") or ""
            target_repo = repo_by_uri.get(repo_source)
            if target_repo is None:
                continue
            for entry in target_repo.fetch_catalogue():
                if entry.base_image == base_runner:
                    bases_in_use.add(base_runner)
                    break

        # If no runner set yet, default to the newest installed base (last by install date)
        effective_runner = project.runner or (base_images[-1] if base_images else "")
        if effective_runner and not project.runner:
            project.runner = effective_runner
            save_project(project)
            if hasattr(self, "_sel_active_row"):
                self._sel_active_row.set_subtitle(effective_runner)

        first_check: Gtk.CheckButton | None = None
        for runner in base_images:
            row = Adw.ActionRow(title=runner)
            check = Gtk.CheckButton()
            check.set_valign(Gtk.Align.CENTER)
            if first_check is None:
                first_check = check
            else:
                check.set_group(first_check)
            check.set_active(runner == effective_runner)
            check.connect("toggled", self._on_base_radio_toggled, runner)
            row.add_prefix(check)
            row.set_activatable_widget(check)

            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.add_css_class("flat")
            in_use = runner in bases_in_use
            del_btn.set_sensitive(not in_use)
            del_btn.set_tooltip_text(
                "Base image is used by a published app" if in_use else "Delete base image"
            )
            del_btn.connect("clicked", self._on_delete_base_clicked, runner)
            row.add_suffix(del_btn)

            self._sel_expander.add(row)

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
        if hasattr(self, "_sel_active_row"):
            self._sel_active_row.set_subtitle(runner)

    def _on_download_base_clicked(self, _btn) -> None:
        """Open the base picker to download a base image from a repo."""
        project = self._project
        dialog = BasePickerDialog(
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

    def _resolve_runner_name(self, project: "Project") -> str:
        """Return the GE-Proton runner name to pass to umu for *project*.

        For base projects, ``project.runner`` is already the runner name.
        For app projects, ``project.runner`` is a base image name; look up
        the corresponding base entry to get the underlying runner name.
        """
        if project.project_type != "app":
            return project.runner
        base_name = project.runner
        for repo in self._all_repos:
            entry = repo._bases.get(base_name)
            if entry is not None:
                return entry.runner
        # Fallback — hope the base name is also a valid runner directory.
        return base_name

    def _on_init_prefix_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if project.project_type in ("linux", "dos"):
            project.content_path.mkdir(parents=True, exist_ok=True)
            self._on_init_done(project, True)
            return
        if not project.runner:
            return
        project.content_path.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label="Initializing prefix…")
        progress.present(self)

        runner_name = self._resolve_runner_name(project)

        # App projects seed from the installed base image (CoW copy)
        # instead of running init_prefix + setup_prefix — those components
        # are already in the base.  Base projects do the full init + setup.
        from cellar.backend.base_store import base_path, is_base_installed
        base_dir = (
            base_path(project.runner)
            if project.project_type == "app" and is_base_installed(project.runner)
            else None
        )

        # Look up the base builder project for its deps_installed list.
        base_project = None
        if base_dir:
            for p in load_projects():
                if p.project_type == "base" and p.name == project.runner:
                    base_project = p
                    break

        def _work():
            if base_dir and base_dir.is_dir():
                GLib.idle_add(progress.set_label, "Copying base prefix…")
                from cellar.backend.installer import _seed_from_base
                _seed_from_base(base_dir, project.content_path)
                return True

            from cellar.backend.umu import init_prefix, setup_prefix
            result = init_prefix(
                project.content_path,
                runner_name,
                steam_appid=project.steam_appid,
            )
            # umu-run "" initializes the prefix then tries to execute an
            # empty string, which Wine rejects with exit code 1.  Use the
            # presence of drive_c as the real success indicator.
            ok = result.returncode == 0 or (project.content_path / "drive_c").is_dir()
            if not ok:
                return False
            def _step(label, current, total):
                GLib.idle_add(progress.set_label, f"{label} ({current}/{total})")
            setup_prefix(
                project.content_path,
                runner_name,
                steam_appid=project.steam_appid,
                step_cb=_step,
            )
            return True

        def _finish(ok: bool) -> None:
            progress.force_close()
            if ok:
                if base_project:
                    # Carry base deps into the app project so the dependency
                    # picker shows them as already installed.
                    for verb in base_project.deps_installed:
                        if verb not in project.deps_installed:
                            project.deps_installed.append(verb)
                elif base_dir:
                    for verb in ("corefonts", "msls31", "d3dx9"):
                        if verb not in project.deps_installed:
                            project.deps_installed.append(verb)
                else:
                    for verb in ("corefonts", "msls31", "d3dx9"):
                        if verb not in project.deps_installed:
                            project.deps_installed.append(verb)
            if self.get_root() is None:
                return
            self._on_init_done(project, ok)
            if not ok:
                self._show_toast("Prefix initialization failed. Check logs.")

        def _on_err(msg: str) -> None:
            log.error("init_prefix failed: %s", msg)
            _finish(False)

        run_in_background(_work, on_done=_finish, on_error=_on_err)

    def _on_init_done(self, project: Project, ok: bool) -> None:
        if ok:
            project.initialized = True
            save_project(project)
            self._show_project(project)
            # Auto-trigger folder copy if a source_dir is pending import
            if (
                project.project_type == "app"
                and project.source_dir
                and not project.installer_path
                and Path(project.source_dir).is_dir()
            ):
                self._on_import_folder_to_prefix(None)

    # ------------------------------------------------------------------
    # Signal handlers — metadata
    # ------------------------------------------------------------------

    def _on_meta_steam_lookup(self, _btn) -> None:
        if self._project is None:
            return
        from cellar.views.steam_picker import SteamPickerDialog
        query = self._project.name
        if hasattr(self, "_meta_name_row"):
            query = self._meta_name_row.get_text().strip() or query
        picker = SteamPickerDialog(query=query, on_picked=self._apply_steam_to_meta)
        picker.present(self.get_root())

    def _apply_steam_to_meta(self, result: dict) -> None:
        if self._project is None:
            return
        p = self._project
        if result.get("name") and hasattr(self, "_meta_name_row"):
            self._meta_name_row.set_text(result["name"])
        if result.get("developer") and not p.developer:
            p.developer = result["developer"]
        if result.get("publisher") and not p.publisher:
            p.publisher = result["publisher"]
        if result.get("year") and not p.release_year:
            p.release_year = result["year"]
        if result.get("summary") and not p.summary:
            p.summary = result["summary"]
        if result.get("summary") and not p.description:
            p.description = result["summary"]
        if result.get("steam_appid") and p.steam_appid is None:
            p.steam_appid = result["steam_appid"]
        if result.get("website") and not p.website:
            p.website = result["website"]
        if result.get("genres") and not p.genres:
            p.genres = list(result["genres"])
        if result.get("category") and not p.category:
            from cellar.backend.packager import BASE_CATEGORIES as _BASE_CATS
            if result["category"] in _BASE_CATS:
                p.category = result["category"]
        save_project(p)
        self._show_project(p)

    def _on_edit_metadata_clicked(self, _btn) -> None:
        if self._project is None:
            return
        dialog = MetadataEditorDialog(
            context=ProjectContext(project=self._project),
            on_changed=lambda: self._show_project(self._project),
        )
        dialog.present(self)

    def _make_metadata_summary(self, project: Project) -> str:
        """One-line summary of filled optional metadata for the Details row subtitle."""
        parts: list[str] = []
        if project.category:
            parts.append(project.category)
        if project.developer:
            parts.append(project.developer)
        if project.release_year:
            parts.append(str(project.release_year))
        return "  ·  ".join(parts)

    # ------------------------------------------------------------------
    # Signal handlers — dependencies
    # ------------------------------------------------------------------

    def _on_add_dep_clicked(self, _btn) -> None:
        if self._project is None:
            return
        if not self._project.runner:
            what = "a base image" if self._project.project_type == "app" else "a runner"
            self._show_toast(f"Select {what} before adding dependencies.")
            return
        dialog = DependencyPickerDialog(
            project=self._project,
            on_dep_changed=lambda: self._show_project(self._project),
            runner_name=self._resolve_runner_name(self._project),
        )
        dialog.present(self)

    # ------------------------------------------------------------------
    # Signal handlers — files
    # ------------------------------------------------------------------

    def _scan_entry_points_after_install(
        self,
        project: Project,
        pre_install_exes: set[Path] | None = None,
    ) -> None:
        """Scan drive_c for exe files and auto-populate launch targets if empty.

        When *pre_install_exes* is provided (a snapshot taken before the
        installer ran), only newly created executables are used as
        candidates.  Falls back to the full list if the diff is empty.
        """
        if project.entry_points:
            return
        drive_c = project.content_path / "drive_c"
        if not drive_c.is_dir():
            return
        from cellar.backend.detect import scan_prefix_exes
        from cellar.utils.paths import to_win32_path
        all_exes = scan_prefix_exes(project.content_path)
        if not all_exes:
            return
        # Prefer only exes the installer created
        candidates = sorted(all_exes, key=lambda p: p.name.lower())
        if pre_install_exes is not None:
            new_exes = [c for c in candidates if c not in pre_install_exes]
            if new_exes:
                candidates = new_exes
        project.entry_points = [
            {
                "name": c.stem,
                "path": to_win32_path(str(c), str(drive_c)),
            }
            for c in candidates[:5]
        ]
        save_project(project)

    def _on_launch_prefilled_installer(self, _btn) -> None:
        """Launch the pre-filled installer from smart import."""
        if self._project is None or not self._project.installer_path:
            return
        if not self._project.runner:
            self._show_toast("Select a base image before running an installer.")
            return
        project = self._project
        exe_path = project.installer_path

        # Snapshot existing exes so we can detect what the installer adds
        from cellar.backend.detect import scan_prefix_exes
        pre_exes = scan_prefix_exes(project.content_path)

        def _on_installer_done(ok: bool) -> None:
            log.info("Installer exited ok=%s", ok)
            # Revert to normal "Choose…" button so user can run DLC/patches
            project.installer_path = ""

            # Check if the installed game is a DOSBox game — offer conversion
            if ok and self._check_dosbox_after_install(project):
                return  # conversion flow takes over

            self._scan_entry_points_after_install(project, pre_exes)
            save_project(project)
            if self._project is project:
                self._show_project(project)

        self._run_in_prefix_with_progress(
            project,
            exe=exe_path,
            label=f"Running {Path(exe_path).name}\u2026",
            on_done=_on_installer_done,
        )

    def _on_import_folder_to_prefix(self, _btn) -> None:
        """Copy a Windows folder into the prefix's drive_c (smart import)."""
        if self._project is None or not self._project.source_dir:
            return
        project = self._project
        src = Path(project.source_dir)
        if not src.is_dir():
            self._show_toast("Source folder no longer exists.")
            return

        dest = project.content_path / "drive_c" / src.name

        cancel = threading.Event()
        progress = ProgressDialog(
            label=f"Copying {src.name}\u2026", cancel_event=cancel,
        )
        progress.present(self)

        def _work():
            import time

            from cellar.utils.progress import fmt_stats

            dest.parent.mkdir(parents=True, exist_ok=True)

            # Try CoW copy first (near-instant on btrfs/XFS)
            try:
                result = subprocess.run(
                    ["cp", "--reflink=auto", "-a", str(src), str(dest)],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    GLib.idle_add(progress.set_fraction, 1.0)
                    GLib.idle_add(progress.set_stats, "CoW copy complete")
                    return True
            except FileNotFoundError:
                pass  # cp not available (shouldn't happen on Linux)

            # Fallback: file-by-file copy with progress
            if dest.exists():
                shutil.rmtree(dest)

            total_bytes = 0
            for dirpath, _dirs, files in os.walk(src):
                for f in files:
                    total_bytes += os.path.getsize(os.path.join(dirpath, f))

            copied_bytes = 0
            t0 = time.monotonic()
            last_ui = t0

            for dirpath, dirs, files in os.walk(src):
                if cancel.is_set():
                    raise RuntimeError("Cancelled")
                rel = os.path.relpath(dirpath, src)
                dst_dir = dest / rel if rel != "." else dest
                dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copystat(dirpath, str(dst_dir))
                for fname in files:
                    if cancel.is_set():
                        raise RuntimeError("Cancelled")
                    s = os.path.join(dirpath, fname)
                    d = dst_dir / fname
                    shutil.copy2(s, str(d))
                    copied_bytes += os.path.getsize(s)
                    now = time.monotonic()
                    if now - last_ui >= 0.1:
                        last_ui = now
                        elapsed = now - t0
                        speed = copied_bytes / elapsed if elapsed > 0 else 0
                        frac = copied_bytes / total_bytes if total_bytes else 1.0
                        stats = fmt_stats(copied_bytes, total_bytes, speed)
                        GLib.idle_add(progress.set_fraction, frac)
                        GLib.idle_add(progress.set_stats, stats)
            return True

        def _done(_ok):
            progress.force_close()

            # Check if the imported folder is a DOSBox game — offer conversion
            if self._check_dosbox_after_install(project):
                return  # conversion flow takes over

            # Detect exe candidates for entry points
            from cellar.backend.detect import scan_prefix_exes
            from cellar.utils.paths import to_win32_path
            drive_c = project.content_path / "drive_c"
            all_exes = scan_prefix_exes(project.content_path)
            if all_exes and not project.entry_points:
                candidates = sorted(all_exes, key=lambda p: p.name.lower())
                project.entry_points = [
                    {
                        "name": c.stem,
                        "path": to_win32_path(str(c), str(drive_c)),
                    }
                    for c in candidates[:5]
                ]
            project.source_dir = ""  # clear — data is now in the prefix
            save_project(project)
            self._show_project(project)
            self._show_toast(f"Copied {src.name} into prefix.")

        def _err(msg):
            progress.force_close()
            if "Cancelled" not in str(msg):
                self._show_toast(f"Copy failed: {msg}")

        run_in_background(_work, on_done=_done, on_error=_err)

    def _on_run_installer_clicked(self, _btn) -> None:
        if self._project is None or not self._project.runner:
            if self._project:
                what = "a base image" if self._project.project_type == "app" else "a runner"
                self._show_toast(f"Select {what} before running an installer.")
            return
        project = self._project
        chooser = Gtk.FileChooserNative(
            title="Select Installer",
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Run",
        )
        win = self.get_root()
        if isinstance(win, Gtk.Window):
            chooser.set_transient_for(win)
        f = Gtk.FileFilter()
        f.set_name("Windows executables")
        for ext in ("exe", "msi", "bat", "cmd", "com", "lnk"):
            f.add_pattern(f"*.{ext}")
            f.add_pattern(f"*.{ext.upper()}")
        chooser.add_filter(f)
        all_filter = Gtk.FileFilter()
        all_filter.set_name("All files")
        all_filter.add_pattern("*")
        chooser.add_filter(all_filter)
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

        # Snapshot existing exes so we can detect what the installer adds
        from cellar.backend.detect import scan_prefix_exes
        pre_exes = scan_prefix_exes(project.content_path)

        def _on_manual_installer_done(ok: bool) -> None:
            log.info("Installer exited ok=%s", ok)
            self._scan_entry_points_after_install(project, pre_exes)
            if self._project is project:
                self._show_project(project)

        self._run_in_prefix_with_progress(
            project,
            exe=exe_path,
            label=f"Running {Path(exe_path).name}\u2026",
            on_done=_on_manual_installer_done,
        )

    def _on_linux_browse_file(self, _btn) -> None:
        """Browse for a Linux binary to set as the project source."""
        if self._project is None:
            return
        project = self._project
        chooser = Gtk.FileChooserNative(
            title="Select Executable",
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Import",
        )
        chooser.connect("response", lambda c, r: self._on_linux_file_chosen(c, r, project))
        win = self.get_root()
        if isinstance(win, Gtk.Window):
            chooser.set_transient_for(win)
        chooser.show()
        self._file_chooser = chooser

    def _on_linux_file_chosen(self, chooser, response, project) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        path = Path(chooser.get_file().get_path())
        project.source_dir = str(path.parent)
        project.initialized = True
        project.entry_points = [{"name": path.stem, "path": path.name}]
        save_project(project)
        self._show_project(project)

    def _on_dos_browse_file(self, _btn) -> None:
        """Browse for disc images to import into the current DOS project."""
        if self._project is None:
            return
        project = self._project
        chooser = Gtk.FileChooserNative(
            title="Select Disc Images",
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Import",
        )
        chooser.set_select_multiple(True)
        f = Gtk.FileFilter()
        f.set_name("Disc Images")
        for pat in ("*.iso", "*.ISO", "*.cue", "*.CUE",
                    "*.img", "*.IMG", "*.ima", "*.IMA",
                    "*.vfd", "*.VFD", "*.bin", "*.BIN"):
            f.add_pattern(pat)
        all_f = Gtk.FileFilter()
        all_f.set_name("All Files")
        all_f.add_pattern("*")
        chooser.add_filter(f)
        chooser.add_filter(all_f)

        def _on_response(_chooser, response):
            if response != Gtk.ResponseType.ACCEPT:
                return
            files = _chooser.get_files()
            paths = [Path(files.get_item(i).get_path()) for i in range(files.get_n_items())]
            if not paths:
                return
            self._import_disc_to_project(project, paths)

        chooser.connect("response", _on_response)
        win = self.get_root()
        if isinstance(win, Gtk.Window):
            chooser.set_transient_for(win)
        chooser.show()
        self._file_chooser = chooser

    @staticmethod
    def _no_disc_images_body(disc_set) -> str:
        """Build error body for 'no disc images found' dialogs."""
        body = "No recognised disc image files were found."
        if disc_set.unknown:
            names = ", ".join(p.name for p in disc_set.unknown[:5])
            body += (
                f"\n\nUnsupported files: {names}"
                "\n\nSupported formats: ISO, CUE/BIN, IMG/IMA/VFD."
                "\nStandalone BIN files need a matching CUE sheet."
            )
        return body

    def _import_disc_to_project(self, project, paths: list[Path]) -> None:
        """Import disc images into an existing project.

        Delegates to ``_add_disc_images_to_project`` which handles CDDA
        track conversion and copying files into the project content
        directory.
        """
        self._add_disc_images_to_project(project, paths)

    def _add_disc_images_to_project(self, project, paths: list[Path]) -> None:
        """Add disc/floppy images to an existing DOS project.

        CD/CUE images are copied to ``content/cd/``, floppies to
        ``content/floppy/``.  If the project already has disc images
        of the same type, the reorder dialog is shown.
        """
        from cellar.backend.disc_image import group_disc_files

        disc_set = group_disc_files(paths)

        # Show reorder dialog for multi-disc sets
        all_cd = list(disc_set.isos) + [cs.cue_path for cs in disc_set.cue_bins]
        if len(all_cd) > 1 or len(disc_set.floppies) > 1:
            self._show_add_disc_order_dialog(project, disc_set)
            return

        self._do_disc_copy(project, disc_set)

    def _show_add_disc_order_dialog(self, project, disc_set) -> None:
        """Show a reorder dialog before adding multiple discs to a project."""
        from cellar.views.widgets import make_dialog_header

        dlg = Adw.Dialog(title="Disc Order", content_width=400, content_height=400)
        toolbar, _header, ok_btn = make_dialog_header(
            dlg, action_label="Continue",
        )

        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=18, margin_bottom=18, margin_start=18, margin_end=18,
        )
        desc = Gtk.Label(
            label="Confirm the disc order. Use the arrows to reorder if needed.",
            wrap=True,
        )
        desc.add_css_class("dim-label")
        box.append(desc)

        group = Adw.PreferencesGroup(title="Discs")
        all_paths: list[Path] = list(disc_set.isos)
        for cs in disc_set.cue_bins:
            all_paths.append(cs.cue_path)
        all_paths.extend(disc_set.floppies)

        rows: list[tuple[Adw.ActionRow, Path]] = []
        for p in all_paths:
            subtitle = p.suffix.upper().lstrip(".")
            row = Adw.ActionRow(title=p.name, subtitle=subtitle)

            up_btn = Gtk.Button(icon_name="go-up-symbolic", valign=Gtk.Align.CENTER)
            up_btn.add_css_class("flat")
            down_btn = Gtk.Button(icon_name="go-down-symbolic", valign=Gtk.Align.CENTER)
            down_btn.add_css_class("flat")
            row.add_suffix(up_btn)
            row.add_suffix(down_btn)
            rows.append((row, p))
            group.add(row)

            def _make_move(direction, current_row=row):
                def _move(_btn):
                    idx = next(i for i, (r, _) in enumerate(rows) if r is current_row)
                    new_idx = idx + direction
                    if 0 <= new_idx < len(rows):
                        rows[idx], rows[new_idx] = rows[new_idx], rows[idx]
                        for r, _ in rows:
                            group.remove(r)
                        for r, _ in rows:
                            group.add(r)
                return _move

            up_btn.connect("clicked", _make_move(-1))
            down_btn.connect("clicked", _make_move(1))

        box.append(group)
        toolbar.set_content(box)
        dlg.set_child(toolbar)

        def _on_ok(_btn):
            dlg.close()
            from cellar.backend.disc_image import DiscSet
            new_set = DiscSet()
            for _, p in rows:
                suffix = p.suffix.lower()
                if suffix == ".iso":
                    new_set.isos.append(p)
                elif suffix == ".cue":
                    for cs in disc_set.cue_bins:
                        if cs.cue_path == p:
                            new_set.cue_bins.append(cs)
                            break
                elif suffix in {".img", ".ima", ".vfd"}:
                    new_set.floppies.append(p)
            self._do_disc_copy(project, new_set)

        ok_btn.connect("clicked", _on_ok)
        dlg.present(self)

    def _do_disc_copy(self, project, disc_set) -> None:
        """Copy disc images into a project in a background thread."""
        from cellar.backend.project import save_project
        from cellar.utils.async_work import run_in_background

        progress = ProgressDialog("Copying disc images\u2026")
        progress.present(self)

        def _work():
            content = project.content_path
            content.mkdir(parents=True, exist_ok=True)

            # Copy CD images to content/cd/
            cd_dir = content / "cd"
            new_cd: list[Path] = []
            if disc_set.isos or disc_set.cue_bins:
                from cellar.backend.disc_image import convert_cdda_tracks, has_cdda_tools

                cd_dir.mkdir(parents=True, exist_ok=True)
                for iso in disc_set.isos:
                    dest = cd_dir / iso.name
                    shutil.copy2(iso, dest)
                    new_cd.append(dest)
                for cue_sheet in disc_set.cue_bins:
                    if cue_sheet.has_audio and has_cdda_tools():
                        new_cue = convert_cdda_tracks(cue_sheet.cue_path, cd_dir)
                        new_cd.append(new_cue)
                    else:
                        dest_cue = cd_dir / cue_sheet.cue_path.name
                        shutil.copy2(cue_sheet.cue_path, dest_cue)
                        for bin_path in cue_sheet.bin_files:
                            if bin_path.is_file():
                                shutil.copy2(bin_path, cd_dir / bin_path.name)
                        new_cd.append(dest_cue)

            # Copy floppy images to content/floppy/
            floppy_added = 0
            if disc_set.floppies:
                floppy_dir = content / "floppy"
                floppy_dir.mkdir(parents=True, exist_ok=True)
                existing_floppy = list(project.floppy_images or [])
                for fp in disc_set.floppies:
                    shutil.copy2(fp, floppy_dir / fp.name)
                    rel = str((floppy_dir / fp.name).relative_to(content))
                    if rel not in existing_floppy:
                        existing_floppy.append(rel)
                        floppy_added += 1
                project.floppy_images = existing_floppy

            # Update project disc_images list
            existing_cd = list(project.disc_images or [])
            cd_added = 0
            for p in new_cd:
                rel = str(p.relative_to(content))
                if rel not in existing_cd:
                    existing_cd.append(rel)
                    cd_added += 1
            project.disc_images = existing_cd
            save_project(project)
            return cd_added + floppy_added, bool(new_cd)

        def _done(result):
            progress.force_close()
            count, has_cd = result
            kind = "disc" if has_cd else "floppy"
            self._show_toast(f"{count} {kind} image{'s' if count != 1 else ''} added.")
            self._show_project(project)

        def _error(msg):
            progress.force_close()
            err = Adw.AlertDialog(heading="Import Failed", body=str(msg))
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, _done, _error)

    def _import_dos_folder_to_hdd(self, project, src_path: Path) -> None:
        """Copy a dropped folder into content/hdd/<folder> for DOSBox C:\\ access."""
        from cellar.backend.project import save_project
        from cellar.utils.async_work import run_in_background

        content = project.content_path
        content.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label="Copying folder\u2026")
        progress.present(self)

        def _work():
            hdd_dir = content / "hdd"
            hdd_dir.mkdir(parents=True, exist_ok=True)
            dest = hdd_dir / src_path.name
            dest.mkdir(parents=True, exist_ok=True)
            for src in src_path.rglob("*"):
                rel = src.relative_to(src_path)
                dst = dest / rel
                if src.is_dir():
                    dst.mkdir(parents=True, exist_ok=True)
                elif src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

        def _done(_result):
            progress.force_close()
            if not project.initialized:
                project.source_dir = str(content)
                project.initialized = True
            save_project(project)
            self._show_toast(f"Folder '{src_path.name}' added to C:\\")
            self._show_project(project)

        def _error(msg):
            progress.force_close()
            log.error("DOS folder import failed: %s", msg)
            err = Adw.AlertDialog(
                heading="Import failed",
                body=f"Could not import folder:\n{msg}",
            )
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(work=_work, on_done=_done, on_error=_error)

    def _on_choose_source_dir_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        chooser = Gtk.FileChooserNative(
            title="Select Installation Folder",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="Select",
        )
        win = self.get_root()
        if isinstance(win, Gtk.Window):
            chooser.set_transient_for(win)
        if project.source_dir and Path(project.source_dir).parent.is_dir():
            chooser.set_current_folder(
                Gio.File.new_for_path(str(Path(project.source_dir).parent))
            )
        chooser.connect("response", lambda c, r: self._on_source_dir_chosen(c, r, project))
        chooser.show()
        self._source_dir_chooser = chooser

    def _on_source_dir_chosen(
        self, chooser: Gtk.FileChooserNative, response: int, project: Project
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        path = chooser.get_file().get_path()
        project.source_dir = path
        save_project(project)
        self._show_project(project)

    def _on_run_linux_installer_clicked(self, _btn) -> None:
        """Open a file chooser for a Linux installer to run in bwrap sandbox."""
        if self._project is None:
            return
        project = self._project
        chooser = Gtk.FileChooserNative(
            title="Select Linux Installer",
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Run",
        )
        win = self.get_root()
        if isinstance(win, Gtk.Window):
            chooser.set_transient_for(win)
        f = Gtk.FileFilter()
        f.set_name("Linux installers")
        for ext in ("sh", "run"):
            f.add_pattern(f"*.{ext}")
            f.add_pattern(f"*.{ext.upper()}")
        chooser.add_filter(f)
        all_filter = Gtk.FileFilter()
        all_filter.set_name("All files")
        all_filter.add_pattern("*")
        chooser.add_filter(all_filter)
        chooser.connect(
            "response",
            lambda c, r: self._on_linux_installer_chosen(c, r, project),
        )
        chooser.show()
        self._linux_installer_chooser = chooser

    def _on_linux_installer_chosen(
        self, chooser: Gtk.FileChooserNative, response: int, project: Project,
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        src_path = Path(chooser.get_file().get_path())

        # Route GOG installers through ZIP extraction, others through bwrap
        from cellar.utils.gog import is_gog_installer

        if is_gog_installer(src_path):
            self._extract_gog_dlc(project, src_path)
        else:
            self._run_isolated_installer(project, src_path)

    def _on_browse_dlc_folder_clicked(self, _btn) -> None:
        """Open a folder chooser to install all DLC from a directory."""
        if self._project is None:
            return
        project = self._project
        chooser = Gtk.FileChooserNative(
            title="Select DLC Folder",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="Open",
        )
        win = self.get_root()
        if isinstance(win, Gtk.Window):
            chooser.set_transient_for(win)
        chooser.connect(
            "response",
            lambda c, r: self._on_dlc_folder_chosen(c, r, project),
        )
        chooser.show()
        self._dlc_folder_chooser = chooser

    def _on_dlc_folder_chosen(
        self, chooser: Gtk.FileChooserNative, response: int, project: Project,
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        folder = Path(chooser.get_file().get_path())
        self._install_dlc_from_folder(project, folder)

    def _on_rerun_isolated_installer(self, _btn) -> None:
        """Re-run the stored installer path in a bwrap sandbox."""
        if self._project is None or not self._project.installer_path:
            return
        src_path = Path(self._project.installer_path)
        if not src_path.is_file():
            self._show_toast("Installer file no longer exists.")
            return
        self._run_isolated_installer(self._project, src_path)

    def _run_isolated_installer(self, project: Project, src_path: Path) -> None:
        """Run a Linux installer in a bwrap sandbox.

        The installer runs interactively — the user sees and interacts with
        its GUI (MojoSetup, etc.).  Same flow as running a Windows installer
        in a WINEPREFIX: run, wait for exit, scan for results.
        """
        from cellar.backend.detect import find_linux_executables
        from cellar.backend.sandbox import (
            cleanup_captured_install,
            run_isolated_installer,
        )

        content = project.content_path
        content.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label=f"Running {src_path.name}\u2026")
        progress.present(self.get_root())

        def _work():
            exit_code = run_isolated_installer(src_path, content)
            if exit_code != 0:
                log.warning("Isolated installer exited with code %d", exit_code)
            root = cleanup_captured_install(content)
            return find_linux_executables(root)

        def _done(candidates):
            progress.force_close()
            root = content / "root"
            effective = root if root.is_dir() else content
            project.source_dir = str(effective)
            project.initialized = True
            if candidates:
                base = Path(project.source_dir)
                project.entry_points = [
                    {
                        "name": project.name if c.name == "start.sh" else c.name,
                        "path": str(c.relative_to(base)),
                    }
                    for c in candidates[:5]
                ]
            # Clear installer_path so the button reverts to "Choose…" for DLC/patches
            project.installer_path = ""
            save_project(project)
            if self._project is project:
                self._show_project(project)
            self._show_toast(f"Installer finished — {len(candidates)} executable(s) found.")

        def _error(msg):
            progress.force_close()
            log.error("Isolated installer failed: %s", msg)
            self._show_toast(f"Installer failed: {msg}")

        run_in_background(work=_work, on_done=_done, on_error=_error)

    # ── Installer drop zone (shared by Windows + Linux detail views) ──

    _DROP_ZONE_CSS = b"""
.installer-drop-zone {
    border: 2px dashed alpha(@borders, 0.8);
    border-radius: 12px;
    min-height: 110px;
}
.installer-drop-zone.drag-hover {
    border-color: @accent_color;
    background-color: alpha(@accent_color, 0.08);
}
"""
    _drop_zone_css_loaded = False

    def _build_installer_drop_zone(
        self,
        hint: str,
        on_browse_file: Callable | None,
        on_browse_folder: Callable,
        *,
        subtitle: str = "",
        show_browse_content: bool = False,
        on_browse_content: Callable | None = None,
    ) -> Gtk.Box:
        """Build a drop zone + browse buttons for additional installers (DLC, patches).

        Returns a ``Gtk.Box`` containing a dashed-border drop frame (matching
        the New Project dialog's style) and Browse File / Browse Folder pill
        buttons underneath.

        Dropped files/folders are dispatched to the appropriate installer flow
        based on the current project type.
        """
        if not PackageBuilderView._drop_zone_css_loaded:
            provider = Gtk.CssProvider()
            provider.load_from_data(self._DROP_ZONE_CSS)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
            PackageBuilderView._drop_zone_css_loaded = True

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)

        # ── Drop zone frame ──────────────────────────────────────────
        frame = Gtk.Frame()
        frame.add_css_class("installer-drop-zone")

        drop_inner = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            margin_top=18,
            margin_bottom=18,
            margin_start=12,
            margin_end=12,
        )

        icon = Gtk.Image.new_from_icon_name("document-open-symbolic")
        icon.set_pixel_size(36)
        icon.add_css_class("dim-label")
        drop_inner.append(icon)

        heading = Gtk.Label(label=hint)
        heading.add_css_class("heading")
        drop_inner.append(heading)

        caption = Gtk.Label(label=subtitle)
        caption.add_css_class("dim-label")
        caption.add_css_class("caption")
        caption.set_visible(bool(subtitle))
        drop_inner.append(caption)

        frame.set_child(drop_inner)
        outer.append(frame)

        # Drag-and-drop
        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_installer_drop)
        drop.connect(
            "enter",
            lambda t, *_: frame.add_css_class("drag-hover") or Gdk.DragAction.COPY,
        )
        drop.connect("leave", lambda *_: frame.remove_css_class("drag-hover"))
        frame.add_controller(drop)

        # ── Browse buttons ───────────────────────────────────────────
        browse_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            homogeneous=True,
        )
        file_btn = Gtk.Button(label="Browse File\u2026")
        file_btn.add_css_class("pill")
        if on_browse_file:
            file_btn.connect("clicked", on_browse_file)
        else:
            file_btn.set_visible(False)
        folder_btn = Gtk.Button(label="Browse Folder\u2026")
        folder_btn.add_css_class("pill")
        folder_btn.connect("clicked", on_browse_folder)
        browse_box.append(file_btn)
        browse_box.append(folder_btn)
        outer.append(browse_box)

        # Optional "Browse Content" row below the buttons
        if show_browse_content and on_browse_content is not None:
            content_btn = Gtk.Button()
            content_btn.add_css_class("flat")
            btn_box = Gtk.Box(
                orientation=Gtk.Orientation.HORIZONTAL,
                spacing=6,
                halign=Gtk.Align.CENTER,
            )
            btn_box.append(Gtk.Image.new_from_icon_name("folder-open-symbolic"))
            btn_box.append(Gtk.Label(label="Browse Content"))
            content_btn.set_child(btn_box)
            content_btn.connect("clicked", on_browse_content)
            outer.append(content_btn)

        return outer

    def _on_installer_drop(self, _target, value, _x, _y) -> bool:
        """Handle file or folder drops onto the installer drop zone."""
        if self._project is None:
            return False
        gfiles = value.get_files() if hasattr(value, "get_files") else []
        if not gfiles:
            return False

        paths = [Path(f.get_path()) for f in gfiles]
        project = self._project

        # DOS projects: route disc images and folders containing them
        if project.project_type == "dos":
            disc_paths = _NewProjectDialog._collect_disc_images(paths)
            if disc_paths:
                self._add_disc_images_to_project(project, disc_paths)
                return True
            # Folder without disc images: copy into hdd/ as subfolder
            if len(paths) == 1 and paths[0].is_dir():
                self._import_dos_folder_to_hdd(project, paths[0])
                return True
            return True

        # Single folder drop → scan the folder for installers
        if len(paths) == 1 and paths[0].is_dir():
            self._install_dlc_from_folder(project, paths[0])
            return True

        # Single file → install directly
        installers = [p for p in paths if p.is_file()]
        if len(installers) == 1:
            self._install_single_dlc(project, installers[0])
            return True

        # Multiple files → filter, confirm, and queue
        if installers:
            skipped = self._filter_already_installed(project, installers)
            remaining = [p for p in installers if p not in skipped]
            if not remaining:
                self._show_toast("All dropped installers have already been applied.")
                return True
            self._confirm_and_queue_dlc(project, remaining, len(skipped))
        return True

    def _confirm_and_queue_dlc(
        self, project: Project, installers: list[Path], skipped_count: int,
    ) -> None:
        """Show a confirmation dialog for multiple DLC installers, then queue them."""
        skip_msg = f" ({skipped_count} skipped)" if skipped_count else ""
        names = "\n".join(f"  \u2022 {p.name}" for p in installers)
        dlg = Adw.AlertDialog(
            heading=f"Install {len(installers)} item(s){skip_msg}",
            body=names,
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("install", "Install All")
        dlg.set_response_appearance("install", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("install")
        dlg.set_close_response("cancel")

        def _on_response(_dlg, response):
            if response == "install":
                self._run_dlc_queue(project, list(installers))

        dlg.connect("response", _on_response)
        dlg.present(self.get_root())

    def _install_single_dlc(self, project: Project, path: Path) -> None:
        """Route a single DLC installer file to the appropriate handler."""
        if project.project_type == "app":
            # Windows project — run exe/msi in prefix
            from cellar.backend.detect import scan_prefix_exes
            pre_exes = scan_prefix_exes(project.content_path)

            def _on_done(ok: bool) -> None:
                self._scan_entry_points_after_install(project, pre_exes)
                if self._project is project:
                    self._show_project(project)

            self._run_in_prefix_with_progress(
                project, exe=str(path),
                label=f"Running {path.name}\u2026",
                on_done=_on_done,
            )
        elif project.installer_type == "isolated":
            self._run_isolated_installer(project, path)
        else:
            from cellar.utils.gog import is_gog_installer
            if is_gog_installer(path):
                self._extract_gog_dlc(project, path)
            else:
                self._run_isolated_installer(project, path)

    def _install_dlc_from_folder(self, project: Project, folder: Path) -> None:
        """Find all installers in *folder*, skip already-installed ones, and queue the rest."""
        if project.project_type == "app":
            exts = {".exe", ".msi"}
        else:
            exts = {".sh", ".run"}

        candidates = sorted(
            p for p in folder.iterdir()
            if p.is_file() and p.suffix.lower() in exts
        )

        if not candidates:
            self._show_toast(f"No installers found in {folder.name}")
            return

        skipped = self._filter_already_installed(project, candidates)
        remaining = [c for c in candidates if c not in skipped]

        if not remaining:
            self._show_toast("All installers in this folder have already been applied.")
            return

        self._confirm_and_queue_dlc(project, remaining, len(skipped))

    def _filter_already_installed(
        self, project: Project, candidates: list[Path],
    ) -> set[Path]:
        """Return the subset of *candidates* that appear to be already installed.

        For GOG Linux projects, compares installer stems against the project
        name and existing content.  For Windows projects, checks if the exe
        was the original smart-import installer.
        """
        skipped: set[Path] = set()
        # The original installer that created this project
        orig = Path(project.installer_path).name.lower() if project.installer_path else ""
        project_name = project.name.lower().replace(" ", "_")

        for c in candidates:
            name_lower = c.name.lower()
            # Skip if it's the exact same file as the original installer
            if orig and name_lower == orig:
                skipped.add(c)
                continue
            # Skip if the installer name matches the project name closely
            # (e.g. "dead_cells_1_26_0.sh" matches project "Dead Cells")
            stem = c.stem.lower()
            if project_name and len(project_name) >= 3 and stem.startswith(project_name):
                skipped.add(c)
                continue

        return skipped

    def _run_dlc_queue(self, project: Project, queue: list[Path]) -> None:
        """Install DLC files one at a time from *queue*."""
        if not queue:
            self._show_toast("All DLC installed.")
            if self._project is project:
                self._show_project(project)
            return

        current = queue.pop(0)
        remaining = queue

        # After each DLC finishes, run the next one
        if project.project_type == "app":
            from cellar.backend.detect import scan_prefix_exes
            pre_exes = scan_prefix_exes(project.content_path)

            def _on_done(ok: bool) -> None:
                self._scan_entry_points_after_install(project, pre_exes)
                save_project(project)
                self._run_dlc_queue(project, remaining)

            self._run_in_prefix_with_progress(
                project, exe=str(current),
                label=f"Running {current.name}\u2026",
                on_done=_on_done,
            )
        elif project.installer_type == "gog":
            self._extract_gog_dlc_queued(project, current, remaining)
        else:
            # bwrap — sequential (each one is interactive)
            self._run_isolated_installer(project, current)
            # Can't easily queue interactive installers — just do one at a time

    def _extract_gog_dlc_queued(
        self, project: Project, src_path: Path, remaining: list[Path],
    ) -> None:
        """Extract a GOG DLC and then continue with the remaining queue."""
        from cellar.backend.detect import find_linux_executables
        from cellar.utils.gog import extract_gog_game_data

        dest = Path(project.source_dir) if project.source_dir else project.content_path
        dest.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label=f"Extracting {src_path.name}\u2026")
        progress.present(self.get_root())

        def _work():
            last_pct = -1

            def _on_progress(extracted, total):
                nonlocal last_pct
                if total <= 0:
                    return
                pct = int(extracted * 100 / total)
                if pct != last_pct:
                    last_pct = pct
                    GLib.idle_add(progress.set_fraction, extracted / total)

            extract_gog_game_data(src_path, dest, progress_cb=_on_progress)
            return find_linux_executables(dest)

        def _done(candidates):
            progress.force_close()
            save_project(project)
            self._show_toast(f"Extracted {src_path.name}")
            self._run_dlc_queue(project, remaining)

        def _error(msg):
            progress.force_close()
            self._show_toast(f"Extraction failed: {msg}")

        run_in_background(work=_work, on_done=_done, on_error=_error)

    def _extract_gog_dlc(self, project: Project, src_path: Path) -> None:
        """Extract a GOG DLC .sh installer into the project's source dir."""
        from cellar.backend.detect import find_linux_executables
        from cellar.utils.gog import extract_gog_game_data

        dest = Path(project.source_dir) if project.source_dir else project.content_path
        dest.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label=f"Extracting {src_path.name}\u2026")
        progress.present(self.get_root())

        def _work():
            last_pct = -1

            def _on_progress(extracted, total):
                nonlocal last_pct
                if total <= 0:
                    return
                pct = int(extracted * 100 / total)
                if pct != last_pct:
                    last_pct = pct
                    GLib.idle_add(progress.set_fraction, extracted / total)

            extract_gog_game_data(src_path, dest, progress_cb=_on_progress)
            return find_linux_executables(dest)

        def _done(candidates):
            progress.force_close()
            if candidates and not project.entry_points:
                base = Path(project.source_dir) if project.source_dir else project.content_path
                project.entry_points = [
                    {
                        "name": project.name if c.name == "start.sh" else c.name,
                        "path": str(c.relative_to(base)),
                    }
                    for c in candidates[:5]
                ]
            save_project(project)
            if self._project is project:
                self._show_project(project)
            self._show_toast(f"Extracted {src_path.name}")

        def _error(msg):
            progress.force_close()
            self._show_toast(f"Extraction failed: {msg}")

        run_in_background(work=_work, on_done=_done, on_error=_error)

    def _do_test_launch_scummvm(self, project) -> None:
        """Test launch a ScummVM game from the builder."""
        from cellar.backend.scummvm import (
            build_scummvm_launch_cmd,
            is_scummvm_available,
            read_scummvm_id,
        )

        if not is_scummvm_available():
            self._show_toast("ScummVM is not installed")
            return

        game_dir = project.content_path
        scummvm_id = project.scummvm_id or read_scummvm_id(game_dir)
        if not scummvm_id:
            self._show_toast("ScummVM game ID not found")
            return

        cmd, _ = build_scummvm_launch_cmd(game_dir, scummvm_id)
        log.info("ScummVM test launch: %s", " ".join(cmd))
        try:
            subprocess.Popen(cmd, cwd=str(game_dir), start_new_session=True)
        except Exception as exc:
            log.error("ScummVM launch failed: %s", exc)
            self._show_toast(f"ScummVM launch failed: {exc}")

    def _on_browse_prefix_clicked(self, _btn) -> None:
        if self._project is None:
            return
        if self._project.project_type == "dos":
            target = self._project.content_path if self._project.source_dir else None
        elif self._project.project_type == "linux":
            target = Path(self._project.source_dir) if self._project.source_dir else None
        else:
            target = self._project.content_path / "drive_c"
            if not target.is_dir():
                target = self._project.content_path
        if not target or not target.is_dir():
            self._show_toast("Directory not set yet.")
            return
        subprocess.Popen(["xdg-open", str(target)], start_new_session=True)

    def _on_winecfg_clicked(self, _btn) -> None:
        if self._project is None or not self._project.runner:
            if self._project:
                what = "a base image" if self._project.project_type == "app" else "a runner"
                self._show_toast(f"Select {what} first.")
            return
        from cellar.backend.umu import launch_app
        launch_app(
            app_id=f"project-{self._project.slug}",
            entry_point="winecfg",
            runner_name=self._resolve_runner_name(self._project),
            steam_appid=self._project.steam_appid,
            prefix_dir=self._project.content_path,
        )

    def _on_add_entry_point_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if project.project_type in ("linux", "dos"):
            if not project.source_dir:
                self._show_toast("Choose a source folder first.")
                return
            content_path = Path(project.source_dir)
            platform = project.project_type
        else:
            content_path = project.content_path
            platform = "windows"
        dialog = AddLaunchTargetDialog(
            content_path=content_path,
            platform=platform,
            on_added=lambda ep: self._on_entry_point_added(project, ep),
        )
        dialog.present(self)

    def _on_entry_point_added(self, project: Project, ep: dict) -> None:
        project.entry_points.append(ep)
        save_project(project)
        self._show_project(project)

    def _build_target_expander_row(self, ep: dict, *, is_proton: bool) -> Adw.ExpanderRow:
        """Build an expandable launch-target row matching the metadata editor style."""
        name = ep.get("name", "")
        path = ep.get("path", "")

        row = Adw.ExpanderRow(title=GLib.markup_escape_text(name) if name else "Unnamed")
        row.set_subtitle(GLib.markup_escape_text(path) if path else "Not set")
        row.set_subtitle_lines(1)

        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.connect("clicked", self._on_remove_entry_point_clicked, ep)
        row.add_suffix(del_btn)

        name_entry = Adw.EntryRow(title="Name")
        name_entry.set_text(name)
        name_entry.connect("changed", self._on_ep_field_changed, ep, "name", row)
        row.add_row(name_entry)

        args_entry = Adw.EntryRow(title="Arguments")
        args_entry.set_text(ep.get("args", ""))
        args_entry.connect("changed", self._on_ep_field_changed, ep, "args", None)
        row.add_row(args_entry)

        env_entry = Adw.EntryRow(title="Environment")
        env_entry.set_text(ep.get("env", ""))
        env_entry.set_tooltip_text(
            "Environment variables. Paste Steam launch options directly, e.g. "
            "PROTON_USE_WINED3D=1 PROTON_NO_ESYNC=1 %command% \u2014 "
            "%command% and unrecognised tokens are ignored automatically."
        )
        env_entry.connect("changed", self._on_ep_field_changed, ep, "env", None)
        row.add_row(env_entry)

        if is_proton:
            admin_row = Adw.SwitchRow(
                title="Run as Administrator",
                subtitle="Set Wine to run this executable with admin privileges",
            )
            admin_row.set_active(ep.get("run_as_admin", False))
            admin_row.connect("notify::active", self._on_ep_admin_changed, ep)
            row.add_row(admin_row)

        return row

    def _on_ep_field_changed(
        self, entry: Adw.EntryRow, ep: dict, field: str,
        parent_row: Adw.ExpanderRow | None,
    ) -> None:
        if self._project is None:
            return
        text = entry.get_text().strip()
        if text:
            ep[field] = text
        else:
            ep.pop(field, None)
        if parent_row is not None and field == "name":
            parent_row.set_title(GLib.markup_escape_text(text) if text else "Unnamed")
        save_project(self._project)

    def _on_ep_admin_changed(self, switch: Adw.SwitchRow, _pspec, ep: dict) -> None:
        if self._project is None:
            return
        if switch.get_active():
            ep["run_as_admin"] = True
        else:
            ep.pop("run_as_admin", None)
        save_project(self._project)

    # ── Launch-option handlers ────────────────────────────────────────────

    def _on_launch_opt_toggled(self, switch: Adw.SwitchRow, _pspec, field: str) -> None:
        if self._project is None:
            return
        setattr(self._project, field, switch.get_active())
        save_project(self._project)

    def _on_audio_driver_changed(self, combo: Adw.ComboRow, _pspec) -> None:
        if self._project is None:
            return
        choices = ["auto", "pulseaudio", "alsa", "oss"]
        idx = combo.get_selected()
        self._project.audio_driver = choices[idx] if idx < len(choices) else "auto"
        save_project(self._project)

    def _on_remove_entry_point_clicked(self, _btn, ep: dict) -> None:
        if self._project is None:
            return
        try:
            self._project.entry_points.remove(ep)
        except ValueError:
            return
        save_project(self._project)
        self._show_project(self._project)

    # ------------------------------------------------------------------
    # Signal handlers — package
    # ------------------------------------------------------------------

    def _on_test_launch_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project

        # ScummVM: launch directly, no entry points needed
        if project.engine == "scummvm":
            self._do_test_launch_scummvm(project)
            return

        if project.project_type not in ("linux", "dos") and not project.initialized:
            self._show_toast("Initialize the prefix before test launching.")
            return
        if not project.entry_points:
            self._show_toast("Add a launch target before test launching.")
            return
        if len(project.entry_points) == 1:
            self._do_test_launch(project, project.entry_points[0])
            return
        # Multiple targets — let the user pick
        dialog = Adw.AlertDialog(
            heading="Select Launch Target",
            body="Choose which target to test:",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.set_response_appearance("cancel", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        for i, ep in enumerate(project.entry_points):
            dialog.add_response(str(i), ep.get("name", ep.get("path", "")))
        dialog.connect("response", self._on_launch_target_chosen, project)
        dialog.present(self)

    def _on_launch_target_chosen(self, _dialog, response: str, project) -> None:
        if response == "cancel":
            return
        try:
            idx = int(response)
        except ValueError:
            return
        if 0 <= idx < len(project.entry_points):
            self._do_test_launch(project, project.entry_points[idx])

    def _do_test_launch(self, project, ep: dict) -> None:
        entry_path = ep.get("path", "")
        entry_args = ep.get("args", "")
        if not entry_path:
            self._show_toast("Launch target has no executable path.")
            return
        if project.project_type == "dos":
            if not project.source_dir:
                self._show_toast("Set a source folder first.")
                return
            from cellar.backend.dosbox import build_dos_launch_cmd
            game_dir = Path(project.source_dir)
            dosbox_bin = game_dir / "dosbox" / "dosbox"
            if not dosbox_bin.is_file():
                self._show_toast("DOSBox Staging binary not found. Re-convert the project.")
                return
            cmd, _ = build_dos_launch_cmd(game_dir, entry_path, entry_args)
            subprocess.Popen(cmd, cwd=str(game_dir), start_new_session=True)
            return
        if project.project_type == "linux":
            if not project.source_dir:
                self._show_toast("Set a source folder first.")
                return
            exe = Path(project.source_dir) / entry_path
            if not exe.exists():
                self._show_toast(f"Executable not found: {exe}")
                return
            import shlex

            from cellar.backend.umu import is_cellar_sandboxed
            cmd = [str(exe)]
            if entry_args:
                cmd += shlex.split(entry_args)
            if is_cellar_sandboxed():
                cmd = ["flatpak-spawn", "--host"] + cmd
            subprocess.Popen(cmd, cwd=str(exe.parent), start_new_session=True)
            return
        if not project.runner:
            what = "a base image" if project.project_type == "app" else "a runner"
            self._show_toast(f"Select {what} before test launching.")
            return
        from cellar.backend.umu import dll_overrides, launch_app, proton_compat_env
        extra_env: dict[str, str] = {}
        if project.debug:
            extra_env["PROTON_LOG"] = "1"
        extra_env.update(proton_compat_env(dxvk=project.dxvk, vkd3d=project.vkd3d))
        overrides = dll_overrides(
            dxvk=project.dxvk, vkd3d=project.vkd3d,
            audio_driver=project.audio_driver,
            no_lsteamclient=project.no_lsteamclient,
        )
        if overrides:
            extra_env["WINEDLLOVERRIDES"] = overrides
        launch_app(
            app_id=f"project-{project.slug}",
            entry_point=entry_path,
            runner_name=self._resolve_runner_name(project),
            steam_appid=project.steam_appid,
            prefix_dir=project.content_path,
            launch_args=entry_args,
            extra_env=extra_env or None,
            direct_proton=project.direct_proton,
        )

    def _on_publish_app_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if self._publish_queue and self._publish_queue.is_pending(project.slug):
            self._show_toast("This project is already being published.")
            return
        if project.project_type not in ("linux", "dos") and not project.initialized:
            self._show_toast("Initialize the prefix before publishing.")
            return
        if not project.entry_point:
            self._show_toast("Add a launch target before publishing.")
            return
        if project.project_type in ("linux", "dos") and not project.source_dir:
            self._show_toast("Choose a source folder before publishing.")
            return
        if project.project_type not in ("linux", "dos") and not project.runner:
            what = "a base image" if project.project_type == "app" else "a runner"
            self._show_toast(f"Select {what} before publishing.")
            return
        if not project.category:
            self._show_toast("Set a category in Metadata before publishing.")
            return
        if not self._writable_repos:
            self._show_toast("No writable repository configured.")
            return

        if len(self._writable_repos) > 1:
            pick_repo(
                self._writable_repos,
                self,
                lambda repo: self._do_publish_app(project, repo),
            )
            return
        self._do_publish_app(project, self._writable_repos[0])

    def _do_publish_app(self, project: Project, repo) -> None:
        # Build AppEntry from project metadata.
        from cellar.models.app_entry import AppEntry

        _slug = project.slug
        _raw_icon_ext = Path(project.icon_path).suffix.lower() if project.icon_path else ".png"
        _icon_ext = ".png" if _raw_icon_ext in (".ico", ".bmp") else _raw_icon_ext
        _cover_ext = Path(project.cover_path).suffix if project.cover_path else ".jpg"
        entry = AppEntry(
            id=_slug,
            name=project.name,
            version=project.version or "1.0",
            category=project.category,
            summary=project.summary,
            description=project.description,
            developer=project.developer,
            publisher=project.publisher,
            release_year=project.release_year,
            website=project.website,
            genres=tuple(project.genres),
            steam_appid=project.steam_appid,
            icon=f"apps/{_slug}/icon{_icon_ext}" if project.icon_path else "",
            cover=f"apps/{_slug}/cover{_cover_ext}" if project.cover_path else "",
            logo=f"apps/{_slug}/logo.png" if project.logo_path else "",
            hide_title=project.hide_title,
            screenshots=tuple(
                f"apps/{_slug}/screenshots/{i + 1:02d}{Path(p).suffix}"
                for i, p in enumerate(project.screenshot_paths)
            ),
            archive=f"apps/{_slug}/{_slug}.tar.zst",
            launch_targets=tuple(project.entry_points),
            update_strategy="safe",
            platform={"linux": "linux", "dos": "dos"}.get(project.project_type, "windows"),
            engine=project.engine,
            scummvm_id=project.scummvm_id,
            dxvk=project.dxvk,
            vkd3d=project.vkd3d,
            audio_driver=project.audio_driver,
            debug=project.debug,
            direct_proton=project.direct_proton,
            no_lsteamclient=project.no_lsteamclient,
            lock_runner=project.lock_runner,
        )
        images: dict = {}
        if project.icon_path:
            images["icon"] = project.icon_path
        if project.cover_path:
            images["cover"] = project.cover_path
        if project.logo_path:
            images["logo"] = project.logo_path
        if project.screenshot_paths:
            images["screenshots"] = list(project.screenshot_paths)

        # Ask keep/delete *before* enqueueing so the user decides up-front.
        self._ask_keep_then_enqueue(project, repo, entry, images)

    def _ask_keep_then_enqueue(self, project, repo, entry, images) -> None:
        """Show keep/delete dialog, then enqueue the publish job."""
        from cellar.backend.publish_queue import PublishJob

        dlg = Adw.AlertDialog(
            heading="Keep project?",
            body=(
                "Do you want to keep the project in the builder for future "
                "updates, or delete it after publishing?"
            ),
        )
        dlg.add_response("delete", "Delete After Publish")
        dlg.add_response("keep", "Keep")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("keep")

        def _on_response(_dlg, response):
            delete_after = response == "delete"
            job = PublishJob(
                app_id=entry.id,
                app_name=project.name,
                project=project,
                repo=repo,
                all_repos=list(self._all_repos),
                entry=entry,
                images=images,
                delete_after=delete_after,
            )
            self._publish_queue.enqueue(job)
            self._show_toast(f"Publishing \u2018{project.name}\u2019 in the background\u2026")
            self._project = None
            self._reload_projects()
            self._pop_detail()

        dlg.connect("response", _on_response)
        dlg.present(self)


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

        if len(self._writable_repos) > 1:
            pick_repo(
                self._writable_repos,
                self,
                lambda repo: self._do_publish_base(project, repo),
            )
            return
        self._do_publish_base(project, self._writable_repos[0])

    def _do_publish_base(self, project: Project, repo) -> None:
        cancel_event = threading.Event()
        progress = ProgressDialog(
            label="Compressing and uploading\u2026", cancel_event=cancel_event,
        )
        progress.present(self)

        import time

        from cellar.utils.progress import fmt_size

        _prevb: list[tuple[float, int]] = []

        def _bytes_cb(n: int) -> None:
            now = time.monotonic()
            _prevb.append((now, n))
            cutoff = now - 2.0
            while _prevb and _prevb[0][0] < cutoff:
                _prevb.pop(0)
            if len(_prevb) >= 2:
                dt = _prevb[-1][0] - _prevb[0][0]
                db = _prevb[-1][1] - _prevb[0][1]
                speed = db / dt if dt > 0 else 0
                spd = f" ({fmt_size(int(speed))}/s)" if speed > 0 else ""
            else:
                spd = ""
            GLib.idle_add(progress.set_stats, fmt_size(n) + " written" + spd)

        base_name = project.name

        def _work():
            from cellar.backend.base_store import install_base_from_dir
            from cellar.backend.packager import (
                CancelledError,
                compress_prefix_zst,
                compress_runner_zst,
                upsert_base,
                upsert_runner,
            )
            from cellar.backend.umu import runners_dir

            runner = project.runner
            repo_root = repo.writable_path()
            _partial_files = []  # track files to clean up on cancel

            try:
                # ── Compress and upload the runner ────────────────────────
                runner_src = runners_dir() / runner
                runner_archive_rel = f"runners/{runner}.tar.zst"
                runner_archive_dest = repo_root / runner_archive_rel
                runner_archive_dest.parent.mkdir(parents=True, exist_ok=True)

                GLib.idle_add(progress.set_label, "Compressing and uploading runner\u2026")
                GLib.idle_add(progress.set_stats, "")
                _partial_files.append(runner_archive_dest)
                runner_size, runner_crc32, runner_chunks = compress_runner_zst(
                    runner_src,
                    runner_archive_dest,
                    cancel_event=cancel_event,
                    bytes_cb=_bytes_cb,
                )

                # ── Compress and upload the base image ────────────────────
                GLib.idle_add(progress.set_label, "Compressing and uploading base image\u2026")
                GLib.idle_add(progress.set_stats, "")
                archive_dest_rel = f"bases/{base_name}-base.tar.zst"
                archive_dest = repo_root / archive_dest_rel
                archive_dest.parent.mkdir(parents=True, exist_ok=True)

                _partial_files.append(archive_dest)
                size, crc32, base_chunks = compress_prefix_zst(
                    project.content_path,
                    archive_dest,
                    cancel_event=cancel_event,
                    bytes_cb=_bytes_cb,
                )
            except CancelledError:
                from cellar.backend.packager import _cleanup_chunks
                for f in _partial_files:
                    try:
                        _cleanup_chunks(f)
                    except Exception:
                        pass
                raise

            GLib.idle_add(progress.set_label, "Finalizing\u2026")
            GLib.idle_add(progress.set_stats, "")
            GLib.idle_add(progress.start_pulse)
            upsert_runner(
                repo_root, runner, runner_archive_rel, runner_crc32, runner_size,
                runner_chunks,
            )
            upsert_base(
                repo_root, base_name, runner, archive_dest_rel, crc32, size,
                base_chunks,
            )

            GLib.idle_add(progress.set_label, "Installing base locally\u2026")
            GLib.idle_add(progress.set_stats, "")
            install_base_from_dir(
                project.content_path,
                base_name,
                repo_source=repo.uri,
            )

        def _done(_result) -> None:
            progress.force_close()
            self._show_toast(f"Base '{base_name}' published.")
            if self._on_catalogue_changed:
                self._on_catalogue_changed()
            self._ask_keep_project(project.slug)

        def _error(msg: str) -> None:
            progress.force_close()
            if cancel_event.is_set():
                self._show_toast("Publish cancelled.")
                return
            err = Adw.AlertDialog(heading="Failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

    def _ask_keep_project(self, slug: str) -> None:
        """Ask the user whether to keep or delete the project after publishing."""
        dlg = Adw.AlertDialog(
            heading="Keep project?",
            body=(
                "The project was published successfully."
                " Do you want to keep it in the builder for future updates?"
            ),
        )
        dlg.add_response("delete", "Delete")
        dlg.add_response("keep", "Keep")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("keep")

        def _on_response(_dlg, response):
            def _after():
                self._project = None
                self._reload_projects()
                self._pop_detail()

            if response == "delete":
                run_in_background(
                    lambda: delete_project(slug),
                    on_done=lambda _r: _after(),
                )
            else:
                _after()

        dlg.connect("response", _on_response)
        dlg.present(self)

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
            what = "a base image" if project.project_type == "app" else "a runner"
            self._show_toast(f"Select {what} first.")
            return

        project.content_path.mkdir(parents=True, exist_ok=True)
        runner_name = self._resolve_runner_name(project)

        progress = ProgressDialog(label=label)
        progress.present(self)

        def _work():
            from cellar.backend.umu import run_in_prefix
            result = run_in_prefix(
                project.content_path,
                runner_name,
                exe,
                timeout=600,
            )
            return result.returncode == 0

        def _finish(ok: bool) -> None:
            progress.force_close()
            if not self.get_realized():
                return
            on_done(ok)
            if not ok:
                self._show_toast("Command exited with non-zero status. Check logs.")

        def _on_err(msg: str) -> None:
            log.error("run_in_prefix failed: %s", msg)
            _finish(False)

        run_in_background(_work, on_done=_finish, on_error=_on_err)

    def _show_toast(self, message: str) -> None:
        win = self.get_root()
        if hasattr(win, "toast_overlay"):
            win.toast_overlay.add_toast(Adw.Toast(title=message))

    def _on_dosbox_settings_clicked(self, _btn) -> None:
        """Open the DOSBox Settings dialog for the current DOS project."""
        if self._project is None or not self._project.source_dir:
            return
        from cellar.views.dosbox_settings import DosboxSettingsDialog

        src = Path(self._project.source_dir)
        DosboxSettingsDialog(
            config_dir=src / "config",
            assets_dir=src / "assets",
            on_saved=lambda: self._show_project(self._project) if self._project else None,
        ).present(self)

    def _on_open_dosbox_prompt_clicked(self, _btn) -> None:
        """Launch DOSBox with drives mounted but no auto-run — just a prompt."""
        if self._project is None:
            return
        project = self._project
        content = project.content_path
        content.mkdir(parents=True, exist_ok=True)

        disc_image_paths = [
            content / p for p in project.disc_images
        ] if project.disc_images else []

        floppy_paths = [
            content / p for p in project.floppy_images
        ] if project.floppy_images else []

        _launch_dos_installer(
            self, project, content, disc_image_paths, floppy_paths,
        )

    # ── DOS config helpers ─────────────────────────────────────────

    def _on_dosbox_fullscreen_toggled(self, row, _pspec) -> None:
        if self._project is None or not self._project.source_dir:
            return
        from cellar.backend.dosbox import write_override
        conf = Path(self._project.source_dir) / "config" / "dosbox-overrides.conf"
        write_override(conf, "sdl", "fullscreen",
                       "true" if row.get_active() else "false")

    # ── DOS audio asset management ──────────────────────────────────

    def _on_add_dos_asset_clicked(self, _btn) -> None:
        """Browse for SoundFont or MT-32 ROM files to add to the DOS project."""
        if self._project is None or self._project.project_type != "dos":
            return
        chooser = Gtk.FileChooserNative(
            title="Select SoundFont or MT-32 ROM Files",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Add",
        )
        chooser.set_select_multiple(True)

        audio_filter = Gtk.FileFilter()
        audio_filter.set_name("SoundFonts & ROMs (*.sf2, *.sf3, *.rom)")
        audio_filter.add_pattern("*.sf2")
        audio_filter.add_pattern("*.SF2")
        audio_filter.add_pattern("*.sf3")
        audio_filter.add_pattern("*.SF3")
        audio_filter.add_pattern("*.rom")
        audio_filter.add_pattern("*.ROM")
        chooser.add_filter(audio_filter)

        all_filter = Gtk.FileFilter()
        all_filter.set_name("All files")
        all_filter.add_pattern("*")
        chooser.add_filter(all_filter)

        chooser.connect("response", self._on_dos_asset_chosen, chooser)
        chooser.show()
        self._asset_chooser = chooser

    def _on_dos_asset_chosen(self, _c, response, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT or self._project is None:
            return
        project = self._project
        src_dir = Path(project.source_dir)
        files = chooser.get_files()

        added_sf = False
        added_rom = False

        for gfile in files:
            path = Path(gfile.get_path())
            suffix = path.suffix.lower()

            if suffix in (".sf2", ".sf3"):
                dest_dir = src_dir / "assets" / "soundfonts"
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest_dir / path.name)
                added_sf = True
            elif suffix == ".rom":
                dest_dir = src_dir / "assets" / "mt32-roms"
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest_dir / path.name)
                added_rom = True

        # Auto-update DOSBox overrides config
        if added_sf or added_rom:
            self._update_dosbox_audio_config(project, added_sf, added_rom)
            save_project(project)
            self._show_project(project)

        if added_sf and added_rom:
            self._show_toast("Added SoundFont and MT-32 ROMs")
        elif added_sf:
            self._show_toast("Added SoundFont")
        elif added_rom:
            self._show_toast("Added MT-32 ROMs")

    def _on_remove_dos_asset(self, _btn, project: Project, asset_path: Path) -> None:
        """Remove a single audio asset file and update config."""
        if not asset_path.is_file():
            return
        asset_path.unlink()
        # Check if any soundfonts/roms remain
        sf_dir = Path(project.source_dir) / "assets" / "soundfonts"
        rom_dir = Path(project.source_dir) / "assets" / "mt32-roms"
        has_sf = sf_dir.is_dir() and any(sf_dir.iterdir())
        has_rom = rom_dir.is_dir() and any(rom_dir.iterdir())
        self._update_dosbox_audio_config(project, has_sf, has_rom, remove_missing=True)
        save_project(project)
        self._show_project(project)
        self._show_toast(f"Removed {asset_path.name}")

    def _on_remove_dos_asset_dir(self, _btn, project: Project, dir_path: Path) -> None:
        """Remove an entire asset directory (e.g. mt32-roms) and update config."""
        if dir_path.is_dir():
            shutil.rmtree(dir_path)
        sf_dir = Path(project.source_dir) / "assets" / "soundfonts"
        has_sf = sf_dir.is_dir() and any(sf_dir.iterdir())
        self._update_dosbox_audio_config(project, has_sf, False, remove_missing=True)
        save_project(project)
        self._show_project(project)
        self._show_toast("Removed MT-32 ROMs")

    def _update_dosbox_audio_config(
        self,
        project: Project,
        has_soundfont: bool,
        has_mt32: bool,
        *,
        remove_missing: bool = False,
    ) -> None:
        """Update dosbox-overrides.conf with audio asset paths."""
        from cellar.backend.dosbox import update_audio_config
        src = Path(project.source_dir)
        update_audio_config(
            src / "config" / "dosbox-overrides.conf",
            src / "assets",
            has_soundfont,
            has_mt32,
        )

    def _open_file_in_editor(self, path: Path | None) -> None:
        """Open a file in the default text editor via xdg-open."""
        if path is None or not path.is_file():
            return
        Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)

    def _open_folder(self, path: Path | None) -> None:
        """Open a folder in the default file manager."""
        if path is None or not path.is_dir():
            return
        Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)

    # ── GOG DOSBox game detection and conversion ─────────────────────

    def _check_dosbox_after_install(self, project: Project) -> bool:
        """Check a WINEPREFIX for a GOG DOSBox game after installer/import.

        If found, prompts the user and converts the project from Windows to
        DOS.  Returns ``True`` if a DOSBox game was detected (conversion may
        be pending user confirmation), ``False`` otherwise.
        """
        if not self.get_realized():
            return False
        from cellar.backend.dosbox import detect_gog_dosbox_in_prefix

        result = detect_gog_dosbox_in_prefix(project.content_path)
        if result is None:
            return False

        game_folder, dosbox_info = result

        dlg = Adw.AlertDialog(
            heading="DOSBox game detected",
            body=(
                f'"{dosbox_info.game_name}" uses DOSBox.\n\n'
                "Convert to a native DOS package with DOSBox Staging? "
                "This avoids running DOSBox through Wine."
            ),
        )
        dlg.add_response("cancel", "Keep as Windows")
        dlg.add_response("convert", "Convert to DOS")
        dlg.set_response_appearance("convert", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("convert")
        dlg.set_close_response("cancel")

        def _on_response(_dlg, response):
            if response == "convert":
                self._convert_prefix_to_dos(project, game_folder, dosbox_info)
            else:
                # User chose to keep as Windows — proceed normally
                self._scan_entry_points_after_install(project)
                save_project(project)
                if self._project is project:
                    self._show_project(project)

        dlg.connect("response", _on_response)
        dlg.present(self)
        return True

    def _convert_prefix_to_dos(
        self, project: Project, game_folder: Path, dosbox_info,
    ) -> None:
        """Convert a Windows WINEPREFIX project to a DOS project.

        Extracts the game files from the WINEPREFIX, strips Wine artifacts,
        and runs the standard DOSBox conversion pipeline.
        """
        import tempfile

        progress = ProgressDialog(label="Converting to DOS package\u2026")
        progress.present(self)

        def _work():
            from cellar.backend.dosbox import convert_gog_dosbox

            with tempfile.TemporaryDirectory() as tmp:
                tmp_dest = Path(tmp) / "converted"
                def _on_progress(downloaded, total):
                    if total > 0:
                        GLib.idle_add(progress.set_fraction, downloaded / total)
                entry_points = convert_gog_dosbox(
                    game_folder, tmp_dest, dosbox_info, progress_cb=_on_progress,
                )

                content = project.content_path
                shutil.rmtree(content, ignore_errors=True)
                shutil.move(str(tmp_dest), str(content))

            return entry_points

        def _done(entry_points):
            progress.force_close()
            project.project_type = "dos"
            project.source_dir = str(project.content_path)
            project.initialized = True
            project.runner = ""
            if entry_points:
                project.entry_points = entry_points

                # Game files are on the HDD now — detect a profile.
                from cellar.backend.dosbox_profiles import apply_profile
                apply_profile(project.content_path)

            save_project(project)
            # Reload project list so the card icon/label updates
            self._reload_projects()
            if self._project is project:
                self._show_project(project)
            self._show_toast("Converted to DOS package")

            if entry_points:
                _check_scummvm_compat(
                    project.content_path, project, self,
                    on_refreshed=lambda: self._show_project(project),
                )

        def _error(msg):
            progress.force_close()
            log.error("DOSBox conversion failed: %s", msg)
            err = Adw.AlertDialog(
                heading="Conversion failed",
                body=f"Could not convert to DOS package:\n{msg}",
            )
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(work=_work, on_done=_done, on_error=_error)


# ---------------------------------------------------------------------------
# Helper widgets
# ---------------------------------------------------------------------------

_TYPE_ICONS = {
    "base": "package-x-generic-symbolic",
    "linux": "penguin-alt-symbolic",
    "dos": "floppy-symbolic",
    "app": "grid-large-symbolic",
}
_TYPE_LABELS = {"app": "Proton App", "linux": "Native App", "dos": "DOS App", "base": "Base Image"}

# Map internal project_type / kind values to filter-pill identifiers.
_FILTER_TYPE_PROTON = "proton"
_FILTER_TYPE_NATIVE = "native"
_FILTER_TYPE_BASE = "base"

def _resolve_filter_type(project_type: str, platform: str = "windows") -> str:
    """Return the filter-pill type id for a project type + platform combo."""
    if project_type == "base":
        return _FILTER_TYPE_BASE
    if project_type in ("linux", "dos") or platform in ("linux", "dos"):
        return _FILTER_TYPE_NATIVE
    return _FILTER_TYPE_PROTON


class _NewProjectDialog(Adw.Dialog):
    """Guided new-project chooser — smart import drop zone + manual platform selection."""

    def __init__(
        self,
        *,
        on_windows: Callable[[], None],
        on_linux: Callable[[], None],
        on_dos: Callable[[], None],
        on_base: Callable[[], None],
        on_import: Callable,
        parent_view,
    ) -> None:
        super().__init__(title="New Project", content_width=420, content_height=520)
        self._on_windows = on_windows
        self._on_linux = on_linux
        self._on_dos = on_dos
        self._on_base = on_base
        self._on_import = on_import
        self._parent_view = parent_view
        self._file_chooser = None  # prevent GC

        from cellar.views.widgets import make_dialog_header
        toolbar, _header, _action_btn = make_dialog_header(self)

        # ── Outer scrollable container ──────────────────────────────────
        scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        scroll.set_child(outer)

        # ── Drop zone frame ─────────────────────────────────────────────
        self._drop_frame = Gtk.Frame()
        self._drop_frame.add_css_class("drop-zone")

        drop_inner = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            margin_top=18,
            margin_bottom=18,
            margin_start=12,
            margin_end=12,
        )
        icon = Gtk.Image.new_from_icon_name("document-open-symbolic")
        icon.set_pixel_size(36)
        icon.add_css_class("dim-label")
        drop_inner.append(icon)

        heading = Gtk.Label(label="Drop files or folders here")
        heading.add_css_class("heading")
        drop_inner.append(heading)

        caption = Gtk.Label(label="Platform and type will be detected automatically")
        caption.add_css_class("dim-label")
        caption.add_css_class("caption")
        drop_inner.append(caption)

        self._drop_frame.set_child(drop_inner)
        outer.append(self._drop_frame)

        # Drop zone CSS
        _css = b"""
.drop-zone {
    border: 2px dashed alpha(@borders, 0.8);
    border-radius: 12px;
    min-height: 110px;
}
.drop-zone.drag-hover {
    border-color: @accent_color;
    background-color: alpha(@accent_color, 0.08);
}
"""
        _provider = Gtk.CssProvider()
        _provider.load_from_data(_css)
        self._drop_frame.get_style_context().add_provider(
            _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        drop.connect("enter", self._on_drag_enter)
        drop.connect("leave", self._on_drag_leave)
        self._drop_frame.add_controller(drop)

        # ── Browse buttons ──────────────────────────────────────────────
        browse_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            homogeneous=True,
        )
        file_btn = Gtk.Button(label="Browse File\u2026")
        file_btn.add_css_class("pill")
        file_btn.connect("clicked", self._on_browse_file)
        folder_btn = Gtk.Button(label="Browse Folder\u2026")
        folder_btn.add_css_class("pill")
        folder_btn.connect("clicked", self._on_browse_folder)
        browse_box.append(file_btn)
        browse_box.append(folder_btn)
        outer.append(browse_box)

        # ── Separator ───────────────────────────────────────────────────
        sep_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=6,
            margin_bottom=6,
        )
        sep_box.append(Gtk.Separator(hexpand=True, valign=Gtk.Align.CENTER))
        or_lbl = Gtk.Label(label="or create manually")
        or_lbl.add_css_class("dim-label")
        or_lbl.add_css_class("caption")
        sep_box.append(or_lbl)
        sep_box.append(Gtk.Separator(hexpand=True, valign=Gtk.Align.CENTER))
        outer.append(sep_box)

        # ── Manual platform group ────────────────────────────────────────
        group = Adw.PreferencesGroup()

        has_bases = any(repo._bases for repo in self._parent_view._all_repos)
        win_row = Adw.ActionRow(
            title="Proton Package",
            subtitle=(
                "App running in Proton/Wine" if has_bases
                else "No base images available — add a repo with base images first"
            ),
            activatable=has_bases,
            sensitive=has_bases,
        )
        win_row.add_prefix(Gtk.Image.new_from_icon_name("grid-large-symbolic"))
        win_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        win_row.connect("activated", self._on_windows_activated)
        group.add(win_row)

        linux_row = Adw.ActionRow(
            title="Native Package",
            subtitle="Native Linux application",
            activatable=True,
        )
        linux_row.add_prefix(Gtk.Image.new_from_icon_name("penguin-alt-symbolic"))
        linux_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        linux_row.connect("activated", self._on_linux_activated)
        group.add(linux_row)

        dos_row = Adw.ActionRow(
            title="DOS Package",
            subtitle="DOS game with DOSBox Staging",
            activatable=True,
        )
        dos_row.add_prefix(Gtk.Image.new_from_icon_name("floppy-symbolic"))
        dos_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        dos_row.connect("activated", self._on_dos_activated)
        group.add(dos_row)

        base_row = Adw.ActionRow(
            title="Base Image",
            subtitle="Reusable Wine runtime for Proton packages",
            activatable=True,
        )
        base_row.add_prefix(Gtk.Image.new_from_icon_name("package-x-generic-symbolic"))
        base_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        base_row.connect("activated", self._on_base_activated)
        group.add(base_row)

        clamp = Adw.Clamp(maximum_size=400)
        clamp.set_child(group)
        outer.append(clamp)

        toolbar.set_content(scroll)
        self.set_child(toolbar)

    # ── Drop-zone handlers ──────────────────────────────────────────────

    @staticmethod
    def _collect_disc_images(paths: list[Path]) -> list[Path]:
        """Return disc image files from *paths*, scanning directories one level deep."""
        _disc_exts = {".iso", ".cue", ".img", ".ima", ".vfd", ".bin"}
        found: list[Path] = []
        for p in paths:
            if p.is_dir():
                for child in p.iterdir():
                    if child.is_file() and child.suffix.lower() in _disc_exts:
                        found.append(child)
            elif p.is_file() and p.suffix.lower() in _disc_exts:
                found.append(p)
        return found

    def _on_drag_enter(self, _target, _x, _y) -> Gdk.DragAction:
        self._drop_frame.add_css_class("drag-hover")
        return Gdk.DragAction.COPY

    def _on_drag_leave(self, _target) -> None:
        self._drop_frame.remove_css_class("drag-hover")

    def _on_drop(self, _target, value, _x, _y) -> bool:
        self._drop_frame.remove_css_class("drag-hover")
        files = value.get_files()
        if not files:
            return False
        paths = [Path(f.get_path()) for f in files]
        disc_paths = self._collect_disc_images(paths)
        if disc_paths:
            self.close()
            self._start_disc_import(disc_paths)
        else:
            self._try_import(paths[0])
        return True

    # ── Browse handlers ─────────────────────────────────────────────────

    def _on_browse_file(self, _btn) -> None:
        chooser = Gtk.FileChooserNative(
            title="Select Installer, Executable, or Disc Image",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Import",
        )
        chooser.set_select_multiple(True)
        f = Gtk.FileFilter()
        f.set_name("Windows Executables")
        for pat in ("*.exe", "*.EXE", "*.msi", "*.MSI",
                    "*.bat", "*.BAT", "*.cmd", "*.CMD",
                    "*.com", "*.COM", "*.lnk", "*.LNK"):
            f.add_pattern(pat)
        disc_f = Gtk.FileFilter()
        disc_f.set_name("Disc Images")
        for pat in ("*.iso", "*.ISO", "*.cue", "*.CUE",
                    "*.img", "*.IMG", "*.ima", "*.IMA",
                    "*.vfd", "*.VFD", "*.bin", "*.BIN"):
            disc_f.add_pattern(pat)
        all_f = Gtk.FileFilter()
        all_f.set_name("All Files")
        all_f.add_pattern("*")
        chooser.add_filter(f)
        chooser.add_filter(disc_f)
        chooser.add_filter(all_f)
        chooser.connect("response", self._on_file_chosen, chooser)
        chooser.show()
        self._file_chooser = chooser

    def _on_browse_folder(self, _btn) -> None:
        chooser = Gtk.FileChooserNative(
            title="Select App Folder",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="Import",
        )
        chooser.connect("response", self._on_file_chosen, chooser)
        chooser.show()
        self._file_chooser = chooser

    def _on_file_chosen(self, _chooser, response: int, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        # Support multi-select for disc images
        files = chooser.get_files()
        if files is None:
            return
        paths = [Path(files.get_item(i).get_path()) for i in range(files.get_n_items())]
        if not paths:
            return
        disc_paths = self._collect_disc_images(paths)
        if disc_paths:
            self.close()
            self._start_disc_import(disc_paths)
        else:
            self._try_import(paths[0])

    # ── Import dispatch ─────────────────────────────────────────────────

    def _try_import(self, path: Path) -> None:
        """Validate the file, then close the dialog and start import.

        If the file is unsupported, show an error without closing the
        dialog so the user can try again.
        """
        from cellar.backend.detect import detect_platform, unsupported_reason

        platform = detect_platform(path)
        if platform == "unsupported":
            msg = unsupported_reason(path)
            err = Adw.AlertDialog(heading="Cannot import", body=msg)
            err.add_response("ok", "OK")
            err.present(self)
            return

        self.close()
        self._start_import(path)

    def _start_import(self, path: Path) -> None:
        """Detect platform, parse name, and open MetadataEditorDialog pre-filled."""
        from cellar.backend.detect import (
            detect_platform,
            find_gameinfo,
            parse_app_name,
            parse_version_hint,
            unsupported_reason,
        )

        platform = detect_platform(path)

        if platform == "unsupported":
            msg = unsupported_reason(path)
            err = Adw.AlertDialog(heading="Cannot import", body=msg)
            err.add_response("ok", "OK")
            err.present(self._parent_view)
            return

        if platform == "ambiguous":
            self._show_platform_picker(path)
            return

        app_name = parse_app_name(path)
        version = parse_version_hint(path)

        # Check for GOG DOSBox game — auto-convert to DOS platform
        self._dosbox_info = None
        if platform == "windows" and path.is_dir():
            from cellar.backend.dosbox import detect_gog_dosbox
            dosbox_info = detect_gog_dosbox(path)
            if dosbox_info is not None:
                platform = "dos"
                self._dosbox_info = dosbox_info
                if dosbox_info.game_name:
                    app_name = dosbox_info.game_name

        # Check for GoG gameinfo — inside folders or GOG .sh installers
        if path.is_dir():
            gi = find_gameinfo(path)
        elif path.suffix.lower() == ".sh":
            from cellar.utils.gog import read_gog_gameinfo
            gi = read_gog_gameinfo(path)
        else:
            gi = None
        if gi:
            if gi["name"]:
                app_name = gi["name"]
            if gi["version"]:
                version = gi["version"]

        self._open_metadata_editor(path, platform, app_name, version)

    def _show_platform_picker(self, path: Path) -> None:
        """Show a small dialog to disambiguate platform."""
        from cellar.backend.detect import find_gameinfo, parse_app_name, parse_version_hint

        dlg = Adw.AlertDialog(
            heading="Which platform?",
            body="Could not auto-detect the platform. Please choose:",
        )
        dlg.add_response("windows", "Proton (Windows)")
        dlg.add_response("linux", "Native (Linux)")
        dlg.add_response("cancel", "Cancel")
        dlg.set_default_response("windows")
        dlg.set_close_response("cancel")

        def _on_response(_dlg, response):
            if response in ("windows", "linux"):
                app_name = parse_app_name(path)
                version = parse_version_hint(path)
                if path.is_dir():
                    gi = find_gameinfo(path)
                    if gi:
                        if gi["name"]:
                            app_name = gi["name"]
                        if gi["version"]:
                            version = gi["version"]
                self._open_metadata_editor(path, response, app_name, version)

        dlg.connect("response", _on_response)
        dlg.present(self._parent_view)

    # ── Disc image import ──────────────────────────────────────────────

    def _start_disc_import(self, paths: list[Path]) -> None:
        """Handle dropped/selected disc image files."""
        from cellar.backend.disc_image import group_disc_files

        disc_set = group_disc_files(paths)

        # Nothing usable?
        if not disc_set.isos and not disc_set.cue_bins and not disc_set.floppies:
            err = Adw.AlertDialog(
                heading="No disc images found",
                body=PackageBuilderView._no_disc_images_body(disc_set),
            )
            err.add_response("ok", "OK")
            err.present(self._parent_view)
            return

        # Check if ordering is ambiguous (multiple discs, might need reorder)
        all_cd_paths = list(disc_set.isos) + [cs.cue_path for cs in disc_set.cue_bins]
        if len(all_cd_paths) > 1 or len(disc_set.floppies) > 1:
            # Show ordering dialog for multi-disc sets
            self._show_disc_order_dialog(disc_set)
            return

        self._proceed_disc_import(disc_set)

    def _show_disc_order_dialog(self, disc_set) -> None:
        """Show a dialog for the user to confirm/reorder multi-disc sets."""
        from cellar.views.widgets import make_dialog_header

        dlg = Adw.Dialog(title="Disc Order", content_width=400, content_height=400)
        toolbar, _header, ok_btn = make_dialog_header(
            dlg, action_label="Continue",
        )

        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            margin_top=18, margin_bottom=18, margin_start=18, margin_end=18,
        )

        # Collect all disc paths in current order for display
        all_paths: list[Path] = list(disc_set.isos)
        for cs in disc_set.cue_bins:
            all_paths.append(cs.cue_path)
        all_paths.extend(disc_set.floppies)

        if not all_paths:
            dlg.close()
            return

        desc = Gtk.Label(
            label="Confirm the disc order. Use the arrows to reorder if needed.",
            wrap=True,
        )
        desc.add_css_class("dim-label")
        content.append(desc)

        group = Adw.PreferencesGroup(title="Discs")
        rows: list[tuple[Adw.ActionRow, Path]] = []

        for p in all_paths:
            subtitle = p.suffix.upper().lstrip(".")
            row = Adw.ActionRow(title=p.name, subtitle=subtitle)

            up_btn = Gtk.Button(icon_name="go-up-symbolic", valign=Gtk.Align.CENTER)
            up_btn.add_css_class("flat")
            down_btn = Gtk.Button(icon_name="go-down-symbolic", valign=Gtk.Align.CENTER)
            down_btn.add_css_class("flat")
            row.add_suffix(up_btn)
            row.add_suffix(down_btn)

            rows.append((row, p))
            group.add(row)

            def _make_move(direction, current_row=row):
                def _move(_btn):
                    idx = next(i for i, (r, _) in enumerate(rows) if r is current_row)
                    new_idx = idx + direction
                    if 0 <= new_idx < len(rows):
                        rows[idx], rows[new_idx] = rows[new_idx], rows[idx]
                        # Rebuild the group
                        for r, _ in rows:
                            group.remove(r)
                        for r, _ in rows:
                            group.add(r)
                return _move

            up_btn.connect("clicked", _make_move(-1))
            down_btn.connect("clicked", _make_move(1))

        content.append(group)
        toolbar.set_content(content)
        dlg.set_child(toolbar)

        def _on_ok(_btn):
            dlg.close()
            # Rebuild disc_set with new order
            from cellar.backend.disc_image import DiscSet
            new_set = DiscSet()
            for _, p in rows:
                suffix = p.suffix.lower()
                if suffix == ".iso":
                    new_set.isos.append(p)
                elif suffix == ".cue":
                    # Find the matching CueSheet
                    for cs in disc_set.cue_bins:
                        if cs.cue_path == p:
                            new_set.cue_bins.append(cs)
                            break
                elif suffix in {".img", ".ima", ".vfd"}:
                    new_set.floppies.append(p)
            self._proceed_disc_import(new_set)

        ok_btn.connect("clicked", _on_ok)
        dlg.present(self._parent_view)

    def _proceed_disc_import(self, disc_set) -> None:
        """Continue disc import after ordering is confirmed."""
        from cellar.backend.detect import parse_app_name

        # Try to get a name from the first disc image filename
        app_name = ""
        if disc_set.isos:
            app_name = parse_app_name(disc_set.isos[0])
        elif disc_set.cue_bins:
            app_name = parse_app_name(disc_set.cue_bins[0].cue_path)
        elif disc_set.floppies:
            app_name = parse_app_name(disc_set.floppies[0])

        # Store disc set for use in _on_created
        self._disc_set = disc_set
        self._dosbox_info = None

        # Use the first disc path as the import path reference
        first_path = (
            disc_set.isos[0] if disc_set.isos
            else disc_set.cue_bins[0].cue_path if disc_set.cue_bins
            else disc_set.floppies[0]
        )

        self._open_metadata_editor(first_path, "dos", app_name, None)

    def _open_metadata_editor(
        self, path: Path, platform: str, app_name: str, version: str | None,
    ) -> None:
        """Open the standard MetadataEditorDialog with smart-import pre-fill."""
        from cellar.backend.detect import find_linux_executables

        project_type = {"linux": "linux", "dos": "dos"}.get(platform, "app")
        ctx = ProjectContext(project_type=project_type)

        def _on_created(project):
            # Post-creation: set import-specific fields on the project
            changed = False
            if platform == "dos" and hasattr(self, '_disc_set') and self._disc_set:
                # Disc image import: set up hdd/cd layout and run installer
                self._install_from_disc(project, self._disc_set)
                self._disc_set = None
                return  # _install_from_disc calls _on_import when done
            elif (platform == "dos" and path.is_dir()
                    and hasattr(self, '_dosbox_info') and self._dosbox_info):
                # GOG DOSBox game: convert to native Linux DOSBox in background
                self._convert_dosbox_game(project, path, self._dosbox_info)
                return  # _convert_dosbox_game calls _on_import when done
            elif platform == "windows" and path.is_file():
                # .exe import: store installer path
                project.installer_path = str(path)
                changed = True
            elif platform == "linux" and path.is_file() and path.suffix.lower() in (".sh", ".run"):
                from cellar.utils.gog import is_gog_installer

                if is_gog_installer(path):
                    # GOG installer: extract game data from embedded ZIP
                    self._extract_gog_installer(project, path)
                    return  # _extract_gog_installer calls _on_import when done
                # Non-GOG installer: store path, user runs from detail view
                project.installer_path = str(path)
                project.installer_type = "isolated"
                changed = True
            elif platform == "dos" and path.is_dir():
                # DOS folder: copy into content/hdd/<folder> so it
                # appears as C:\<folder> in DOSBox.
                self._import_dos_folder(project, path)
                return  # _import_dos_folder calls _on_import when done
            elif platform == "linux" and path.is_dir():
                # Linux folder: set source_dir and detect entry points
                project.source_dir = str(path)
                project.initialized = True
                candidates = find_linux_executables(path)
                if candidates:
                    project.entry_points = [
                        {"name": c.name, "path": str(c.relative_to(path))}
                        for c in candidates[:5]
                    ]
                changed = True
            elif platform == "windows" and path.is_dir():
                # Windows folder: store source_dir for later import
                project.source_dir = str(path)
                changed = True

            if version and project.version == "1.0":
                project.version = version
                changed = True

            if changed:
                save_project(project)
            self._on_import(project)

        dialog = MetadataEditorDialog(
            context=ctx,
            on_created=_on_created,
            auto_steam_query=app_name,
            auto_version=version or "",
        )
        dialog.present(self._parent_view)

    # ── GOG Linux installer extraction ────────────────────────────────

    def _extract_gog_installer(self, project, src_path: Path) -> None:
        """Extract a GOG Linux .sh installer into the project in a background thread."""
        from cellar.backend.detect import find_linux_executables
        from cellar.utils.gog import extract_gog_game_data

        content = project.content_path
        content.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label="Extracting GOG game\u2026")
        progress.present(self._parent_view)

        def _work():
            extract_gog_game_data(src_path, content)
            return find_linux_executables(content)

        def _done(candidates):
            progress.force_close()
            project.source_dir = str(content)
            project.initialized = True
            project.installer_type = "gog"
            if candidates:
                project.entry_points = [
                    {
                        "name": project.name if c.name == "start.sh" else c.name,
                        "path": str(c.relative_to(content)),
                    }
                    for c in candidates[:5]
                ]
            save_project(project)
            self._on_import(project)

        def _error(msg):
            progress.force_close()
            log.error("GOG extraction failed: %s", msg)
            err = Adw.AlertDialog(
                heading="Extraction failed",
                body=f"Could not extract GOG installer:\n{msg}",
            )
            err.add_response("ok", "OK")
            err.present(self._parent_view)

        run_in_background(work=_work, on_done=_done, on_error=_error)

    def _import_dos_folder(self, project, src_path: Path) -> None:
        """Copy a dropped folder into content/hdd/<folder> for DOSBox C:\\ access."""
        from cellar.backend.dosbox import prepare_dos_layout
        from cellar.backend.project import save_project
        from cellar.utils.async_work import run_in_background
        from cellar.views.builder.progress import ProgressDialog

        content = project.content_path
        content.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label="Copying folder\u2026")
        progress.present(self._parent_view)

        def _work():
            prepare_dos_layout(content)
            hdd_dir = content / "hdd"
            dest = hdd_dir / src_path.name
            dest.mkdir(parents=True, exist_ok=True)
            for src in src_path.rglob("*"):
                rel = src.relative_to(src_path)
                dst = dest / rel
                if src.is_dir():
                    dst.mkdir(parents=True, exist_ok=True)
                elif src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)

        def _done(_result):
            progress.force_close()
            project.source_dir = str(content)
            project.initialized = True
            save_project(project)
            self._on_import(project)

        def _error(msg):
            progress.force_close()
            log.error("DOS folder import failed: %s", msg)
            err = Adw.AlertDialog(
                heading="Import failed",
                body=f"Could not import folder:\n{msg}",
            )
            err.add_response("ok", "OK")
            err.present(self._parent_view)

        run_in_background(work=_work, on_done=_done, on_error=_error)

    def _convert_dosbox_game(self, project, src_path: Path, dosbox_info) -> None:
        """Convert a GOG DOSBox Windows game to native Linux DOSBox Staging."""
        from cellar.backend.dosbox import convert_gog_dosbox

        content = project.content_path
        content.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label="Setting up DOS game\u2026")
        progress.present(self._parent_view)

        def _work():
            def _on_progress(downloaded, total):
                if total > 0:
                    GLib.idle_add(progress.set_fraction, downloaded / total)
            return convert_gog_dosbox(
                src_path, content, dosbox_info, progress_cb=_on_progress,
            )

        def _done(entry_points):
            progress.force_close()
            project.source_dir = str(content)
            project.initialized = True
            if entry_points:
                project.entry_points = entry_points
            save_project(project)
            self._on_import(project)

            if entry_points:
                _check_scummvm_compat(
                    content, project, self._parent_view,
                )

        def _error(msg):
            progress.force_close()
            log.error("DOSBox conversion failed: %s", msg)
            err = Adw.AlertDialog(
                heading="Conversion failed",
                body=f"Could not convert DOSBox game:\n{msg}",
            )
            err.add_response("ok", "OK")
            err.present(self._parent_view)

        run_in_background(work=_work, on_done=_done, on_error=_error)

    # ── Disc image installation ──────────────────────────────────────────

    def _install_from_disc(self, project, disc_set) -> None:
        """Set up a DOS game from disc images — copy and CDDA conversion."""
        from cellar.backend.disc_image import (
            convert_cdda_tracks,
            has_cdda_tools,
        )
        from cellar.backend.dosbox import prepare_dos_layout

        content = project.content_path
        content.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label="Preparing disc images\u2026")
        progress.present(self._parent_view)

        def _work():
            # Step 1: Copy disc images to content/cd/
            cd_dir = content / "cd"
            cd_dir.mkdir(parents=True, exist_ok=True)

            disc_image_paths: list[Path] = []  # paths in cd_dir

            for iso_path in disc_set.isos:
                dest = cd_dir / iso_path.name
                shutil.copy2(iso_path, dest)
                disc_image_paths.append(dest)

            for cue_sheet in disc_set.cue_bins:
                if cue_sheet.has_audio and has_cdda_tools():
                    GLib.idle_add(progress.set_label, "Converting CD audio\u2026")
                    def _audio_progress(done, total):
                        if total > 0:
                            GLib.idle_add(
                                progress.set_label,
                                f"Converting audio track {done}/{total}\u2026",
                            )
                    new_cue = convert_cdda_tracks(
                        cue_sheet.cue_path, cd_dir, progress_cb=_audio_progress,
                    )
                    disc_image_paths.append(new_cue)
                else:
                    # Copy CUE and BIN files as-is
                    dest_cue = cd_dir / cue_sheet.cue_path.name
                    shutil.copy2(cue_sheet.cue_path, dest_cue)
                    for bin_path in cue_sheet.bin_files:
                        if bin_path.is_file():
                            shutil.copy2(bin_path, cd_dir / bin_path.name)
                    disc_image_paths.append(dest_cue)

            # Copy floppy images (kept for installer and DOSBox prompt)
            floppy_rel: list[str] = []
            if disc_set.floppies:
                floppy_dir = content / "floppy"
                floppy_dir.mkdir(parents=True, exist_ok=True)
                for fp in disc_set.floppies:
                    dest = floppy_dir / fp.name
                    shutil.copy2(fp, dest)
                    floppy_rel.append(str(dest.relative_to(content)))

            # Step 2: Prepare DOSBox layout (hdd/, config/, dosbox/)
            GLib.idle_add(progress.set_label, "Setting up DOSBox\u2026")
            prepare_dos_layout(content)

            return floppy_rel

        def _done(floppy_paths):
            progress.force_close()

            # Store disc image paths relative to content
            cd_dir = content / "cd"
            if cd_dir.is_dir():
                project.disc_images = [
                    str(p.relative_to(content))
                    for p in sorted(cd_dir.iterdir())
                    if p.suffix.lower() in {".iso", ".cue"}
                ]

            # Store floppy image paths
            if floppy_paths:
                project.floppy_images = floppy_paths

            project.source_dir = str(content)
            project.initialized = True
            save_project(project)
            self._on_import(project)

        def _error(msg):
            progress.force_close()
            log.error("Disc import failed: %s", msg)
            err = Adw.AlertDialog(
                heading="Import failed",
                body=f"Could not import disc images:\n{msg}",
            )
            err.add_response("ok", "OK")
            err.present(self._parent_view)

        run_in_background(work=_work, on_done=_done, on_error=_error)

    # _run_disc_installer removed — use module-level _launch_dos_installer()

    # ── Manual platform row handlers ────────────────────────────────────

    def _on_windows_activated(self, _row) -> None:
        self.close()
        self._on_windows()

    def _on_linux_activated(self, _row) -> None:
        self.close()
        self._on_linux()

    def _on_dos_activated(self, _row) -> None:
        self.close()
        self._on_dos()

    def _on_base_activated(self, _row) -> None:
        self.close()
        self._on_base()


class _NewProjectCard(Gtk.FlowBoxChild):
    """Persistent 'New Project' card — always first in the grid."""

    def __init__(self) -> None:
        from cellar.views.widgets import CARD_HEIGHT, CARD_WIDTH, FixedBox

        super().__init__()
        self.add_css_class("app-card-cell")
        set_margins(self, 6)

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        card.add_css_class("new-project-card")
        card.add_css_class("activatable")
        card.set_overflow(Gtk.Overflow.HIDDEN)

        icon = make_card_icon_from_name("list-add-symbolic", dim=True)
        card.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_hexpand(True)
        text_box.set_margin_start(22)
        text_box.set_margin_end(18)
        card.append(text_box)

        title = Gtk.Label(label="New Project")
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.START)
        text_box.append(title)

        subtitle = Gtk.Label(label="Create a new package")
        subtitle.add_css_class("dim-label")
        subtitle.set_halign(Gtk.Align.START)
        text_box.append(subtitle)

        fixed = FixedBox(CARD_WIDTH, CARD_HEIGHT, clip=False)
        fixed.set_child(card)
        self.set_child(fixed)



class _ProjectCard(BaseCard):
    """A project card matching the browse view's AppCard layout."""

    def __init__(self, project: Project) -> None:
        icon_name = _TYPE_ICONS.get(project.project_type, "grid-large-symbolic")
        type_label = _TYPE_LABELS.get(project.project_type, "")
        if not project.origin_app_id:
            type_label += " \u00b7 Draft"

        super().__init__(
            name=project.name,
            subtitle=type_label,
            icon_widget=make_card_icon_from_name(icon_name, dim=True),
        )
        self.project = project
        self._name_label.set_tooltip_text(project.name)

    def matches(self, search: str, active_types: set[str], active_repos: set[str]) -> bool:
        """Return True if this card should be visible given the current filters."""
        if active_types:
            ft = _resolve_filter_type(self.project.project_type)
            if ft not in active_types:
                return False
        if search and search.lower() not in self.project.name.lower():
            return False
        return True

    def refresh_label(self) -> None:
        """Update the displayed name."""
        self._name_label.set_label(self.project.name)
        self._name_label.set_tooltip_text(self.project.name)


class _CatalogueCard(BaseCard):
    """A dimmed card for a published catalogue entry — edit, download, or delete actions."""

    def __init__(
        self,
        entry,
        repo,
        kind: str,
        *,
        on_download: Callable,
        on_delete: Callable,
        on_edit: Callable | None = None,
        on_change_base: Callable | None = None,
        has_dependants: bool = False,
        show_repo: bool = False,
    ) -> None:
        if kind == "base":
            icon_name = "package-x-generic-symbolic"
            type_label = "Base Image"
        else:
            platform = getattr(entry, "platform", "windows")
            ptype = {"linux": "linux", "dos": "dos"}.get(platform, "app")
            icon_name = _TYPE_ICONS.get(ptype, "grid-large-symbolic")
            type_label = {"linux": "Native App", "dos": "DOS App"}.get(platform, "Proton App")

        subtitle_text = (repo.name or repo.uri) if show_repo else type_label
        icon_widget = make_card_icon_from_name(icon_name, dim=True)

        super().__init__(
            name=entry.name,
            subtitle=subtitle_text,
            icon_widget=icon_widget,
            activatable=False,
        )
        self.entry = entry
        self.repo = repo
        self.kind = kind  # "app" or "base"
        self._name_label.set_tooltip_text(entry.name)

        # Right: single actions menu button
        action_group = Gio.SimpleActionGroup()

        dl_action = Gio.SimpleAction.new("download", None)
        dl_action.connect("activate", lambda *_: on_download(self))
        action_group.add_action(dl_action)

        del_action = Gio.SimpleAction.new("delete", None)
        del_action.set_enabled(not has_dependants)
        del_action.connect("activate", lambda *_: on_delete(self))
        action_group.add_action(del_action)

        menu = Gio.Menu()
        if on_edit:
            edit_action = Gio.SimpleAction.new("edit", None)
            edit_action.connect("activate", lambda *_: on_edit(self))
            action_group.add_action(edit_action)
            menu.append("Edit metadata", "card.edit")
        if on_change_base:
            cb_action = Gio.SimpleAction.new("change_base", None)
            cb_action.connect("activate", lambda *_: on_change_base(self))
            action_group.add_action(cb_action)
            menu.append("Change base image\u2026", "card.change_base")
        menu.append("Download for editing", "card.download")
        del_label = (
            "Delete from catalogue" if not has_dependants else "Delete (base has dependants)"
        )
        menu.append(del_label, "card.delete")

        menu_btn = make_gear_button(menu_model=menu)
        menu_btn.set_valign(Gtk.Align.CENTER)
        menu_btn.set_margin_end(8)
        self._card.append(menu_btn)

        # Dim icon + text but keep action button fully opaque
        icon_widget.set_opacity(0.6)
        self._text_box.set_opacity(0.6)

        self.insert_action_group("card", action_group)

    def matches(self, search: str, active_types: set[str], active_repos: set[str]) -> bool:
        """Return True if this card should be visible given the current filters."""
        if active_repos and self.repo.uri not in active_repos:
            return False
        if active_types:
            platform = getattr(self.entry, "platform", "windows")
            ft = _resolve_filter_type(self.kind, platform)
            if ft not in active_types:
                return False
        if search and search.lower() not in self.entry.name.lower():
            return False
        return True



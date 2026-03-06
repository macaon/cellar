"""Add-app dialog — lets the user add an app to a local Cellar repo.

Flow — Bottles backup
---------------------
1. User opens a ``.tar.gz`` Bottles backup via a file chooser in the main window.
2. This dialog opens, pre-filling what we can from ``bottle.yml`` inside the
   archive (Name, Runner, DXVK, VKD3D, Environment → category suggestion).
3. User completes the metadata form and optionally attaches images.
4. On "Add to Catalogue" the form is replaced by a progress view while a
   background thread copies the archive and writes the catalogue entry.
5. On success the dialog closes and the main window reloads the browse view.

Flow — Linux native app
-----------------------
1. User selects a directory via a folder chooser in the main window.
2. This dialog opens in Linux mode (Wine fields hidden, entry point required).
3. User picks the entry point binary from a synchronous directory tree, fills
   in metadata, and attaches images.
4. On "Add to Catalogue" the directory is compressed to ``.tar.zst`` on a
   background thread, then imported into the repo.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from cellar.utils.progress import fmt_file_count as _fmt_file_count, fmt_stats as _fmt_stats

_STRATEGIES = ["safe", "full"]
_STRATEGY_LABELS = ["Safe (preserve user data)", "Full (complete replacement)"]


class AddAppDialog(Adw.Dialog):
    """Dialog for adding an app to a local Cellar repo.

    Pass *archive_path* for a Bottles ``.tar.gz`` backup, or *source_dir* for
    a Linux native app directory.  Exactly one must be non-empty.
    """

    def __init__(
        self,
        *,
        archive_path: str = "",
        source_dir: str = "",
        repos,          # list[cellar.backend.repo.Repo] — all writable repos
        on_done,        # callable()
    ) -> None:
        super().__init__(title="Add App to Catalogue", content_width=560)

        self._archive_path = archive_path
        self._source_dir = source_dir
        self._repos = repos
        self._repo = repos[0]
        self._on_done = on_done
        self._cancel_event = threading.Event()

        # Image selections
        self._icon_path: str = ""
        self._cover_path: str = ""
        self._logo_path: str = ""
        self._screenshot_paths: list[str] = []

        # Track whether the user has manually edited the ID field
        self._id_user_edited = False
        self._install_size: int = 0

        # Platform mode — set by _prefill
        self._is_linux: bool = bool(source_dir)  # directories are always Linux mode

        # Delta packaging state (set after prefill reads bottle.yml)
        self._runner: str = ""         # Runner: field from bottle.yml, e.g. "soda-9.0-1"
        self._use_delta: bool = False  # True when repo has base AND it's installed locally
        self._base_ok: bool = True     # False blocks the Add button
        self._delta_dl_btn: Gtk.Button | None = None  # Download button on delta row
        self._pending_base_entry = None  # BaseEntry for download dialog
        self._pending_base_repo = None   # Repo for download dialog

        self._pulse_id: int | None = None
        self._progress_pulse_id: int | None = None

        # IGDB lookup availability (checked once at init)
        from cellar.backend.config import load_igdb_creds as _load_igdb
        self._igdb_configured: bool = _load_igdb() is not None

        # Load category list and icon hints from repo
        from cellar.backend.packager import BASE_CATEGORIES as _BASE_CATS, BASE_CATEGORY_ICONS
        try:
            self._categories = self._repo.fetch_categories()
        except Exception:
            self._categories = list(_BASE_CATS)
        try:
            self._category_icon_hints = self._repo.fetch_category_icons()
        except Exception:
            self._category_icon_hints = dict(BASE_CATEGORY_ICONS)
        self._category_icon: str = self._category_icon_hints.get(
            self._categories[0] if self._categories else "", ""
        )

        self._build_ui()
        self._pulse_id = GLib.timeout_add(80, self._do_pulse)
        threading.Thread(target=self._prefill, daemon=True).start()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Outer toolbar view so we get a header bar inside the dialog
        toolbar_view = Adw.ToolbarView()

        # Header bar
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        self._cancel_btn = Gtk.Button(label="Cancel")
        self._cancel_btn.connect("clicked", self._on_cancel_clicked)
        header.pack_start(self._cancel_btn)

        self._add_btn = Gtk.Button(label="Add to Catalogue")
        self._add_btn.add_css_class("suggested-action")
        self._add_btn.set_sensitive(False)
        self._add_btn.connect("clicked", self._on_add_clicked)
        header.pack_end(self._add_btn)

        toolbar_view.add_top_bar(header)

        # Stack: scan → form → progress
        self._stack = Gtk.Stack()
        self._stack.add_named(self._build_scan_page(), "scan")
        self._stack.add_named(self._build_form(), "form")
        self._stack.add_named(self._build_progress(), "progress")
        self._stack.set_visible_child_name("scan")

        toolbar_view.set_content(self._stack)
        self.set_child(toolbar_view)

    def _build_form(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                    vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(True)

        page = Adw.PreferencesPage()
        scroll.set_child(page)

        # ── Archive ───────────────────────────────────────────────────────
        archive_group = Adw.PreferencesGroup(title="Archive")

        # Repository selector — only shown when more than one writable repo
        # is configured, so single-repo users see no extra clutter.
        if len(self._repos) > 1:
            self._repo_row = Adw.ComboRow(title="Repository")
            repo_model = Gtk.StringList()
            for r in self._repos:
                repo_model.append(r.name)
            self._repo_row.set_model(repo_model)
            self._repo_row.connect("notify::selected", self._on_repo_changed)
            archive_group.add(self._repo_row)

        source_label = (
            Path(self._source_dir).name if self._source_dir else Path(self._archive_path).name
        )
        self._archive_row = Adw.ActionRow(
            title="Directory" if self._source_dir else "File",
            subtitle=GLib.markup_escape_text(source_label),
        )
        archive_group.add(self._archive_row)

        # Delta status row — hidden until prefill completes and detects a base
        self._delta_icon = Gtk.Image()
        self._delta_icon.set_icon_size(Gtk.IconSize.NORMAL)
        self._delta_row = Adw.ActionRow()
        self._delta_row.add_prefix(self._delta_icon)
        self._delta_row.set_visible(False)
        archive_group.add(self._delta_row)

        page.add(archive_group)

        # ── Identity ──────────────────────────────────────────────────────
        identity_group = Adw.PreferencesGroup(title="Identity")

        self._name_entry = Adw.EntryRow(title="Name *")
        self._name_entry.connect("changed", self._on_name_changed)
        if self._igdb_configured:
            igdb_btn = Gtk.Button(icon_name="system-search-symbolic")
            igdb_btn.add_css_class("flat")
            igdb_btn.set_valign(Gtk.Align.CENTER)
            igdb_btn.set_tooltip_text("Look up on IGDB")
            igdb_btn.connect("clicked", self._on_igdb_lookup)
            self._name_entry.add_suffix(igdb_btn)
        identity_group.add(self._name_entry)

        self._id_entry = Adw.EntryRow(title="App ID")
        self._id_entry.connect("changed", self._on_id_changed)
        identity_group.add(self._id_entry)

        self._version_entry = Adw.EntryRow(title="Version")
        self._version_entry.set_text("1.0")
        identity_group.add(self._version_entry)

        page.add(identity_group)

        # ── Category ──────────────────────────────────────────────────────
        self._category_row = Adw.ComboRow(title="Category")
        self._category_row.set_model(Gtk.StringList.new(self._categories + ["Other"]))
        self._category_row.connect("notify::selected", self._on_category_selected)
        identity_group.add(self._category_row)

        self._custom_category_entry = Adw.EntryRow(title="New Category Name")
        self._custom_category_entry.set_visible(False)
        self._custom_category_entry.connect("changed", self._on_field_changed)
        identity_group.add(self._custom_category_entry)

        identity_group.add(self._build_icon_picker_row())

        # ── Details ───────────────────────────────────────────────────────
        details_group = Adw.PreferencesGroup(title="Details")

        self._summary_entry = Adw.EntryRow(title="Summary")
        details_group.add(self._summary_entry)

        # Description (multi-line) in a card-styled frame
        desc_row = Adw.ActionRow(title="Description")
        desc_row.set_activatable(False)
        self._desc_view = Gtk.TextView()
        self._desc_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._desc_view.set_margin_top(6)
        self._desc_view.set_margin_bottom(6)
        self._desc_view.set_margin_start(6)
        self._desc_view.set_margin_end(6)
        self._desc_view.set_size_request(-1, 80)
        self._desc_view.add_css_class("monospace")
        desc_frame = Gtk.Frame()
        desc_frame.set_child(self._desc_view)
        desc_frame.set_margin_top(6)
        desc_frame.set_margin_bottom(6)
        desc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        desc_box.append(desc_row)
        desc_box.append(desc_frame)
        desc_box.add_css_class("card")
        desc_wrapper = Adw.PreferencesGroup()
        desc_wrapper.add(desc_box)
        page.add(desc_wrapper)

        page.add(details_group)

        # ── Attribution ───────────────────────────────────────────────────
        attr_group = Adw.PreferencesGroup(title="Attribution")
        self._developer_entry = Adw.EntryRow(title="Developer")
        self._publisher_entry = Adw.EntryRow(title="Publisher")
        self._year_entry = Adw.EntryRow(title="Release Year")
        attr_group.add(self._developer_entry)
        attr_group.add(self._publisher_entry)
        attr_group.add(self._year_entry)
        page.add(attr_group)

        # ── Wine Components ───────────────────────────────────────────────
        self._wine_group = Adw.PreferencesGroup(title="Wine Components")

        self._runner_row = Adw.ActionRow(title="Runner")
        self._runner_row.set_subtitle("")
        self._runner_row.set_subtitle_selectable(True)
        self._lock_runner_btn = Gtk.ToggleButton()
        self._lock_runner_btn.set_icon_name("changes-prevent-symbolic")
        self._lock_runner_btn.set_valign(Gtk.Align.CENTER)
        self._lock_runner_btn.set_tooltip_text("Lock runner — users cannot change the runner after install")
        self._lock_runner_btn.connect("toggled", self._on_lock_runner_toggled)
        self._runner_row.add_suffix(self._lock_runner_btn)
        self._wine_group.add(self._runner_row)

        self._dxvk_row = Adw.ActionRow(title="DXVK")
        self._dxvk_row.set_subtitle("")
        self._dxvk_row.set_subtitle_selectable(True)
        self._wine_group.add(self._dxvk_row)

        self._vkd3d_row = Adw.ActionRow(title="VKD3D")
        self._vkd3d_row.set_subtitle("")
        self._vkd3d_row.set_subtitle_selectable(True)
        self._wine_group.add(self._vkd3d_row)

        self._steam_appid_entry = Adw.EntryRow(title="Steam App ID (optional)")
        self._steam_appid_entry.set_tooltip_text(
            "Used to set GAMEID for protonfixes. Leave empty to use GAMEID=0."
        )
        self._wine_group.add(self._steam_appid_entry)

        page.add(self._wine_group)

        # ── Images ────────────────────────────────────────────────────────
        images_group = Adw.PreferencesGroup(title="Images (optional)")

        self._icon_row = self._make_image_row("Icon", self._pick_icon)
        self._cover_row = self._make_image_row("Cover", self._pick_cover)
        self._logo_row = self._make_image_row("Logo", self._pick_logo)
        self._hide_title_btn = Gtk.ToggleButton()
        self._hide_title_btn.set_icon_name("view-conceal-symbolic")
        self._hide_title_btn.set_valign(Gtk.Align.CENTER)
        self._hide_title_btn.set_tooltip_text("Hide title — logo contains the app name")
        self._logo_row.add_suffix(self._hide_title_btn)

        self._screenshots_row = self._make_image_row(
            "Screenshots", self._pick_screenshots, multi=True
        )

        images_group.add(self._icon_row)
        images_group.add(self._cover_row)
        images_group.add(self._logo_row)
        images_group.add(self._screenshots_row)
        page.add(images_group)

        # ── Install ───────────────────────────────────────────────────────
        self._install_group = Adw.PreferencesGroup(title="Install")

        self._strategy_row = Adw.ComboRow(title="Update Strategy")
        strat_model = Gtk.StringList()
        for label in _STRATEGY_LABELS:
            strat_model.append(label)
        self._strategy_row.set_model(strat_model)
        self._install_group.add(self._strategy_row)

        # Linux platform badge — shown in place of Wine Components when Linux
        self._linux_badge_row = Adw.ActionRow(
            title="Platform",
            subtitle="Linux (native)",
        )
        self._linux_badge_row.add_prefix(
            Gtk.Image.new_from_icon_name("computer-symbolic")
        )
        self._linux_badge_row.set_visible(False)
        self._install_group.add(self._linux_badge_row)

        self._entry_point_entry = Adw.EntryRow(title="Entry Point (optional)")
        self._entry_point_entry.set_tooltip_text(
            "Relative path from drive_c to the main .exe, e.g. Program Files/App/app.exe"
        )
        self._entry_point_entry.connect("changed", self._on_field_changed)
        # "Pick from archive" button — shown only in Linux mode
        self._ep_pick_btn = Gtk.Button(label="Pick\u2026")
        self._ep_pick_btn.set_valign(Gtk.Align.CENTER)
        self._ep_pick_btn.set_visible(False)
        self._ep_pick_btn.connect("clicked", self._on_pick_entry_point)
        self._entry_point_entry.add_suffix(self._ep_pick_btn)
        self._install_group.add(self._entry_point_entry)

        page.add(self._install_group)

        return scroll

    def _make_image_row(self, label: str, handler, *, multi: bool = False) -> Adw.ActionRow:
        row = Adw.ActionRow(title=label)
        row.set_subtitle("No file selected")
        btn = Gtk.Button(label="Choose…")
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect("clicked", handler)
        row.add_suffix(btn)
        return row

    def _build_scan_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(48)
        box.set_margin_bottom(48)
        box.set_margin_start(24)
        box.set_margin_end(24)

        label = Gtk.Label(label="Reading archive…")
        label.add_css_class("dim-label")

        self._scan_bar = Gtk.ProgressBar()
        self._scan_bar.set_pulse_step(0.05)

        box.append(label)
        box.append(self._scan_bar)
        return box

    def _do_pulse(self) -> bool:
        self._scan_bar.pulse()
        return True  # keep firing

    def _do_progress_pulse(self) -> bool:
        self._progress_bar.pulse()
        return True

    def _on_delta_phase(self, label: str) -> None:
        """Handle delta phase transitions: pulse during extraction, deterministic otherwise."""
        if self._progress_pulse_id is not None:
            GLib.source_remove(self._progress_pulse_id)
            self._progress_pulse_id = None
        self._progress_label.set_text(label)
        self._progress_bar.set_text("")
        self._progress_bar.set_fraction(0.0)
        if "Extracting" in label:
            self._progress_bar.set_show_text(True)
            self._progress_pulse_id = GLib.timeout_add(80, self._do_progress_pulse)
        else:
            self._progress_bar.set_show_text(True)

    def _stop_progress_pulse(self) -> None:
        if self._progress_pulse_id is not None:
            GLib.source_remove(self._progress_pulse_id)
            self._progress_pulse_id = None

    def _build_progress(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(24)
        box.set_margin_end(24)

        self._progress_label = Gtk.Label(label="Copying archive\u2026", xalign=0)
        self._progress_label.add_css_class("dim-label")

        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(True)
        self._progress_bar.set_fraction(0.0)
        self._progress_bar.set_size_request(0, -1)

        self._cancel_progress_btn = Gtk.Button(label="Cancel")
        self._cancel_progress_btn.set_halign(Gtk.Align.CENTER)
        self._cancel_progress_btn.set_margin_top(6)
        self._cancel_progress_btn.connect("clicked", self._on_cancel_progress_clicked)

        box.append(self._progress_label)
        box.append(self._progress_bar)
        box.append(self._cancel_progress_btn)
        return box

    # ── Pre-fill ──────────────────────────────────────────────────────────

    def _prefill(self) -> None:
        """Inspect the source in a background thread, then reveal the form.

        For a directory source: immediately switches to Linux mode.
        For an archive source: reads ``bottle.yml`` if present (legacy Bottles
        backup) for pre-filling.  Archives without ``bottle.yml`` are accepted
        as umu Windows archives (no pre-fill).
        """
        from cellar.backend.packager import read_bottle_yml

        if self._source_dir:
            # Directory — always Linux native mode, no inspection needed.
            yml: dict = {}
            is_linux = True
        else:
            yml = read_bottle_yml(self._archive_path)
            is_linux = False  # archives are always Windows apps

        runner = yml.get("Runner", "")

        def _apply() -> None:
            if self._pulse_id is not None:
                GLib.source_remove(self._pulse_id)
                self._pulse_id = None

            if is_linux:
                self._switch_to_linux_mode()
            elif yml:
                # Legacy Bottles archive — pre-fill from bottle.yml
                name = yml.get("Name", "")
                if name:
                    self._name_entry.set_text(name)

                if runner:
                    self._runner_row.set_subtitle(runner)

                dxvk = yml.get("DXVK", "")
                if dxvk:
                    self._dxvk_row.set_subtitle(dxvk)

                vkd3d = yml.get("VKD3D", "")
                if vkd3d:
                    self._vkd3d_row.set_subtitle(vkd3d)

                env = yml.get("Environment", "")
                if env.lower() == "game":
                    GLib.idle_add(
                        self._category_row.set_selected,
                        self._categories.index("Games") if "Games" in self._categories else 0,
                    )

                if runner:
                    self._runner = runner
                    self._check_delta_base(runner)

            self._stack.set_visible_child_name("form")

        GLib.idle_add(_apply)

    def _switch_to_linux_mode(self) -> None:
        """Reconfigure the form for a Linux native app archive."""
        self._is_linux = True
        # Hide Wine-specific sections
        self._wine_group.set_visible(False)
        self._delta_row.set_visible(False)
        self._strategy_row.set_visible(True)  # update strategy still applies
        # Show Linux badge and make entry point required
        self._linux_badge_row.set_visible(True)
        self._entry_point_entry.set_title("Entry Point \u2731")
        self._entry_point_entry.set_tooltip_text(
            "Executable path within the archive, e.g. \u201cbin/mygame\u201d or \u201cMyGame.x86_64\u201d"
        )
        self._ep_pick_btn.set_visible(True)
        # Default category to Games since most Linux native packages are games
        if "Games" in self._categories:
            self._category_row.set_selected(self._categories.index("Games"))
        self._update_add_button()

    # ── Signal handlers — form fields ─────────────────────────────────────

    def _on_lock_runner_toggled(self, btn) -> None:
        if btn.get_active():
            btn.add_css_class("destructive-action")
        else:
            btn.remove_css_class("destructive-action")

    def _on_repo_changed(self, row, _param) -> None:
        idx = row.get_selected()
        if 0 <= idx < len(self._repos):
            self._repo = self._repos[idx]
            self._refresh_categories()
            if self._runner:
                self._check_delta_base(self._runner)

    def _refresh_categories(self) -> None:
        from cellar.backend.packager import BASE_CATEGORIES as _BASE_CATS
        try:
            self._categories = self._repo.fetch_categories()
        except Exception:
            self._categories = list(_BASE_CATS)
        self._category_row.set_model(Gtk.StringList.new(self._categories + ["Other"]))
        if self._category_row.get_selected() > len(self._categories):
            self._category_row.set_selected(0)
            self._custom_category_entry.set_visible(False)

    def _check_delta_base(self, runner: str) -> None:
        """Determine delta eligibility; update the delta status row accordingly."""
        from cellar.backend.base_store import is_base_installed

        bases: dict = {}
        try:
            bases = self._repo.fetch_bases()
        except Exception:
            pass

        # Remove any previous download button from the delta row
        if hasattr(self, "_delta_dl_btn") and self._delta_dl_btn is not None:
            self._delta_row.remove(self._delta_dl_btn)
            self._delta_dl_btn = None

        if runner not in bases:
            # No base in this repo — fall back to full archive upload.
            self._use_delta = False
            self._base_ok = True
            self._delta_row.set_visible(False)
            self._update_add_button()
            return

        if is_base_installed(runner):
            self._use_delta = True
            self._base_ok = True
            _theme = Gtk.IconTheme.get_for_display(self.get_display())
            _icon_name = next(
                (n for n in ("branch-fork-symbolic", "emblem-synchronizing-symbolic", "system-run-symbolic")
                 if _theme.has_icon(n)),
                "system-run-symbolic",
            )
            self._delta_icon.set_from_icon_name(_icon_name)
            self._delta_row.set_title("Delta archive")
            self._delta_row.set_subtitle(
                f"Only files that differ from the {runner} base will be stored"
            )
        else:
            self._use_delta = False
            self._base_ok = False
            self._delta_icon.set_from_icon_name("dialog-warning-symbolic")
            self._delta_row.set_title(f"{runner} base not installed locally")
            self._delta_row.set_subtitle("Download the base image to enable delta packaging")
            # Store base entry + repo for the download dialog
            self._pending_base_entry = bases[runner]
            self._pending_base_repo = self._repo
            # Add a download button to the row
            self._delta_dl_btn = Gtk.Button(
                label="Download",
                valign=Gtk.Align.CENTER,
            )
            self._delta_dl_btn.add_css_class("suggested-action")
            self._delta_dl_btn.connect("clicked", self._on_download_base_clicked)
            self._delta_row.add_suffix(self._delta_dl_btn)

        self._delta_row.set_visible(True)
        self._update_add_button()

    def _on_download_base_clicked(self, _btn: Gtk.Button) -> None:
        """Open the base download dialog, then re-check delta state on success."""
        from cellar.views.settings import InstallBaseFromRepoDialog

        def _on_base_downloaded() -> None:
            if self._runner:
                self._check_delta_base(self._runner)

        dialog = InstallBaseFromRepoDialog(
            runner=self._runner,
            base_entry=self._pending_base_entry,
            repo=self._pending_base_repo,
            on_done=_on_base_downloaded,
        )
        dialog.present(self)

    def _on_name_changed(self, _entry) -> None:
        name = self._name_entry.get_text().strip()
        if not self._id_user_edited:
            from cellar.backend.packager import slugify
            # Block the id_changed signal temporarily to avoid triggering
            # the "user edited" flag from our own programmatic update
            self._id_updating_programmatically = True
            self._id_entry.set_text(slugify(name) if name else "")
            self._id_updating_programmatically = False
        self._update_add_button()

    def _on_id_changed(self, _entry) -> None:
        if not getattr(self, "_id_updating_programmatically", False):
            # User typed in the ID field — lock it from auto-updates
            self._id_user_edited = bool(self._id_entry.get_text())
        self._update_add_button()

    def _on_field_changed(self, *_args) -> None:
        self._update_add_button()

    def _build_icon_picker_row(self) -> Adw.ActionRow:
        from cellar.backend.packager import CATEGORY_ICON_OPTIONS
        row = Adw.ActionRow(title="Category Icon")

        self._cat_icon_image = Gtk.Image.new_from_icon_name(
            self._category_icon or "tag-symbolic"
        )
        self._cat_icon_image.set_pixel_size(24)
        self._cat_icon_image.set_valign(Gtk.Align.CENTER)

        btn = Gtk.Button()
        btn.add_css_class("flat")
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_child(self._cat_icon_image)
        btn.set_tooltip_text("Choose icon")

        flow = Gtk.FlowBox()
        flow.set_max_children_per_line(4)
        flow.set_min_children_per_line(4)
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_margin_top(6)
        flow.set_margin_bottom(6)
        flow.set_margin_start(6)
        flow.set_margin_end(6)
        flow.set_column_spacing(4)
        flow.set_row_spacing(4)

        popover = Gtk.Popover()
        popover.set_child(flow)
        popover.set_parent(btn)

        for icon_name in CATEGORY_ICON_OPTIONS:
            icon_btn = Gtk.Button()
            icon_btn.add_css_class("flat")
            img = Gtk.Image.new_from_icon_name(icon_name)
            img.set_pixel_size(24)
            icon_btn.set_child(img)
            icon_btn.set_tooltip_text(
                icon_name.removesuffix("-symbolic").replace("-", " ").title()
            )
            def _on_icon_clicked(_b, name=icon_name):
                self._category_icon = name
                self._cat_icon_image.set_from_icon_name(name)
                popover.popdown()
            icon_btn.connect("clicked", _on_icon_clicked)
            flow.append(icon_btn)

        btn.connect("clicked", lambda _: popover.popup())
        row.add_suffix(btn)
        row.set_activatable_widget(btn)
        return row

    def _on_category_selected(self, row, _param) -> None:
        is_other = row.get_selected() == len(self._categories)
        self._custom_category_entry.set_visible(is_other)
        self._update_add_button()
        if not is_other:
            hint = self._category_icon_hints.get(self._get_category(), "")
            if hint:
                self._category_icon = hint
                self._cat_icon_image.set_from_icon_name(hint)

    def _get_category(self) -> str:
        idx = self._category_row.get_selected()
        if idx == len(self._categories):
            return self._custom_category_entry.get_text().strip()
        return self._categories[idx] if 0 <= idx < len(self._categories) else ""

    def _update_add_button(self) -> None:
        name_ok = bool(self._name_entry.get_text().strip())
        ep_ok = not self._is_linux or bool(self._entry_point_entry.get_text().strip())
        cat_ok = (
            self._category_row.get_selected() != len(self._categories)
            or bool(self._custom_category_entry.get_text().strip())
        )
        self._add_btn.set_sensitive(name_ok and ep_ok and self._base_ok and cat_ok)

    def _on_pick_entry_point(self, _btn) -> None:
        """Open a native file chooser to pick the entry point from the source directory."""
        chooser = Gtk.FileChooserNative(
            title="Select Entry Point",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
        )
        chooser.set_current_folder(Gio.File.new_for_path(self._source_dir))
        chooser.connect("response", self._on_ep_chosen, chooser)
        chooser.show()

    def _on_ep_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            f = chooser.get_file()
            if f:
                path = f.get_path()
                try:
                    rel = os.path.relpath(path, self._source_dir)
                except ValueError:
                    rel = path
                self._entry_point_entry.set_text(rel)
                self._update_add_button()

    # ── Image pickers ─────────────────────────────────────────────────────

    def _pick_image(self, title: str, multi: bool, callback) -> None:
        chooser = Gtk.FileChooserNative(
            title=title,
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            select_multiple=multi,
        )
        img_filter = Gtk.FileFilter()
        img_filter.set_name("Images (PNG, JPG, ICO, SVG)")
        img_filter.add_mime_type("image/png")
        img_filter.add_mime_type("image/jpeg")
        img_filter.add_mime_type("image/x-icon")
        img_filter.add_mime_type("image/vnd.microsoft.icon")
        img_filter.add_mime_type("image/svg+xml")
        chooser.add_filter(img_filter)
        chooser.connect("response", callback, chooser)
        chooser.show()

    def _pick_icon(self, _btn) -> None:
        self._pick_image("Select Icon", False, self._on_icon_chosen)

    def _on_icon_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._icon_path = chooser.get_file().get_path()
            self._icon_row.set_subtitle(GLib.markup_escape_text(Path(self._icon_path).name))

    def _pick_cover(self, _btn) -> None:
        self._pick_image("Select Cover", False, self._on_cover_chosen)

    def _on_cover_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._cover_path = chooser.get_file().get_path()
            self._cover_row.set_subtitle(GLib.markup_escape_text(Path(self._cover_path).name))

    def _pick_logo(self, _btn) -> None:
        self._pick_image("Select Logo (transparent PNG)", False, self._on_logo_chosen)

    def _on_logo_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._logo_path = chooser.get_file().get_path()
            self._logo_row.set_subtitle(GLib.markup_escape_text(Path(self._logo_path).name))
            if not self._hide_title_btn.get_active():
                self._hide_title_btn.set_active(True)

    def _pick_screenshots(self, _btn) -> None:
        self._pick_image("Select Screenshots", True, self._on_screenshots_chosen)

    def _on_screenshots_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            files = chooser.get_files()
            self._screenshot_paths = [files.get_item(i).get_path()
                                       for i in range(files.get_n_items())]
            count = len(self._screenshot_paths)
            self._screenshots_row.set_subtitle(
                f"{count} file{'s' if count != 1 else ''} selected"
            )

    # ── IGDB lookup ───────────────────────────────────────────────────────

    def _on_igdb_lookup(self, _btn) -> None:
        from cellar.views.igdb_picker import IGDBPickerDialog

        query = self._name_entry.get_text().strip()
        picker = IGDBPickerDialog(query=query, on_picked=self._apply_igdb_result)
        picker.present(self)

    def _apply_igdb_result(self, result: dict) -> None:
        """Pre-fill form fields from an IGDB result dict."""
        if result.get("name"):
            self._name_entry.set_text(result["name"])
        if result.get("developer") and not self._developer_entry.get_text().strip():
            self._developer_entry.set_text(result["developer"])
        if result.get("publisher") and not self._publisher_entry.get_text().strip():
            self._publisher_entry.set_text(result["publisher"])
        if result.get("year") and not self._year_entry.get_text().strip():
            self._year_entry.set_text(str(result["year"]))
        if result.get("summary") and not self._summary_entry.get_text().strip():
            self._summary_entry.set_text(result["summary"])
        if result.get("summary"):
            buf = self._desc_view.get_buffer()
            if not buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip():
                buf.set_text(result["summary"])
        if result.get("steam_appid") and not self._steam_appid_entry.get_text().strip():
            self._steam_appid_entry.set_text(str(result["steam_appid"]))

        # Try to set category from IGDB genre mapping
        if result.get("category") and result["category"] in self._categories:
            idx = self._categories.index(result["category"])
            self._category_row.set_selected(idx)

        # Download cover art to temp file and set as icon
        cover_id = result.get("cover_image_id")
        if cover_id and not self._icon_path:
            def _fetch_cover() -> None:
                try:
                    import tempfile
                    from cellar.backend.config import load_igdb_creds
                    from cellar.backend.igdb import IGDBClient

                    creds = load_igdb_creds()
                    if not creds:
                        return
                    client = IGDBClient(creds["client_id"], creds["client_secret"])
                    data = client.fetch_cover(cover_id)
                    suffix = ".jpg"
                    with tempfile.NamedTemporaryFile(
                        delete=False, suffix=suffix, prefix="cellar-igdb-cover-"
                    ) as f:
                        f.write(data)
                        tmp_path = f.name
                    GLib.idle_add(self._set_igdb_cover, tmp_path)
                except Exception:  # noqa: BLE001
                    pass

            import threading as _threading
            _threading.Thread(target=_fetch_cover, daemon=True).start()

    def _set_igdb_cover(self, path: str) -> None:
        self._icon_path = path
        self._icon_row.set_subtitle(Path(path).name)

    # ── Add / cancel ──────────────────────────────────────────────────────

    def _on_cancel_clicked(self, _btn) -> None:
        self.close()

    def _on_add_clicked(self, _btn) -> None:
        name = self._name_entry.get_text().strip()
        app_id = self._id_entry.get_text().strip()
        version = self._version_entry.get_text().strip() or "1.0"
        category = self._get_category()
        summary = self._summary_entry.get_text().strip()
        desc_buf = self._desc_view.get_buffer()
        description = desc_buf.get_text(
            desc_buf.get_start_iter(), desc_buf.get_end_iter(), False
        ).strip()
        developer = self._developer_entry.get_text().strip()
        publisher = self._publisher_entry.get_text().strip()
        year_text = self._year_entry.get_text().strip()
        release_year = int(year_text) if year_text.isdigit() else None
        runner = self._runner_row.get_subtitle() or ""
        dxvk = self._dxvk_row.get_subtitle() or ""
        vkd3d = self._vkd3d_row.get_subtitle() or ""
        steam_appid_text = self._steam_appid_entry.get_text().strip()
        steam_appid = int(steam_appid_text) if steam_appid_text.isdigit() else None
        strategy = _STRATEGIES[self._strategy_row.get_selected()]
        entry_point = self._entry_point_entry.get_text().strip()

        if not app_id:
            from cellar.backend.packager import slugify
            app_id = slugify(name)

        # Build archive filename: apps/<id>/<name>.tar.zst for dirs, else source basename
        if self._source_dir:
            archive_filename = f"{Path(self._source_dir).name}.tar.zst"
            archive_size = 0  # unknown until compression; updated in background thread
        else:
            archive_filename = Path(self._archive_path).name
            archive_size = Path(self._archive_path).stat().st_size
        archive_rel = f"apps/{app_id}/{archive_filename}"

        # Build image relative paths (only for images the user picked)
        icon_ext = ".png" if self._icon_path and Path(self._icon_path).suffix.lower() == ".ico" else (Path(self._icon_path).suffix if self._icon_path else "")
        icon_rel = f"apps/{app_id}/icon{icon_ext}" if self._icon_path else ""
        cover_rel = f"apps/{app_id}/cover{Path(self._cover_path).suffix}" if self._cover_path else ""
        hero_rel = ""
        logo_rel = f"apps/{app_id}/logo.png" if self._logo_path else ""
        screenshot_rels = tuple(
            f"apps/{app_id}/screenshots/{i + 1:02d}{Path(p).suffix}"
            for i, p in enumerate(self._screenshot_paths)
        )

        from cellar.models.app_entry import AppEntry, BuiltWith

        if self._is_linux:
            built_with = None
            base_runner_val = ""
            lock_runner_val = False
            platform_val = "linux"
        else:
            built_with = BuiltWith(runner=runner, dxvk=dxvk, vkd3d=vkd3d) if runner else None
            base_runner_val = self._runner if self._use_delta else ""
            lock_runner_val = self._lock_runner_btn.get_active()
            platform_val = "windows"

        entry = AppEntry(
            id=app_id,
            name=name,
            version=version,
            category=category,
            summary=summary,
            description=description,
            developer=developer,
            publisher=publisher,
            release_year=release_year,
            icon=icon_rel,
            cover=cover_rel,
            hero=hero_rel,
            logo=logo_rel,
            hide_title=self._hide_title_btn.get_active(),
            screenshots=screenshot_rels,
            archive=archive_rel,
            archive_size=archive_size,
            install_size_estimate=self._install_size,
            built_with=built_with,
            update_strategy=strategy,
            entry_point=entry_point,
            base_runner=base_runner_val,
            lock_runner=lock_runner_val,
            steam_appid=steam_appid if not self._is_linux else None,
            platform=platform_val,
        )

        images = {
            "icon": self._icon_path,
            "cover": self._cover_path,
            "logo": self._logo_path,
            "screenshots": self._screenshot_paths,
        }

        self._cancel_event.clear()
        self.set_content_width(360)
        self.set_content_height(200)
        self._cancel_btn.set_visible(False)
        self._add_btn.set_visible(False)
        self._stack.set_visible_child_name("progress")
        self._progress_bar.set_fraction(0.0)
        self._progress_label.set_text(
            "Compressing & Uploading\u2026" if self._source_dir else "Copying archive\u2026"
        )

        repo_root = self._repo.writable_path()
        category_icon = self._category_icon

        use_delta = self._use_delta
        runner = self._runner
        source_dir = self._source_dir

        def _run():
            import shutil as _shutil
            import tempfile
            from dataclasses import replace as _replace
            from cellar.backend.packager import (
                CancelledError,
                compress_directory_zst,
                import_to_repo,
            )

            def _phase(label: str) -> None:
                GLib.idle_add(self._progress_label.set_text, label)
                GLib.idle_add(self._progress_bar.set_text, "")

            _last_stats_t = [0.0]

            def _stats(copied: int, total: int, speed: float) -> None:
                now = time.monotonic()
                if now - _last_stats_t[0] >= 0.1:
                    _last_stats_t[0] = now
                    GLib.idle_add(self._progress_bar.set_text, _fmt_stats(copied, total, speed))

            def _progress(fraction: float) -> None:
                GLib.idle_add(self._progress_bar.set_fraction, fraction)

            archive_to_upload = self._archive_path
            entry_to_upload = entry
            tmp_delta: str | None = None
            archive_in_place = False

            def _cleanup_tmp(tmp: Path) -> None:
                """Remove tmp file and its parent dir if empty (new, cancelled import)."""
                tmp.unlink(missing_ok=True)
                try:
                    tmp.parent.rmdir()
                except OSError:
                    pass  # not empty — pre-existing app dir, leave it alone

            # ── Directory compression (Linux native) ───────────────────────
            if source_dir:
                archive_dest = repo_root / entry_to_upload.archive
                archive_dest.parent.mkdir(parents=True, exist_ok=True)
                tmp_archive = archive_dest.parent / (archive_dest.name + ".tmp")
                try:
                    compressed_size, crc32 = compress_directory_zst(
                        Path(source_dir),
                        tmp_archive,
                        cancel_event=self._cancel_event,
                        progress_cb=_progress,
                        stats_cb=_stats,
                    )
                    tmp_archive.rename(archive_dest)
                except CancelledError:
                    _cleanup_tmp(tmp_archive)
                    GLib.idle_add(self._on_import_cancelled)
                    return
                except Exception as exc:
                    _cleanup_tmp(tmp_archive)
                    GLib.idle_add(self._on_import_error, f"Failed to compress directory: {exc}")
                    return

                archive_to_upload = str(archive_dest)
                archive_in_place = True
                entry_to_upload = _replace(
                    entry_to_upload,
                    archive_size=compressed_size,
                    archive_crc32=crc32,
                )

            # ── Delta archive creation (Windows/Wine only) ─────────────────
            elif use_delta and not self._is_linux:
                from cellar.backend.packager import create_delta_archive
                from cellar.backend.base_store import base_path as _base_path

                GLib.idle_add(self._progress_bar.set_fraction, 0.0)
                GLib.idle_add(self._progress_bar.set_text, "")

                def _delta_phase(label: str) -> None:
                    GLib.idle_add(self._on_delta_phase, label)

                def _delta_file(current: int, total: int) -> None:
                    GLib.idle_add(self._progress_bar.set_text, _fmt_file_count(current, total))

                # Delta archives are recompressed as .tar.zst (zstd level 3).
                orig_name = Path(self._archive_path).name
                delta_name = (
                    orig_name[: -len(".tar.gz")] + ".tar.zst"
                    if orig_name.endswith(".tar.gz")
                    else orig_name
                )
                archive_dest = repo_root / "apps" / entry_to_upload.id / delta_name
                archive_dest.parent.mkdir(parents=True, exist_ok=True)
                tmp_archive = archive_dest.parent / (archive_dest.name + ".tmp")

                try:
                    delta_uncompressed_size, crc32 = create_delta_archive(
                        self._archive_path,
                        _base_path(runner),
                        tmp_archive,
                        progress_cb=lambda f: GLib.idle_add(
                            self._progress_bar.set_fraction, f
                        ),
                        phase_cb=_delta_phase,
                        file_cb=_delta_file,
                        cancel_event=self._cancel_event,
                    )
                    tmp_archive.rename(archive_dest)
                except CancelledError:
                    _cleanup_tmp(tmp_archive)
                    GLib.idle_add(self._on_import_cancelled)
                    return
                except Exception as exc:
                    _cleanup_tmp(tmp_archive)
                    GLib.idle_add(
                        self._on_import_error,
                        f"Failed to create delta archive: {exc}",
                    )
                    return

                archive_to_upload = str(archive_dest)
                archive_in_place = True
                entry_to_upload = _replace(
                    entry_to_upload,
                    archive=f"apps/{entry_to_upload.id}/{delta_name}",
                    archive_size=archive_dest.stat().st_size,
                    archive_crc32=crc32,
                    install_size_estimate=delta_uncompressed_size,
                )

            # ── Import into repo ───────────────────────────────────────────
            try:
                import_to_repo(
                    repo_root,
                    entry_to_upload,
                    archive_to_upload,
                    images,
                    progress_cb=_progress,
                    phase_cb=_phase,
                    stats_cb=_stats,
                    cancel_event=self._cancel_event,
                    archive_in_place=archive_in_place,
                )
                if category_icon:
                    from cellar.backend.packager import save_category_icon
                    save_category_icon(repo_root, entry_to_upload.category, category_icon)
                GLib.idle_add(self._on_import_done)
            except CancelledError:
                GLib.idle_add(self._on_import_cancelled)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_import_error, str(exc))
            finally:
                if tmp_delta:
                    _shutil.rmtree(tmp_delta, ignore_errors=True)

        threading.Thread(target=_run, daemon=True).start()

    def _on_cancel_progress_clicked(self, _btn) -> None:
        self._cancel_event.set()
        self._progress_label.set_text("Cancelling…")
        self._cancel_progress_btn.set_sensitive(False)

    # ── Import result callbacks (called on main thread via GLib.idle_add) ─

    def _on_import_done(self) -> None:
        self._stop_progress_pulse()
        root = self.get_root()
        if hasattr(root, "_show_toast"):
            root._show_toast("App added to catalogue")
        self.close()
        self._on_done()

    def _on_import_cancelled(self) -> None:
        self._stop_progress_pulse()
        self.set_content_width(560)
        self.set_content_height(-1)
        self._cancel_btn.set_visible(True)
        self._add_btn.set_visible(True)
        self._stack.set_visible_child_name("form")
        self._cancel_progress_btn.set_sensitive(True)

    def _on_import_error(self, message: str) -> None:
        self._stop_progress_pulse()
        self.set_content_width(560)
        self.set_content_height(-1)
        self._cancel_btn.set_visible(True)
        self._add_btn.set_visible(True)
        self._stack.set_visible_child_name("form")
        self._cancel_progress_btn.set_sensitive(True)
        alert = Adw.AlertDialog(
            heading="Import Failed",
            body=message,
        )
        alert.add_response("ok", "OK")
        alert.present(self)

"""Add-app dialog — lets the user add a Bottles backup to a local Cellar repo.

Flow
----
1. User opens a ``.tar.gz`` Bottles backup via a file chooser in the main window.
2. This dialog opens, pre-filling what we can from ``bottle.yml`` inside the
   archive (Name, Runner, DXVK, VKD3D, Environment → category suggestion).
3. User completes the metadata form and optionally attaches images.
4. On "Add to Catalogue" the form is replaced by a progress view while a
   background thread copies the archive and writes the catalogue entry.
5. On success the dialog closes and the main window reloads the browse view.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk


_STRATEGIES = ["safe", "full"]
_STRATEGY_LABELS = ["Safe (preserve user data)", "Full (complete replacement)"]


def _fmt_ul_stats(copied: int, total: int, speed: float) -> str:
    """Format upload progress as e.g. '2.6 MB / 349 MB (1.3 MB/s)'."""
    def _sz(n: int) -> str:
        if n < 1024:
            return f"{n} B"
        if n < 1024 ** 2:
            return f"{n / 1024:.1f} KB"
        if n < 1024 ** 3:
            return f"{n / 1024 ** 2:.1f} MB"
        return f"{n / 1024 ** 3:.2f} GB"

    size_str = f"{_sz(copied)} / {_sz(total)}" if total > 0 else _sz(copied)
    speed_str = f"{_sz(int(speed))}/s" if speed > 0 else "\u2026"
    return f"{size_str} ({speed_str})"


class AddAppDialog(Adw.Dialog):
    """Dialog for adding a Bottles backup archive to a local Cellar repo."""

    def __init__(
        self,
        *,
        archive_path: str,
        repos,          # list[cellar.backend.repo.Repo] — all writable repos
        on_done,        # callable()
    ) -> None:
        super().__init__(title="Add App to Catalogue", content_width=560)

        self._archive_path = archive_path
        self._repos = repos
        self._repo = repos[0]
        self._on_done = on_done
        self._cancel_event = threading.Event()

        # Category list: base categories + any custom ones stored in the repo
        from cellar.backend.packager import BASE_CATEGORIES
        try:
            self._categories: list[str] = self._repo.fetch_categories()
        except Exception:
            self._categories = list(BASE_CATEGORIES)

        # Image selections
        self._icon_path: str = ""
        self._cover_path: str = ""
        self._hero_path: str = ""
        self._logo_path: str = ""
        self._screenshot_paths: list[str] = []

        # Track whether the user has manually edited the ID field
        self._id_user_edited = False
        self._install_size: int = 0

        # Delta packaging state (set after prefill reads bottle.yml)
        self._runner: str = ""         # Runner: field from bottle.yml, e.g. "soda-9.0-1"
        self._use_delta: bool = False  # True when repo has base AND it's installed locally
        self._base_ok: bool = True     # False blocks the Add button
        self._delta_dl_btn: Gtk.Button | None = None  # Download button on delta row
        self._pending_base_entry = None  # BaseEntry for download dialog
        self._pending_base_repo = None   # Repo for download dialog

        self._pulse_id: int | None = None
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

        self._archive_row = Adw.ActionRow(
            title="File",
            subtitle=Path(self._archive_path).name,
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
        identity_group.add(self._name_entry)

        self._id_entry = Adw.EntryRow(title="App ID")
        self._id_entry.connect("changed", self._on_id_changed)
        identity_group.add(self._id_entry)

        self._version_entry = Adw.EntryRow(title="Version")
        self._version_entry.set_text("1.0")
        identity_group.add(self._version_entry)

        page.add(identity_group)

        # ── Details ───────────────────────────────────────────────────────
        details_group = Adw.PreferencesGroup(title="Details")

        self._category_row = Adw.ComboRow(title="Category *")
        cat_model = Gtk.StringList()
        for c in self._categories:
            cat_model.append(c)
        cat_model.append("Custom…")
        self._category_row.set_model(cat_model)
        self._category_row.connect("notify::selected", self._on_category_changed)
        details_group.add(self._category_row)

        self._custom_category_row = Adw.EntryRow(title="Custom category")
        self._custom_category_row.set_visible(False)
        self._custom_category_row.connect("changed", self._on_field_changed)
        details_group.add(self._custom_category_row)

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
        wine_group = Adw.PreferencesGroup(title="Wine Components")

        self._runner_row = Adw.ActionRow(title="Runner")
        self._runner_row.set_subtitle("")
        self._runner_row.set_subtitle_selectable(True)
        self._lock_runner_btn = Gtk.ToggleButton()
        self._lock_runner_btn.set_icon_name("changes-prevent-symbolic")
        self._lock_runner_btn.set_valign(Gtk.Align.CENTER)
        self._lock_runner_btn.set_tooltip_text("Lock runner — users cannot change the runner after install")
        self._lock_runner_btn.connect("toggled", self._on_lock_runner_toggled)
        self._runner_row.add_suffix(self._lock_runner_btn)
        wine_group.add(self._runner_row)

        self._dxvk_row = Adw.ActionRow(title="DXVK")
        self._dxvk_row.set_subtitle("")
        self._dxvk_row.set_subtitle_selectable(True)
        wine_group.add(self._dxvk_row)

        self._vkd3d_row = Adw.ActionRow(title="VKD3D")
        self._vkd3d_row.set_subtitle("")
        self._vkd3d_row.set_subtitle_selectable(True)
        wine_group.add(self._vkd3d_row)

        page.add(wine_group)

        # ── Images ────────────────────────────────────────────────────────
        images_group = Adw.PreferencesGroup(title="Images (optional)")

        self._icon_row = self._make_image_row("Icon", self._pick_icon)
        self._cover_row = self._make_image_row("Cover", self._pick_cover)
        self._hero_row = self._make_image_row("Hero", self._pick_hero)
        self._logo_row = self._make_image_row("Logo", self._pick_logo)
        self._screenshots_row = self._make_image_row(
            "Screenshots", self._pick_screenshots, multi=True
        )

        images_group.add(self._icon_row)
        images_group.add(self._cover_row)
        images_group.add(self._hero_row)
        images_group.add(self._logo_row)
        images_group.add(self._screenshots_row)
        page.add(images_group)

        # ── Install ───────────────────────────────────────────────────────
        install_group = Adw.PreferencesGroup(title="Install")

        self._strategy_row = Adw.ComboRow(title="Update Strategy")
        strat_model = Gtk.StringList()
        for label in _STRATEGY_LABELS:
            strat_model.append(label)
        self._strategy_row.set_model(strat_model)
        install_group.add(self._strategy_row)

        self._entry_point_entry = Adw.EntryRow(title="Entry Point (optional)")
        self._entry_point_entry.set_tooltip_text(
            "Relative path from drive_c to the main .exe, e.g. Program Files/App/app.exe"
        )
        install_group.add(self._entry_point_entry)

        page.add(install_group)

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
        """Read bottle.yml in a background thread, then reveal the form."""
        from cellar.backend.packager import read_bottle_yml

        yml = read_bottle_yml(self._archive_path)
        runner = yml.get("Runner", "")

        def _apply() -> None:
            if self._pulse_id is not None:
                GLib.source_remove(self._pulse_id)
                self._pulse_id = None
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
            if env.lower() == "game" and "Games" in self._categories:
                self._category_row.set_selected(self._categories.index("Games"))

            if runner:
                self._runner = runner
                self._check_delta_base(runner)

            self._stack.set_visible_child_name("form")

        GLib.idle_add(_apply)

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
            if self._runner:
                self._check_delta_base(self._runner)

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

    def _on_category_changed(self, _row, _param) -> None:
        is_custom = self._category_row.get_selected() == len(self._categories)
        self._custom_category_row.set_visible(is_custom)
        if is_custom:
            self._custom_category_row.grab_focus()
        self._update_add_button()

    def _on_field_changed(self, *_args) -> None:
        self._update_add_button()

    def _get_category(self) -> str:
        idx = self._category_row.get_selected()
        if idx == len(self._categories):  # "Custom…" sentinel
            return self._custom_category_row.get_text().strip()
        if 0 <= idx < len(self._categories):
            return self._categories[idx]
        return ""

    def _update_add_button(self) -> None:
        name_ok = bool(self._name_entry.get_text().strip())
        category_ok = bool(self._get_category())
        self._add_btn.set_sensitive(name_ok and category_ok and self._base_ok)

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
            self._icon_row.set_subtitle(Path(self._icon_path).name)

    def _pick_cover(self, _btn) -> None:
        self._pick_image("Select Cover", False, self._on_cover_chosen)

    def _on_cover_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._cover_path = chooser.get_file().get_path()
            self._cover_row.set_subtitle(Path(self._cover_path).name)

    def _pick_hero(self, _btn) -> None:
        self._pick_image("Select Hero Banner", False, self._on_hero_chosen)

    def _on_hero_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._hero_path = chooser.get_file().get_path()
            self._hero_row.set_subtitle(Path(self._hero_path).name)

    def _pick_logo(self, _btn) -> None:
        self._pick_image("Select Logo (transparent PNG)", False, self._on_logo_chosen)

    def _on_logo_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._logo_path = chooser.get_file().get_path()
            self._logo_row.set_subtitle(Path(self._logo_path).name)

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
        strategy = _STRATEGIES[self._strategy_row.get_selected()]
        entry_point = self._entry_point_entry.get_text().strip()

        if not app_id:
            from cellar.backend.packager import slugify
            app_id = slugify(name)

        # Build archive filename: apps/<id>/<basename of source>
        archive_filename = Path(self._archive_path).name
        archive_rel = f"apps/{app_id}/{archive_filename}"
        archive_size = Path(self._archive_path).stat().st_size

        # Build image relative paths (only for images the user picked)
        icon_ext = ".png" if self._icon_path and Path(self._icon_path).suffix.lower() == ".ico" else (Path(self._icon_path).suffix if self._icon_path else "")
        icon_rel = f"apps/{app_id}/icon{icon_ext}" if self._icon_path else ""
        cover_rel = f"apps/{app_id}/cover{Path(self._cover_path).suffix}" if self._cover_path else ""
        hero_rel = f"apps/{app_id}/hero{Path(self._hero_path).suffix}" if self._hero_path else ""
        logo_rel = f"apps/{app_id}/logo.png" if self._logo_path else ""
        screenshot_rels = tuple(
            f"apps/{app_id}/screenshots/{i + 1:02d}{Path(p).suffix}"
            for i, p in enumerate(self._screenshot_paths)
        )

        from cellar.models.app_entry import AppEntry, BuiltWith

        built_with = BuiltWith(runner=runner, dxvk=dxvk, vkd3d=vkd3d) if runner else None

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
            screenshots=screenshot_rels,
            archive=archive_rel,
            archive_size=archive_size,
            install_size_estimate=self._install_size,
            built_with=built_with,
            update_strategy=strategy,
            entry_point=entry_point,
            base_runner=self._runner if self._use_delta else "",
            lock_runner=self._lock_runner_btn.get_active(),
        )

        images = {
            "icon": self._icon_path,
            "cover": self._cover_path,
            "hero": self._hero_path,
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
        self._progress_label.set_text("Copying archive\u2026")

        repo_root = self._repo.writable_path()

        use_delta = self._use_delta
        runner = self._runner

        def _run():
            import shutil as _shutil
            import tempfile
            from dataclasses import replace as _replace
            from cellar.backend.packager import (
                BASE_CATEGORIES,
                CancelledError,
                add_catalogue_category,
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
                    GLib.idle_add(self._progress_bar.set_text, _fmt_ul_stats(copied, total, speed))

            def _progress(fraction: float) -> None:
                GLib.idle_add(self._progress_bar.set_fraction, fraction)

            # ── Delta archive creation ─────────────────────────────────────
            archive_to_upload = self._archive_path
            entry_to_upload = entry
            tmp_delta: str | None = None

            if use_delta:
                from cellar.backend.packager import create_delta_archive
                from cellar.backend.base_store import base_path as _base_path

                GLib.idle_add(self._progress_bar.set_fraction, 0.0)
                GLib.idle_add(self._progress_bar.set_text, "")

                def _delta_phase(label: str) -> None:
                    GLib.idle_add(self._progress_label.set_text, label)
                    GLib.idle_add(self._progress_bar.set_text, "")

                def _delta_file(current: int, total: int) -> None:
                    if total > 0:
                        GLib.idle_add(
                            self._progress_bar.set_text,
                            f"File {current} / {total}",
                        )
                    else:
                        GLib.idle_add(
                            self._progress_bar.set_text,
                            f"File {current}",
                        )

                tmp_delta = tempfile.mkdtemp(prefix="cellar-delta-upload-")
                # Delta archives are recompressed as .tar.zst (zstd level 3).
                orig_name = Path(self._archive_path).name
                delta_name = (
                    orig_name[: -len(".tar.gz")] + ".tar.zst"
                    if orig_name.endswith(".tar.gz")
                    else orig_name
                )
                delta_path = Path(tmp_delta) / delta_name

                try:
                    delta_uncompressed_size = create_delta_archive(
                        self._archive_path,
                        _base_path(runner),
                        delta_path,
                        progress_cb=lambda f: GLib.idle_add(
                            self._progress_bar.set_fraction, f
                        ),
                        phase_cb=_delta_phase,
                        file_cb=_delta_file,
                        cancel_event=self._cancel_event,
                    )
                except CancelledError:
                    _shutil.rmtree(tmp_delta, ignore_errors=True)
                    GLib.idle_add(self._on_import_cancelled)
                    return
                except Exception as exc:
                    _shutil.rmtree(tmp_delta, ignore_errors=True)
                    GLib.idle_add(
                        self._on_import_error,
                        f"Failed to create delta archive: {exc}",
                    )
                    return

                archive_to_upload = str(delta_path)
                entry_to_upload = _replace(
                    entry_to_upload,
                    archive=f"apps/{entry_to_upload.id}/{delta_name}",
                    archive_size=delta_path.stat().st_size,
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
                )
                if category not in BASE_CATEGORIES:
                    try:
                        add_catalogue_category(repo_root, category)
                    except Exception as exc:
                        import logging
                        logging.getLogger(__name__).warning(
                            "Could not persist custom category %r: %s", category, exc
                        )
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
        # Show a toast on the main window
        root = self.get_root()
        if hasattr(root, "_show_toast"):
            root._show_toast("App added to catalogue")
        self.close()
        self._on_done()

    def _on_import_cancelled(self) -> None:
        self.set_content_width(560)
        self.set_content_height(-1)
        self._cancel_btn.set_visible(True)
        self._add_btn.set_visible(True)
        self._stack.set_visible_child_name("form")
        self._cancel_progress_btn.set_sensitive(True)

    def _on_import_error(self, message: str) -> None:
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

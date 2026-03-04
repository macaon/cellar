"""Edit-app dialog — lets the repo maintainer update or delete a catalogue entry.

Flow
----
1. Opened from the detail view's Edit button (writable repos only).
2. All form fields are pre-filled from the existing ``AppEntry``.
3. The user may update any metadata field, swap individual images, or replace
   the archive entirely.
4. On "Save Changes" a background thread calls ``update_in_repo()``.
5. The "Danger Zone" section exposes a "Delete Entry…" button which prompts
   the user to either delete or move the archive before removing the entry
   from the catalogue.
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


class EditAppDialog(Adw.Dialog):
    """Dialog for editing or deleting an existing catalogue entry."""

    def __init__(
        self,
        *,
        entry,          # AppEntry
        repo,           # cellar.backend.repo.Repo
        on_done,        # callable() — called after a successful save
        on_deleted,     # callable() — called after a successful delete
    ) -> None:
        super().__init__(title="Edit Catalogue Entry", content_width=560)

        self._old_entry = entry
        self._repo = repo
        self._on_done = on_done
        self._on_deleted = on_deleted
        self._cancel_event = threading.Event()

        # Optional archive replacement
        self._new_archive_src: str = ""

        # Image selections
        # None = keep existing; "" = clear from catalogue; str = replace with new file
        self._icon_path: str | None = None
        self._cover_path: str | None = None
        self._logo_path: str | None = None

        # Screenshot list + dirty flag
        self._screenshot_paths: list[str] = []   # effective local paths
        self._screenshots_dirty: bool = False    # True once user adds or removes anything

        self._build_ui()
        self._prefill()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        header.pack_start(cancel_btn)

        self._save_btn = Gtk.Button(label="Save Changes")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.set_sensitive(False)
        self._save_btn.connect("clicked", self._on_save_clicked)
        header.pack_end(self._save_btn)

        toolbar_view.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.add_named(self._build_form(), "form")
        self._stack.add_named(self._build_progress(), "progress")
        self._stack.add_named(self._build_spinner(), "spinner")
        self._stack.set_visible_child_name("form")

        toolbar_view.set_content(self._stack)
        self.set_child(toolbar_view)

    def _build_form(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        scroll.set_propagate_natural_height(True)

        page = Adw.PreferencesPage()
        scroll.set_child(page)

        # ── Archive ───────────────────────────────────────────────────────
        archive_group = Adw.PreferencesGroup(title="Archive")
        self._archive_row = Adw.ActionRow(
            title="Current Archive",
            subtitle=GLib.markup_escape_text(Path(self._old_entry.archive).name) if self._old_entry.archive else "—",
        )
        replace_btn = Gtk.Button(label="Replace…")
        replace_btn.set_valign(Gtk.Align.CENTER)
        replace_btn.connect("clicked", self._pick_archive)
        self._archive_row.add_suffix(replace_btn)
        archive_group.add(self._archive_row)
        page.add(archive_group)

        # ── Identity ──────────────────────────────────────────────────────
        identity_group = Adw.PreferencesGroup(title="Identity")

        self._name_entry = Adw.EntryRow(title="Name *")
        self._name_entry.connect("changed", self._on_name_changed)
        identity_group.add(self._name_entry)

        # App ID is read-only — renaming would break installed records
        self._id_row = Adw.ActionRow(title="App ID", subtitle=self._old_entry.id)
        self._id_row.set_subtitle_selectable(True)
        identity_group.add(self._id_row)

        self._version_entry = Adw.EntryRow(title="Version")
        identity_group.add(self._version_entry)

        page.add(identity_group)

        # ── Category ──────────────────────────────────────────────────────
        from cellar.backend.packager import BASE_CATEGORIES
        self._category_row = Adw.ComboRow(title="Category")
        self._category_row.set_model(Gtk.StringList.new(BASE_CATEGORIES))
        identity_group.add(self._category_row)

        # ── Details ───────────────────────────────────────────────────────
        details_group = Adw.PreferencesGroup(title="Details")

        self._summary_entry = Adw.EntryRow(title="Summary")
        details_group.add(self._summary_entry)

        page.add(details_group)

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
        self._runner_row.set_subtitle_selectable(True)
        self._lock_runner_btn = Gtk.ToggleButton()
        self._lock_runner_btn.set_icon_name("changes-prevent-symbolic")
        self._lock_runner_btn.set_valign(Gtk.Align.CENTER)
        self._lock_runner_btn.set_tooltip_text("Lock runner — users cannot change the runner after install")
        self._lock_runner_btn.connect("toggled", self._on_lock_runner_toggled)
        self._runner_row.add_suffix(self._lock_runner_btn)
        wine_group.add(self._runner_row)

        self._dxvk_row = Adw.ActionRow(title="DXVK")
        self._dxvk_row.set_subtitle_selectable(True)
        wine_group.add(self._dxvk_row)

        self._vkd3d_row = Adw.ActionRow(title="VKD3D")
        self._vkd3d_row.set_subtitle_selectable(True)
        wine_group.add(self._vkd3d_row)

        page.add(wine_group)

        # ── Images ────────────────────────────────────────────────────────
        images_group = Adw.PreferencesGroup(title="Images")

        self._icon_row, self._icon_clear_btn = self._make_image_row("Icon", self._pick_icon)
        self._cover_row, self._cover_clear_btn = self._make_image_row("Cover", self._pick_cover)
        self._logo_row, self._logo_clear_btn = self._make_image_row("Logo", self._pick_logo)
        self._hide_title_btn = Gtk.ToggleButton()
        self._hide_title_btn.set_icon_name("view-conceal-symbolic")
        self._hide_title_btn.set_valign(Gtk.Align.CENTER)
        self._hide_title_btn.set_tooltip_text("Hide title — logo contains the app name")
        self._logo_row.add_suffix(self._hide_title_btn)

        self._icon_clear_btn.connect("clicked", self._on_icon_clear)
        self._cover_clear_btn.connect("clicked", self._on_cover_clear)
        self._logo_clear_btn.connect("clicked", self._on_logo_clear)

        images_group.add(self._icon_row)
        images_group.add(self._cover_row)
        images_group.add(self._logo_row)
        page.add(images_group)

        # ── Screenshots ───────────────────────────────────────────────────
        ss_group = Adw.PreferencesGroup(title="Screenshots")

        self._ss_empty_label = Gtk.Label(label="No screenshots")
        self._ss_empty_label.add_css_class("dim-label")
        self._ss_empty_label.set_margin_top(6)
        self._ss_empty_label.set_margin_bottom(6)
        ss_group.add(self._ss_empty_label)

        self._ss_listbox = Gtk.ListBox()
        self._ss_listbox.add_css_class("boxed-list")
        self._ss_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._ss_listbox.set_visible(False)
        ss_group.add(self._ss_listbox)

        add_ss_row = Adw.ActionRow(title="Add Screenshots…")
        add_ss_row.set_activatable(True)
        add_ss_row.add_prefix(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        add_ss_row.connect("activated", lambda _: self._pick_screenshots(None))
        ss_group.add(add_ss_row)

        page.add(ss_group)

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

        # ── Danger Zone ───────────────────────────────────────────────────
        danger_group = Adw.PreferencesGroup(title="Danger Zone")

        delete_btn = Gtk.Button(label="Delete Entry…")
        delete_btn.add_css_class("destructive-action")
        delete_btn.set_halign(Gtk.Align.START)
        delete_btn.set_margin_top(6)
        delete_btn.set_margin_bottom(6)
        delete_btn.connect("clicked", self._on_delete_clicked)
        danger_group.add(delete_btn)

        page.add(danger_group)

        return scroll

    def _make_image_row(self, label: str, handler) -> tuple[Adw.ActionRow, Gtk.Button]:
        row = Adw.ActionRow(title=label)
        row.set_subtitle("No image set")

        clear_btn = Gtk.Button(icon_name="edit-clear-symbolic", tooltip_text="Remove image")
        clear_btn.add_css_class("flat")
        clear_btn.set_valign(Gtk.Align.CENTER)
        clear_btn.set_sensitive(False)
        row.add_suffix(clear_btn)

        change_btn = Gtk.Button(label="Change…")
        change_btn.set_valign(Gtk.Align.CENTER)
        change_btn.connect("clicked", handler)
        row.add_suffix(change_btn)

        return row, clear_btn

    def _build_progress(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(24)
        box.set_margin_end(24)

        self._progress_label = Gtk.Label(label="Saving changes\u2026", xalign=0)
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

    def _build_spinner(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(48)
        box.set_margin_bottom(48)
        box.set_margin_start(24)
        box.set_margin_end(24)

        spinner = Gtk.Spinner(spinning=True)
        spinner.set_size_request(32, 32)
        spinner.set_halign(Gtk.Align.CENTER)

        self._spinner_label = Gtk.Label(label="Deleting entry…")
        self._spinner_label.add_css_class("dim-label")

        self._cancel_spinner_btn = Gtk.Button(label="Cancel")
        self._cancel_spinner_btn.set_halign(Gtk.Align.CENTER)
        self._cancel_spinner_btn.connect("clicked", self._on_cancel_spinner_clicked)

        box.append(spinner)
        box.append(self._spinner_label)
        box.append(self._cancel_spinner_btn)
        return box

    # ── Pre-fill ──────────────────────────────────────────────────────────

    def _prefill(self) -> None:
        e = self._old_entry

        self._name_entry.set_text(e.name)
        self._version_entry.set_text(e.version)
        from cellar.backend.packager import BASE_CATEGORIES
        if e.category in BASE_CATEGORIES:
            self._category_row.set_selected(BASE_CATEGORIES.index(e.category))
        self._summary_entry.set_text(e.summary or "")

        if e.description:
            self._desc_view.get_buffer().set_text(e.description)

        self._developer_entry.set_text(e.developer or "")
        self._publisher_entry.set_text(e.publisher or "")
        if e.release_year:
            self._year_entry.set_text(str(e.release_year))

        bw = e.built_with
        if bw:
            self._runner_row.set_subtitle(bw.runner or "")
            self._dxvk_row.set_subtitle(bw.dxvk or "")
            self._vkd3d_row.set_subtitle(bw.vkd3d or "")
        if e.lock_runner:
            self._lock_runner_btn.set_active(True)

        # Single images — show current filename + enable clear button
        if e.icon:
            self._icon_row.set_subtitle(GLib.markup_escape_text(Path(e.icon).name))
            self._icon_clear_btn.set_sensitive(True)
        if e.cover:
            self._cover_row.set_subtitle(GLib.markup_escape_text(Path(e.cover).name))
            self._cover_clear_btn.set_sensitive(True)
        if e.logo:
            self._logo_row.set_subtitle(GLib.markup_escape_text(Path(e.logo).name))
            self._logo_clear_btn.set_sensitive(True)
        if e.hide_title:
            self._hide_title_btn.set_active(True)

        # Screenshots — resolve relative paths to absolute local paths
        try:
            repo_root = self._repo.writable_path()
            self._screenshot_paths = [str(repo_root / rel) for rel in e.screenshots]
        except Exception:
            self._screenshot_paths = []
        self._rebuild_screenshot_list()

        strategy = e.update_strategy or "safe"
        if strategy in _STRATEGIES:
            self._strategy_row.set_selected(_STRATEGIES.index(strategy))

        self._entry_point_entry.set_text(e.entry_point or "")

        self._update_save_button()

    # ── Form validation ───────────────────────────────────────────────────

    def _get_category(self) -> str:
        from cellar.backend.packager import BASE_CATEGORIES
        idx = self._category_row.get_selected()
        return BASE_CATEGORIES[idx] if 0 <= idx < len(BASE_CATEGORIES) else ""

    def _on_lock_runner_toggled(self, btn) -> None:
        if btn.get_active():
            btn.add_css_class("destructive-action")
        else:
            btn.remove_css_class("destructive-action")

    def _on_name_changed(self, _entry) -> None:
        self._update_save_button()

    def _update_save_button(self) -> None:
        name_ok = bool(self._name_entry.get_text().strip())
        self._save_btn.set_sensitive(name_ok)

    # ── Archive picker ────────────────────────────────────────────────────

    def _pick_archive(self, _btn) -> None:
        chooser = Gtk.FileChooserNative(
            title="Select Replacement Archive",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
        )
        f = Gtk.FileFilter()
        f.set_name("Bottles backup (*.tar.gz)")
        f.add_pattern("*.tar.gz")
        chooser.add_filter(f)
        chooser.connect("response", self._on_archive_chosen, chooser)
        chooser.show()

    def _on_archive_chosen(self, _chooser, response, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        self._new_archive_src = chooser.get_file().get_path()
        self._archive_row.set_subtitle(GLib.markup_escape_text(Path(self._new_archive_src).name))
        # Refresh Wine component rows from the new archive in a background thread
        threading.Thread(target=self._read_new_archive_yml, daemon=True).start()

    def _read_new_archive_yml(self) -> None:
        from cellar.backend.packager import read_bottle_yml

        yml = read_bottle_yml(self._new_archive_src)
        runner = yml.get("Runner", "")
        dxvk = yml.get("DXVK", "")
        vkd3d = yml.get("VKD3D", "")
        if runner:
            GLib.idle_add(self._runner_row.set_subtitle, runner)
        if dxvk:
            GLib.idle_add(self._dxvk_row.set_subtitle, dxvk)
        if vkd3d:
            GLib.idle_add(self._vkd3d_row.set_subtitle, vkd3d)

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
            self._icon_clear_btn.set_sensitive(True)

    def _pick_cover(self, _btn) -> None:
        self._pick_image("Select Cover", False, self._on_cover_chosen)

    def _on_cover_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._cover_path = chooser.get_file().get_path()
            self._cover_row.set_subtitle(GLib.markup_escape_text(Path(self._cover_path).name))
            self._cover_clear_btn.set_sensitive(True)

    def _pick_logo(self, _btn) -> None:
        self._pick_image("Select Logo (transparent PNG)", False, self._on_logo_chosen)

    def _on_logo_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._logo_path = chooser.get_file().get_path()
            self._logo_row.set_subtitle(GLib.markup_escape_text(Path(self._logo_path).name))
            self._logo_clear_btn.set_sensitive(True)
            if not self._old_entry.logo and not self._hide_title_btn.get_active():
                self._hide_title_btn.set_active(True)

    def _pick_screenshots(self, _btn) -> None:
        self._pick_image("Select Screenshots", True, self._on_screenshots_chosen)

    def _on_screenshots_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            files = chooser.get_files()
            new_paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
            self._screenshot_paths.extend(new_paths)
            self._screenshots_dirty = True
            self._rebuild_screenshot_list()

    # ── Image clear handlers ──────────────────────────────────────────────

    def _on_icon_clear(self, _btn) -> None:
        self._icon_path = ""
        self._icon_row.set_subtitle("Will be removed")
        self._icon_clear_btn.set_sensitive(False)

    def _on_cover_clear(self, _btn) -> None:
        self._cover_path = ""
        self._cover_row.set_subtitle("Will be removed")
        self._cover_clear_btn.set_sensitive(False)

    def _on_logo_clear(self, _btn) -> None:
        self._logo_path = ""
        self._logo_row.set_subtitle("Will be removed")
        self._logo_clear_btn.set_sensitive(False)

    # ── Screenshot list helpers ───────────────────────────────────────────

    def _rebuild_screenshot_list(self) -> None:
        # Hide before removing rows so GTK doesn't try to move focus to a
        # row that is being unparented (avoids gtk_list_box_row_grab_focus assertion).
        self._ss_listbox.set_visible(False)
        row = self._ss_listbox.get_row_at_index(0)
        while row is not None:
            self._ss_listbox.remove(row)
            row = self._ss_listbox.get_row_at_index(0)
        for path in self._screenshot_paths:
            self._ss_listbox.append(self._make_ss_row(path))
        has = bool(self._screenshot_paths)
        self._ss_listbox.set_visible(has)
        self._ss_empty_label.set_visible(not has)

    def _make_ss_row(self, path: str) -> Adw.ActionRow:
        row = Adw.ActionRow(title=Path(path).name)
        btn = Gtk.Button(icon_name="edit-delete-symbolic", tooltip_text="Remove")
        btn.add_css_class("flat")
        btn.add_css_class("circular")
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect("clicked", lambda _b, p=path: self._remove_screenshot(p))
        row.add_suffix(btn)
        return row

    def _remove_screenshot(self, path: str) -> None:
        self._screenshot_paths.remove(path)
        self._screenshots_dirty = True
        self._rebuild_screenshot_list()

    # ── Save flow ─────────────────────────────────────────────────────────

    def _on_cancel_clicked(self, _btn) -> None:
        self.close()

    def _on_save_clicked(self, _btn) -> None:
        e = self._old_entry
        app_id = e.id
        name = self._name_entry.get_text().strip()
        version = self._version_entry.get_text().strip() or e.version
        category = self._get_category() or e.category
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

        # Archive: use new filename if replaced, otherwise keep old path
        if self._new_archive_src:
            archive_rel = f"apps/{app_id}/{Path(self._new_archive_src).name}"
        else:
            archive_rel = e.archive

        # Single images: None=keep existing, ""=clear, str=new file
        if self._icon_path is None:
            icon_rel = e.icon
        elif self._icon_path == "":
            icon_rel = ""
        else:
            ext = ".png" if Path(self._icon_path).suffix.lower() == ".ico" else Path(self._icon_path).suffix
            icon_rel = f"apps/{app_id}/icon{ext}"

        if self._cover_path is None:
            cover_rel = e.cover
        elif self._cover_path == "":
            cover_rel = ""
        else:
            cover_rel = f"apps/{app_id}/cover{Path(self._cover_path).suffix}"

        hero_rel = e.hero

        if self._logo_path is None:
            logo_rel = e.logo
        elif self._logo_path == "":
            logo_rel = ""
        else:
            logo_rel = f"apps/{app_id}/logo.png"

        # Screenshots: dirty flag controls whether to send None (keep) or list (replace/clear)
        if self._screenshots_dirty:
            screenshot_rels = tuple(
                f"apps/{app_id}/screenshots/{i + 1:02d}{Path(p).suffix}"
                for i, p in enumerate(self._screenshot_paths)
            )
        else:
            screenshot_rels = e.screenshots

        from cellar.models.app_entry import AppEntry, BuiltWith

        built_with = BuiltWith(runner=runner, dxvk=dxvk, vkd3d=vkd3d) if runner else None

        new_entry = AppEntry(
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
            archive_size=e.archive_size,
            archive_crc32="" if self._new_archive_src else e.archive_crc32,
            install_size_estimate=e.install_size_estimate,
            built_with=built_with,
            update_strategy=strategy,
            entry_point=entry_point,
            compatibility_notes=e.compatibility_notes,
            changelog=e.changelog,
            lock_runner=self._lock_runner_btn.get_active(),
        )

        images = {
            "icon": self._icon_path,      # None / "" / path
            "cover": self._cover_path,
            "logo": self._logo_path,
            "screenshots": self._screenshot_paths if self._screenshots_dirty else None,
        }

        self._cancel_event.clear()
        self._saved_entry = new_entry  # made available to _on_save_done
        self._stack.set_visible_child_name("progress")
        self._progress_bar.set_fraction(0.0)
        self._progress_label.set_text("Saving changes\u2026")

        repo_root = self._repo.writable_path()

        def _run():
            from cellar.backend.packager import (
                CancelledError,
                update_in_repo,
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

            try:
                update_in_repo(
                    repo_root,
                    e,
                    new_entry,
                    images,
                    self._new_archive_src or None,
                    progress_cb=_progress,
                    phase_cb=_phase,
                    stats_cb=_stats,
                    cancel_event=self._cancel_event,
                )
                GLib.idle_add(self._on_save_done)
            except CancelledError:
                GLib.idle_add(self._on_save_cancelled)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_save_error, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _on_cancel_progress_clicked(self, _btn) -> None:
        self._cancel_event.set()
        self._progress_label.set_text("Cancelling…")
        self._cancel_progress_btn.set_sensitive(False)

    def _on_save_done(self) -> None:
        self.close()
        self._on_done(self._saved_entry)

    def _on_save_cancelled(self) -> None:
        self._stack.set_visible_child_name("form")
        self._cancel_progress_btn.set_sensitive(True)

    def _on_save_error(self, message: str) -> None:
        self._stack.set_visible_child_name("form")
        self._cancel_progress_btn.set_sensitive(True)
        alert = Adw.AlertDialog(heading="Save Failed", body=message)
        alert.add_response("ok", "OK")
        alert.present(self)

    # ── Delete flow ───────────────────────────────────────────────────────

    def _on_delete_clicked(self, _btn) -> None:
        alert = Adw.AlertDialog(
            heading=f'Delete "{self._old_entry.name}"?',
            body=(
                "The archive file must be removed from or moved out of the repository. "
                "This cannot be undone."
            ),
        )
        alert.add_response("cancel", "Cancel")
        alert.add_response("move", "Move Archive…")
        alert.add_response("delete", "Delete Archive")
        alert.set_response_appearance("move", Adw.ResponseAppearance.SUGGESTED)
        alert.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.set_default_response("cancel")
        alert.connect("response", self._on_delete_response)
        alert.present(self)

    def _on_delete_response(self, _alert, response: str) -> None:
        if response == "cancel":
            return
        elif response == "delete":
            self._do_delete(move_to=None)
        elif response == "move":
            chooser = Gtk.FileChooserNative(
                title="Move Archive To…",
                transient_for=self.get_root(),
                action=Gtk.FileChooserAction.SELECT_FOLDER,
            )
            chooser.connect("response", self._on_move_folder_chosen, chooser)
            chooser.show()

    def _on_move_folder_chosen(self, _chooser, response, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        folder = chooser.get_file().get_path()
        self._do_delete(move_to=folder)

    def _do_delete(self, *, move_to: str | None) -> None:
        self._cancel_event.clear()
        self._stack.set_visible_child_name("spinner")
        self._spinner_label.set_text("Deleting entry…")

        repo_root = self._repo.writable_path()

        def _run():
            from cellar.backend.packager import CancelledError, remove_from_repo

            try:
                remove_from_repo(
                    repo_root,
                    self._old_entry,
                    move_archive_to=move_to,
                    cancel_event=self._cancel_event,
                )
                GLib.idle_add(self._on_delete_done)
            except CancelledError:
                GLib.idle_add(self._on_delete_cancelled)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_delete_error, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _on_cancel_spinner_clicked(self, _btn) -> None:
        self._cancel_event.set()
        self._spinner_label.set_text("Cancelling…")
        self._cancel_spinner_btn.set_sensitive(False)

    def _on_delete_done(self) -> None:
        root = self.get_root()
        if hasattr(root, "_show_toast"):
            root._show_toast("Entry deleted")
        self.close()
        self._on_deleted()

    def _on_delete_cancelled(self) -> None:
        self._stack.set_visible_child_name("form")
        self._cancel_spinner_btn.set_sensitive(True)

    def _on_delete_error(self, message: str) -> None:
        self._stack.set_visible_child_name("form")
        self._cancel_spinner_btn.set_sensitive(True)
        alert = Adw.AlertDialog(heading="Delete Failed", body=message)
        alert.add_response("ok", "OK")
        alert.present(self)

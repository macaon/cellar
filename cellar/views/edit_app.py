"""Edit-app dialog — lets the repo maintainer update or delete a catalogue entry.

Flow
----
1. Opened from the detail view's Edit button (writable repos only).
2. All form fields are pre-filled from the existing ``AppEntry``.
3. The user may update any metadata field or swap individual images.
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

from cellar.utils.async_work import run_in_background
from cellar.utils.progress import fmt_stats as _fmt_stats
from cellar.views.widgets import make_progress_page

_STRATEGIES = ["safe", "full"]
_STRATEGY_LABELS = ["Safe (preserve user data)", "Full (complete replacement)"]


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
        super().__init__(title="Edit Catalogue Entry", content_width=1100, content_height=680)

        self._old_entry = entry
        self._repo = repo
        self._on_done = on_done
        self._on_deleted = on_deleted
        self._cancel_event = threading.Event()

        # Image selections
        # None = keep existing; "" = clear from catalogue; str = replace with new file
        self._icon_path: str | None = None
        self._cover_path: str | None = None
        self._logo_path: str | None = None

        # Screenshot dirty flag — True once grid has been touched
        self._screenshots_dirty: bool = False

        # Check whether the app is installed locally (determines entry-point editability)
        self._locally_installed: bool = self._check_locally_installed(entry)

        # Load category list from repo
        from cellar.backend.packager import BASE_CATEGORIES as _BASE_CATS
        try:
            self._categories = self._repo.fetch_categories()
        except Exception:
            self._categories = list(_BASE_CATS)

        self._build_ui()
        self._prefill()

    @staticmethod
    def _check_locally_installed(entry) -> bool:
        """Return True if the app has a local prefix/install we can browse."""
        try:
            if entry.platform == "linux":
                from cellar.backend.database import get_installed
                rec = get_installed(entry.id)
                return bool(rec and rec.get("install_path"))
            else:
                from cellar.backend.umu import prefixes_dir
                return (prefixes_dir() / entry.id / "drive_c").is_dir()
        except Exception:
            return False

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
        # Single scroll wraps both panes — no nested scrolling
        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        scroll.set_child(hbox)

        # ── Left column: metadata (fixed width, never grows) ─────────────
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left_box.set_size_request(360, -1)
        left_box.set_hexpand(False)

        page = Adw.PreferencesPage()
        page.set_vexpand(True)
        page.set_hexpand(False)
        left_box.append(page)
        hbox.append(left_box)

        # Identity
        identity_group = Adw.PreferencesGroup()

        self._name_entry = Adw.EntryRow(title="Title *")
        self._name_entry.connect("changed", self._on_name_changed)
        steam_btn = Gtk.Button(icon_name="system-search-symbolic")
        steam_btn.add_css_class("flat")
        steam_btn.set_valign(Gtk.Align.CENTER)
        steam_btn.set_tooltip_text("Look up on Steam")
        steam_btn.connect("clicked", self._on_steam_lookup)
        self._name_entry.add_suffix(steam_btn)
        identity_group.add(self._name_entry)

        self._id_row = Adw.ActionRow(title="App ID", subtitle=self._old_entry.id)
        self._id_row.set_subtitle_selectable(True)
        identity_group.add(self._id_row)

        page.add(identity_group)

        # Details
        details_group = Adw.PreferencesGroup(title="Details")

        self._version_entry = Adw.EntryRow(title="Version")
        details_group.add(self._version_entry)

        self._category_row = Adw.ComboRow(title="Category")
        self._category_row.set_model(Gtk.StringList.new(self._categories))
        details_group.add(self._category_row)

        self._developer_entry = Adw.EntryRow(title="Developer")
        details_group.add(self._developer_entry)

        self._publisher_entry = Adw.EntryRow(title="Publisher")
        details_group.add(self._publisher_entry)

        self._year_entry = Adw.EntryRow(title="Release Year")
        details_group.add(self._year_entry)

        self._steam_appid_entry = Adw.EntryRow(title="Steam App ID")
        self._steam_appid_entry.set_tooltip_text(
            "Used to set GAMEID for protonfixes. Leave empty to use GAMEID=0."
        )
        details_group.add(self._steam_appid_entry)

        self._website_entry = Adw.EntryRow(title="Website")
        details_group.add(self._website_entry)

        self._genres_entry = Adw.EntryRow(title="Genres")
        self._genres_entry.set_tooltip_text("Comma-separated, e.g. Action, RPG")
        details_group.add(self._genres_entry)

        page.add(details_group)

        # Descriptions — Summary + Description editor in one unified card
        desc_group = Adw.PreferencesGroup(title="Descriptions")

        desc_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        desc_outer.add_css_class("card")

        # Summary row inside the card
        self._summary_entry = Adw.EntryRow(title="Summary")
        desc_outer.append(self._summary_entry)

        desc_outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Description header with formatting toolbar
        desc_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        desc_header.set_margin_top(8)
        desc_header.set_margin_bottom(4)
        desc_header.set_margin_start(12)
        desc_header.set_margin_end(6)
        desc_label = Gtk.Label(label="Description")
        desc_label.set_hexpand(True)
        desc_label.set_xalign(0)
        desc_header.append(desc_label)

        fmt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        bold_btn = Gtk.Button(label="B")
        bold_btn.add_css_class("flat")
        bold_btn.set_tooltip_text("Bold (<b>text</b>)")
        bold_btn.connect("clicked", lambda _: self._desc_fmt_wrap("b"))
        italic_btn = Gtk.Button(label="I")
        italic_btn.add_css_class("flat")
        italic_btn.set_tooltip_text("Italic (<i>text</i>)")
        italic_btn.connect("clicked", lambda _: self._desc_fmt_wrap("i"))
        h2_btn = Gtk.Button(label="H2")
        h2_btn.add_css_class("flat")
        h2_btn.set_tooltip_text("Heading (<h2>text</h2>)")
        h2_btn.connect("clicked", lambda _: self._desc_fmt_wrap("h2"))
        bullet_btn = Gtk.Button(icon_name="view-list-bullet-symbolic")
        bullet_btn.add_css_class("flat")
        bullet_btn.set_tooltip_text("Bullet list (<li>item</li>)")
        bullet_btn.connect("clicked", lambda _: self._desc_fmt_bullet())
        hr_btn = Gtk.Button(label="—")
        hr_btn.add_css_class("flat")
        hr_btn.set_tooltip_text("Horizontal rule (<hr>)")
        hr_btn.connect("clicked", lambda _: self._desc_fmt_hr())
        fmt_box.append(bold_btn)
        fmt_box.append(italic_btn)
        fmt_box.append(h2_btn)
        fmt_box.append(bullet_btn)
        fmt_box.append(hr_btn)
        desc_header.append(fmt_box)
        desc_outer.append(desc_header)

        desc_outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self._desc_view = Gtk.TextView()
        self._desc_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._desc_view.set_margin_top(8)
        self._desc_view.set_margin_bottom(8)
        self._desc_view.set_margin_start(12)
        self._desc_view.set_margin_end(12)
        self._desc_view.set_size_request(-1, 100)
        desc_outer.append(self._desc_view)

        desc_group.add(desc_outer)
        page.add(desc_group)

        # Launch Settings
        launch_group = Adw.PreferencesGroup(title="Launch Settings")

        self._strategy_row = Adw.ComboRow(title="Update Strategy")
        strat_model = Gtk.StringList()
        for label in _STRATEGY_LABELS:
            strat_model.append(label)
        self._strategy_row.set_model(strat_model)
        launch_group.add(self._strategy_row)

        self._entry_point_entry = Adw.ActionRow(title="Launch Target")
        self._entry_point_entry.set_subtitle("Not set")
        self._entry_point_entry.set_subtitle_selectable(True)
        ep_browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
        ep_browse_btn.add_css_class("flat")
        ep_browse_btn.set_valign(Gtk.Align.CENTER)
        ep_browse_btn.set_sensitive(self._locally_installed)
        ep_browse_btn.set_tooltip_text(
            "Browse for executable…" if self._locally_installed
            else "Not installed locally"
        )
        ep_browse_btn.connect("clicked", self._on_browse_entry_point)
        self._entry_point_entry.add_suffix(ep_browse_btn)
        if self._locally_installed:
            self._entry_point_entry.set_activatable_widget(ep_browse_btn)
        launch_group.add(self._entry_point_entry)

        self._launch_args_entry = Adw.EntryRow(title="Launch Arguments")
        self._launch_args_entry.set_tooltip_text(
            "Optional arguments passed to the entry point on launch."
        )
        launch_group.add(self._launch_args_entry)

        page.add(launch_group)

        # Danger Zone
        danger_group = Adw.PreferencesGroup(title="Danger Zone")

        delete_btn = Gtk.Button(label="Delete Entry…")
        delete_btn.add_css_class("destructive-action")
        delete_btn.set_halign(Gtk.Align.START)
        delete_btn.set_margin_top(6)
        delete_btn.set_margin_bottom(6)
        delete_btn.connect("clicked", self._on_delete_clicked)
        danger_group.add(delete_btn)

        page.add(danger_group)

        # ── Vertical separator ────────────────────────────────────────────
        hbox.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # ── Right column: media ───────────────────────────────────────────
        right_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        right_box.set_hexpand(True)
        right_box.set_margin_top(16)
        right_box.set_margin_bottom(16)
        right_box.set_margin_start(16)
        right_box.set_margin_end(16)
        hbox.append(right_box)

        # Image rows in a boxed-list
        img_list = Gtk.ListBox()
        img_list.add_css_class("boxed-list")
        img_list.set_selection_mode(Gtk.SelectionMode.NONE)

        self._icon_row, self._icon_clear_btn, self._icon_thumb = self._make_image_row(
            "Icon", self._pick_icon
        )
        self._cover_row, self._cover_clear_btn, self._cover_thumb = self._make_image_row(
            "Cover", self._pick_cover
        )

        self._hide_title_btn = Gtk.ToggleButton()
        self._hide_title_btn.set_icon_name("eye-open-negative-filled-symbolic")
        self._hide_title_btn.set_valign(Gtk.Align.CENTER)
        self._hide_title_btn.set_visible(False)
        self._hide_title_btn.set_tooltip_text("Hide title — logo contains the app name")
        self._hide_title_btn.connect("toggled", self._on_hide_title_toggled)
        self._logo_row, self._logo_clear_btn, self._logo_thumb = self._make_image_row(
            "Logo", self._pick_logo, extra_suffix=self._hide_title_btn
        )

        self._icon_clear_btn.connect("clicked", self._on_icon_clear)
        self._cover_clear_btn.connect("clicked", self._on_cover_clear)
        self._logo_clear_btn.connect("clicked", self._on_logo_clear)

        img_list.append(self._icon_row)
        img_list.append(self._cover_row)
        img_list.append(self._logo_row)
        right_box.append(img_list)

        # Screenshots section heading with Add button
        ss_heading = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        ss_heading.set_margin_top(16)
        ss_heading.set_margin_bottom(6)
        ss_label = Gtk.Label(label="Screenshots")
        ss_label.add_css_class("heading")
        ss_label.set_margin_start(4)
        ss_heading.append(ss_label)
        ss_spacer = Gtk.Box()
        ss_spacer.set_hexpand(True)
        ss_heading.append(ss_spacer)
        ss_add_btn = Gtk.Button(label="Add…")
        ss_add_btn.connect("clicked", lambda _: self._screenshot_grid.open_file_chooser())
        ss_heading.append(ss_add_btn)
        right_box.append(ss_heading)

        ss_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        ss_box.add_css_class("card")
        ss_box.set_vexpand(True)

        from cellar.views.screenshot_grid import ScreenshotGridWidget
        self._screenshot_grid = ScreenshotGridWidget(
            on_changed=self._on_screenshots_changed,
            scrolled=False,
            vexpand=True,
        )
        ss_box.append(self._screenshot_grid)
        right_box.append(ss_box)

        return scroll

    def _make_image_row(
        self, label: str, handler, extra_suffix=None
    ) -> tuple[Adw.ActionRow, Gtk.Button, Gtk.Picture]:
        row = Adw.ActionRow(title=label)
        row.set_subtitle("No image set")

        thumb = Gtk.Picture()
        thumb.set_size_request(64, 64)
        thumb.set_content_fit(Gtk.ContentFit.CONTAIN)
        thumb.add_css_class("image-row-thumb")
        row.add_prefix(thumb)

        clear_btn = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove image")
        clear_btn.add_css_class("flat")
        clear_btn.set_valign(Gtk.Align.CENTER)
        clear_btn.set_visible(False)
        row.add_suffix(clear_btn)

        if extra_suffix is not None:
            row.add_suffix(extra_suffix)

        change_btn = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Browse…")
        change_btn.add_css_class("flat")
        change_btn.set_valign(Gtk.Align.CENTER)
        change_btn.connect("clicked", handler)
        row.add_suffix(change_btn)

        return row, clear_btn, thumb

    def _build_progress(self) -> Gtk.Widget:
        box, self._progress_label, self._progress_bar, self._cancel_progress_btn = (
            make_progress_page("Saving changes\u2026", self._on_cancel_progress_clicked)
        )
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
        if e.category in self._categories:
            self._category_row.set_selected(self._categories.index(e.category))
        self._developer_entry.set_text(e.developer or "")
        self._publisher_entry.set_text(e.publisher or "")
        if e.release_year:
            self._year_entry.set_text(str(e.release_year))
        self._website_entry.set_text(e.website or "")
        self._genres_entry.set_text(", ".join(e.genres) if e.genres else "")
        self._summary_entry.set_text(e.summary or "")

        if e.description:
            self._desc_view.get_buffer().set_text(e.description)

        if e.platform == "linux":
            self._steam_appid_entry.set_visible(False)
            self._entry_point_entry.set_title("Launch Target")
            self._entry_point_entry.set_tooltip_text(
                "Executable path within the app directory, e.g. \u201cbin/mygame\u201d"
            )
        else:
            if e.steam_appid is not None:
                self._steam_appid_entry.set_text(str(e.steam_appid))

        if e.entry_point:
            self._entry_point_entry.set_subtitle(GLib.markup_escape_text(e.entry_point))
        else:
            self._entry_point_entry.set_subtitle("Not set")
        self._launch_args_entry.set_text(e.launch_args or "")

        # Single images — subtitle from filename (sync), thumbnail via resolve (async for SMB)
        if e.icon:
            self._icon_row.set_subtitle(GLib.markup_escape_text(Path(e.icon).name))
            self._icon_clear_btn.set_visible(True)
        if e.cover:
            self._cover_row.set_subtitle(GLib.markup_escape_text(Path(e.cover).name))
            self._cover_clear_btn.set_visible(True)
        if e.logo:
            self._logo_row.set_subtitle(GLib.markup_escape_text(Path(e.logo).name))
            self._logo_clear_btn.set_visible(True)
            self._hide_title_btn.set_visible(True)
        if e.hide_title:
            self._hide_title_btn.set_active(True)

        # Resolve image assets via repo (handles SMB/SSH cache download) in background
        _assets = {k: v for k, v in [("icon", e.icon), ("cover", e.cover), ("logo", e.logo)] if v}
        _ss_rels = list(e.screenshots)

        def _resolve_assets():
            result = {}
            for key, rel in _assets.items():
                try:
                    result[key] = self._repo.resolve_asset_uri(rel)
                except Exception:
                    pass
            ss_paths = []
            for rel in _ss_rels:
                try:
                    p = self._repo.resolve_asset_uri(rel)
                    if p:
                        ss_paths.append(p)
                except Exception:
                    pass
            return result, ss_paths

        def _on_assets_resolved(res):
            resolved, ss_paths = res
            if "icon" in resolved:
                self._icon_thumb.set_filename(resolved["icon"])
            if "cover" in resolved:
                self._cover_thumb.set_filename(resolved["cover"])
            if "logo" in resolved:
                self._logo_thumb.set_filename(resolved["logo"])
            if ss_paths:
                self._screenshot_grid.set_local_items(ss_paths)

        run_in_background(_resolve_assets, on_done=_on_assets_resolved)

        # Steam screenshot suggestions — fetch if steam_appid is set
        if e.steam_appid:
            def _fetch_steam_ss():
                from cellar.backend.steam import fetch_details, SteamError
                try:
                    details = fetch_details(e.steam_appid)
                    return details.get("screenshots", [])
                except Exception:
                    return []

            def _on_steam_ss(screenshots):
                if screenshots:
                    self._screenshot_grid.add_steam(screenshots)

            run_in_background(_fetch_steam_ss, on_done=_on_steam_ss)

        strategy = e.update_strategy or "safe"
        if strategy in _STRATEGIES:
            self._strategy_row.set_selected(_STRATEGIES.index(strategy))

        self._update_save_button()

    # ── Form validation ───────────────────────────────────────────────────

    def _get_category(self) -> str:
        idx = self._category_row.get_selected()
        return self._categories[idx] if 0 <= idx < len(self._categories) else ""

    def _on_name_changed(self, _entry) -> None:
        self._update_save_button()

    def _update_save_button(self) -> None:
        self._save_btn.set_sensitive(bool(self._name_entry.get_text().strip()))

    # ── Description formatting helpers ────────────────────────────────────

    def _desc_fmt_wrap(self, tag: str) -> None:
        buf = self._desc_view.get_buffer()
        buf.begin_user_action()
        if buf.get_has_selection():
            start, end = buf.get_selection_bounds()
            text = buf.get_text(start, end, False)
            buf.delete(start, end)
            buf.insert(buf.get_iter_at_mark(buf.get_insert()), f"<{tag}>{text}</{tag}>")
        else:
            buf.insert_at_cursor(f"<{tag}></{tag}>")
        buf.end_user_action()

    def _desc_fmt_bullet(self) -> None:
        buf = self._desc_view.get_buffer()
        buf.begin_user_action()
        if buf.get_has_selection():
            start, end = buf.get_selection_bounds()
            text = buf.get_text(start, end, False)
            buf.delete(start, end)
            buf.insert(buf.get_iter_at_mark(buf.get_insert()), f"<li>{text}</li>")
        else:
            buf.insert_at_cursor("<li></li>")
        buf.end_user_action()

    def _desc_fmt_hr(self) -> None:
        buf = self._desc_view.get_buffer()
        it = buf.get_iter_at_mark(buf.get_insert())
        it.set_line_offset(0)
        buf.begin_user_action()
        buf.insert(it, "<hr>\n")
        buf.end_user_action()

    # ── Image pickers ─────────────────────────────────────────────────────

    def _pick_image(self, title: str, multi: bool, callback) -> None:
        chooser = Gtk.FileChooserNative(
            title=title,
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            select_multiple=multi,
        )
        img_filter = Gtk.FileFilter()
        img_filter.set_name("Images (PNG, JPG, ICO, BMP, SVG)")
        img_filter.add_mime_type("image/png")
        img_filter.add_mime_type("image/jpeg")
        img_filter.add_mime_type("image/x-icon")
        img_filter.add_mime_type("image/vnd.microsoft.icon")
        img_filter.add_mime_type("image/bmp")
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
            self._icon_clear_btn.set_visible(True)
            self._icon_thumb.set_filename(self._icon_path)

    def _pick_cover(self, _btn) -> None:
        self._pick_image("Select Cover", False, self._on_cover_chosen)

    def _on_cover_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._cover_path = chooser.get_file().get_path()
            self._cover_row.set_subtitle(GLib.markup_escape_text(Path(self._cover_path).name))
            self._cover_clear_btn.set_visible(True)
            self._cover_thumb.set_filename(self._cover_path)

    def _pick_logo(self, _btn) -> None:
        self._pick_image("Select Logo (transparent PNG)", False, self._on_logo_chosen)

    def _on_logo_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._logo_path = chooser.get_file().get_path()
            self._logo_row.set_subtitle(GLib.markup_escape_text(Path(self._logo_path).name))
            self._logo_clear_btn.set_visible(True)
            self._logo_thumb.set_filename(self._logo_path)
            self._hide_title_btn.set_visible(True)
            if not self._old_entry.logo and not self._hide_title_btn.get_active():
                self._hide_title_btn.set_active(True)

    def _on_screenshots_changed(self) -> None:
        self._screenshots_dirty = True

    # ── Hide-title toggle ─────────────────────────────────────────────────

    def _on_hide_title_toggled(self, btn) -> None:
        if btn.get_active():
            btn.set_icon_name("eye-not-looking-symbolic")
        else:
            btn.set_icon_name("eye-open-negative-filled-symbolic")

    # ── Entry point browser ───────────────────────────────────────────────

    def _on_browse_entry_point(self, _btn) -> None:
        import os
        from cellar.backend.umu import prefixes_dir
        e = self._old_entry
        if e.platform == "linux":
            from cellar.backend.database import get_installed
            rec = get_installed(e.id)
            install_path = Path(rec["install_path"]) if rec and rec.get("install_path") else Path.home()
            browse_root = install_path
            title = "Select Executable"
        else:
            prefix = prefixes_dir() / e.id / "drive_c"
            browse_root = prefix if prefix.is_dir() else Path.home()
            title = "Select Executable (.exe)"

        chooser = Gtk.FileChooserNative(
            title=title,
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Select",
        )
        from gi.repository import Gio
        chooser.set_current_folder(Gio.File.new_for_path(str(browse_root)))
        if e.platform != "linux":
            exe_filter = Gtk.FileFilter()
            exe_filter.set_name("Windows executables (*.exe)")
            exe_filter.add_pattern("*.exe")
            chooser.add_filter(exe_filter)
        chooser.connect("response", self._on_entry_point_chosen, chooser, browse_root, e.platform)
        chooser.show()
        self._ep_chooser = chooser  # keep reference alive

    def _on_entry_point_chosen(self, _c, response, chooser, browse_root: Path, platform: str) -> None:
        import os
        if response != Gtk.ResponseType.ACCEPT:
            return
        abs_path = chooser.get_file().get_path()
        if platform == "linux":
            try:
                rel = os.path.relpath(abs_path, str(browse_root))
            except ValueError:
                rel = abs_path
            formatted = rel
        else:
            drive_c = browse_root
            try:
                rel = os.path.relpath(abs_path, str(drive_c))
                formatted = "C:\\" + rel.replace("/", "\\")
            except ValueError:
                formatted = abs_path
        self._entry_point_entry.set_subtitle(GLib.markup_escape_text(formatted))

    # ── Image clear handlers ──────────────────────────────────────────────

    def _on_icon_clear(self, _btn) -> None:
        self._icon_path = ""
        self._icon_row.set_subtitle("Will be removed")
        self._icon_clear_btn.set_visible(False)
        self._icon_thumb.set_paintable(None)

    def _on_cover_clear(self, _btn) -> None:
        self._cover_path = ""
        self._cover_row.set_subtitle("Will be removed")
        self._cover_clear_btn.set_visible(False)
        self._cover_thumb.set_paintable(None)

    def _on_logo_clear(self, _btn) -> None:
        self._logo_path = ""
        self._logo_row.set_subtitle("Will be removed")
        self._logo_clear_btn.set_visible(False)
        self._logo_thumb.set_paintable(None)
        self._hide_title_btn.set_visible(False)

    # ── Steam lookup ──────────────────────────────────────────────────────

    def _on_steam_lookup(self, _btn) -> None:
        from cellar.views.steam_picker import SteamPickerDialog

        query = self._name_entry.get_text().strip()
        picker = SteamPickerDialog(query=query, on_picked=self._apply_steam_result)
        picker.present(self)

    def _apply_steam_result(self, result: dict) -> None:
        """Pre-fill empty form fields from a Steam result (keeps existing values)."""
        if result.get("name") and not self._name_entry.get_text().strip():
            self._name_entry.set_text(result["name"])
        if result.get("developer") and not self._developer_entry.get_text().strip():
            self._developer_entry.set_text(result["developer"])
        if result.get("publisher") and not self._publisher_entry.get_text().strip():
            self._publisher_entry.set_text(result["publisher"])
        if result.get("year") and not self._year_entry.get_text().strip():
            self._year_entry.set_text(str(result["year"]))
        if result.get("website") and not self._website_entry.get_text().strip():
            self._website_entry.set_text(result["website"])
        if result.get("genres") and not self._genres_entry.get_text().strip():
            genres = result["genres"]
            if isinstance(genres, list):
                self._genres_entry.set_text(", ".join(genres))
            else:
                self._genres_entry.set_text(str(genres))
        if result.get("summary") and not self._summary_entry.get_text().strip():
            self._summary_entry.set_text(result["summary"])
        if result.get("description"):
            buf = self._desc_view.get_buffer()
            if not buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip():
                buf.set_text(result["description"])
        if result.get("screenshots"):
            self._screenshot_grid.add_steam(result["screenshots"])

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
        steam_appid_text = self._steam_appid_entry.get_text().strip()
        steam_appid = int(steam_appid_text) if steam_appid_text.isdigit() else None
        website = self._website_entry.get_text().strip()
        genres_text = self._genres_entry.get_text().strip()
        genres = tuple(g.strip() for g in genres_text.split(",") if g.strip()) if genres_text else ()
        strategy = _STRATEGIES[self._strategy_row.get_selected()]
        _ep_sub = self._entry_point_entry.get_subtitle() or ""
        entry_point = "" if _ep_sub == "Not set" else _ep_sub
        launch_args = self._launch_args_entry.get_text().strip()

        # Single images: None=keep existing, ""=clear, str=new file
        if self._icon_path is None:
            icon_rel = e.icon
        elif self._icon_path == "":
            icon_rel = ""
        else:
            ext = ".png" if Path(self._icon_path).suffix.lower() in (".ico", ".bmp") else Path(self._icon_path).suffix
            icon_rel = f"apps/{app_id}/icon{ext}"

        if self._cover_path is None:
            cover_rel = e.cover
        elif self._cover_path == "":
            cover_rel = ""
        else:
            cover_rel = f"apps/{app_id}/cover{Path(self._cover_path).suffix}"

        if self._logo_path is None:
            logo_rel = e.logo
        elif self._logo_path == "":
            logo_rel = ""
        else:
            logo_rel = f"apps/{app_id}/logo.png"

        # Screenshots resolved in the background thread (may need Steam downloads).
        # Use existing rels as placeholder; thread replaces when dirty.
        screenshot_rels = e.screenshots
        _grid_items = self._screenshot_grid.get_items() if self._screenshots_dirty else None
        # Local items the user unchecked — their repo files get deleted after save.
        _excluded_locals = (
            self._screenshot_grid.get_excluded_local_items()
            if self._screenshots_dirty else []
        )

        from cellar.models.app_entry import AppEntry

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
            logo=logo_rel,
            hide_title=self._hide_title_btn.get_active(),
            screenshots=screenshot_rels,
            website=website,
            genres=genres,
            archive=e.archive,
            archive_size=e.archive_size,
            archive_crc32=e.archive_crc32,
            install_size_estimate=e.install_size_estimate,
            update_strategy=strategy,
            entry_point=entry_point,
            launch_args=launch_args,
            compatibility_notes=e.compatibility_notes,
            changelog=e.changelog,
            lock_runner=e.lock_runner,
            steam_appid=steam_appid if e.platform != "linux" else None,
            platform=e.platform,
        )

        images = {
            "icon": self._icon_path,      # None / "" / path
            "cover": self._cover_path,
            "logo": self._logo_path,
            "screenshots": None,          # filled in thread when dirty
        }

        self._cancel_event.clear()
        self._saved_entry = new_entry  # may be replaced in thread; read by _on_save_done
        self._stack.set_visible_child_name("progress")
        self._progress_bar.set_fraction(0.0)
        self._progress_label.set_text("Saving changes\u2026")

        repo_root = self._repo.writable_path()

        def _run():
            import tempfile as _tmp

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
                    GLib.idle_add(self._progress_bar.set_text, _fmt_stats(copied, total, speed))

            def _progress(fraction: float) -> None:
                GLib.idle_add(self._progress_bar.set_fraction, fraction)

            _run_entry = new_entry  # local alias; replaced below when screenshots are dirty

            if _grid_items is not None:
                # Resolve screenshots: download any Steam-pending items first.
                _phase("Downloading screenshots\u2026")
                dl_dir = Path(_tmp.mkdtemp(prefix="cellar_ss_"))
                final_paths: list[str] = []
                from cellar.utils.http import make_session as _make_session
                _session = _make_session()
                for _item in _grid_items:
                    if _item.local_path:
                        final_paths.append(_item.local_path)
                    elif _item.full_url:
                        _fname = _item.full_url.split("/")[-1].split("?")[0] or "screenshot.jpg"
                        _dest = dl_dir / _fname
                        try:
                            _r = _session.get(_item.full_url, timeout=30)
                            _r.raise_for_status()
                            _dest.write_bytes(_r.content)
                            final_paths.append(str(_dest))
                        except Exception as _exc:  # noqa: BLE001
                            import logging as _log
                            _log.getLogger(__name__).warning(
                                "Screenshot download failed: %s", _exc
                            )
                images["screenshots"] = final_paths
                ss_rels = tuple(
                    f"apps/{app_id}/screenshots/{i + 1:02d}{Path(p).suffix}"
                    for i, p in enumerate(final_paths)
                )
                from dataclasses import replace as _dc_replace
                _run_entry = _dc_replace(new_entry, screenshots=ss_rels)
                self._saved_entry = _run_entry

            try:
                update_in_repo(
                    repo_root,
                    e,
                    _run_entry,
                    images,
                    progress_cb=_progress,
                    phase_cb=_phase,
                    stats_cb=_stats,
                    cancel_event=self._cancel_event,
                )
                # Delete repo screenshot files the user unchecked.
                if _excluded_locals:
                    new_rels_set = set(_run_entry.screenshots)
                    for _excl in _excluded_locals:
                        if not _excl.local_path:
                            continue
                        # Match against old entry's screenshot relative paths
                        for old_rel in e.screenshots:
                            if str(repo_root / old_rel) == _excl.local_path:
                                if old_rel not in new_rels_set:
                                    try:
                                        (repo_root / old_rel).unlink(missing_ok=True)
                                    except Exception:
                                        pass
                                break
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

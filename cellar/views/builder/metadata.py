"""App metadata creation / editing dialog."""

from __future__ import annotations

import logging
from cellar.utils.async_work import run_in_background
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.backend.project import Project, save_project

log = logging.getLogger(__name__)


class AppMetadataDialog(Adw.Dialog):
    """Unified dialog for creating or editing an App project's metadata.

    Pass ``project=None`` for create mode (shows Cancel + Create buttons).
    Pass an existing ``project`` for edit mode (shows Done button, auto-saves).
    In edit mode the title is locked once the prefix has been initialized.
    """

    def __init__(
        self,
        *,
        project: Project | None = None,
        project_type: str = "app",
        on_created: Callable | None = None,
        on_changed: Callable | None = None,
    ) -> None:
        self._is_edit = project is not None
        self._project_type = project.project_type if project is not None else project_type
        _new_titles = {"linux": "New Linux App"}
        super().__init__(
            title="Details" if self._is_edit else _new_titles.get(self._project_type, "New App"),
            content_width=1100,
            content_height=680,
        )
        self._project = project
        self._on_created = on_created
        self._on_changed = on_changed
        # Temp image paths used in create mode until the project exists
        self._tmp_icon: str = ""
        self._tmp_cover: str = ""
        self._tmp_logo: str = ""
        self._tmp_screenshots: list[str] = []
        self._chooser = None

        self._build_ui()

    def _build_ui(self) -> None:
        from cellar.backend.packager import BASE_CATEGORIES as _BASE_CATS

        p = self._project
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        if self._is_edit:
            done_btn = Gtk.Button(label="Done")
            done_btn.add_css_class("suggested-action")
            done_btn.connect("clicked", lambda _: self.close())
            header.pack_end(done_btn)
        else:
            cancel_btn = Gtk.Button(label="Cancel")
            cancel_btn.connect("clicked", lambda _: self.close())
            header.pack_start(cancel_btn)
            self._create_btn = Gtk.Button(label="Create")
            self._create_btn.add_css_class("suggested-action")
            self._create_btn.set_sensitive(False)
            self._create_btn.connect("clicked", self._on_create_clicked)
            header.pack_end(self._create_btn)

        toolbar.add_top_bar(header)

        # Single scroll wraps both panes
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
        id_group = Adw.PreferencesGroup()

        title_locked = self._is_edit and bool(p and p.initialized)
        if title_locked:
            self._title_row = Adw.ActionRow(title="Title")
            self._title_row.set_subtitle(p.name if p else "")
            self._title_row.add_css_class("property")
        else:
            self._title_row = Adw.EntryRow(title="Title")
            self._title_row.set_text(p.name if p else "")
            self._title_row.connect("changed", self._on_title_changed)
            steam_btn = Gtk.Button(icon_name="system-search-symbolic")
            steam_btn.add_css_class("flat")
            steam_btn.set_valign(Gtk.Align.CENTER)
            steam_btn.set_tooltip_text("Look up on Steam")
            steam_btn.connect("clicked", self._on_steam_lookup)
            self._title_row.add_suffix(steam_btn)
        id_group.add(self._title_row)

        slug_subtitle = p.slug if p else ""
        self._slug_row = Adw.ActionRow(title="App ID", subtitle=slug_subtitle)
        self._slug_row.add_css_class("property")
        id_group.add(self._slug_row)

        page.add(id_group)

        # Details
        det_group = Adw.PreferencesGroup(title="Details")

        self._version_row = Adw.EntryRow(title="Version")
        self._version_row.set_text(p.version if p else "1.0")
        if self._is_edit:
            self._version_row.connect("changed", lambda r: self._save("version", r.get_text()))
        det_group.add(self._version_row)

        self._cats = list(_BASE_CATS)
        self._cat_row = Adw.ComboRow(title="Category")
        self._cat_row.set_model(Gtk.StringList.new(["(none)"] + self._cats))
        cat_val = p.category if p else ""
        if cat_val in self._cats:
            self._cat_row.set_selected(self._cats.index(cat_val) + 1)
        if self._is_edit:
            self._cat_row.connect("notify::selected", self._on_cat_changed)
        det_group.add(self._cat_row)

        self._dev_row = Adw.EntryRow(title="Developer")
        self._dev_row.set_text(p.developer if p else "")
        if self._is_edit:
            self._dev_row.connect("changed", lambda r: self._save("developer", r.get_text()))
        det_group.add(self._dev_row)

        self._pub_row = Adw.EntryRow(title="Publisher")
        self._pub_row.set_text(p.publisher if p else "")
        if self._is_edit:
            self._pub_row.connect("changed", lambda r: self._save("publisher", r.get_text()))
        det_group.add(self._pub_row)

        self._year_row = Adw.EntryRow(title="Release Year")
        if p and p.release_year:
            self._year_row.set_text(str(p.release_year))
        if self._is_edit:
            self._year_row.connect("changed", self._on_year_changed)
        det_group.add(self._year_row)

        self._steam_row = Adw.EntryRow(title="Steam App ID")
        self._steam_row.set_tooltip_text("Used for protonfixes. Leave empty for GAMEID=0.")
        if p and p.steam_appid is not None:
            self._steam_row.set_text(str(p.steam_appid))
        if self._is_edit:
            self._steam_row.connect("changed", self._on_steam_changed)
        det_group.add(self._steam_row)

        self._website_row = Adw.EntryRow(title="Website")
        self._website_row.set_text(p.website if p else "")
        if self._is_edit:
            self._website_row.connect("changed", lambda r: self._save("website", r.get_text()))
        det_group.add(self._website_row)

        self._genres_row = Adw.EntryRow(title="Genres")
        self._genres_row.set_tooltip_text("Comma-separated list, e.g. Action, RPG")
        if p and p.genres:
            self._genres_row.set_text(", ".join(p.genres))
        if self._is_edit:
            self._genres_row.connect("changed", self._on_genres_changed)
        det_group.add(self._genres_row)

        page.add(det_group)

        # Description
        desc_group = Adw.PreferencesGroup(title="Description")

        self._summary_row = Adw.EntryRow(title="Summary")
        self._summary_row.set_text(p.summary if p else "")
        if self._is_edit:
            self._summary_row.connect("changed", lambda r: self._save("summary", r.get_text()))
        desc_group.add(self._summary_row)

        self._desc_row = Adw.EntryRow(title="Description")
        self._desc_row.set_text(p.description if p else "")
        if self._is_edit:
            self._desc_row.connect("changed", lambda r: self._save("description", r.get_text()))
        desc_group.add(self._desc_row)

        page.add(desc_group)

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
            "Icon", self._on_pick_icon
        )
        self._cover_row, self._cover_clear_btn, self._cover_thumb = self._make_image_row(
            "Cover", self._on_pick_cover
        )

        self._hide_title_btn = Gtk.ToggleButton()
        self._hide_title_btn.set_icon_name("eye-open-negative-filled-symbolic")
        self._hide_title_btn.set_valign(Gtk.Align.CENTER)
        self._hide_title_btn.set_visible(False)
        self._hide_title_btn.set_tooltip_text("Hide title \u2014 logo contains the app name")
        self._hide_title_btn.set_active(bool(p and p.hide_title))
        self._hide_title_btn.connect("toggled", self._on_hide_title_toggled)
        self._logo_row, self._logo_clear_btn, self._logo_thumb = self._make_image_row(
            "Logo", self._on_pick_logo, extra_suffix=self._hide_title_btn
        )

        self._icon_clear_btn.connect("clicked", self._on_icon_clear)
        self._cover_clear_btn.connect("clicked", self._on_cover_clear)
        self._logo_clear_btn.connect("clicked", self._on_logo_clear)

        # Prefill thumbnails in edit mode
        if p and p.icon_path:
            self._icon_row.set_subtitle(GLib.markup_escape_text(Path(p.icon_path).name))
            self._icon_clear_btn.set_visible(True)
            self._icon_thumb.set_filename(p.icon_path)
        if p and p.cover_path:
            self._cover_row.set_subtitle(GLib.markup_escape_text(Path(p.cover_path).name))
            self._cover_clear_btn.set_visible(True)
            self._cover_thumb.set_filename(p.cover_path)
        if p and p.logo_path:
            self._logo_row.set_subtitle(GLib.markup_escape_text(Path(p.logo_path).name))
            self._logo_clear_btn.set_visible(True)
            self._logo_thumb.set_filename(p.logo_path)
            self._hide_title_btn.set_visible(True)
        if p and p.hide_title:
            self._hide_title_btn.set_icon_name("eye-not-looking-symbolic")

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
        self._screenshot_grid.set_local_items(list(p.screenshot_paths) if p else [])
        ss_box.append(self._screenshot_grid)
        right_box.append(ss_box)

        toolbar.set_content(scroll)
        self.set_child(toolbar)

    def _make_image_row(
        self, label: str, handler, extra_suffix=None
    ) -> tuple[Adw.ActionRow, Gtk.Button, Gtk.Picture]:
        row = Adw.ActionRow(title=label)
        row.set_subtitle("Not set")

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

        change_btn = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Browse\u2026")
        change_btn.add_css_class("flat")
        change_btn.set_valign(Gtk.Align.CENTER)
        change_btn.connect("clicked", handler)
        row.add_suffix(change_btn)

        return row, clear_btn, thumb

    # ── Helpers ───────────────────────────────────────────────────────────

    def _save(self, attr: str, value) -> None:
        if self._project:
            setattr(self._project, attr, value)
            save_project(self._project)
            if self._on_changed:
                self._on_changed()

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
        self._chooser = chooser

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_title_changed(self, row) -> None:
        from cellar.backend.packager import slugify
        title = row.get_text().strip()
        if self._is_edit:
            self._save("name", title)
        else:
            slug = slugify(title) if title else ""
            self._slug_row.set_subtitle(slug)
            self._create_btn.set_sensitive(bool(title))

    def _on_cat_changed(self, row, _param) -> None:
        if self._project:
            idx = row.get_selected()
            self._project.category = self._cats[idx - 1] if idx > 0 else ""
            save_project(self._project)
            if self._on_changed:
                self._on_changed()

    def _on_year_changed(self, row) -> None:
        txt = row.get_text().strip()
        try:
            val = int(txt) if txt else None
        except ValueError:
            return
        self._save("release_year", val)

    def _on_steam_changed(self, row) -> None:
        txt = row.get_text().strip()
        self._save("steam_appid", int(txt) if txt.isdigit() else None)

    def _on_genres_changed(self, row) -> None:
        txt = row.get_text().strip()
        genres = [g.strip() for g in txt.split(",") if g.strip()] if txt else []
        self._save("genres", genres)

    def _on_steam_lookup(self, _btn) -> None:
        from cellar.views.steam_picker import SteamPickerDialog
        if isinstance(self._title_row, Adw.EntryRow):
            query = self._title_row.get_text().strip()
        elif self._project:
            query = self._project.name
        else:
            query = ""
        picker = SteamPickerDialog(query=query, on_picked=self._apply_steam)
        picker.present(self.get_root())

    def _apply_steam(self, result: dict) -> None:
        if result.get("name") and isinstance(self._title_row, Adw.EntryRow):
            self._title_row.set_text(result["name"])
        if result.get("developer") and not self._dev_row.get_text().strip():
            self._dev_row.set_text(result["developer"])
        if result.get("publisher") and not self._pub_row.get_text().strip():
            self._pub_row.set_text(result["publisher"])
        if result.get("year") and not self._year_row.get_text().strip():
            self._year_row.set_text(str(result["year"]))
        if result.get("summary") and not self._summary_row.get_text().strip():
            self._summary_row.set_text(result["summary"])
        if result.get("summary") and not self._desc_row.get_text().strip():
            self._desc_row.set_text(result["summary"])
        if result.get("steam_appid") and not self._steam_row.get_text().strip():
            self._steam_row.set_text(str(result["steam_appid"]))
        if result.get("website") and not self._website_row.get_text().strip():
            self._website_row.set_text(result["website"])
        if result.get("genres") and not self._genres_row.get_text().strip():
            self._genres_row.set_text(", ".join(result["genres"]))
        if result.get("category") and result["category"] in self._cats:
            self._cat_row.set_selected(self._cats.index(result["category"]) + 1)
        if result.get("screenshots"):
            self._screenshot_grid.add_steam(result["screenshots"])

    def _on_screenshots_changed(self) -> None:
        """Called by the grid whenever the screenshot list changes.

        Saves local items immediately.  Any Steam-pending items are downloaded
        eagerly to the project dir (or a temp dir in create mode), then
        promoted to local items in the grid so they persist with the project.
        """
        items = self._screenshot_grid.get_items()
        local_paths = [i.local_path for i in items if i.local_path]
        if self._project:
            self._project.screenshot_paths = local_paths
            save_project(self._project)
        else:
            self._tmp_screenshots = local_paths

        steam_items = [i for i in items if i.is_steam]
        if not steam_items:
            return

        if self._project:
            dl_dir = self._project.project_dir / "screenshots"
            dl_dir.mkdir(parents=True, exist_ok=True)
        else:
            import tempfile as _tmp
            dl_dir = Path(_tmp.mkdtemp(prefix="cellar-ss-"))

        def _download(items=steam_items, dl_dir=dl_dir) -> list[str]:
            from cellar.utils.http import make_session
            session = make_session()
            downloaded: list[str] = []
            for i, item in enumerate(items):
                try:
                    resp = session.get(item.full_url, timeout=30)
                    if resp.ok:
                        suffix = ".jpg" if item.full_url.lower().endswith(".jpg") else ".png"
                        dest = dl_dir / f"steam_{i:02d}{suffix}"
                        dest.write_bytes(resp.content)
                        downloaded.append(str(dest))
                except Exception as exc:  # noqa: BLE001
                    log.warning("Screenshot download failed: %s", exc)
            return downloaded

        def _done(downloaded: list[str]) -> None:
            # Swap steam items out of the grid; add downloaded local items.
            # clear_steam() doesn't fire on_changed; add_local() will, which
            # then saves the updated paths to the project.
            self._screenshot_grid.clear_steam()
            if downloaded:
                self._screenshot_grid.add_local(downloaded)

        run_in_background(_download, on_done=_done)

    def _on_pick_icon(self, _btn) -> None:
        self._pick_image("Select Icon", False, self._on_icon_chosen)

    def _on_icon_clear(self, _btn) -> None:
        self._icon_row.set_subtitle("Not set")
        self._icon_clear_btn.set_visible(False)
        self._icon_thumb.set_paintable(None)
        if self._project:
            self._project.icon_path = ""
            save_project(self._project)
        else:
            self._tmp_icon = ""

    def _on_cover_clear(self, _btn) -> None:
        self._cover_row.set_subtitle("Not set")
        self._cover_clear_btn.set_visible(False)
        self._cover_thumb.set_paintable(None)
        if self._project:
            self._project.cover_path = ""
            save_project(self._project)
        else:
            self._tmp_cover = ""

    def _on_logo_clear(self, _btn) -> None:
        self._logo_row.set_subtitle("Not set")
        self._logo_clear_btn.set_visible(False)
        self._logo_thumb.set_paintable(None)
        self._hide_title_btn.set_visible(False)
        if self._project:
            self._project.logo_path = ""
            save_project(self._project)
        else:
            self._tmp_logo = ""

    def _on_icon_chosen(self, _c, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = chooser.get_file().get_path()
            self._icon_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
            self._icon_clear_btn.set_visible(True)
            self._icon_thumb.set_filename(path)
            if self._project:
                self._project.icon_path = path
                save_project(self._project)
            else:
                self._tmp_icon = path

    def _on_pick_cover(self, _btn) -> None:
        self._pick_image("Select Cover", False, self._on_cover_chosen)

    def _on_cover_chosen(self, _c, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = chooser.get_file().get_path()
            self._cover_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
            self._cover_clear_btn.set_visible(True)
            self._cover_thumb.set_filename(path)
            if self._project:
                self._project.cover_path = path
                save_project(self._project)
            else:
                self._tmp_cover = path

    def _on_pick_logo(self, _btn) -> None:
        self._pick_image("Select Logo (transparent PNG)", False, self._on_logo_chosen)

    def _on_logo_chosen(self, _c, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = chooser.get_file().get_path()
            self._logo_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
            self._logo_clear_btn.set_visible(True)
            self._logo_thumb.set_filename(path)
            self._hide_title_btn.set_visible(True)
            if not self._hide_title_btn.get_active():
                self._hide_title_btn.set_active(True)
            if self._project:
                self._project.logo_path = path
                self._project.hide_title = self._hide_title_btn.get_active()
                save_project(self._project)
            else:
                self._tmp_logo = path

    def _on_hide_title_toggled(self, btn: Gtk.ToggleButton) -> None:
        if btn.get_active():
            btn.set_icon_name("eye-not-looking-symbolic")
        else:
            btn.set_icon_name("eye-open-negative-filled-symbolic")
        if self._project:
            self._project.hide_title = btn.get_active()
            save_project(self._project)

    def _on_create_clicked(self, _btn) -> None:
        from cellar.backend.project import create_project

        if isinstance(self._title_row, Adw.EntryRow):
            name = self._title_row.get_text().strip()
        else:
            return
        if not name:
            return

        cat_idx = self._cat_row.get_selected()
        year_txt = self._year_row.get_text().strip()
        steam_txt = self._steam_row.get_text().strip()
        summary = self._summary_row.get_text().strip()

        project = create_project(name, self._project_type)
        project.category = self._cats[cat_idx - 1] if cat_idx > 0 else ""
        project.developer = self._dev_row.get_text().strip()
        project.publisher = self._pub_row.get_text().strip()
        project.version = self._version_row.get_text().strip() or "1.0"
        try:
            project.release_year = int(year_txt) if year_txt else None
        except ValueError:
            project.release_year = None
        project.summary = summary
        project.description = self._desc_row.get_text().strip() or summary
        project.steam_appid = int(steam_txt) if steam_txt.isdigit() else None
        project.website = self._website_row.get_text().strip()
        genres_txt = self._genres_row.get_text().strip()
        project.genres = [g.strip() for g in genres_txt.split(",") if g.strip()] if genres_txt else []
        if self._tmp_icon:
            project.icon_path = self._tmp_icon
        if self._tmp_cover:
            project.cover_path = self._tmp_cover
        if self._tmp_logo:
            project.logo_path = self._tmp_logo
        project.hide_title = self._hide_title_btn.get_active()
        local_ss = [i.local_path for i in self._screenshot_grid.get_items() if i.local_path]
        if local_ss:
            project.screenshot_paths = local_ss
        save_project(project)

        self.close()
        if self._on_created:
            self._on_created(project)

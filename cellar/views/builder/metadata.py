"""App metadata creation / editing dialog."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.backend.project import Project, save_project
from cellar.views.browse import _FixedBox

log = logging.getLogger(__name__)


class AppMetadataDialog(Adw.Dialog):
    """Unified dialog for creating or editing an App project's metadata.

    Pass ``project=None`` for create mode (shows Cancel + Create buttons).
    Pass an existing ``project`` for edit mode (shows Cancel + Done buttons).
    In edit mode changes are only persisted when Done is clicked.
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
        # Snapshot for cancel/revert in edit mode
        self._snapshot: dict | None = project.to_dict() if project else None
        # Temp image paths used in create mode until the project exists
        self._tmp_icon: str = ""
        self._tmp_cover: str = ""
        self._tmp_logo: str = ""
        self._tmp_screenshots: list[str] = []
        self._tmp_steam_screenshots: list[dict] = []
        self._tmp_selected_steam_urls: list[str] = []
        self._chooser = None

        self._build_ui()

    def _build_ui(self) -> None:
        from cellar.backend.packager import BASE_CATEGORIES as _BASE_CATS

        p = self._project
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        if self._is_edit:
            cancel_btn = Gtk.Button(label="Cancel")
            cancel_btn.connect("clicked", self._on_cancel_clicked)
            header.pack_start(cancel_btn)
            done_btn = Gtk.Button(label="Done")
            done_btn.add_css_class("suggested-action")
            done_btn.connect("clicked", self._on_done_clicked)
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

        # Plain box instead of AdwPreferencesPage — the page has a built-in
        # ScrolledWindow which creates a second scrollbar when nested inside ours.
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        page.set_margin_top(12)
        page.set_margin_bottom(12)
        page.set_margin_start(12)
        page.set_margin_end(12)
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

        page.append(id_group)

        # Details
        det_group = Adw.PreferencesGroup(title="Details")

        self._version_row = Adw.EntryRow(title="Version")
        self._version_row.set_text(p.version if p else "1.0")
        det_group.add(self._version_row)

        self._cats = list(_BASE_CATS)
        self._cat_row = Adw.ComboRow(title="Category")
        self._cat_row.set_model(Gtk.StringList.new(["(none)"] + self._cats))
        cat_val = p.category if p else ""
        if cat_val in self._cats:
            self._cat_row.set_selected(self._cats.index(cat_val) + 1)
        det_group.add(self._cat_row)

        self._dev_row = Adw.EntryRow(title="Developer")
        self._dev_row.set_text(p.developer if p else "")
        det_group.add(self._dev_row)

        self._pub_row = Adw.EntryRow(title="Publisher")
        self._pub_row.set_text(p.publisher if p else "")
        det_group.add(self._pub_row)

        self._year_row = Adw.EntryRow(title="Release Year")
        if p and p.release_year:
            self._year_row.set_text(str(p.release_year))
        det_group.add(self._year_row)

        self._steam_row = Adw.EntryRow(title="Steam App ID")
        self._steam_row.set_tooltip_text("Used for protonfixes. Leave empty for GAMEID=0.")
        if p and p.steam_appid is not None:
            self._steam_row.set_text(str(p.steam_appid))
        det_group.add(self._steam_row)

        self._website_row = Adw.EntryRow(title="Website")
        self._website_row.set_text(p.website if p else "")
        det_group.add(self._website_row)

        self._genres_row = Adw.EntryRow(title="Genres")
        self._genres_row.set_tooltip_text("Comma-separated list, e.g. Action, RPG")
        if p and p.genres:
            self._genres_row.set_text(", ".join(p.genres))
        det_group.add(self._genres_row)

        page.append(det_group)

        # Description
        desc_group = Adw.PreferencesGroup(title="Description")

        self._summary_row = Adw.EntryRow(title="Summary")
        self._summary_row.set_text(p.summary if p else "")
        desc_group.add(self._summary_row)

        self._desc_row = Adw.EntryRow(title="Description")
        self._desc_row.set_text(p.description if p else "")
        desc_group.add(self._desc_row)

        page.append(desc_group)

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

        self._icon_row, self._icon_clear_btn, self._icon_thumb, self._icon_thumb_wrap = self._make_image_row(
            "Icon", self._on_pick_icon, thumb_w=52, thumb_h=52, steam_slot="icon",
        )
        self._cover_row, self._cover_clear_btn, self._cover_thumb, self._cover_thumb_wrap = self._make_image_row(
            "Cover", self._on_pick_cover, thumb_w=52, thumb_h=70, steam_slot="cover",
        )

        self._hide_title_btn = Gtk.ToggleButton()
        self._hide_title_btn.set_icon_name("eye-open-negative-filled-symbolic")
        self._hide_title_btn.set_valign(Gtk.Align.CENTER)
        self._hide_title_btn.set_visible(False)
        self._hide_title_btn.set_tooltip_text("Hide title \u2014 logo contains the app name")
        self._hide_title_btn.set_active(bool(p and p.hide_title))
        self._hide_title_btn.connect("toggled", self._on_hide_title_toggled)
        self._logo_row, self._logo_clear_btn, self._logo_thumb, self._logo_thumb_wrap = self._make_image_row(
            "Logo", self._on_pick_logo, extra_suffix=self._hide_title_btn, thumb_w=130, thumb_h=52,
            steam_slot="logo",
        )

        self._icon_clear_btn.connect("clicked", self._on_icon_clear)
        self._cover_clear_btn.connect("clicked", self._on_cover_clear)
        self._logo_clear_btn.connect("clicked", self._on_logo_clear)

        # Prefill thumbnails in edit mode
        if p and p.icon_path:
            icon_display = self._convert_if_needed(p.icon_path)
            self._icon_row.set_subtitle(GLib.markup_escape_text(Path(p.icon_path).name))
            self._icon_clear_btn.set_visible(True)
            self._icon_thumb.set_filename(icon_display)
            self._icon_thumb_wrap.set_visible(True)
        if p and p.cover_path:
            cover_display = self._convert_if_needed(p.cover_path)
            self._cover_row.set_subtitle(GLib.markup_escape_text(Path(p.cover_path).name))
            self._cover_clear_btn.set_visible(True)
            self._cover_thumb.set_filename(cover_display)
            self._cover_thumb_wrap.set_visible(True)
        if p and p.logo_path:
            logo_display = self._convert_if_needed(p.logo_path)
            self._logo_row.set_subtitle(GLib.markup_escape_text(Path(p.logo_path).name))
            self._logo_clear_btn.set_visible(True)
            self._logo_thumb.set_filename(logo_display)
            self._logo_thumb_wrap.set_visible(True)
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
        if p and p.steam_screenshots:
            self._screenshot_grid.add_steam(p.steam_screenshots, notify=False)
            if p.selected_steam_urls:
                self._screenshot_grid.select_steam_by_urls(set(p.selected_steam_urls))
        ss_box.append(self._screenshot_grid)
        right_box.append(ss_box)

        # Fetch Steam screenshots if steam_appid is set (deduplicates automatically)
        if p and p.steam_appid:
            self._fetch_steam_screenshots(p.steam_appid)

        toolbar.set_content(scroll)
        self.set_child(toolbar)

    def _make_image_row(
        self, label: str, handler, extra_suffix=None, thumb_w: int = 64, thumb_h: int = 64,
        steam_slot: str = "",
    ) -> tuple[Adw.ActionRow, Gtk.Button, Gtk.Picture, _FixedBox]:
        row = Adw.ActionRow(title=label)
        row.set_subtitle("Not set")

        thumb = Gtk.Picture()
        thumb.set_content_fit(Gtk.ContentFit.CONTAIN)
        thumb.add_css_class("image-row-thumb")

        thumb_wrap = _FixedBox(thumb_w, thumb_h)
        thumb_wrap.set_halign(Gtk.Align.CENTER)
        thumb_wrap.set_valign(Gtk.Align.CENTER)
        thumb_wrap.set_visible(False)
        thumb_wrap.set_child(thumb)
        row.add_prefix(thumb_wrap)

        clear_btn = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove image")
        clear_btn.add_css_class("flat")
        clear_btn.set_valign(Gtk.Align.CENTER)
        clear_btn.set_visible(False)
        row.add_suffix(clear_btn)

        if extra_suffix is not None:
            row.add_suffix(extra_suffix)

        if steam_slot:
            dl_btn = Gtk.Button(
                icon_name="folder-download-symbolic",
                tooltip_text="Download from Steam",
            )
            dl_btn.add_css_class("flat")
            dl_btn.set_valign(Gtk.Align.CENTER)
            dl_btn.connect("clicked", lambda _b: self._on_steam_image_download(steam_slot))
            row.add_suffix(dl_btn)

        change_btn = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Browse\u2026")
        change_btn.add_css_class("flat")
        change_btn.set_valign(Gtk.Align.CENTER)
        change_btn.connect("clicked", handler)
        row.add_suffix(change_btn)

        return row, clear_btn, thumb, thumb_wrap

    # ── Helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _import_image_to_project(project: "Project", src_path: str, slot: str) -> str:
        """Copy *src_path* into the project directory as ``<slot><ext>``.

        ICO and BMP files are converted to PNG on import so that GTK can
        display them as thumbnails immediately.

        Returns the destination path (always inside the project directory).
        If *src_path* is already inside the project directory the file is left
        in place and the same path is returned.
        """
        import shutil
        src = Path(src_path)
        ext = src.suffix.lower()
        project.project_dir.mkdir(parents=True, exist_ok=True)

        if ext in (".ico", ".bmp"):
            from cellar.utils.images import Image
            dest = project.project_dir / f"{slot}.png"
            with Image.open(src) as img:
                img.convert("RGBA").save(dest, format="PNG")
            return str(dest)

        dest = project.project_dir / f"{slot}{src.suffix}"
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return str(dest)

    def _collect_from_widgets(self) -> dict:
        """Read current values from all widgets and return as a dict of project attrs."""
        cat_idx = self._cat_row.get_selected()
        year_txt = self._year_row.get_text().strip()
        steam_txt = self._steam_row.get_text().strip()
        genres_txt = self._genres_row.get_text().strip()

        vals: dict = {
            "version": self._version_row.get_text().strip() or "1.0",
            "category": self._cats[cat_idx - 1] if cat_idx > 0 else "",
            "developer": self._dev_row.get_text().strip(),
            "publisher": self._pub_row.get_text().strip(),
            "release_year": None,
            "steam_appid": int(steam_txt) if steam_txt.isdigit() else None,
            "website": self._website_row.get_text().strip(),
            "genres": [g.strip() for g in genres_txt.split(",") if g.strip()] if genres_txt else [],
            "summary": self._summary_row.get_text().strip(),
            "description": self._desc_row.get_text().strip(),
            "hide_title": self._hide_title_btn.get_active(),
        }
        try:
            vals["release_year"] = int(year_txt) if year_txt else None
        except ValueError:
            pass

        if isinstance(self._title_row, Adw.EntryRow):
            vals["name"] = self._title_row.get_text().strip()

        # Image paths — use tmp_ in create mode, direct path in edit mode
        vals["icon_path"] = self._tmp_icon
        vals["cover_path"] = self._tmp_cover
        vals["logo_path"] = self._tmp_logo

        return vals

    def _on_done_clicked(self, _btn) -> None:
        """Persist all widget values to the project and close."""
        if not self._project:
            self.close()
            return

        vals = self._collect_from_widgets()
        for attr, value in vals.items():
            if attr in ("icon_path", "cover_path", "logo_path"):
                # Image paths handled below
                continue
            setattr(self._project, attr, value)

        # Image paths — only update if user picked a new file (non-empty tmp_)
        if self._tmp_icon:
            self._project.icon_path = self._import_image_to_project(self._project, self._tmp_icon, "icon")
        elif self._tmp_icon == "" and self._icon_row.get_subtitle() == "Not set":
            self._project.icon_path = ""

        if self._tmp_cover:
            self._project.cover_path = self._import_image_to_project(self._project, self._tmp_cover, "cover")
        elif self._tmp_cover == "" and self._cover_row.get_subtitle() == "Not set":
            self._project.cover_path = ""

        if self._tmp_logo:
            self._project.logo_path = self._import_image_to_project(self._project, self._tmp_logo, "logo")
        elif self._tmp_logo == "" and self._logo_row.get_subtitle() == "Not set":
            self._project.logo_path = ""

        # Screenshots
        items = self._screenshot_grid.get_items()
        self._project.screenshot_paths = [i.local_path for i in items if i.local_path]
        all_steam = self._screenshot_grid.get_all_steam_items()
        self._project.steam_screenshots = [
            {"full": i.full_url, "thumbnail": i.thumb_url or ""}
            for i in all_steam if i.full_url
        ]
        self._project.selected_steam_urls = [
            i.full_url for i in items if i.is_steam and i.full_url
        ]

        save_project(self._project)
        if self._on_changed:
            self._on_changed()
        self.close()

    def _on_cancel_clicked(self, _btn) -> None:
        """Discard changes and close. The project on disk is unchanged."""
        if self._project and self._snapshot:
            # Restore the in-memory project to its original state so the
            # caller's reference reflects the on-disk version.
            restored = Project.from_dict(self._snapshot)
            for attr in (
                "name", "version", "category", "developer", "publisher",
                "release_year", "website", "genres", "summary", "description",
                "steam_appid", "icon_path", "cover_path", "logo_path",
                "hide_title", "screenshot_paths", "steam_screenshots",
                "selected_steam_urls",
            ):
                setattr(self._project, attr, getattr(restored, attr))
        self.close()

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
        if not self._is_edit:
            from cellar.backend.packager import slugify
            title = row.get_text().strip()
            slug = slugify(title) if title else ""
            self._slug_row.set_subtitle(slug)
            self._create_btn.set_sensitive(bool(title))

    def _on_screenshots_changed(self) -> None:
        """Called by the grid whenever the screenshot list changes.

        In create mode, stash into tmp fields. In edit mode, no-op —
        values are read from the grid on Done.
        """
        if not self._project:
            items = self._screenshot_grid.get_items()
            self._tmp_screenshots = [i.local_path for i in items if i.local_path]
            all_steam = self._screenshot_grid.get_all_steam_items()
            self._tmp_steam_screenshots = [
                {"full": i.full_url, "thumbnail": i.thumb_url or ""}
                for i in all_steam if i.full_url
            ]
            self._tmp_selected_steam_urls = [
                i.full_url for i in items if i.is_steam and i.full_url
            ]

    def _fetch_steam_screenshots(self, steam_appid: int) -> None:
        """Fetch Steam screenshots in the background and add to the grid."""
        from cellar.utils.async_work import run_in_background

        def _work():
            from cellar.backend.steam import fetch_details
            try:
                details = fetch_details(steam_appid)
                return details.get("screenshots", [])
            except Exception:
                return []

        def _done(screenshots):
            if screenshots:
                self._screenshot_grid.add_steam(screenshots)

        run_in_background(_work, on_done=_done)

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

    def _on_pick_icon(self, _btn) -> None:
        self._pick_image("Select Icon", False, self._on_icon_chosen)

    def _on_icon_clear(self, _btn) -> None:
        self._icon_row.set_subtitle("Not set")
        self._icon_clear_btn.set_visible(False)
        self._icon_thumb.set_paintable(None)
        self._icon_thumb_wrap.set_visible(False)
        self._tmp_icon = ""

    def _on_cover_clear(self, _btn) -> None:
        self._cover_row.set_subtitle("Not set")
        self._cover_clear_btn.set_visible(False)
        self._cover_thumb.set_paintable(None)
        self._cover_thumb_wrap.set_visible(False)
        self._tmp_cover = ""

    def _on_logo_clear(self, _btn) -> None:
        self._logo_row.set_subtitle("Not set")
        self._logo_clear_btn.set_visible(False)
        self._logo_thumb.set_paintable(None)
        self._logo_thumb_wrap.set_visible(False)
        self._hide_title_btn.set_visible(False)
        self._tmp_logo = ""

    @staticmethod
    def _convert_if_needed(path: str) -> str:
        """Convert ICO/BMP to a temp PNG so GTK can display it."""
        ext = Path(path).suffix.lower()
        if ext not in (".ico", ".bmp"):
            return path
        import tempfile
        from cellar.utils.images import Image
        with Image.open(path) as img:
            tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            img.convert("RGBA").save(tmp.name, format="PNG")
            return tmp.name

    def _on_icon_chosen(self, _c, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = self._convert_if_needed(chooser.get_file().get_path())
            self._icon_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
            self._icon_clear_btn.set_visible(True)
            self._icon_thumb.set_filename(path)
            self._icon_thumb_wrap.set_visible(True)
            self._tmp_icon = path

    def _on_pick_cover(self, _btn) -> None:
        self._pick_image("Select Cover", False, self._on_cover_chosen)

    def _on_cover_chosen(self, _c, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = self._convert_if_needed(chooser.get_file().get_path())
            self._cover_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
            self._cover_clear_btn.set_visible(True)
            self._cover_thumb.set_filename(path)
            self._cover_thumb_wrap.set_visible(True)
            self._tmp_cover = path

    def _on_pick_logo(self, _btn) -> None:
        self._pick_image("Select Logo (transparent PNG)", False, self._on_logo_chosen)

    def _on_logo_chosen(self, _c, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = self._convert_if_needed(chooser.get_file().get_path())
            self._logo_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
            self._logo_clear_btn.set_visible(True)
            self._logo_thumb.set_filename(path)
            self._logo_thumb_wrap.set_visible(True)
            self._hide_title_btn.set_visible(True)
            if not self._hide_title_btn.get_active():
                self._hide_title_btn.set_active(True)
            self._tmp_logo = path

    def _on_hide_title_toggled(self, btn: Gtk.ToggleButton) -> None:
        if btn.get_active():
            btn.set_icon_name("eye-not-looking-symbolic")
        else:
            btn.set_icon_name("eye-open-negative-filled-symbolic")

    def _on_steam_image_download(self, slot: str) -> None:
        """Download an icon, cover, or logo from Steam for the given slot."""
        steam_txt = self._steam_row.get_text().strip()
        if not steam_txt.isdigit():
            return
        appid = int(steam_txt)

        from cellar.backend.config import load_sgdb_key
        from cellar.backend.steam import fetch_steam_images, download_steam_image
        from cellar.utils.async_work import run_in_background

        sgdb_key = load_sgdb_key()

        def _work():
            urls = fetch_steam_images(appid, sgdb_key)
            url = urls.get(slot, "")
            if not url:
                return None
            import tempfile
            ext = ".ico" if url.endswith(".ico") else Path(url).suffix or ".png"
            dest = tempfile.NamedTemporaryFile(suffix=ext, delete=False).name
            download_steam_image(url, dest, sgdb_key)
            return dest

        def _done(path):
            if not path:
                return
            display = self._convert_if_needed(path)
            if slot == "icon":
                self._icon_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
                self._icon_clear_btn.set_visible(True)
                self._icon_thumb.set_filename(display)
                self._icon_thumb_wrap.set_visible(True)
                self._tmp_icon = path
            elif slot == "cover":
                self._cover_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
                self._cover_clear_btn.set_visible(True)
                self._cover_thumb.set_filename(display)
                self._cover_thumb_wrap.set_visible(True)
                self._tmp_cover = path
            elif slot == "logo":
                self._logo_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
                self._logo_clear_btn.set_visible(True)
                self._logo_thumb.set_filename(display)
                self._logo_thumb_wrap.set_visible(True)
                self._hide_title_btn.set_visible(True)
                if not self._hide_title_btn.get_active():
                    self._hide_title_btn.set_active(True)
                self._tmp_logo = path

        run_in_background(_work, on_done=_done)

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
            project.icon_path = self._import_image_to_project(project, self._tmp_icon, "icon")
        if self._tmp_cover:
            project.cover_path = self._import_image_to_project(project, self._tmp_cover, "cover")
        if self._tmp_logo:
            project.logo_path = self._import_image_to_project(project, self._tmp_logo, "logo")
        project.hide_title = self._hide_title_btn.get_active()
        local_ss = [i.local_path for i in self._screenshot_grid.get_items() if i.local_path]
        if local_ss:
            project.screenshot_paths = local_ss
        all_steam = self._screenshot_grid.get_all_steam_items()
        project.steam_screenshots = [
            {"full": i.full_url, "thumbnail": i.thumb_url or ""}
            for i in all_steam if i.full_url
        ]
        project.selected_steam_urls = [
            i.full_url for i in self._screenshot_grid.get_items()
            if i.is_steam and i.full_url
        ]
        save_project(project)

        self.close()
        if self._on_created:
            self._on_created(project)

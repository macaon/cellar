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
from cellar.views.builder.media_panel import MediaPanel

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

        # ── Right column: media (via shared MediaPanel) ───────────────────
        self._media = MediaPanel(on_changed=self._on_screenshots_changed)
        hbox.append(self._media)

        # Wire steam appid changes to media panel
        self._steam_row.connect("changed", self._on_steam_appid_changed)
        self._on_steam_appid_changed(self._steam_row)

        # Prefill media panel in edit mode
        if p:
            self._media.set_images(
                p.icon_path or "", p.cover_path or "", p.logo_path or "",
                bool(p.hide_title),
            )
            self._media.set_screenshots_local(list(p.screenshot_paths))
            if p.steam_screenshots:
                self._media.add_steam_screenshots(p.steam_screenshots)
                if p.selected_steam_urls:
                    self._media.select_steam_by_urls(set(p.selected_steam_urls))

        toolbar.set_content(scroll)
        self.set_child(toolbar)

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

    def _on_done_clicked(self, _btn) -> None:
        """Persist all widget values to the project and close."""
        if not self._project:
            self.close()
            return

        p = self._project
        cat_idx = self._cat_row.get_selected()
        year_txt = self._year_row.get_text().strip()
        steam_txt = self._steam_row.get_text().strip()
        genres_txt = self._genres_row.get_text().strip()

        if isinstance(self._title_row, Adw.EntryRow):
            p.name = self._title_row.get_text().strip()
        p.version = self._version_row.get_text().strip() or "1.0"
        p.category = self._cats[cat_idx - 1] if cat_idx > 0 else ""
        p.developer = self._dev_row.get_text().strip()
        p.publisher = self._pub_row.get_text().strip()
        try:
            p.release_year = int(year_txt) if year_txt else None
        except ValueError:
            pass
        p.steam_appid = int(steam_txt) if steam_txt.isdigit() else None
        p.website = self._website_row.get_text().strip()
        p.genres = [g.strip() for g in genres_txt.split(",") if g.strip()] if genres_txt else []
        p.summary = self._summary_row.get_text().strip()
        p.description = self._desc_row.get_text().strip()
        p.hide_title = self._media.get_hide_title()

        # Image paths — only update if user picked a new file
        icon = self._media.get_icon_path()
        if icon:
            p.icon_path = self._import_image_to_project(p, icon, "icon")
        elif self._media.icon_changed:
            p.icon_path = ""

        cover = self._media.get_cover_path()
        if cover:
            p.cover_path = self._import_image_to_project(p, cover, "cover")
        elif self._media.cover_changed:
            p.cover_path = ""

        logo = self._media.get_logo_path()
        if logo:
            p.logo_path = self._import_image_to_project(p, logo, "logo")
        elif self._media.logo_changed:
            p.logo_path = ""

        # Screenshots
        grid = self._media.screenshot_grid
        items = grid.get_items()
        p.screenshot_paths = [i.local_path for i in items if i.local_path]
        all_steam = grid.get_all_steam_items()
        p.steam_screenshots = [
            {"full": i.full_url, "thumbnail": i.thumb_url or ""}
            for i in all_steam if i.full_url
        ]
        p.selected_steam_urls = [
            i.full_url for i in items if i.is_steam and i.full_url
        ]

        save_project(p)
        if self._on_changed:
            self._on_changed()
        self.close()

    def _on_cancel_clicked(self, _btn) -> None:
        """Discard changes and close. The project on disk is unchanged."""
        if self._project and self._snapshot:
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

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_title_changed(self, row) -> None:
        if not self._is_edit:
            from cellar.backend.packager import slugify
            title = row.get_text().strip()
            slug = slugify(title) if title else ""
            self._slug_row.set_subtitle(slug)
            self._create_btn.set_sensitive(bool(title))

    def _on_steam_appid_changed(self, _row) -> None:
        steam_txt = self._steam_row.get_text().strip()
        appid = int(steam_txt) if steam_txt.isdigit() else None
        self._media.set_steam_appid(appid)

    def _on_screenshots_changed(self) -> None:
        """Called by the media panel whenever the screenshot list changes.

        In create mode, no-op — values are read from the grid on Create.
        """
        pass

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
        """Overwrite form fields from a Steam picker result."""
        if result.get("name") and isinstance(self._title_row, Adw.EntryRow):
            self._title_row.set_text(result["name"])
        if result.get("developer"):
            self._dev_row.set_text(result["developer"])
        if result.get("publisher"):
            self._pub_row.set_text(result["publisher"])
        if result.get("year"):
            self._year_row.set_text(str(result["year"]))
        if result.get("summary"):
            self._summary_row.set_text(result["summary"])
        if result.get("summary"):
            self._desc_row.set_text(result["summary"])
        if result.get("steam_appid"):
            self._steam_row.set_text(str(result["steam_appid"]))
        if result.get("website"):
            self._website_row.set_text(result["website"])
        if result.get("genres"):
            self._genres_row.set_text(", ".join(result["genres"]))
        if result.get("category") and result["category"] in self._cats:
            self._cat_row.set_selected(self._cats.index(result["category"]) + 1)
        if result.get("screenshots"):
            self._media.add_steam_screenshots(result["screenshots"])

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

        icon = self._media.get_icon_path()
        if icon:
            project.icon_path = self._import_image_to_project(project, icon, "icon")
        cover = self._media.get_cover_path()
        if cover:
            project.cover_path = self._import_image_to_project(project, cover, "cover")
        logo = self._media.get_logo_path()
        if logo:
            project.logo_path = self._import_image_to_project(project, logo, "logo")
        project.hide_title = self._media.get_hide_title()

        grid = self._media.screenshot_grid
        local_ss = [i.local_path for i in grid.get_items() if i.local_path]
        if local_ss:
            project.screenshot_paths = local_ss
        all_steam = grid.get_all_steam_items()
        project.steam_screenshots = [
            {"full": i.full_url, "thumbnail": i.thumb_url or ""}
            for i in all_steam if i.full_url
        ]
        project.selected_steam_urls = [
            i.full_url for i in grid.get_items()
            if i.is_steam and i.full_url
        ]
        save_project(project)

        self.close()
        if self._on_created:
            self._on_created(project)

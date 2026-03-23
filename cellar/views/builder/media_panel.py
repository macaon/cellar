"""Shared media panel for image and screenshot editing.

Used by ``MetadataEditorDialog`` for all metadata editing entry points
to avoid duplicating the image-row, picker, Steam-download, and
screenshot-grid code.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from cellar.views.screenshot_grid import ScreenshotGridWidget

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.views.widgets import FixedBox as _FixedBox, set_margins

log = logging.getLogger(__name__)


class MediaPanel(Gtk.Box):
    """Vertical box containing image rows (icon/cover/logo) and a screenshot grid.

    Provides a unified API for setting, clearing, and reading back image
    selections, so callers only need to wire up prefill and collection.
    """

    def __init__(self, *, on_changed: Callable | None = None) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_hexpand(True)
        set_margins(self, 16)

        self._on_changed = on_changed
        self._chooser = None  # prevent GC of FileChooserNative

        # Track image paths: "" means cleared, non-empty means set/changed.
        # _orig_* tracks the initial state so callers can detect changes.
        self._icon_path: str = ""
        self._cover_path: str = ""
        self._logo_path: str = ""
        self._orig_icon: str = ""
        self._orig_cover: str = ""
        self._orig_logo: str = ""

        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        # Image rows in a boxed-list
        img_list = Gtk.ListBox()
        img_list.add_css_class("boxed-list")
        img_list.set_selection_mode(Gtk.SelectionMode.NONE)

        (
            self._icon_row,
            self._icon_clear_btn,
            self._icon_thumb,
            self._icon_thumb_wrap,
            self._icon_dl_btn,
        ) = self._make_image_row(
            "Icon", self._on_pick_icon, thumb_w=52, thumb_h=52, steam_slot="icon",
        )

        (
            self._cover_row,
            self._cover_clear_btn,
            self._cover_thumb,
            self._cover_thumb_wrap,
            self._cover_dl_btn,
        ) = self._make_image_row(
            "Cover", self._on_pick_cover, thumb_w=52, thumb_h=70, steam_slot="cover",
        )

        self._hide_title_btn = Gtk.ToggleButton()
        self._hide_title_btn.set_icon_name("eye-open-negative-filled-symbolic")
        self._hide_title_btn.set_valign(Gtk.Align.CENTER)
        self._hide_title_btn.set_visible(False)
        self._hide_title_btn.set_tooltip_text("Hide title \u2014 logo contains the app name")
        self._hide_title_btn.connect("toggled", self._on_hide_title_toggled)

        (
            self._logo_row,
            self._logo_clear_btn,
            self._logo_thumb,
            self._logo_thumb_wrap,
            self._logo_dl_btn,
        ) = self._make_image_row(
            "Logo", self._on_pick_logo,
            extra_suffix=self._hide_title_btn, thumb_w=130, thumb_h=52, steam_slot="logo",
        )

        self._steam_dl_btns = [
            b for b in (self._icon_dl_btn, self._cover_dl_btn, self._logo_dl_btn) if b
        ]

        self._icon_clear_btn.connect("clicked", self._on_icon_clear)
        self._cover_clear_btn.connect("clicked", self._on_cover_clear)
        self._logo_clear_btn.connect("clicked", self._on_logo_clear)

        img_list.append(self._icon_row)
        img_list.append(self._cover_row)
        img_list.append(self._logo_row)
        self.append(img_list)

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
        ss_add_btn = Gtk.Button(label="Add\u2026")
        ss_add_btn.connect("clicked", lambda _: self._screenshot_grid.open_file_chooser())
        ss_heading.append(ss_add_btn)
        self.append(ss_heading)

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
        self.append(ss_box)

    def _make_image_row(
        self,
        label: str,
        handler,
        extra_suffix=None,
        thumb_w: int = 64,
        thumb_h: int = 64,
        steam_slot: str = "",
    ) -> tuple[Adw.ActionRow, Gtk.Button, Gtk.Picture, _FixedBox, Gtk.Button | None]:
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

        dl_btn = None
        if steam_slot:
            from cellar.backend.config import load_sgdb_key
            dl_btn = Gtk.Button(
                icon_name="folder-download-symbolic",
                tooltip_text="Download from Steam",
            )
            dl_btn.add_css_class("flat")
            dl_btn.set_valign(Gtk.Align.CENTER)
            dl_btn.connect("clicked", lambda _b: self._on_steam_image_download(steam_slot))
            dl_btn.set_visible(bool(load_sgdb_key()))
            dl_btn.set_sensitive(False)
            row.add_suffix(dl_btn)

        change_btn = Gtk.Button(icon_name="folder-open-symbolic", tooltip_text="Browse\u2026")
        change_btn.add_css_class("flat")
        change_btn.set_valign(Gtk.Align.CENTER)
        change_btn.connect("clicked", handler)
        row.add_suffix(change_btn)

        return row, clear_btn, thumb, thumb_wrap, dl_btn

    # ------------------------------------------------------------------
    # Public API — prefill
    # ------------------------------------------------------------------

    def set_images(self, icon: str, cover: str, logo: str, hide_title: bool) -> None:
        """Pre-fill image rows with paths and thumbnails."""
        self._orig_icon = icon
        self._orig_cover = cover
        self._orig_logo = logo
        for path, row, clear_btn, thumb, thumb_wrap, slot in [
            (icon, self._icon_row, self._icon_clear_btn,
             self._icon_thumb, self._icon_thumb_wrap, "icon"),
            (cover, self._cover_row, self._cover_clear_btn,
             self._cover_thumb, self._cover_thumb_wrap, "cover"),
            (logo, self._logo_row, self._logo_clear_btn,
             self._logo_thumb, self._logo_thumb_wrap, "logo"),
        ]:
            if path:
                display = self._convert_if_needed(path)
                row.set_subtitle(GLib.markup_escape_text(Path(path).name))
                clear_btn.set_visible(True)
                thumb.set_filename(display)
                thumb_wrap.set_visible(True)
                if slot == "icon":
                    self._icon_path = path
                elif slot == "cover":
                    self._cover_path = path
                elif slot == "logo":
                    self._logo_path = path
                    self._hide_title_btn.set_visible(True)
        if hide_title:
            self._hide_title_btn.set_active(True)
            self._hide_title_btn.set_icon_name("eye-not-looking-symbolic")

    def set_image_subtitles(
        self, icon_rel: str, cover_rel: str, logo_rel: str, hide_title: bool
    ) -> None:
        """Set subtitle text from relative paths (for edit_app where thumbnails load async)."""
        self._orig_icon = icon_rel
        self._orig_cover = cover_rel
        self._orig_logo = logo_rel
        self._icon_path = icon_rel
        self._cover_path = cover_rel
        self._logo_path = logo_rel
        if icon_rel:
            self._icon_row.set_subtitle(GLib.markup_escape_text(Path(icon_rel).name))
            self._icon_clear_btn.set_visible(True)
        if cover_rel:
            self._cover_row.set_subtitle(GLib.markup_escape_text(Path(cover_rel).name))
            self._cover_clear_btn.set_visible(True)
        if logo_rel:
            self._logo_row.set_subtitle(GLib.markup_escape_text(Path(logo_rel).name))
            self._logo_clear_btn.set_visible(True)
            self._hide_title_btn.set_visible(True)
        if hide_title:
            self._hide_title_btn.set_active(True)
            self._hide_title_btn.set_icon_name("eye-not-looking-symbolic")

    def set_thumbnail(self, slot: str, path: str) -> None:
        """Set a thumbnail image for a slot (icon/cover/logo) after async load."""
        if slot == "icon":
            self._icon_thumb.set_filename(path)
            self._icon_thumb_wrap.set_visible(True)
        elif slot == "cover":
            self._cover_thumb.set_filename(path)
            self._cover_thumb_wrap.set_visible(True)
        elif slot == "logo":
            self._logo_thumb.set_filename(path)
            self._logo_thumb_wrap.set_visible(True)

    def set_screenshots_local(
        self, paths: list[str], source_urls: list[str | None] | None = None
    ) -> None:
        self._screenshot_grid.set_local_items(paths, source_urls)

    def add_steam_screenshots(self, screenshots: list[dict]) -> None:
        self._screenshot_grid.add_steam(screenshots)

    def replace_steam_screenshots(self, screenshots: list[dict]) -> None:
        self._screenshot_grid.clear_steam()
        self._screenshot_grid.add_steam(screenshots)

    def select_steam_by_urls(self, urls: set[str]) -> None:
        self._screenshot_grid.select_steam_by_urls(urls)

    def set_steam_appid(self, appid: int | None, *, notify: bool = True) -> None:
        """Enable/disable Steam download buttons based on appid."""
        has_appid = appid is not None
        for btn in self._steam_dl_btns:
            btn.set_sensitive(has_appid)
        self._steam_appid = appid
        if has_appid:
            self._fetch_steam_screenshots(appid, notify=notify)

    # ------------------------------------------------------------------
    # Public API — read back
    # ------------------------------------------------------------------

    def get_icon_path(self) -> str:
        return self._icon_path

    def get_cover_path(self) -> str:
        return self._cover_path

    def get_logo_path(self) -> str:
        return self._logo_path

    def get_hide_title(self) -> bool:
        return self._hide_title_btn.get_active()

    @property
    def screenshot_grid(self) -> "ScreenshotGridWidget":
        return self._screenshot_grid

    @property
    def icon_changed(self) -> bool:
        """True if the icon was changed or cleared (vs original)."""
        return self._icon_path != self._orig_icon

    @property
    def cover_changed(self) -> bool:
        return self._cover_path != self._orig_cover

    @property
    def logo_changed(self) -> bool:
        return self._logo_path != self._orig_logo

    # ------------------------------------------------------------------
    # Image picking
    # ------------------------------------------------------------------

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

    def _on_pick_icon(self, _btn) -> None:
        self._pick_image("Select Icon", False, self._on_icon_chosen)

    def _on_icon_chosen(self, _c, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = chooser.get_file().get_path()
            display = self._convert_if_needed(path)
            self._icon_path = path
            self._icon_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
            self._icon_clear_btn.set_visible(True)
            self._icon_thumb.set_filename(display)
            self._icon_thumb_wrap.set_visible(True)

    def _on_pick_cover(self, _btn) -> None:
        self._pick_image("Select Cover", False, self._on_cover_chosen)

    def _on_cover_chosen(self, _c, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = chooser.get_file().get_path()
            display = self._convert_if_needed(path)
            self._cover_path = path
            self._cover_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
            self._cover_clear_btn.set_visible(True)
            self._cover_thumb.set_filename(display)
            self._cover_thumb_wrap.set_visible(True)

    def _on_pick_logo(self, _btn) -> None:
        self._pick_image("Select Logo (transparent PNG)", False, self._on_logo_chosen)

    def _on_logo_chosen(self, _c, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            path = chooser.get_file().get_path()
            display = self._convert_if_needed(path)
            self._logo_path = path
            self._logo_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
            self._logo_clear_btn.set_visible(True)
            self._logo_thumb.set_filename(display)
            self._logo_thumb_wrap.set_visible(True)
            self._hide_title_btn.set_visible(True)
            if not self._hide_title_btn.get_active():
                self._hide_title_btn.set_active(True)

    # ------------------------------------------------------------------
    # Image clearing
    # ------------------------------------------------------------------

    def _on_icon_clear(self, _btn) -> None:
        self._icon_path = ""
        self._icon_row.set_subtitle("Not set")
        self._icon_clear_btn.set_visible(False)
        self._icon_thumb.set_paintable(None)
        self._icon_thumb_wrap.set_visible(False)

    def _on_cover_clear(self, _btn) -> None:
        self._cover_path = ""
        self._cover_row.set_subtitle("Not set")
        self._cover_clear_btn.set_visible(False)
        self._cover_thumb.set_paintable(None)
        self._cover_thumb_wrap.set_visible(False)

    def _on_logo_clear(self, _btn) -> None:
        self._logo_path = ""
        self._logo_row.set_subtitle("Not set")
        self._logo_clear_btn.set_visible(False)
        self._logo_thumb.set_paintable(None)
        self._logo_thumb_wrap.set_visible(False)
        self._hide_title_btn.set_visible(False)

    # ------------------------------------------------------------------
    # Hide-title toggle
    # ------------------------------------------------------------------

    def _on_hide_title_toggled(self, btn: Gtk.ToggleButton) -> None:
        if btn.get_active():
            btn.set_icon_name("eye-not-looking-symbolic")
        else:
            btn.set_icon_name("eye-open-negative-filled-symbolic")

    # ------------------------------------------------------------------
    # Screenshots
    # ------------------------------------------------------------------

    def _on_screenshots_changed(self) -> None:
        if self._on_changed:
            self._on_changed()

    def _fetch_steam_screenshots(self, steam_appid: int, *, notify: bool = True) -> None:
        from cellar.utils.async_work import run_in_background

        def _work():
            from cellar.backend.steam import fetch_details
            try:
                details = fetch_details(steam_appid)
                return details.get("screenshots", [])
            except Exception:
                return []

        def _done(screenshots):
            self._screenshot_grid.clear_steam()
            if screenshots:
                self._screenshot_grid.add_steam(screenshots, notify=notify)

        run_in_background(_work, on_done=_done)

    # ------------------------------------------------------------------
    # Steam image download
    # ------------------------------------------------------------------

    def _dl_btn_for_slot(self, slot: str) -> Gtk.Button | None:
        return {
            "icon": self._icon_dl_btn,
            "cover": self._cover_dl_btn,
            "logo": self._logo_dl_btn,
        }.get(slot)

    def _on_steam_image_download(self, slot: str) -> None:
        appid = getattr(self, "_steam_appid", None)
        if not appid:
            return

        from cellar.backend.config import load_sgdb_key, load_sgdb_language
        from cellar.backend.steam import download_steam_image, fetch_steam_images
        from cellar.utils.async_work import run_in_background

        sgdb_key = load_sgdb_key()
        sgdb_lang = load_sgdb_language()

        # Replace button content with a spinner (keep sensitive so it animates)
        dl_btn = self._dl_btn_for_slot(slot)
        if dl_btn:
            if getattr(dl_btn, "_downloading", False):
                return  # already in progress
            dl_btn._downloading = True
            spinner = Adw.Spinner()
            dl_btn.set_child(spinner)

        def _work():
            urls = fetch_steam_images(appid, sgdb_key, language=sgdb_lang)
            url = urls.get(slot, "")
            if not url:
                return None
            import tempfile
            from urllib.parse import urlparse
            # Extract clean extension from URL path (ignore query string)
            url_path = urlparse(url).path
            ext = Path(url_path).suffix or ".png"
            dest = tempfile.NamedTemporaryFile(suffix=ext, delete=False).name
            fallbacks = urls.get(f"{slot}_candidates", [])[1:]
            download_steam_image(url, dest, sgdb_key, fallback_urls=fallbacks)
            return dest

        def _restore_btn():
            if dl_btn:
                dl_btn._downloading = False
                dl_btn.set_child(None)
                dl_btn.set_icon_name("folder-download-symbolic")

        def _error(msg):
            _restore_btn()
            log.warning("Steam %s download failed for appid %s: %s", slot, appid, msg)

        def _done(path):
            _restore_btn()
            if not path:
                return
            display = self._convert_if_needed(path)
            if slot == "icon":
                self._icon_path = path
                self._icon_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
                self._icon_clear_btn.set_visible(True)
                self._icon_thumb.set_filename(display)
                self._icon_thumb_wrap.set_visible(True)
            elif slot == "cover":
                self._cover_path = path
                self._cover_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
                self._cover_clear_btn.set_visible(True)
                self._cover_thumb.set_filename(display)
                self._cover_thumb_wrap.set_visible(True)
            elif slot == "logo":
                self._logo_path = path
                self._logo_row.set_subtitle(GLib.markup_escape_text(Path(path).name))
                self._logo_clear_btn.set_visible(True)
                self._logo_thumb.set_filename(display)
                self._logo_thumb_wrap.set_visible(True)
                self._hide_title_btn.set_visible(True)
                if not self._hide_title_btn.get_active():
                    self._hide_title_btn.set_active(True)

        run_in_background(_work, on_done=_done, on_error=_error)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

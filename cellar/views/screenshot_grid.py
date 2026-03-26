"""Screenshot manager widget.

A single GtkFlowBox showing local screenshots first, then Steam suggestions.

Every tile has a checkmark: checked = included in catalogue / will be uploaded.
Local tiles are pre-checked; unchecking marks the file for deletion from repo on save.
Steam tiles start unchecked; checking triggers download + inclusion on save.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gdk, Gtk

from cellar.utils.async_work import run_in_background
from cellar.views.widgets import set_margins

log = logging.getLogger(__name__)

_THUMB_W = 240
_THUMB_H = 135


@dataclass
class ScreenshotItem:
    """One screenshot entry — either a committed local file or a Steam suggestion."""

    local_path: str | None          # absolute path on disk (None = Steam pending)
    full_url: str | None = None     # Steam full-res URL, downloaded on save
    thumb_url: str | None = None    # Steam thumbnail URL (for display)
    source_url: str | None = None   # original URL this local file was sourced from
    thumb_path: str | None = None   # temp path for Steam thumbnail display

    @property
    def is_steam(self) -> bool:
        return self.local_path is None and self.full_url is not None

    @property
    def display_path(self) -> str | None:
        return self.local_path or self.thumb_path


class ScreenshotGridWidget(Gtk.Box):
    """Reusable screenshot manager with grid view, selection, reordering and Steam suggestions.

    Every tile has a checkmark toggle:
    - Local tiles: pre-checked; uncheck = exclude on save (caller handles deletion).
    - Steam tiles: unchecked by default; check = download + include on save.

    Usage::

        grid = ScreenshotGridWidget(on_changed=lambda: ...)
        grid.set_local_items(paths, source_urls)   # pre-fill from existing repo data
        grid.add_local(new_paths)
        grid.add_steam(steam_data)                 # list[{"thumbnail": url, "full": url}]
        grid.open_file_chooser()                   # trigger file picker from external button

        # On save:
        items = grid.get_items()           # included items (selected local + selected steam)
        excluded = grid.get_excluded_local_items() # local items unchecked by user
    """

    def __init__(self, *, on_changed=None, scrolled: bool = True, **kwargs) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, **kwargs)
        self._on_changed = on_changed or (lambda: None)
        self._scrolled = scrolled

        self._local: list[ScreenshotItem] = []
        self._steam: list[ScreenshotItem] = []
        self._selected_local: set[int] = set()
        self._selected_steam: set[int] = set()

        self._tmp_dir: Path | None = None

        self._build_ui()
        self.connect("destroy", self._on_destroy)

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._flow = Gtk.FlowBox()
        self._flow.set_valign(Gtk.Align.START)
        self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow.set_activate_on_single_click(False)
        if not self._scrolled:
            # Embedded two-pane: exactly 2 columns, tiles fill their column
            self._flow.set_min_children_per_line(2)
            self._flow.set_max_children_per_line(2)
        else:
            # Standalone: natural flow, tiles at fixed width
            self._flow.set_min_children_per_line(1)
            self._flow.set_max_children_per_line(10)
        self._flow.set_row_spacing(8)
        self._flow.set_column_spacing(8)
        set_margins(self._flow, 8)

        if self._scrolled:
            scroll = Gtk.ScrolledWindow(
                hscrollbar_policy=Gtk.PolicyType.NEVER,
                vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            )
            scroll.set_min_content_height(160)
            scroll.set_vexpand(True)
            scroll.set_child(self._flow)
            self.append(scroll)
        else:
            self._flow.set_vexpand(True)
            self.append(self._flow)

        self._rebuild()

    # ── Public API ────────────────────────────────────────────────────────

    def set_local_items(
        self,
        paths: list[str],
        source_urls: list[str | None] | None = None,
    ) -> None:
        """Replace the local list (used for initial prefill from repo). All pre-checked."""
        srcs = source_urls or [None] * len(paths)
        self._local = [
            ScreenshotItem(local_path=p, source_url=s)
            for p, s in zip(paths, srcs)
        ]
        self._selected_local = set(range(len(self._local)))
        self._rebuild()

    def add_local(self, paths: list[str]) -> None:
        """Append local file paths to the local section. New items are pre-checked."""
        start = len(self._local)
        for p in paths:
            self._local.append(ScreenshotItem(local_path=p))
        for i in range(start, len(self._local)):
            self._selected_local.add(i)
        self._rebuild()
        self._on_changed()

    def add_steam(self, steam_data: list[dict], *, notify: bool = True) -> None:
        """Add Steam screenshot suggestions, deduplicating against existing local items.

        *steam_data* is a list of ``{"thumbnail": url, "full": url}`` dicts.
        Steam items start unchecked; the user checks the ones they want.
        Pass ``notify=False`` when restoring saved state to suppress the on_changed callback.
        """
        existing_filenames = {
            Path(item.local_path).name
            for item in self._local
            if item.local_path
        }
        # Strip query strings (?t=...) for URL comparison — Steam's ?t= is an
        # app-update timestamp that changes on store edits but doesn't affect
        # which screenshot it is.
        def _url_base(url: str) -> str:
            return url.split("?")[0]

        existing_source_bases = {
            _url_base(item.source_url) for item in self._local if item.source_url
        }
        existing_steam_full = {item.full_url for item in self._steam}

        added: list[ScreenshotItem] = []
        for d in steam_data:
            full_url = d.get("full", "")
            if not full_url:
                continue
            if full_url in existing_steam_full or _url_base(full_url) in existing_source_bases:
                continue
            if Path(full_url.split("?")[0]).name in existing_filenames:
                continue
            item = ScreenshotItem(
                local_path=None,
                full_url=full_url,
                thumb_url=d.get("thumbnail", ""),
            )
            added.append(item)
            existing_steam_full.add(full_url)

        if not added:
            return

        self._steam.extend(added)
        self._rebuild()
        self._load_steam_thumbnails(added)
        if notify:
            self._on_changed()

    def set_items(self, items: list[ScreenshotItem]) -> None:
        """Restore full widget state (local + steam-pending). Local items are pre-checked."""
        self._local = [i for i in items if not i.is_steam]
        self._steam = [i for i in items if i.is_steam]
        self._selected_local = set(range(len(self._local)))
        self._selected_steam.clear()
        self._rebuild()

    def clear_steam(self) -> None:
        """Remove all Steam-pending items (used after eager download promotes them to local)."""
        self._steam.clear()
        self._selected_steam.clear()
        self._rebuild()

    def get_items(self) -> list[ScreenshotItem]:
        """Return items to include on save: selected local + selected steam."""
        selected_local = [
            self._local[i]
            for i in sorted(self._selected_local)
            if i < len(self._local)
        ]
        selected_steam = [
            self._steam[i]
            for i in sorted(self._selected_steam)
            if i < len(self._steam)
        ]
        return selected_local + selected_steam

    def get_all_steam_items(self) -> list[ScreenshotItem]:
        """Return all Steam suggestion items regardless of checked state."""
        return list(self._steam)

    def select_steam_by_urls(self, urls: set[str]) -> None:
        """Pre-check Steam items whose full_url is in *urls*. Call after add_steam()."""
        self._selected_steam = {
            i for i, item in enumerate(self._steam)
            if item.full_url in urls
        }
        self._rebuild()

    def get_excluded_local_items(self) -> list[ScreenshotItem]:
        """Return local items the user deselected (caller may delete the files on save)."""
        return [
            self._local[i]
            for i in range(len(self._local))
            if i not in self._selected_local
        ]

    def open_file_chooser(self) -> None:
        """Open the file chooser to add screenshots (call from an external Add button)."""
        self._on_browse_clicked(None)

    # ── Thumbnail loading ─────────────────────────────────────────────────

    def _ensure_tmp(self) -> Path:
        if self._tmp_dir is None:
            self._tmp_dir = Path(tempfile.mkdtemp(prefix="cellar-ss-"))
        return self._tmp_dir

    def _load_steam_thumbnails(self, items: list[ScreenshotItem]) -> None:
        tmp = self._ensure_tmp()
        for item in items:
            url = item.thumb_url
            if not url:
                continue
            from pathlib import PurePosixPath
            ext = PurePosixPath(url.split("?")[0]).suffix or ".jpg"
            dest = tmp / f"thumb_{id(item)}{ext}"

            def _work(url=url, dest=dest) -> str | None:
                from cellar.utils.http import make_session
                try:
                    r = make_session().get(url, timeout=15)
                    if r.ok:
                        dest.write_bytes(r.content)
                        return str(dest)
                except Exception:
                    pass
                return None

            def _done(path: str | None, item=item) -> None:
                if path:
                    item.thumb_path = path
                    self._refresh_tile_picture(item)

            run_in_background(_work, on_done=_done)

    def _refresh_tile_picture(self, item: ScreenshotItem) -> None:
        child = self._flow.get_first_child()
        while child is not None:
            if getattr(child, "_ss_item", None) is item:
                pic = getattr(child, "_ss_picture", None)
                if pic and item.display_path:
                    # Use set_paintable with an explicit Texture — set_filename
                    # does not reliably trigger a redraw for all image formats.
                    from gi.repository import Gdk
                    try:
                        texture = Gdk.Texture.new_from_filename(item.display_path)
                        pic.set_paintable(texture)
                    except Exception:
                        pic.set_filename(item.display_path)
                break
            child = child.get_next_sibling()

    # ── FlowBox rebuild ───────────────────────────────────────────────────

    def _rebuild(self) -> None:
        child = self._flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._flow.remove(child)
            child = nxt

        for i, item in enumerate(self._local):
            self._flow.append(self._make_tile(item, "local", i))

        for i, item in enumerate(self._steam):
            self._flow.append(self._make_tile(item, "steam", i))

    def _make_tile(self, item: ScreenshotItem, kind: str, idx: int) -> Gtk.FlowBoxChild:
        fbc = Gtk.FlowBoxChild()
        fbc.set_focusable(True)
        if self._scrolled:
            fbc.set_hexpand(False)
            fbc.set_halign(Gtk.Align.START)
        fbc.add_css_class("ss-tile-cell")
        fbc._ss_item = item

        selected = (
            idx in self._selected_local if kind == "local"
            else idx in self._selected_steam
        )

        overlay = Gtk.Overlay()
        overlay.add_css_class("ss-tile")
        overlay.set_overflow(Gtk.Overflow.HIDDEN)
        if selected:
            overlay.add_css_class("selected")
        fbc._ss_overlay = overlay

        pic = Gtk.Picture()
        pic.set_size_request(_THUMB_W, _THUMB_H)
        pic.set_content_fit(Gtk.ContentFit.COVER)
        pic.add_css_class("ss-tile-pic")
        if item.display_path:
            pic.set_filename(item.display_path)
        fbc._ss_picture = pic
        overlay.set_child(pic)

        # Checkmark badge — top-right corner, on all tiles
        check_wrap = Gtk.Box()
        check_wrap.set_halign(Gtk.Align.END)
        check_wrap.set_valign(Gtk.Align.START)
        check_icon = Gtk.Image.new_from_icon_name("object-select-symbolic")
        check_icon.add_css_class("ss-check-badge")
        check_icon.set_visible(selected)
        fbc._ss_check = check_icon
        check_wrap.append(check_icon)
        overlay.add_overlay(check_wrap)

        # Cloud badge — bottom-left, Steam tiles only
        if kind == "steam":
            cloud_wrap = Gtk.Box()
            cloud_wrap.set_halign(Gtk.Align.START)
            cloud_wrap.set_valign(Gtk.Align.END)
            cloud_icon = Gtk.Image.new_from_icon_name("cloud-filled-symbolic")
            cloud_icon.add_css_class("ss-steam-badge")
            cloud_wrap.append(cloud_icon)
            overlay.add_overlay(cloud_wrap)

        # Click to toggle selection on all tiles
        gesture = Gtk.GestureClick()
        gesture.connect(
            "released",
            lambda _g, _n, _x, _y, k=kind, i=idx: self._on_tile_clicked(k, i),
        )
        overlay.add_controller(gesture)

        # Keyboard: Space/Return toggles selection (HIG accessibility)
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.connect(
            "key-pressed",
            lambda _c, kv, _code, _mod, k=kind, i=idx: (
                self._on_tile_clicked(k, i) or True
            )
            if kv in (Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_KP_Enter)
            else False,
        )
        fbc.add_controller(key_ctrl)

        fbc.set_child(overlay)
        return fbc

    # ── Selection ─────────────────────────────────────────────────────────

    def _on_tile_clicked(self, kind: str, idx: int) -> None:
        sel = self._selected_local if kind == "local" else self._selected_steam
        if idx in sel:
            sel.discard(idx)
        else:
            sel.add(idx)
        self._refresh_tile_selection(kind, idx)
        self._on_changed()

    def _refresh_tile_selection(self, kind: str, idx: int) -> None:
        pool = self._local if kind == "local" else self._steam
        sel = self._selected_local if kind == "local" else self._selected_steam
        is_sel = idx in sel

        child = self._flow.get_first_child()
        while child is not None:
            item = getattr(child, "_ss_item", None)
            if item is not None:
                c_kind = "steam" if item.is_steam else "local"
                if c_kind == kind:
                    try:
                        c_idx = pool.index(item)
                    except ValueError:
                        child = child.get_next_sibling()
                        continue
                    if c_idx == idx:
                        overlay = getattr(child, "_ss_overlay", None)
                        check = getattr(child, "_ss_check", None)
                        if overlay:
                            if is_sel:
                                overlay.add_css_class("selected")
                            else:
                                overlay.remove_css_class("selected")
                        if check:
                            check.set_visible(is_sel)
                        break
            child = child.get_next_sibling()

    # ── Browse ────────────────────────────────────────────────────────────

    def _on_browse_clicked(self, _btn) -> None:
        chooser = Gtk.FileChooserNative(
            title="Add Screenshots",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            select_multiple=True,
        )
        img_filter = Gtk.FileFilter()
        img_filter.set_name("Images (PNG, JPG)")
        img_filter.add_mime_type("image/png")
        img_filter.add_mime_type("image/jpeg")
        chooser.add_filter(img_filter)
        chooser.connect("response", self._on_browse_response, chooser)
        chooser.show()

    def _on_browse_response(self, _c, response: int, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        files = chooser.get_files()
        paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
        self.add_local([p for p in paths if p])

    # ── Cleanup ───────────────────────────────────────────────────────────

    def _on_destroy(self, _widget) -> None:
        if self._tmp_dir and self._tmp_dir.exists():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None

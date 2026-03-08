"""Screenshot manager widget.

A single GtkFlowBox showing local screenshots first, then Steam suggestions
(labelled with a cloud badge) below a "Available on Steam" divider row.

Click a tile to toggle selection (checkmark + accent outline); drag local
tiles to reorder.  Steam-pending items are downloaded transparently when
``get_items()`` results are handed to the save pipeline.
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
from gi.repository import Gdk, GLib, GObject, Gtk

from cellar.utils.async_work import run_in_background

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

    Usage::

        grid = ScreenshotGridWidget(on_changed=lambda: ...)
        grid.set_local_items(paths, source_urls)   # pre-fill from existing repo data
        grid.add_local(new_paths)
        grid.add_steam(steam_data)                 # list[{"thumbnail": url, "full": url}]

        # On save:
        items = grid.get_items()                   # ordered list[ScreenshotItem]
    """

    def __init__(self, *, on_changed=None, scrolled: bool = True, **kwargs) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0, **kwargs)
        self._on_changed = on_changed or (lambda: None)
        self._scrolled = scrolled

        self._local: list[ScreenshotItem] = []
        self._steam: list[ScreenshotItem] = []
        self._selected_local: set[int] = set()
        self._selected_steam: set[int] = set()
        self._drag_src_idx: int | None = None

        self._tmp_dir: Path | None = None

        self._build_ui()
        self.connect("destroy", self._on_destroy)

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._flow = Gtk.FlowBox()
        self._flow.set_valign(Gtk.Align.START)
        self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow.set_max_children_per_line(2 if not self._scrolled else 10)
        self._flow.set_min_children_per_line(1)
        self._flow.set_row_spacing(8)
        self._flow.set_column_spacing(8)
        self._flow.set_margin_top(8)
        self._flow.set_margin_bottom(8)
        self._flow.set_margin_start(8)
        self._flow.set_margin_end(8)

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

        # Action bar
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        bar.set_margin_top(6)
        bar.set_margin_start(8)
        bar.set_margin_end(8)
        bar.set_margin_bottom(0)

        browse_btn = Gtk.Button(label="Add…")
        browse_btn.connect("clicked", self._on_browse_clicked)
        bar.append(browse_btn)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        bar.append(spacer)

        self._remove_btn = Gtk.Button(label="Remove Selected")
        self._remove_btn.add_css_class("destructive-action")
        self._remove_btn.set_sensitive(False)
        self._remove_btn.connect("clicked", self._on_remove_clicked)
        bar.append(self._remove_btn)

        self.append(bar)

        self._rebuild()

    # ── Public API ────────────────────────────────────────────────────────

    def set_local_items(
        self,
        paths: list[str],
        source_urls: list[str | None] | None = None,
    ) -> None:
        """Replace the local list (used for initial prefill from repo)."""
        srcs = source_urls or [None] * len(paths)
        self._local = [
            ScreenshotItem(local_path=p, source_url=s)
            for p, s in zip(paths, srcs)
        ]
        self._selected_local.clear()
        self._rebuild()

    def add_local(self, paths: list[str]) -> None:
        """Append local file paths to the local section."""
        for p in paths:
            self._local.append(ScreenshotItem(local_path=p))
        self._rebuild()
        self._on_changed()

    def add_steam(self, steam_data: list[dict]) -> None:
        """Add Steam screenshot suggestions, deduplicating against existing local items.

        *steam_data* is a list of ``{"thumbnail": url, "full": url}`` dicts.
        Items whose full URL filename or source URL already exists locally are skipped.
        """
        existing_filenames = {
            Path(item.local_path).name
            for item in self._local
            if item.local_path
        }
        existing_source_urls = {
            item.source_url for item in self._local if item.source_url
        }
        existing_steam_full = {item.full_url for item in self._steam}

        added: list[ScreenshotItem] = []
        for d in steam_data:
            full_url = d.get("full", "")
            if not full_url:
                continue
            if full_url in existing_steam_full or full_url in existing_source_urls:
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
        self._on_changed()

    def set_items(self, items: list[ScreenshotItem]) -> None:
        """Restore full widget state (local + steam-pending) preserving cached thumb_paths."""
        self._local = [i for i in items if not i.is_steam]
        self._steam = [i for i in items if i.is_steam]
        self._selected_local.clear()
        self._selected_steam.clear()
        self._rebuild()

    def clear_steam(self) -> None:
        """Remove all Steam-pending items (used after eager download promotes them to local)."""
        self._steam.clear()
        self._selected_steam.clear()
        self._rebuild()

    def get_items(self) -> list[ScreenshotItem]:
        """Return all items in display order: local first, then Steam pending."""
        return list(self._local) + list(self._steam)

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
            dest = tmp / f"steam_{id(item)}.jpg"

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
                    pic.set_filename(item.display_path)
                break
            child = child.get_next_sibling()

    # ── FlowBox rebuild ───────────────────────────────────────────────────

    def _rebuild(self) -> None:
        # Remove all existing children
        child = self._flow.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self._flow.remove(child)
            child = nxt

        if not self._local and not self._steam:
            fbc = Gtk.FlowBoxChild()
            fbc.set_focusable(False)
            lbl = Gtk.Label(label="No screenshots")
            lbl.add_css_class("dim-label")
            lbl.set_margin_top(16)
            lbl.set_margin_bottom(16)
            fbc.set_child(lbl)
            self._flow.append(fbc)
            self._update_remove_btn()
            return

        for i, item in enumerate(self._local):
            self._flow.append(self._make_tile(item, "local", i))

        if self._steam:
            self._flow.append(self._make_divider())
            for i, item in enumerate(self._steam):
                self._flow.append(self._make_tile(item, "steam", i))

        self._update_remove_btn()

    def _make_divider(self) -> Gtk.FlowBoxChild:
        fbc = Gtk.FlowBoxChild()
        fbc.set_focusable(False)
        fbc.set_hexpand(True)
        fbc.add_css_class("ss-divider-child")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        box.set_margin_top(4)
        box.set_margin_bottom(4)
        box.set_margin_start(4)
        box.set_margin_end(4)

        sep1 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep1.set_hexpand(True)
        sep1.set_valign(Gtk.Align.CENTER)

        lbl = Gtk.Label(label="Available on Steam")
        lbl.add_css_class("dim-label")
        lbl.add_css_class("caption")

        sep2 = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep2.set_hexpand(True)
        sep2.set_valign(Gtk.Align.CENTER)

        box.append(sep1)
        box.append(lbl)
        box.append(sep2)
        fbc.set_child(box)
        return fbc

    def _make_tile(self, item: ScreenshotItem, kind: str, idx: int) -> Gtk.FlowBoxChild:
        fbc = Gtk.FlowBoxChild()
        fbc.set_focusable(False)
        fbc.set_hexpand(False)
        fbc.set_halign(Gtk.Align.START)
        fbc.add_css_class("ss-tile-cell")
        fbc._ss_item = item

        selected = (
            (kind == "local" and idx in self._selected_local)
            or (kind == "steam" and idx in self._selected_steam)
        )

        overlay = Gtk.Overlay()
        overlay.add_css_class("ss-tile")
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

        # Checkmark badge — top-right corner
        check_wrap = Gtk.Box()
        check_wrap.set_halign(Gtk.Align.END)
        check_wrap.set_valign(Gtk.Align.START)
        check_icon = Gtk.Image.new_from_icon_name("checkmark-symbolic")
        check_icon.add_css_class("ss-check-badge")
        check_icon.set_visible(selected)
        fbc._ss_check = check_icon
        check_wrap.append(check_icon)
        overlay.add_overlay(check_wrap)

        # Cloud badge — bottom-left corner, Steam items only
        if item.is_steam:
            cloud_wrap = Gtk.Box()
            cloud_wrap.set_halign(Gtk.Align.START)
            cloud_wrap.set_valign(Gtk.Align.END)
            cloud_icon = Gtk.Image.new_from_icon_name("cloud-filled-symbolic")
            cloud_icon.add_css_class("ss-steam-badge")
            cloud_wrap.append(cloud_icon)
            overlay.add_overlay(cloud_wrap)

        # Click to toggle selection
        gesture = Gtk.GestureClick()
        gesture.connect(
            "released",
            lambda _g, _n, _x, _y, k=kind, i=idx: self._on_tile_clicked(k, i),
        )
        overlay.add_controller(gesture)

        # Drag-and-drop for local items only
        if kind == "local":
            self._attach_dnd(fbc, idx)

        fbc.set_child(overlay)
        return fbc

    # ── Drag-and-drop (local items only) ──────────────────────────────────

    def _attach_dnd(self, fbc: Gtk.FlowBoxChild, idx: int) -> None:
        drag_src = Gtk.DragSource()
        drag_src.set_actions(Gdk.DragAction.MOVE)
        drag_src.connect("prepare", self._on_drag_prepare, idx)
        drag_src.connect("drag-end", self._on_drag_end)
        fbc.add_controller(drag_src)

        drop_tgt = Gtk.DropTarget.new(GObject.TYPE_INT, Gdk.DragAction.MOVE)
        drop_tgt.connect("drop", self._on_drop, idx)
        fbc.add_controller(drop_tgt)

    def _on_drag_prepare(self, _src, _x, _y, idx: int) -> Gdk.ContentProvider:
        self._drag_src_idx = idx
        val = GObject.Value()
        val.init(GObject.TYPE_INT)
        val.set_int(idx)
        return Gdk.ContentProvider.new_for_value(val)

    def _on_drag_end(self, _src, _drag, _delete_data) -> None:
        self._drag_src_idx = None

    def _on_drop(self, _target, _value, _x, _y, dst_idx: int) -> bool:
        src_idx = self._drag_src_idx
        if src_idx is None or src_idx == dst_idx:
            return False

        item = self._local.pop(src_idx)

        # Remap selection indices after the move
        new_sel: set[int] = set()
        for si in self._selected_local:
            if si == src_idx:
                new_sel.add(dst_idx)
            elif src_idx < dst_idx:
                new_sel.add(si - 1 if src_idx < si <= dst_idx else si)
            else:
                new_sel.add(si + 1 if dst_idx <= si < src_idx else si)
        self._selected_local = new_sel

        self._local.insert(dst_idx, item)
        self._rebuild()
        self._on_changed()
        return True

    # ── Selection ─────────────────────────────────────────────────────────

    def _on_tile_clicked(self, kind: str, idx: int) -> None:
        sel = self._selected_local if kind == "local" else self._selected_steam
        if idx in sel:
            sel.discard(idx)
        else:
            sel.add(idx)
        self._refresh_tile_selection(kind, idx)
        self._update_remove_btn()

    def _refresh_tile_selection(self, kind: str, idx: int) -> None:
        """Update a single tile's CSS class and checkmark without a full rebuild."""
        is_sel = (
            idx in self._selected_local if kind == "local" else idx in self._selected_steam
        )
        pool = self._local if kind == "local" else self._steam

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

    def _update_remove_btn(self) -> None:
        n = len(self._selected_local) + len(self._selected_steam)
        self._remove_btn.set_sensitive(n > 0)
        self._remove_btn.set_label(
            f"Remove Selected ({n})" if n > 0 else "Remove Selected"
        )

    def _on_remove_clicked(self, _btn) -> None:
        for idx in sorted(self._selected_local, reverse=True):
            if 0 <= idx < len(self._local):
                del self._local[idx]
        for idx in sorted(self._selected_steam, reverse=True):
            if 0 <= idx < len(self._steam):
                del self._steam[idx]
        self._selected_local.clear()
        self._selected_steam.clear()
        self._rebuild()
        self._on_changed()

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

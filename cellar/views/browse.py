"""Browse view — scrolling grid of app cards with category filter and search."""

from __future__ import annotations

import logging
import os
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk, Pango

from cellar.models.app_entry import AppEntry

log = logging.getLogger(__name__)

_DEFAULT_CAPSULE_WIDTH = 200
_ICON_SIZE_MAX = 64


# ---------------------------------------------------------------------------
# _FixedBox — single-child container with a hard-coded natural size
# ---------------------------------------------------------------------------

class _FixedBox(Gtk.Widget):
    """Single-child container that always reports a fixed natural size.

    ``Gtk.Box`` propagates its children's natural sizes upward.  A full-
    resolution ``Gtk.Picture`` loaded with ``new_for_filename()`` has a
    natural size equal to the image's pixel dimensions, which would cause
    ``FlowBox`` to allocate cards at the image size rather than the capsule
    size.

    This widget solves the problem: ``do_measure`` always returns
    ``(width, height)`` so the FlowBox sees the correct capsule dimensions,
    while the child (``Gtk.Picture``) is allocated the full area and GTK's
    own renderer scales the image at display resolution — giving correct
    HiDPI sharpness without any software pre-scaling.
    """

    __gtype_name__ = "CellarFixedBox"

    def __init__(self, width: int, height: int) -> None:
        super().__init__()
        self._w = width
        self._h = height
        self._child: Gtk.Widget | None = None
        self.set_overflow(Gtk.Overflow.HIDDEN)

    def set_child(self, child: Gtk.Widget | None) -> None:
        if self._child is not None:
            self._child.unparent()
        self._child = child
        if child is not None:
            child.set_parent(self)

    # GTK virtual methods ──────────────────────────────────────────────────

    def do_measure(self, orientation, for_size):
        size = self._w if orientation == Gtk.Orientation.HORIZONTAL else self._h
        return size, size, -1, -1

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        if self._child is not None:
            self._child.allocate(width, height, baseline, None)

    def do_snapshot(self, snapshot) -> None:
        if self._child is not None:
            self.snapshot_child(self._child, snapshot)

    def do_dispose(self) -> None:
        if self._child is not None:
            self._child.unparent()
            self._child = None
        super().do_dispose()


# ---------------------------------------------------------------------------
# AppCard
# ---------------------------------------------------------------------------

class AppCard(Gtk.FlowBoxChild):
    """A single app tile in the browse grid."""

    def __init__(
        self,
        entry: AppEntry,
        *,
        resolve_asset: Callable[[str], str] | None = None,
        cover_width: int = _DEFAULT_CAPSULE_WIDTH,
    ) -> None:
        super().__init__()
        self.entry = entry

        cover_height = cover_width * 3 // 2   # enforce 2:3 portrait ratio

        self.set_margin_start(6)
        self.set_margin_end(6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        # Outer box carries the .card style for the rounded-rect surface.
        # Overflow must be hidden here so children are clipped to the card's
        # border-radius (the rounded corners are on this box, not img_area).
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("card")
        card.set_overflow(Gtk.Overflow.HIDDEN)

        # _FixedBox reports exactly cover_width × cover_height as its natural
        # size so FlowBox layout is unaffected by the child's image dimensions.
        img_area = _FixedBox(cover_width, cover_height)
        card.append(img_area)

        # Cover image — loaded at full resolution; GTK's renderer scales it
        # to the allocated area at display pixel density (HiDPI-correct).
        cover_shown = False
        if resolve_asset and entry.cover:
            cover_path = resolve_asset(entry.cover)
            if os.path.isfile(cover_path):
                pic = Gtk.Picture.new_for_filename(cover_path)
                pic.set_content_fit(Gtk.ContentFit.COVER)
                pic.set_can_shrink(True)
                img_area.set_child(pic)
                cover_shown = True

        # Icon — shown when no cover image is available, centred in img_area.
        if not cover_shown:
            icon_size = min(_ICON_SIZE_MAX, cover_width * 2 // 5)
            icon_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            icon = _load_icon(
                resolve_asset(entry.icon) if resolve_asset and entry.icon else "",
                icon_size,
            )
            icon.set_halign(Gtk.Align.CENTER)
            icon.set_valign(Gtk.Align.CENTER)
            icon.set_vexpand(True)
            icon_box.append(icon)
            img_area.set_child(icon_box)

        # Name label below the image area.
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_margin_start(10)
        inner.set_margin_end(10)
        inner.set_margin_top(8)
        inner.set_margin_bottom(8)
        card.append(inner)

        name_lbl = Gtk.Label(label=entry.name)
        name_lbl.add_css_class("heading")
        name_lbl.set_halign(Gtk.Align.CENTER)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        name_lbl.set_max_width_chars(max(10, cover_width // 10))
        inner.append(name_lbl)

        self.set_child(card)

    def matches(self, category: str | None, search: str) -> bool:
        """Return True if this card should be visible given the current filter."""
        if category is not None and self.entry.category != category:
            return False
        if search:
            needle = search.lower()
            if needle not in self.entry.name.lower() and needle not in self.entry.summary.lower():
                return False
        return True


# ---------------------------------------------------------------------------
# BrowseView
# ---------------------------------------------------------------------------

class BrowseView(Gtk.Box):
    """Main browse page: horizontal category strip + scrolling app grid."""

    __gsignals__ = {
        # Emitted when the user activates an app card.
        "app-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self._cards: list[AppCard] = []
        self._active_category: str | None = None
        self._search_text: str = ""
        self._first_category_button: Gtk.ToggleButton | None = None

        # Stored so cards can be rebuilt when capsule size changes.
        self._entries: list[AppEntry] = []
        self._resolve_asset: Callable[[str], str] | None = None
        self._capsule_width: int = _DEFAULT_CAPSULE_WIDTH

        # ── Category strip ────────────────────────────────────────────────
        self._cat_scroll = Gtk.ScrolledWindow()
        self._cat_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        self._cat_scroll.set_visible(False)

        self._cat_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._cat_box.add_css_class("linked")          # segmented-control look
        self._cat_box.set_margin_start(12)
        self._cat_box.set_margin_end(12)
        self._cat_box.set_margin_top(8)
        self._cat_box.set_margin_bottom(8)
        self._cat_scroll.set_child(self._cat_box)
        self.append(self._cat_scroll)

        self._sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        self._sep.set_visible(False)
        self.append(self._sep)

        # ── Content stack (grid / status page) ───────────────────────────
        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(120)
        self.append(self._stack)

        # Grid page.
        grid_scroll = Gtk.ScrolledWindow()
        grid_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._flow_box = Gtk.FlowBox()
        self._flow_box.set_valign(Gtk.Align.START)
        self._flow_box.set_min_children_per_line(2)
        self._flow_box.set_max_children_per_line(8)
        self._flow_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow_box.set_homogeneous(True)
        self._flow_box.set_margin_start(12)
        self._flow_box.set_margin_end(12)
        self._flow_box.set_margin_top(12)
        self._flow_box.set_margin_bottom(12)
        self._flow_box.connect("child-activated", self._on_card_activated)
        grid_scroll.set_child(self._flow_box)
        self._stack.add_named(grid_scroll, "grid")

        # Status / empty-state page.
        self._status = Adw.StatusPage()
        self._status.set_icon_name("system-software-install-symbolic")
        self._stack.add_named(self._status, "status")

        # Start on the status page with a neutral message.
        self._show_status("No Repository", "Configure a repository to browse apps.")

    # ── Public API ────────────────────────────────────────────────────────

    def load_entries(
        self,
        entries: list[AppEntry],
        resolve_asset: Callable[[str], str] | None = None,
        capsule_width: int | None = None,
    ) -> None:
        """Populate the grid from a list of catalogue entries."""
        self._entries = entries
        self._resolve_asset = resolve_asset
        if capsule_width is not None:
            self._capsule_width = capsule_width
        self._rebuild_cards()

    def set_capsule_width(self, width: int) -> None:
        """Update the capsule size and rebuild cards from the stored entry list."""
        self._capsule_width = width
        if self._entries:
            self._rebuild_cards()

    def _rebuild_cards(self) -> None:
        """Rebuild all cards from the stored entry/resolver/size state."""
        self._clear()

        if not self._entries:
            self._show_status("Empty Catalogue", "The repository contains no apps.")
            return

        # Update the flow box minimum child width to match the capsule.
        self._flow_box.set_min_children_per_line(2)

        # Build category toggle buttons.
        categories = sorted({e.category for e in self._entries})
        all_btn = self._make_category_button("All", None, active=True)
        self._cat_box.append(all_btn)
        self._first_category_button = all_btn

        for cat in categories:
            btn = self._make_category_button(cat, cat)
            btn.set_group(all_btn)
            self._cat_box.append(btn)

        # Add cards sorted alphabetically.
        for entry in sorted(self._entries, key=lambda e: e.name.lower()):
            card = AppCard(
                entry,
                resolve_asset=self._resolve_asset,
                cover_width=self._capsule_width,
            )
            self._cards.append(card)
            self._flow_box.append(card)

        self._cat_scroll.set_visible(True)
        self._sep.set_visible(True)
        self._apply_filter()

    def show_error(self, title: str, description: str) -> None:
        """Display a full-page error / info message."""
        self._entries = []
        self._resolve_asset = None
        self._cat_scroll.set_visible(False)
        self._sep.set_visible(False)
        self._show_status(title, description)

    def set_search_text(self, text: str) -> None:
        self._search_text = text
        self._apply_filter()

    # ── Internals ─────────────────────────────────────────────────────────

    def _make_category_button(
        self, label: str, category: str | None, *, active: bool = False
    ) -> Gtk.ToggleButton:
        btn = Gtk.ToggleButton(label=label, active=active)
        btn.connect("toggled", self._on_category_toggled, category)
        return btn

    def _on_category_toggled(self, button: Gtk.ToggleButton, category: str | None) -> None:
        if button.get_active():
            self._active_category = category
            self._apply_filter()

    def _apply_filter(self) -> None:
        any_visible = False
        for card in self._cards:
            visible = card.matches(self._active_category, self._search_text)
            card.set_visible(visible)
            if visible:
                any_visible = True

        if not any_visible:
            if self._search_text:
                self._show_status(
                    "No Results",
                    f"No apps match \u201c{self._search_text}\u201d.",
                )
            else:
                self._show_status("No Apps Here", "No apps in this category.")
        else:
            self._stack.set_visible_child_name("grid")

    def _show_status(self, title: str, description: str) -> None:
        self._status.set_title(title)
        self._status.set_description(description)
        self._stack.set_visible_child_name("status")

    def _clear(self) -> None:
        while (child := self._flow_box.get_first_child()) is not None:
            self._flow_box.remove(child)
        self._cards.clear()
        while (child := self._cat_box.get_first_child()) is not None:
            self._cat_box.remove(child)
        self._active_category = None
        self._first_category_button = None

    def _on_card_activated(self, _flow_box: Gtk.FlowBox, child: AppCard) -> None:
        log.debug("App selected: %s", child.entry.id)
        self.emit("app-selected", child.entry)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_icon(path: str, size: int) -> Gtk.Image:
    """Return a Gtk.Image for *path*, falling back to a generic icon."""
    if path and os.path.isfile(path):
        try:
            from gi.repository import Gdk
            texture = Gdk.Texture.new_from_filename(path)
            img = Gtk.Image.new_from_paintable(texture)
            img.set_pixel_size(size)
            return img
        except Exception:
            pass
    img = Gtk.Image.new_from_icon_name("application-x-executable")
    img.set_pixel_size(size)
    return img

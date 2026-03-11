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
from cellar.utils.images import load_and_crop, load_and_fit, to_texture
from cellar.views.widgets import set_margins

log = logging.getLogger(__name__)

# Fixed card dimensions (GNOME Software-style horizontal cards).
_CARD_WIDTH   = 300
_CARD_HEIGHT  = 96
_COVER_WIDTH  = 75   # cover thumbnail, flush left, cropped to fill
_ICON_SIZE    = 52   # matches GNOME Software app tiles
_ICON_MARGIN  = 22   # px from left edge — matches vertical centering: (96-52)/2


# ---------------------------------------------------------------------------
# _FixedBox — single-child container with a hard-coded natural size
# ---------------------------------------------------------------------------

class _FixedBox(Gtk.Widget):
    """Single-child container that always reports a fixed natural size.

    ``Gtk.Box`` propagates its children's natural sizes upward, which would
    let the image's pixel dimensions leak into ``FlowBox`` layout.  This
    widget always reports ``(width, height)`` from ``do_measure`` so the
    FlowBox sees the correct capsule dimensions regardless of the child's
    natural size.  The child is always allocated the full area.
    """

    __gtype_name__ = "CellarFixedBox"

    def __init__(self, width: int, height: int, *, clip: bool = True) -> None:
        super().__init__()
        self._w = width
        self._h = height
        self._child: Gtk.Widget | None = None
        if clip:
            self.set_overflow(Gtk.Overflow.HIDDEN)

    def set_child(self, child: Gtk.Widget | None) -> None:
        old = self._child
        if old is not None:
            old.unparent()
        self._child = child
        if child is not None:
            child.set_parent(self)

    # GTK virtual methods ──────────────────────────────────────────────────

    def do_measure(self, orientation, for_size):
        size = self._w if orientation == Gtk.Orientation.HORIZONTAL else self._h
        return size, size, -1, -1

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        child = self.get_first_child()
        if child is not None:
            child.allocate(width, height, baseline, None)

    def do_snapshot(self, snapshot) -> None:
        child = self.get_first_child()
        if child is not None:
            self.snapshot_child(child, snapshot)

    def do_dispose(self) -> None:
        self._child = None
        child = self.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            child.unparent()
            child = nxt
        super().do_dispose()


def _dispose_subtree(widget: Gtk.Widget) -> None:
    """Recursively unparent all descendants of *widget*.

    PyGObject does not reliably call ``do_dispose`` on custom widgets
    when they become orphaned, so GTK may warn about children still
    being present at finalization time.  Walking the subtree explicitly
    ensures every child is unparented before the top-level widget is
    detached.
    """
    child = widget.get_first_child()
    while child is not None:
        nxt = child.get_next_sibling()
        _dispose_subtree(child)
        child.unparent()
        child = nxt


# ---------------------------------------------------------------------------
# AppCard
# ---------------------------------------------------------------------------

class AppCard(Gtk.FlowBoxChild):
    """A single app row in the browse grid.

    Horizontal layout matching GNOME Software's style:
      [75×96 cover or 52×52 icon] [name (bold) / summary (dim)]

    The left column shows a cover thumbnail (cropped to 75×96, flush left)
    when one is available, falling back to the app icon (52 px, 23 px from
    left edge, vertically centred) or a generic icon.
    """

    def __init__(
        self,
        entry: AppEntry,
        *,
        resolve_asset: Callable[[str], str] | None = None,
        is_installed: bool = False,
        repo_uris: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.entry = entry
        self.repo_uris: set[str] = repo_uris or set()
        self.add_css_class("app-card-cell")

        set_margins(self, 6)

        # Outer card — horizontal box with .card styling.
        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        card.add_css_class("card")
        card.add_css_class("activatable")
        card.add_css_class("app-card")
        card.set_overflow(Gtk.Overflow.HIDDEN)

        # ── Left: image column ────────────────────────────────────────────
        # Cover: flush left, cropped to _COVER_WIDTH × _CARD_HEIGHT.
        # Icon:  52×52 with 23 px left margin, vertically centred.
        cover_shown = False
        if resolve_asset and entry.cover:
            cover_path = resolve_asset(entry.cover)
            if os.path.isfile(cover_path):
                png_bytes = load_and_crop(cover_path, _COVER_WIDTH, _CARD_HEIGHT)
                if png_bytes is not None:
                    img_area = _FixedBox(_COVER_WIDTH, _CARD_HEIGHT)
                    pic = Gtk.Picture.new_for_paintable(to_texture(png_bytes))
                    pic.set_content_fit(Gtk.ContentFit.FILL)
                    img_area.set_child(pic)
                    card.append(img_area)
                    cover_shown = True

        if not cover_shown:
            icon_shown = False
            if resolve_asset and entry.icon:
                icon_path = resolve_asset(entry.icon)
                if os.path.isfile(icon_path):
                    png_bytes = load_and_fit(icon_path, _ICON_SIZE)
                    if png_bytes is not None:
                        pic = Gtk.Picture.new_for_paintable(to_texture(png_bytes))
                        pic.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
                        img_area = _FixedBox(_ICON_SIZE, _ICON_SIZE)
                        img_area.set_margin_start(_ICON_MARGIN)
                        img_area.set_valign(Gtk.Align.CENTER)
                        img_area.set_child(pic)
                        card.append(img_area)
                        icon_shown = True
            if not icon_shown:
                icon = Gtk.Image.new_from_icon_name("application-x-executable")
                icon.set_pixel_size(_ICON_SIZE)
                icon.set_halign(Gtk.Align.CENTER)
                icon.set_valign(Gtk.Align.CENTER)
                icon.set_margin_start(_ICON_MARGIN)
                card.append(icon)

        # ── Right: text column ────────────────────────────────────────────
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_hexpand(True)
        text_box.set_margin_start(_ICON_MARGIN)
        text_box.set_margin_end(18)
        card.append(text_box)

        name_lbl = Gtk.Label(label=entry.name)
        name_lbl.add_css_class("heading")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        text_box.append(name_lbl)

        if entry.summary:
            summary_lbl = Gtk.Label(label=entry.summary)
            summary_lbl.add_css_class("dim-label")
            summary_lbl.set_halign(Gtk.Align.START)
            summary_lbl.set_wrap(True)
            summary_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            summary_lbl.set_lines(2)
            summary_lbl.set_ellipsize(Pango.EllipsizeMode.END)
            text_box.append(summary_lbl)

        overlay = Gtk.Overlay()
        overlay.set_child(card)

        if is_installed:
            check = Gtk.Image.new_from_icon_name("check-round-outline2-symbolic")
            check.set_pixel_size(16)
            check.set_halign(Gtk.Align.END)
            check.set_valign(Gtk.Align.START)
            check.set_margin_top(9)
            check.set_margin_end(9)
            check.add_css_class("success")
            overlay.add_overlay(check)

        fixed = _FixedBox(_CARD_WIDTH, _CARD_HEIGHT, clip=False)
        fixed.set_child(overlay)
        self.set_child(fixed)

    def do_dispose(self) -> None:
        # Explicitly tear down the _FixedBox subtree — PyGObject does not
        # reliably call do_dispose on orphaned custom widgets, which causes
        # "Finalizing CellarFixedBox but it still has children" warnings.
        child = self.get_first_child()
        if child is not None:
            _dispose_subtree(child)
        self.set_child(None)
        super().do_dispose()

    def matches(
        self,
        active_categories: set[str],
        search: str,
        active_repos: set[str] | None = None,
    ) -> bool:
        """Return True if this card should be visible given the current filter."""
        if active_repos and not self.repo_uris & active_repos:
            return False
        if active_categories and self.entry.category not in active_categories:
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

    def __init__(
        self,
        *,
        empty_title: str = "Empty Catalogue",
        empty_description: str = "The repository contains no apps.",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self._empty_title = empty_title
        self._empty_description = empty_description
        self._cards: list[AppCard] = []
        self._active_categories: set[str] = set()
        self._active_repos: set[str] = set()
        self._search_text: str = ""

        # Stored so cards can be rebuilt on catalogue reload.
        self._entries: list[AppEntry] = []
        self._resolve_asset: Callable[[str], str] | None = None
        self._installed_ids: set[str] = set()

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
        self._flow_box.set_homogeneous(False)
        self._flow_box.set_halign(Gtk.Align.CENTER)
        set_margins(self._flow_box, 18)
        self._flow_box.connect("child-activated", self._on_card_activated)
        grid_scroll.set_child(self._flow_box)
        self._stack.add_named(grid_scroll, "grid")

        # Status / empty-state page.
        self._status = Adw.StatusPage()
        self._status.set_icon_name("package-x-generic-symbolic")
        self._stack.add_named(self._status, "status")

        # Start on the status page until a catalogue is loaded.
        self._show_status(self._empty_title, self._empty_description)

    # ── Public API ────────────────────────────────────────────────────────

    def load_entries(
        self,
        entries: list[AppEntry],
        resolve_asset: Callable[[str], str] | None = None,
        installed_ids: set[str] | None = None,
        entry_repo_uris: dict[str, set[str]] | None = None,
    ) -> None:
        """Populate the grid from a list of catalogue entries."""
        self._entries = entries
        self._resolve_asset = resolve_asset
        self._installed_ids = installed_ids or set()
        self._entry_repo_uris: dict[str, set[str]] = entry_repo_uris or {}
        self._rebuild_cards()

    def _rebuild_cards(self) -> None:
        """Rebuild all cards from the stored entry/resolver state."""
        self._clear()

        if not self._entries:
            self._show_status(self._empty_title, self._empty_description)
            return

        # Add cards sorted alphabetically.
        for entry in sorted(self._entries, key=lambda e: e.name.lower()):
            card = AppCard(entry, resolve_asset=self._resolve_asset, is_installed=entry.id in self._installed_ids,
                          repo_uris=self._entry_repo_uris.get(entry.id, set()))
            self._cards.append(card)
            self._flow_box.append(card)

        self._apply_filter()

    def show_error(self, title: str, description: str) -> None:
        """Display a full-page error / info message."""
        self._entries = []
        self._resolve_asset = None
        self._show_status(title, description)

    def set_search_text(self, text: str) -> None:
        self._search_text = text
        self._apply_filter()

    def set_active_categories(self, categories: set[str]) -> None:
        self._active_categories = categories
        self._apply_filter()

    def set_active_repos(self, repos: set[str]) -> None:
        self._active_repos = repos
        self._apply_filter()

    # ── Internals ─────────────────────────────────────────────────────────

    def _apply_filter(self) -> None:
        any_visible = False
        for card in self._cards:
            visible = card.matches(self._active_categories, self._search_text, self._active_repos)
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
                self._show_status("No Apps Here", "No apps match the selected filters.")
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
        self._active_categories = set()
        self._active_repos = set()

    def _on_card_activated(self, _flow_box: Gtk.FlowBox, child: AppCard) -> None:
        log.debug("App selected: %s", child.entry.id)
        self.emit("app-selected", child.entry)



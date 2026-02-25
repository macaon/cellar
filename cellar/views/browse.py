"""Browse view — scrolling grid of app cards with category filter and search."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GObject, Gtk, Pango

from cellar.models.app_entry import AppEntry

log = logging.getLogger(__name__)

_ICON_SIZE = 64
_CARD_WIDTH = 160  # minimum card width in pixels


# ---------------------------------------------------------------------------
# AppCard
# ---------------------------------------------------------------------------

class AppCard(Gtk.FlowBoxChild):
    """A single app tile in the browse grid."""

    def __init__(self, entry: AppEntry) -> None:
        super().__init__()
        self.entry = entry

        self.set_margin_start(6)
        self.set_margin_end(6)
        self.set_margin_top(6)
        self.set_margin_bottom(6)

        # Outer box carries the .card style for the rounded-rect surface.
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        card.add_css_class("card")
        card.set_size_request(_CARD_WIDTH, -1)

        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        inner.set_margin_start(14)
        inner.set_margin_end(14)
        inner.set_margin_top(18)
        inner.set_margin_bottom(18)
        card.append(inner)

        # Icon — generic fallback until real icon loading is wired up (phase 7).
        icon = Gtk.Image.new_from_icon_name("application-x-executable")
        icon.set_pixel_size(_ICON_SIZE)
        icon.set_halign(Gtk.Align.CENTER)
        inner.append(icon)

        # App name.
        name_lbl = Gtk.Label(label=entry.name)
        name_lbl.add_css_class("title-4")
        name_lbl.set_halign(Gtk.Align.CENTER)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        name_lbl.set_max_width_chars(18)
        inner.append(name_lbl)

        # One-line summary.
        summary_lbl = Gtk.Label(label=entry.summary)
        summary_lbl.add_css_class("dim-label")
        summary_lbl.add_css_class("caption")
        summary_lbl.set_wrap(True)
        summary_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        summary_lbl.set_lines(2)
        summary_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        summary_lbl.set_halign(Gtk.Align.CENTER)
        summary_lbl.set_justify(Gtk.Justification.CENTER)
        summary_lbl.set_max_width_chars(20)
        inner.append(summary_lbl)

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

    def load_entries(self, entries: list[AppEntry]) -> None:
        """Populate the grid from a list of catalogue entries."""
        self._clear()

        if not entries:
            self._show_status("Empty Catalogue", "The repository contains no apps.")
            return

        # Build category toggle buttons.
        categories = sorted({e.category for e in entries})
        all_btn = self._make_category_button("All", None, active=True)
        self._cat_box.append(all_btn)
        self._first_category_button = all_btn

        for cat in categories:
            btn = self._make_category_button(cat, cat)
            btn.set_group(all_btn)
            self._cat_box.append(btn)

        # Add cards sorted alphabetically.
        for entry in sorted(entries, key=lambda e: e.name.lower()):
            card = AppCard(entry)
            self._cards.append(card)
            self._flow_box.append(card)

        self._cat_scroll.set_visible(True)
        self._sep.set_visible(True)
        self._apply_filter()

    def show_error(self, title: str, description: str) -> None:
        """Display a full-page error / info message."""
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

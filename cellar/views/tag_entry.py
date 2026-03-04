"""Multi-tag chip input widget."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk


class TagEntry(Gtk.Box):
    """Inline chip entry: type a tag name and press Enter to add it as a
    removable pill.  Click the × on any pill to remove that tag.

    Public API
    ----------
    get_tags() -> list[str]
    set_tags(tags: list[str]) -> None
    """

    __gtype_name__ = "CellarTagEntry"

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        self._tags: list[str] = []
        # Maps tag text → the FlowBoxChild wrapper so we can remove it later.
        self._tag_children: dict[str, Gtk.FlowBoxChild] = {}

        self._flow = Gtk.FlowBox()
        self._flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow.set_min_children_per_line(1)
        self._flow.set_max_children_per_line(99)
        self._flow.set_row_spacing(4)
        self._flow.set_column_spacing(6)
        self._flow.set_margin_start(10)
        self._flow.set_margin_end(10)
        self._flow.set_margin_top(8)
        self._flow.set_margin_bottom(8)

        # Frameless entry — always the last child of the flow.
        self._entry = Gtk.Entry()
        self._entry.set_has_frame(False)
        self._entry.set_placeholder_text("Add tag…")
        self._entry.set_size_request(140, -1)
        self._entry.connect("activate", self._on_activate)
        self._flow.append(self._entry)

        self.append(self._flow)

    # ── Public API ────────────────────────────────────────────────────────

    def get_tags(self) -> list[str]:
        return list(self._tags)

    def set_tags(self, tags: list[str]) -> None:
        for child in list(self._tag_children.values()):
            self._flow.remove(child)
        self._tags.clear()
        self._tag_children.clear()
        for tag in tags:
            self._add_tag(tag)

    # ── Internals ─────────────────────────────────────────────────────────

    def _on_activate(self, entry: Gtk.Entry) -> None:
        text = entry.get_text().strip()
        entry.set_text("")
        if not text or text in self._tags:
            return
        self._add_tag(text)

    def _add_tag(self, tag: str) -> None:
        # Insert the pill before the entry (entry is always at the last position).
        position = len(self._tags)
        self._flow.insert(self._make_pill(tag), position)
        child = self._flow.get_child_at_index(position)
        self._tags.append(tag)
        self._tag_children[tag] = child

    def _make_pill(self, tag: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        box.add_css_class("cellar-tag-pill")

        label = Gtk.Label(label=tag)
        box.append(label)

        icon = Gtk.Image.new_from_icon_name("window-close-symbolic")
        icon.set_pixel_size(12)

        btn = Gtk.Button()
        btn.set_child(icon)
        btn.add_css_class("flat")
        btn.add_css_class("circular")
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_focusable(False)
        btn.connect("clicked", lambda _b, t=tag: self._remove_tag(t))
        box.append(btn)

        return box

    def _remove_tag(self, tag: str) -> None:
        if tag not in self._tags:
            return
        child = self._tag_children.pop(tag)
        self._tags.remove(tag)
        self._flow.remove(child)

"""Reusable widget factories for common UI patterns."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk, Pango

# ---------------------------------------------------------------------------
# Card layout constants (GNOME Software-style horizontal cards)
# ---------------------------------------------------------------------------

CARD_WIDTH = 300
CARD_HEIGHT = 96
ICON_SIZE = 52    # matches GNOME Software app tiles
ICON_MARGIN = 22  # px from left edge — matches vertical centering: (96-52)/2

# Capsule card dimensions (portrait cover art, 2:3 ratio like Steam capsules).
CAPSULE_WIDTH = 200
CAPSULE_HEIGHT = 300


# ---------------------------------------------------------------------------
# FixedBox — single-child container with a hard-coded natural size
# ---------------------------------------------------------------------------


class FixedBox(Gtk.Box):
    """Single-child container that always reports a fixed natural size.

    ``Gtk.Box`` propagates its children's natural sizes upward, which would
    let the image's pixel dimensions leak into ``FlowBox`` layout.  This
    subclass overrides ``do_measure`` so the FlowBox sees the correct
    card dimensions regardless of the child's natural size.

    Inheriting from ``Gtk.Box`` (rather than bare ``Gtk.Widget``) ensures
    the native C dispose reliably unparents children even when PyGObject's
    GC does not call a Python ``do_dispose`` override.
    """

    __gtype_name__ = "CellarFixedBox"

    def __init__(self, width: int, height: int, *, clip: bool = True) -> None:
        super().__init__()
        self.set_layout_manager(None)
        self._w = width
        self._h = height
        if clip:
            self.set_overflow(Gtk.Overflow.HIDDEN)

    def set_child(self, child: Gtk.Widget | None) -> None:
        old = self.get_first_child()
        if old is not None:
            self.remove(old)
        if child is not None:
            self.append(child)

    def do_measure(self, orientation, for_size):
        size = self._w if orientation == Gtk.Orientation.HORIZONTAL else self._h
        return size, size, -1, -1

    def do_size_allocate(self, width: int, height: int, baseline: int) -> None:
        child = self.get_first_child()
        if child is not None:
            child.allocate(width, height, baseline, None)


# ---------------------------------------------------------------------------
# BaseCard — shared card shell for app/project/catalogue cards
# ---------------------------------------------------------------------------


class BaseCard(Gtk.FlowBoxChild):
    """Base class for horizontal app cards.

    Builds the common shell: outer card box, icon slot, text column (name +
    subtitle), wrapped in a :class:`FixedBox`.  Subclasses provide content
    via constructor arguments.

    The card body is accessible as ``self._card`` for appending extra widgets
    (e.g. action buttons).  ``self._overlay`` wraps the card for adding
    overlay widgets (e.g. installed checkmark, publish spinner).
    """

    def __init__(
        self,
        *,
        name: str,
        subtitle: str = "",
        icon_widget: Gtk.Widget | None = None,
        activatable: bool = True,
    ) -> None:
        super().__init__()
        self.add_css_class("app-card-cell")
        set_margins(self, 6)

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        card.add_css_class("card")
        if activatable:
            card.add_css_class("activatable")
        card.add_css_class("app-card")
        card.set_overflow(Gtk.Overflow.HIDDEN)
        self._card = card

        # Icon column
        if icon_widget is not None:
            card.append(icon_widget)

        # Text column
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_hexpand(True)
        text_box.set_margin_start(ICON_MARGIN)
        text_box.set_margin_end(18)
        card.append(text_box)
        self._text_box = text_box

        self._name_label = Gtk.Label(label=name)
        self._name_label.add_css_class("heading")
        self._name_label.set_halign(Gtk.Align.START)
        self._name_label.set_ellipsize(Pango.EllipsizeMode.END)
        text_box.append(self._name_label)

        if subtitle:
            self._subtitle_label = Gtk.Label(label=subtitle)
            self._subtitle_label.add_css_class("dim-label")
            self._subtitle_label.set_halign(Gtk.Align.START)
            self._subtitle_label.set_ellipsize(Pango.EllipsizeMode.END)
            text_box.append(self._subtitle_label)
        else:
            self._subtitle_label = None

        overlay = Gtk.Overlay()
        overlay.set_child(card)
        self._overlay = overlay

        fixed = FixedBox(CARD_WIDTH, CARD_HEIGHT, clip=False)
        fixed.set_child(overlay)
        self.set_child(fixed)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_card_icon_from_name(icon_name: str, *, dim: bool = False) -> Gtk.Image:
    """Create a themed icon widget sized for a card."""
    icon = Gtk.Image.new_from_icon_name(icon_name)
    icon.set_pixel_size(ICON_SIZE)
    icon.set_halign(Gtk.Align.CENTER)
    icon.set_valign(Gtk.Align.CENTER)
    icon.set_margin_start(ICON_MARGIN)
    if dim:
        icon.add_css_class("dim-label")
    return icon


def make_gear_button(
    *,
    menu_model: "Gio.MenuModel | None" = None,
    tooltip: str = "Options",
) -> Gtk.MenuButton:
    """Create a flat ``view-more-symbolic`` menu button (three-dot gear icon)."""
    btn = Gtk.MenuButton(icon_name="view-more-symbolic")
    btn.set_tooltip_text(tooltip)
    btn.add_css_class("flat")
    if menu_model is not None:
        btn.set_menu_model(menu_model)
    return btn


def set_margins(widget: Gtk.Widget, size: int) -> None:
    """Apply uniform margins on all four sides."""
    widget.set_margin_top(size)
    widget.set_margin_bottom(size)
    widget.set_margin_start(size)
    widget.set_margin_end(size)


def make_dialog_header(
    dialog: Adw.Dialog,
    *,
    cancel_label: str = "Cancel",
    cancel_cb: Callable | None = None,
    action_label: str | None = None,
    action_cb: Callable | None = None,
    action_sensitive: bool = True,
) -> tuple[Adw.ToolbarView, Adw.HeaderBar, Gtk.Button | None]:
    """Build a standard dialog toolbar with cancel and optional action button.

    Returns ``(toolbar_view, header, action_btn)``.
    """
    toolbar = Adw.ToolbarView()
    header = Adw.HeaderBar()
    header.set_show_end_title_buttons(False)

    cancel_btn = Gtk.Button(label=cancel_label)
    cancel_btn.connect("clicked", cancel_cb or (lambda _: dialog.close()))
    header.pack_start(cancel_btn)

    action_btn = None
    if action_label:
        action_btn = Gtk.Button(label=action_label)
        action_btn.add_css_class("suggested-action")
        action_btn.set_sensitive(action_sensitive)
        if action_cb:
            action_btn.connect("clicked", action_cb)
        header.pack_end(action_btn)

    toolbar.add_top_bar(header)
    return toolbar, header, action_btn


def make_app_grid(
    *,
    on_activated: Callable | None = None,
    min_cols: int = 2,
    max_cols: int = 8,
    margin: int = 18,
) -> Gtk.FlowBox:
    """Build a standard app-card grid (FlowBox).

    Returns a configured ``Gtk.FlowBox`` ready for card widgets.
    """
    flow_box = Gtk.FlowBox()
    flow_box.set_valign(Gtk.Align.START)
    flow_box.set_halign(Gtk.Align.CENTER)
    flow_box.set_homogeneous(False)
    flow_box.set_selection_mode(Gtk.SelectionMode.NONE)
    flow_box.set_min_children_per_line(min_cols)
    flow_box.set_max_children_per_line(max_cols)
    set_margins(flow_box, margin)
    if on_activated:
        flow_box.connect("child-activated", on_activated)
    return flow_box


def make_loading_stack(label: str = "Loading\u2026") -> tuple[Gtk.Stack, Gtk.ListBox]:
    """Build a Stack with a spinner "loading" page and a boxed-list "list" page.

    Returns ``(stack, list_box)``.  The stack starts on the "loading" page.
    Call ``stack.set_visible_child_name("list")`` once content is ready.
    """
    stack = Gtk.Stack()

    spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    spinner_box.set_valign(Gtk.Align.CENTER)
    spinner_box.set_vexpand(True)
    spinner = Adw.Spinner()
    spinner_box.append(spinner)
    lbl = Gtk.Label(label=label)
    lbl.add_css_class("dim-label")
    spinner_box.append(lbl)
    stack.add_named(spinner_box, "loading")

    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
    scroll.set_min_content_height(300)
    list_box = Gtk.ListBox()
    list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
    list_box.add_css_class("boxed-list")
    set_margins(list_box, 12)
    scroll.set_child(list_box)
    stack.add_named(scroll, "list")

    stack.set_visible_child_name("loading")
    return stack, list_box


def make_progress_page(
    label: str,
    on_cancel: Callable,
    *,
    show_text: bool = True,
) -> tuple[Gtk.Box, Gtk.Label, Gtk.ProgressBar, Gtk.Button]:
    """Build a vertical box with phase label, progress bar, and cancel button.

    Returns ``(box, phase_label, progress_bar, cancel_btn)``.
    """
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_valign(Gtk.Align.CENTER)
    box.set_margin_top(12)
    box.set_margin_bottom(12)
    box.set_margin_start(24)
    box.set_margin_end(24)

    phase_label = Gtk.Label(label=label, xalign=0)
    phase_label.add_css_class("dim-label")
    box.append(phase_label)

    progress_bar = Gtk.ProgressBar()
    progress_bar.set_show_text(show_text)
    progress_bar.set_fraction(0.0)
    progress_bar.set_size_request(0, -1)
    box.append(progress_bar)

    cancel_btn = Gtk.Button(label="Cancel")
    cancel_btn.set_halign(Gtk.Align.CENTER)
    cancel_btn.set_margin_top(6)
    cancel_btn.connect("clicked", on_cancel)
    box.append(cancel_btn)

    return box, phase_label, progress_bar, cancel_btn

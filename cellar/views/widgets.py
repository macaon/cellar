"""Reusable widget factories for common UI patterns."""

from __future__ import annotations

from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk


def make_loading_stack(label: str = "Loading\u2026") -> tuple[Gtk.Stack, Gtk.ListBox]:
    """Build a Stack with a spinner "loading" page and a boxed-list "list" page.

    Returns ``(stack, list_box)``.  The stack starts on the "loading" page.
    Call ``stack.set_visible_child_name("list")`` once content is ready.
    """
    stack = Gtk.Stack()

    spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    spinner_box.set_valign(Gtk.Align.CENTER)
    spinner_box.set_vexpand(True)
    spinner = Gtk.Spinner(spinning=True)
    spinner.set_size_request(32, 32)
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
    list_box.set_margin_top(12)
    list_box.set_margin_bottom(12)
    list_box.set_margin_start(12)
    list_box.set_margin_end(12)
    scroll.set_child(list_box)
    stack.add_named(scroll, "list")

    stack.set_visible_child_name("loading")
    return stack, list_box


def make_progress_page(
    label: str,
    on_cancel: Callable[[], None],
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
    cancel_btn.connect("clicked", lambda _: on_cancel())
    box.append(cancel_btn)

    return box, phase_label, progress_bar, cancel_btn

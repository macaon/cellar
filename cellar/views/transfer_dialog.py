"""Transfer dialog — shows active, queued, and recently completed transfers.

Replaces the old ``DownloadQueueDialog`` with a unified view that covers
both the install (download) queue and the publish (upload) queue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

log = logging.getLogger(__name__)


@dataclass
class _ActiveWidgets:
    """References to the updatable widgets inside an active-transfer row."""

    phase_label: Gtk.Label
    progress_bar: Gtk.ProgressBar
    stats_label: Gtk.Label


class TransferDialog(Adw.Dialog):
    """Shows both install and publish queue state in a single dialog."""

    def __init__(self, install_queue, publish_queue) -> None:
        super().__init__(title="Transfers", content_width=420, content_height=380)
        self._install_queue = install_queue
        self._publish_queue = publish_queue
        self._dl_active: _ActiveWidgets | None = None
        self._ul_active: _ActiveWidgets | None = None
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        self._stack = Gtk.Stack()

        # List page — toolbar + scrollable content.
        list_toolbar = Adw.ToolbarView()
        list_toolbar.add_top_bar(Adw.HeaderBar())

        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        self._dl_group = Adw.PreferencesGroup(title="Downloads")
        self._dl_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._dl_list.add_css_class("boxed-list")
        self._dl_group.add(self._dl_list)
        content.append(self._dl_group)

        self._ul_group = Adw.PreferencesGroup(title="Uploads")
        self._ul_group.set_margin_top(18)
        self._ul_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._ul_list.add_css_class("boxed-list")
        self._ul_group.add(self._ul_list)
        content.append(self._ul_group)

        scroll.set_child(content)
        list_toolbar.set_content(scroll)
        self._stack.add_named(list_toolbar, "list")

        # Empty page — toolbar + status page.
        empty_toolbar = Adw.ToolbarView()
        empty_toolbar.add_top_bar(Adw.HeaderBar())
        empty_toolbar.set_content(Adw.StatusPage(
            title="No Transfers",
            description="No active downloads or uploads.",
            icon_name="network-receive-symbolic",
        ))
        self._stack.add_named(empty_toolbar, "empty")

        self.set_child(self._stack)
        self._populate()

    # ── Populate / refresh ────────────────────────────────────────────────

    def _populate(self) -> None:
        """Rebuild lists from current queue state."""
        self._populate_downloads()
        self._populate_uploads()

        has_dl = not self._install_queue.is_empty or bool(self._install_queue.completed_items)
        has_ul = not self._publish_queue.is_empty or bool(self._publish_queue.completed_items)

        self._dl_group.set_visible(has_dl)
        self._ul_group.set_visible(has_ul)
        self._stack.set_visible_child_name("list" if has_dl or has_ul else "empty")

    def _populate_downloads(self) -> None:
        q = self._install_queue
        _clear_listbox(self._dl_list)
        self._dl_active = None

        active = q.active_job
        if active is not None:
            phase = q.active_phase or "Installing\u2026"
            stats = q.active_stats_text
            row, widgets = _make_active_row(
                active.app_name, phase, stats, q.active_fraction,
                on_cancel=lambda aid=active.app_id: self._cancel_install(aid),
            )
            self._dl_active = widgets
            self._dl_list.append(row)

        for job in q.queued_jobs:
            self._dl_list.append(_make_queued_row(
                job.app_name,
                on_cancel=lambda aid=job.app_id: self._cancel_install(aid),
            ))

        for _app_id, app_name in q.completed_items:
            self._dl_list.append(_make_completed_row(app_name))

    def _populate_uploads(self) -> None:
        q = self._publish_queue
        _clear_listbox(self._ul_list)
        self._ul_active = None

        active = q.active_job
        if active is not None:
            phase = q.active_phase or "Publishing\u2026"
            stats = q.active_stats_text
            row, widgets = _make_active_row(
                active.app_name, phase, stats, q.active_fraction,
                on_cancel=lambda aid=active.app_id: self._cancel_publish(aid),
            )
            self._ul_active = widgets
            self._ul_list.append(row)

        for job in q.queued_jobs:
            self._ul_list.append(_make_queued_row(
                job.app_name,
                on_cancel=lambda aid=job.app_id: self._cancel_publish(aid),
            ))

        for _app_id, app_name in q.completed_items:
            self._ul_list.append(_make_completed_row(app_name))

    def update_active_stats(self) -> None:
        """Refresh both active rows from their respective queue states."""
        _update_active(self._dl_active, self._install_queue, "Installing\u2026")
        _update_active(self._ul_active, self._publish_queue, "Publishing\u2026")

    # ── Cancel handlers ───────────────────────────────────────────────────

    def _cancel_install(self, app_id: str) -> None:
        self._install_queue.cancel(app_id)
        self._populate()

    def _cancel_publish(self, app_id: str) -> None:
        self._publish_queue.cancel(app_id)
        self._populate()


# ── Row builders ─────────────────────────────────────────────────────────


def _make_active_row(
    app_name: str,
    phase: str,
    stats: str,
    fraction: float,
    *,
    on_cancel,
) -> tuple[Gtk.ListBoxRow, _ActiveWidgets]:
    """Build a rich active-transfer row with phase, progress bar, and stats."""
    row = Gtk.ListBoxRow(activatable=False, selectable=False)

    outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    outer.set_margin_top(12)
    outer.set_margin_bottom(12)
    outer.set_margin_start(12)
    outer.set_margin_end(12)

    # Top line: spinner + text + cancel button.
    top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)

    spinner = Adw.Spinner()
    spinner.set_valign(Gtk.Align.CENTER)
    top.append(spinner)

    text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
    text_box.set_hexpand(True)
    text_box.set_valign(Gtk.Align.CENTER)

    name_label = Gtk.Label(label=app_name, xalign=0)
    name_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
    name_label.add_css_class("heading")
    text_box.append(name_label)

    phase_label = Gtk.Label(label=phase, xalign=0)
    phase_label.set_ellipsize(3)
    phase_label.add_css_class("dim-label")
    phase_label.add_css_class("caption")
    text_box.append(phase_label)

    top.append(text_box)

    cancel_btn = Gtk.Button(icon_name="process-stop-symbolic")
    cancel_btn.set_valign(Gtk.Align.CENTER)
    cancel_btn.set_tooltip_text("Cancel")
    cancel_btn.add_css_class("flat")
    cancel_btn.add_css_class("circular")
    cancel_btn.connect("clicked", lambda _b: on_cancel())
    top.append(cancel_btn)

    outer.append(top)

    # Full-width progress bar.
    progress_bar = Gtk.ProgressBar(fraction=fraction, show_text=False)
    outer.append(progress_bar)

    # Stats line (e.g. "2.6 MB / 349 MB (1.3 MB/s)").
    stats_label = Gtk.Label(label=stats, xalign=0)
    stats_label.add_css_class("dim-label")
    stats_label.add_css_class("caption")
    stats_label.set_visible(bool(stats))
    outer.append(stats_label)

    row.set_child(outer)
    widgets = _ActiveWidgets(
        phase_label=phase_label,
        progress_bar=progress_bar,
        stats_label=stats_label,
    )
    return row, widgets


def _make_queued_row(app_name: str, *, on_cancel) -> Adw.ActionRow:
    """Build a row for a queued (waiting) transfer."""
    row = Adw.ActionRow(title=app_name, subtitle="Queued")

    icon = Gtk.Image.new_from_icon_name("content-loading-symbolic")
    icon.set_valign(Gtk.Align.CENTER)
    icon.add_css_class("dim-label")
    row.add_prefix(icon)

    cancel_btn = Gtk.Button(icon_name="process-stop-symbolic")
    cancel_btn.set_valign(Gtk.Align.CENTER)
    cancel_btn.set_tooltip_text("Cancel")
    cancel_btn.add_css_class("flat")
    cancel_btn.add_css_class("circular")
    cancel_btn.connect("clicked", lambda _b: on_cancel())
    row.add_suffix(cancel_btn)

    return row


def _make_completed_row(app_name: str) -> Adw.ActionRow:
    """Build a row for a completed transfer."""
    row = Adw.ActionRow(title=app_name, subtitle="Completed")

    icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
    icon.set_valign(Gtk.Align.CENTER)
    icon.add_css_class("success")
    row.add_prefix(icon)

    return row


# ── Helpers ──────────────────────────────────────────────────────────────


def _clear_listbox(lb: Gtk.ListBox) -> None:
    while (child := lb.get_first_child()) is not None:
        lb.remove(child)


def _update_active(
    widgets: _ActiveWidgets | None,
    queue,
    default_phase: str,
) -> None:
    if widgets is None:
        return
    phase = queue.active_phase or default_phase
    widgets.phase_label.set_label(phase)
    widgets.progress_bar.set_fraction(queue.active_fraction)
    stats = queue.active_stats_text
    widgets.stats_label.set_label(stats if stats else "")
    widgets.stats_label.set_visible(bool(stats))

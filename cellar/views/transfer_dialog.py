"""Transfer dialog — shows active, queued, and recently completed transfers.

Replaces the old ``DownloadQueueDialog`` with a unified view that covers
both the install (download) queue and the publish (upload) queue.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

log = logging.getLogger(__name__)


class TransferDialog(Adw.Dialog):
    """Shows both install and publish queue state in a single dialog."""

    def __init__(self, install_queue, publish_queue) -> None:
        super().__init__(title="Transfers", content_width=400, content_height=340)
        self._install_queue = install_queue
        self._publish_queue = publish_queue
        self._dl_active_row: Adw.ActionRow | None = None
        self._dl_active_progress: Gtk.ProgressBar | None = None
        self._ul_active_row: Adw.ActionRow | None = None
        self._ul_active_progress: Gtk.ProgressBar | None = None
        self._build_ui()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(True)
        toolbar_view.add_top_bar(header)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)

        # Downloads section.
        self._dl_group = Adw.PreferencesGroup(title="Downloads")
        self._dl_list = Gtk.ListBox()
        self._dl_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._dl_list.add_css_class("boxed-list")
        self._dl_group.add(self._dl_list)
        content.append(self._dl_group)

        # Uploads section.
        self._ul_group = Adw.PreferencesGroup(title="Uploads")
        self._ul_group.set_margin_top(18)
        self._ul_list = Gtk.ListBox()
        self._ul_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._ul_list.add_css_class("boxed-list")
        self._ul_group.add(self._ul_list)
        content.append(self._ul_group)

        scroll.set_child(content)
        toolbar_view.set_content(scroll)

        # Empty state.
        self._empty_status = Adw.StatusPage(
            title="No Transfers",
            description="No active downloads or uploads.",
            icon_name="folder-download-symbolic",
        )

        self._stack = Gtk.Stack()
        self._stack.add_named(toolbar_view, "list")
        self._stack.add_named(self._empty_status, "empty")
        self.set_child(self._stack)

        self._populate()

    # ── Populate / refresh ────────────────────────────────────────────────

    def _populate(self) -> None:
        """Rebuild lists from current queue state."""
        self._populate_downloads()
        self._populate_uploads()

        has_dl = not self._install_queue.is_empty or bool(self._install_queue.completed_ids)
        has_ul = not self._publish_queue.is_empty or bool(self._publish_queue.completed_ids)

        self._dl_group.set_visible(has_dl)
        self._ul_group.set_visible(has_ul)

        if has_dl or has_ul:
            self._stack.set_visible_child_name("list")
        else:
            self._stack.set_visible_child_name("empty")

    def _populate_downloads(self) -> None:
        q = self._install_queue
        lb = self._dl_list
        _clear_listbox(lb)
        self._dl_active_row = None
        self._dl_active_progress = None

        active = q.active_job
        if active is not None:
            stats = q.active_stats_text
            phase = q.active_phase or "Installing\u2026"
            subtitle = f"{phase}  \u2014  {stats}" if stats else phase
            row = _make_row(
                active.app_name, GLib.markup_escape_text(subtitle),
                spinning=True, app_id=active.app_id,
                on_cancel=lambda aid: self._cancel_install(aid),
            )
            self._dl_active_row = row
            progress = _make_progress_bar(q.active_fraction)
            row.add_suffix(progress)
            self._dl_active_progress = progress
            lb.append(row)

        for job in q.queued_jobs:
            row = _make_row(
                job.app_name, "Queued", app_id=job.app_id,
                on_cancel=lambda aid: self._cancel_install(aid),
            )
            lb.append(row)

        for app_id in q.completed_ids:
            row = _make_row(app_id, "Completed", icon_name="emblem-ok-symbolic")
            lb.append(row)

    def _populate_uploads(self) -> None:
        q = self._publish_queue
        lb = self._ul_list
        _clear_listbox(lb)
        self._ul_active_row = None
        self._ul_active_progress = None

        active = q.active_job
        if active is not None:
            stats = q.active_stats_text
            phase = q.active_phase or "Publishing\u2026"
            subtitle = f"{phase}  \u2014  {stats}" if stats else phase
            row = _make_row(
                active.app_name, GLib.markup_escape_text(subtitle),
                spinning=True, app_id=active.app_id,
                on_cancel=lambda aid: self._cancel_publish(aid),
            )
            self._ul_active_row = row
            progress = _make_progress_bar(q.active_fraction)
            row.add_suffix(progress)
            self._ul_active_progress = progress
            lb.append(row)

        for job in q.queued_jobs:
            row = _make_row(
                job.app_name, "Queued", app_id=job.app_id,
                on_cancel=lambda aid: self._cancel_publish(aid),
            )
            lb.append(row)

        for app_id in q.completed_ids:
            row = _make_row(app_id, "Completed", icon_name="emblem-ok-symbolic")
            lb.append(row)

    def update_active_stats(self) -> None:
        """Refresh both active rows from their respective queue states."""
        _update_row(self._dl_active_row, self._dl_active_progress,
                     self._install_queue, default_phase="Installing\u2026")
        _update_row(self._ul_active_row, self._ul_active_progress,
                     self._publish_queue, default_phase="Publishing\u2026")

    # ── Cancel handlers ───────────────────────────────────────────────────

    def _cancel_install(self, app_id: str) -> None:
        self._install_queue.cancel(app_id)
        self._populate()

    def _cancel_publish(self, app_id: str) -> None:
        self._publish_queue.cancel(app_id)
        self._populate()


# ── Shared helpers ────────────────────────────────────────────────────────


def _clear_listbox(lb: Gtk.ListBox) -> None:
    while (child := lb.get_first_child()) is not None:
        lb.remove(child)


def _make_row(
    title: str,
    subtitle: str,
    *,
    spinning: bool = False,
    icon_name: str = "",
    app_id: str = "",
    on_cancel=None,
) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, subtitle=subtitle)

    if spinning:
        spinner = Adw.Spinner()
        spinner.set_valign(Gtk.Align.CENTER)
        row.add_prefix(spinner)
    elif icon_name:
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_valign(Gtk.Align.CENTER)
        row.add_prefix(icon)

    if app_id and on_cancel:
        cancel_btn = Gtk.Button(icon_name="process-stop-symbolic")
        cancel_btn.set_valign(Gtk.Align.CENTER)
        cancel_btn.set_tooltip_text("Cancel")
        cancel_btn.add_css_class("flat")
        _aid = app_id  # capture for closure
        cancel_btn.connect("clicked", lambda _b: on_cancel(_aid))
        row.add_suffix(cancel_btn)

    return row


def _make_progress_bar(fraction: float) -> Gtk.ProgressBar:
    progress = Gtk.ProgressBar()
    progress.set_fraction(fraction)
    progress.add_css_class("osd")
    progress.set_size_request(-1, 3)
    progress.set_valign(Gtk.Align.CENTER)
    return progress


def _update_row(
    row: Adw.ActionRow | None,
    progress: Gtk.ProgressBar | None,
    queue,
    *,
    default_phase: str,
) -> None:
    if row is None:
        return
    stats = queue.active_stats_text
    phase = queue.active_phase or default_phase
    subtitle = f"{phase}  \u2014  {stats}" if stats else phase
    row.set_subtitle(GLib.markup_escape_text(subtitle))
    if progress is not None:
        progress.set_fraction(queue.active_fraction)

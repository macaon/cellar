"""Download queue dialog — shows active, queued, and recently completed installs."""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

log = logging.getLogger(__name__)


class DownloadQueueDialog(Adw.Dialog):
    """Shows install queue state: active download, queued, completions."""

    def __init__(self, install_queue) -> None:
        super().__init__(title="Downloads", content_width=380, content_height=300)
        self._queue = install_queue
        self._active_row: Adw.ActionRow | None = None
        self._active_progress: Gtk.ProgressBar | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(True)
        toolbar_view.add_top_bar(header)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_top(12)
        self._list_box.set_margin_bottom(12)
        self._list_box.set_margin_start(12)
        self._list_box.set_margin_end(12)

        scroll.set_child(self._list_box)
        toolbar_view.set_content(scroll)

        # Empty state.
        self._empty_status = Adw.StatusPage(
            title="No Downloads",
            description="Install queue is empty.",
            icon_name="folder-download-symbolic",
        )
        self._empty_status.set_visible(False)

        self._stack = Gtk.Stack()
        self._stack.add_named(toolbar_view, "list")
        self._stack.add_named(self._empty_status, "empty")
        self.set_child(self._stack)

        self._populate()

    def _populate(self) -> None:
        """Rebuild the list from the current queue state."""
        # Clear existing rows.
        while (child := self._list_box.get_first_child()) is not None:
            self._list_box.remove(child)
        self._active_row = None
        self._active_progress = None

        has_items = False

        # Active job.
        active = self._queue.active_job
        if active is not None:
            has_items = True
            # Build subtitle from live stats.
            stats = self._queue.active_stats_text
            phase = self._queue.active_phase or "Installing…"
            subtitle = f"{phase}  —  {stats}" if stats else phase

            row = self._make_row(
                active.app_name, GLib.markup_escape_text(subtitle),
                spinning=True, app_id=active.app_id,
            )
            self._active_row = row

            # Add a progress bar below the row content.
            progress = Gtk.ProgressBar()
            progress.set_fraction(self._queue.active_fraction)
            progress.add_css_class("osd")
            progress.set_size_request(-1, 3)
            row.add_suffix(self._make_progress_suffix(progress, active.app_id))
            self._active_progress = progress

            self._list_box.append(row)

        # Queued jobs.
        for job in self._queue.queued_jobs:
            has_items = True
            row = self._make_row(
                job.app_name, "Queued", app_id=job.app_id,
            )
            self._list_box.append(row)

        # Recently completed.
        for app_id in self._queue.completed_ids:
            has_items = True
            row = self._make_row(
                app_id, "Completed", icon_name="emblem-ok-symbolic",
            )
            self._list_box.append(row)

        if has_items:
            self._stack.set_visible_child_name("list")
        else:
            self._stack.set_visible_child_name("empty")

    def update_active_stats(self) -> None:
        """Refresh the active row's subtitle and progress from queue state."""
        if self._active_row is None:
            return
        stats = self._queue.active_stats_text
        phase = self._queue.active_phase or "Installing…"
        subtitle = f"{phase}  —  {stats}" if stats else phase
        # Escape markup entities — phase text may contain '&'.
        self._active_row.set_subtitle(GLib.markup_escape_text(subtitle))
        if self._active_progress is not None:
            self._active_progress.set_fraction(self._queue.active_fraction)

    def _make_row(
        self,
        title: str,
        subtitle: str,
        *,
        spinning: bool = False,
        icon_name: str = "",
        app_id: str = "",
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

        if app_id and not spinning:
            # Cancel button for queued (non-active) jobs.
            cancel_btn = Gtk.Button(icon_name="process-stop-symbolic")
            cancel_btn.set_valign(Gtk.Align.CENTER)
            cancel_btn.set_tooltip_text("Cancel")
            cancel_btn.add_css_class("flat")
            cancel_btn.connect("clicked", self._on_cancel_clicked, app_id)
            row.add_suffix(cancel_btn)

        return row

    def _make_progress_suffix(self, progress: Gtk.ProgressBar, app_id: str) -> Gtk.Box:
        """Build a suffix box with a progress bar and cancel button."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_valign(Gtk.Align.CENTER)

        cancel_btn = Gtk.Button(icon_name="process-stop-symbolic")
        cancel_btn.set_valign(Gtk.Align.CENTER)
        cancel_btn.set_tooltip_text("Cancel")
        cancel_btn.add_css_class("flat")
        cancel_btn.connect("clicked", self._on_cancel_clicked, app_id)
        box.append(cancel_btn)

        return box

    def _on_cancel_clicked(self, _btn, app_id: str) -> None:
        self._queue.cancel(app_id)
        self._populate()

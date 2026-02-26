"""Update confirmation and progress dialog."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.models.app_entry import AppEntry

import logging

log = logging.getLogger(__name__)


def _short_path(path) -> str:
    import os
    return str(path).replace(os.path.expanduser("~"), "~", 1)


class UpdateDialog(Adw.Dialog):
    """Two-phase update dialog: confirmation → progress.

    Phase 1 (confirm): shows current/new version, optional backup chooser,
    and a data-safety warning.  Header has Cancel (start) and Update (end).

    Phase 2 (progress): runs backup (if requested) → download → verify →
    extract → overlay.  Header buttons are hidden; a body Cancel button
    is shown instead.
    """

    def __init__(
        self,
        *,
        entry: AppEntry,
        installed_record: dict,
        bottle_path: Path,
        archive_uri: str,
        on_success: Callable[[], None],
    ) -> None:
        super().__init__(title=f"Update {entry.name}", content_width=440)
        self._entry = entry
        self._installed_record = installed_record
        self._bottle_path = bottle_path
        self._archive_uri = archive_uri
        self._on_success = on_success
        self._backup_path: Path | None = None
        self._cancel_event = threading.Event()

        self._build_ui()
        self.connect("closed", lambda _d: self._cancel_event.set())

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()

        self._header = Adw.HeaderBar()
        self._header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        self._header.pack_start(cancel_btn)

        self._update_header_btn = Gtk.Button(label="Update")
        self._update_header_btn.add_css_class("suggested-action")
        self._update_header_btn.connect("clicked", self._on_proceed_clicked)
        self._header.pack_end(self._update_header_btn)

        toolbar_view.add_top_bar(self._header)

        self._stack = Gtk.Stack()
        self._stack.add_named(self._build_confirm_page(), "confirm")
        self._stack.add_named(self._build_progress_page(), "progress")
        self._stack.set_visible_child_name("confirm")

        toolbar_view.set_content(self._stack)
        self.set_child(toolbar_view)

    def _build_confirm_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        scroll.set_propagate_natural_height(True)

        page = Adw.PreferencesPage()
        scroll.set_child(page)

        # ── Version info ──────────────────────────────────────────────────
        ver_group = Adw.PreferencesGroup(title="Update")
        current = self._installed_record.get("installed_version") or "unknown"
        ver_group.add(Adw.ActionRow(title="Current version", subtitle=current))
        ver_group.add(Adw.ActionRow(title="New version", subtitle=self._entry.version or "unknown"))
        page.add(ver_group)

        # ── Backup ────────────────────────────────────────────────────────
        backup_group = Adw.PreferencesGroup(title="Backup")

        self._backup_row = Adw.ActionRow(
            title="Choose backup location…",
            subtitle="Optional — create a .tar.gz of the current bottle before updating",
            activatable=True,
        )
        self._backup_row.add_suffix(
            Gtk.Image.new_from_icon_name("folder-open-symbolic")
        )
        self._backup_row.connect("activated", self._on_backup_row_activated)
        backup_group.add(self._backup_row)

        page.add(backup_group)

        # ── Warning ───────────────────────────────────────────────────────
        warn_group = Adw.PreferencesGroup()
        warn_row = Adw.ActionRow(
            title="Data safety is not guaranteed",
            subtitle=(
                "Files written by the app to its own directory may be "
                "overwritten if they were included in the original package. "
                "AppData and Documents are always preserved."
            ),
        )
        warn_row.add_prefix(
            Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        )
        warn_group.add(warn_row)
        page.add(warn_group)

        return scroll

    def _build_progress_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(48)
        box.set_margin_bottom(48)
        box.set_margin_start(24)
        box.set_margin_end(24)

        self._phase_label = Gtk.Label(label="Preparing…")
        self._phase_label.add_css_class("dim-label")
        box.append(self._phase_label)

        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(True)
        self._progress_bar.set_fraction(0.0)
        box.append(self._progress_bar)

        self._cancel_body_btn = Gtk.Button(label="Cancel")
        self._cancel_body_btn.set_halign(Gtk.Align.CENTER)
        self._cancel_body_btn.connect("clicked", self._on_cancel_progress_clicked)
        box.append(self._cancel_body_btn)

        return box

    # ── Backup file chooser ───────────────────────────────────────────────

    def _on_backup_row_activated(self, _row) -> None:
        import datetime
        bottle_name = self._installed_record.get("bottle_name", "bottle")
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        suggested = f"{bottle_name}-pre-update-{stamp}.tar.gz"

        chooser = Gtk.FileChooserNative(
            title="Choose Backup Location",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.SAVE,
        )
        chooser.set_current_name(suggested)

        f = Gtk.FileFilter()
        f.set_name("Compressed archive (*.tar.gz)")
        f.add_pattern("*.tar.gz")
        chooser.add_filter(f)

        chooser.connect("response", self._on_backup_chosen, chooser)
        chooser.show()

    def _on_backup_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._backup_path = Path(chooser.get_file().get_path())
            self._backup_row.set_title("Back up to")
            self._backup_row.set_subtitle(_short_path(self._backup_path))
        # If cancelled, the row stays as-is (no backup selected or previous selection kept).

    # ── Proceed / cancel ─────────────────────────────────────────────────

    def _on_proceed_clicked(self, _btn) -> None:
        self._stack.set_visible_child_name("progress")
        self._header.set_visible(False)
        self._start_update()

    def _on_cancel_progress_clicked(self, _btn) -> None:
        self._cancel_event.set()
        self._phase_label.set_text("Cancelling…")
        self._cancel_body_btn.set_sensitive(False)

    # ── Update thread ─────────────────────────────────────────────────────

    def _start_update(self) -> None:
        from cellar.backend.updater import UpdateCancelled, UpdateError, update_app_safe

        def _progress(phase: str, fraction: float) -> None:
            GLib.idle_add(self._phase_label.set_text, phase)
            GLib.idle_add(self._progress_bar.set_fraction, fraction)

        def _run() -> None:
            try:
                update_app_safe(
                    self._entry,
                    self._archive_uri,
                    self._bottle_path,
                    backup_path=self._backup_path,
                    progress_cb=_progress,
                    cancel_event=self._cancel_event,
                )
                GLib.idle_add(self._on_done)
            except UpdateCancelled:
                GLib.idle_add(self._on_cancelled)
            except UpdateError as exc:
                GLib.idle_add(self._on_error, str(exc))
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_error, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _on_done(self) -> None:
        self.close()
        self._on_success()

    def _on_cancelled(self) -> None:
        self.close()

    def _on_error(self, message: str) -> None:
        self._cancel_body_btn.set_sensitive(False)
        alert = Adw.AlertDialog(heading="Update Failed", body=message)
        alert.add_response("ok", "OK")
        alert.connect("response", lambda _d, _r: self.close())
        alert.present(self)

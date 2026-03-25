"""Reusable User Data preferences group.

Provides an ``Adw.PreferencesGroup`` with install-location management
(open / change) and optional backup/import buttons.  The actual logic
for each action lives in the caller (detail view) — this group just
wires up the UI to callbacks.

Usage::

    group = UserDataGroup(
        install_folder="/path/to/prefix",
        on_open_folder=...,
        on_change_location=...,
        on_backup=...,       # optional (Windows/DOS only)
        on_import=...,       # optional (Windows/DOS only)
    )
    page.add(group.widget)
"""

from __future__ import annotations

import logging
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

log = logging.getLogger(__name__)


class UserDataGroup:
    """Manages a User Data ``Adw.PreferencesGroup``."""

    def __init__(
        self,
        install_folder: str,
        *,
        on_open_folder: Callable | None = None,
        on_change_location: Callable | None = None,
        on_backup: Callable | None = None,
        on_import: Callable | None = None,
    ) -> None:
        self._group = Adw.PreferencesGroup(title="User Data")

        # ── Install location row ────────────────────────────────────────
        loc_row = Adw.ActionRow(title="Install Location")
        loc_row.set_subtitle(install_folder or "Unknown")
        loc_row.set_subtitle_lines(1)
        loc_row.set_subtitle_selectable(True)

        if on_open_folder:
            open_btn = Gtk.Button(
                icon_name="folder-open-symbolic",
                valign=Gtk.Align.CENTER,
            )
            open_btn.add_css_class("flat")
            open_btn.set_tooltip_text("Open in file manager")
            open_btn.connect("clicked", lambda _: on_open_folder())
            loc_row.add_suffix(open_btn)

        if on_change_location:
            change_btn = Gtk.Button(
                icon_name="document-send-symbolic",
                valign=Gtk.Align.CENTER,
            )
            change_btn.add_css_class("flat")
            change_btn.set_tooltip_text("Move to a different location\u2026")
            change_btn.connect("clicked", lambda _: on_change_location())
            loc_row.add_suffix(change_btn)

        self._group.add(loc_row)

        # ── Backup / Import row (optional) ──────────────────────────────
        if on_backup or on_import:
            files_row = Adw.ActionRow(title="User Files")
            files_row.set_subtitle(
                "Save games, configs, and other user-modified files"
            )

            if on_import:
                import_btn = Gtk.Button(
                    label="Import\u2026",
                    valign=Gtk.Align.CENTER,
                )
                import_btn.add_css_class("flat")
                import_btn.set_tooltip_text("Restore a user-file backup")
                import_btn.connect("clicked", lambda _: on_import())
                files_row.add_suffix(import_btn)

            if on_backup:
                backup_btn = Gtk.Button(
                    label="Backup\u2026",
                    valign=Gtk.Align.CENTER,
                )
                backup_btn.add_css_class("flat")
                backup_btn.set_tooltip_text("Export user files as archive")
                backup_btn.connect("clicked", lambda _: on_backup())
                files_row.add_suffix(backup_btn)

            self._group.add(files_row)

    @property
    def widget(self) -> Adw.PreferencesGroup:
        return self._group

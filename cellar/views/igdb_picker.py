"""IGDB game search picker dialog.

Presented from AddAppDialog / EditAppDialog when the admin wants to look up
game metadata.  The dialog accepts an initial query string, lets the user
refine it, and calls ``on_picked`` with the selected result dict.

Result dict keys: id, name, year, developer, publisher, summary,
cover_image_id, category.
"""

from __future__ import annotations

import logging
import threading

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

log = logging.getLogger(__name__)

_DEBOUNCE_MS = 400


class IGDBPickerDialog(Adw.Dialog):
    """Modal dialog for searching IGDB and picking a result."""

    def __init__(self, *, query: str = "", on_picked, **kwargs) -> None:
        super().__init__(title="Search IGDB", content_width=420, **kwargs)
        self._on_picked = on_picked
        self._debounce_id: int | None = None
        self._search_gen: int = 0
        self._results: list[dict] = []

        self._build_ui()

        if query:
            self._search_entry.set_text(query)
            self._trigger_search(query)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        close_btn = Gtk.Button(label="Cancel")
        close_btn.connect("clicked", lambda _: self.close())
        header.pack_start(close_btn)
        toolbar_view.add_top_bar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Search entry
        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Game title\u2026")
        self._search_entry.set_margin_top(12)
        self._search_entry.set_margin_bottom(8)
        self._search_entry.set_margin_start(12)
        self._search_entry.set_margin_end(12)
        self._search_entry.connect("search-changed", self._on_search_changed)
        outer.append(self._search_entry)

        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Content stack: spinner / results / empty / error
        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_vexpand(True)

        # — Spinner —
        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        spinner_box.set_valign(Gtk.Align.CENTER)
        spinner = Gtk.Spinner(spinning=True)
        spinner.set_size_request(32, 32)
        spinner.set_halign(Gtk.Align.CENTER)
        spinner.set_margin_top(48)
        spinner.set_margin_bottom(48)
        spinner_box.append(spinner)
        self._stack.add_named(spinner_box, "spinner")

        # — Results —
        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
            propagate_natural_height=True,
        )
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._listbox.add_css_class("boxed-list")
        self._listbox.set_margin_top(6)
        self._listbox.set_margin_bottom(6)
        self._listbox.set_margin_start(12)
        self._listbox.set_margin_end(12)
        self._listbox.connect("row-activated", self._on_row_activated)
        scroll.set_child(self._listbox)
        self._stack.add_named(scroll, "results")

        # — Empty —
        empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        empty_box.set_valign(Gtk.Align.CENTER)
        empty_label = Gtk.Label(label="No results found")
        empty_label.add_css_class("dim-label")
        empty_label.set_halign(Gtk.Align.CENTER)
        empty_label.set_margin_top(48)
        empty_label.set_margin_bottom(48)
        empty_box.append(empty_label)
        self._stack.add_named(empty_box, "empty")

        # — Error —
        error_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        error_box.set_valign(Gtk.Align.CENTER)
        self._error_label = Gtk.Label()
        self._error_label.add_css_class("error")
        self._error_label.set_wrap(True)
        self._error_label.set_halign(Gtk.Align.CENTER)
        self._error_label.set_margin_top(24)
        self._error_label.set_margin_bottom(24)
        self._error_label.set_margin_start(24)
        self._error_label.set_margin_end(24)
        error_box.append(self._error_label)
        self._stack.add_named(error_box, "error")

        # Start with empty state
        self._stack.set_visible_child_name("empty")

        outer.append(self._stack)
        toolbar_view.set_content(outer)
        self.set_child(toolbar_view)

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        query = entry.get_text().strip()
        if self._debounce_id is not None:
            GLib.source_remove(self._debounce_id)
            self._debounce_id = None
        if not query:
            self._stack.set_visible_child_name("empty")
            return
        self._debounce_id = GLib.timeout_add(_DEBOUNCE_MS, self._fire_search, query)

    def _fire_search(self, query: str) -> bool:
        self._debounce_id = None
        self._trigger_search(query)
        return False  # don't repeat

    def _trigger_search(self, query: str) -> None:
        self._search_gen += 1
        gen = self._search_gen
        self._stack.set_visible_child_name("spinner")

        def _run() -> None:
            try:
                from cellar.backend import config as _cfg
                from cellar.backend.igdb import IGDBClient, IGDBError

                creds = _cfg.load_igdb_creds()
                if not creds:
                    GLib.idle_add(self._show_error, "IGDB not configured")
                    return
                client = IGDBClient(creds["client_id"], creds["client_secret"])
                results = client.search_games(query)
                GLib.idle_add(self._on_results, gen, results)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._show_error, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _on_results(self, gen: int, results: list[dict]) -> None:
        if gen != self._search_gen:
            return  # stale response

        self._results = results

        # Clear listbox
        row = self._listbox.get_row_at_index(0)
        while row is not None:
            self._listbox.remove(row)
            row = self._listbox.get_row_at_index(0)

        if not results:
            self._stack.set_visible_child_name("empty")
            return

        for result in results:
            adw_row = Adw.ActionRow(title=GLib.markup_escape_text(result["name"]))
            parts: list[str] = []
            if result.get("developer"):
                parts.append(result["developer"])
            if result.get("year"):
                parts.append(str(result["year"]))
            if parts:
                adw_row.set_subtitle(GLib.markup_escape_text(", ".join(parts)))
            adw_row.set_activatable(True)
            self._listbox.append(adw_row)

        self._stack.set_visible_child_name("results")

    def _show_error(self, message: str) -> None:
        self._error_label.set_text(message)
        self._stack.set_visible_child_name("error")

    # ------------------------------------------------------------------
    # Selection
    # ------------------------------------------------------------------

    def _on_row_activated(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow) -> None:
        idx = row.get_index()
        if 0 <= idx < len(self._results):
            result = self._results[idx]
            self.close()
            self._on_picked(result)

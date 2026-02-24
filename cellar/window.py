"""Main application window."""

from __future__ import annotations

import logging
import os

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

from cellar.utils.paths import ui_file

log = logging.getLogger(__name__)


@Gtk.Template(filename=ui_file("window.ui"))
class CellarWindow(Adw.ApplicationWindow):
    __gtype_name__ = "CellarWindow"

    # Widgets declared in window.ui.
    search_button: Gtk.ToggleButton = Gtk.Template.Child()
    search_bar: Gtk.SearchBar = Gtk.Template.Child()
    search_entry: Gtk.SearchEntry = Gtk.Template.Child()
    refresh_button: Gtk.Button = Gtk.Template.Child()
    menu_button: Gtk.MenuButton = Gtk.Template.Child()
    main_content: Gtk.Box = Gtk.Template.Child()

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)

        # The search bar captures keypresses on the window so typing instantly
        # opens it — set here because key-capture-widget can't reference
        # the template root from within the XML.
        self.search_bar.set_key_capture_widget(self)

        # Keep search toggle and search bar in sync (Escape key, etc.).
        self.search_button.connect("toggled", self._on_search_toggled)
        self.search_bar.connect(
            "notify::search-mode-enabled", self._on_search_mode_changed
        )
        self.search_entry.connect("search-changed", self._on_search_changed)
        self.refresh_button.connect("clicked", self._on_refresh_clicked)

        # Embed the browse view.
        from cellar.views.browse import BrowseView

        self._browse = BrowseView()
        self._browse.set_vexpand(True)
        self.main_content.append(self._browse)

        # Load catalogue after the window is realised so startup feels snappy.
        self.connect("realize", lambda _w: self._load_catalogue())

    # ── Catalogue loading ─────────────────────────────────────────────────

    def _load_catalogue(self) -> None:
        repo_uri = os.environ.get("CELLAR_REPO", "")
        if not repo_uri:
            self._browse.show_error(
                "No Repository Configured",
                "Set the CELLAR_REPO environment variable to a path containing "
                "a catalogue.json, then click Refresh.",
            )
            return
        self._do_load(repo_uri)

    def _do_load(self, repo_uri: str) -> None:
        from cellar.backend.repo import Repo, RepoError

        self.refresh_button.set_sensitive(False)
        try:
            repo = Repo(repo_uri)
            entries = repo.fetch_catalogue()
            self._browse.load_entries(entries)
        except RepoError as exc:
            log.error("Failed to load catalogue from %s: %s", repo_uri, exc)
            self._browse.show_error("Could Not Load Repository", str(exc))
        finally:
            self.refresh_button.set_sensitive(True)

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_search_toggled(self, button: Gtk.ToggleButton) -> None:
        self.search_bar.set_search_mode(button.get_active())

    def _on_search_mode_changed(self, bar: Gtk.SearchBar, _param) -> None:
        # Keep the toggle button state in sync when the bar closes itself
        # (e.g. via Escape key).
        self.search_button.set_active(bar.get_search_mode())

    def _on_search_changed(self, entry: Gtk.SearchEntry) -> None:
        self._browse.set_search_text(entry.get_text())

    def _on_refresh_clicked(self, _button: Gtk.Button) -> None:
        repo_uri = os.environ.get("CELLAR_REPO", "")
        if repo_uri:
            self._do_load(repo_uri)
        else:
            self._load_catalogue()

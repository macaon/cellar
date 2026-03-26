"""Browse view — scrolling grid of app cards with category filter and search."""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, GObject, Gtk, Pango

from cellar.models.app_entry import AppEntry
from cellar.utils import natural_sort_key
from cellar.views.widgets import (
    CAPSULE_HEIGHT,
    CAPSULE_WIDTH,
    CARD_HEIGHT,
    CARD_WIDTH,
    ICON_MARGIN,
    ICON_SIZE,
    BaseCard,
    FixedBox,
    make_app_grid,
    make_card_icon_from_name,
    set_margins,
)

log = logging.getLogger(__name__)

# Backwards-compat aliases for external importers.
_FixedBox = FixedBox
_CARD_WIDTH = CARD_WIDTH
_CARD_HEIGHT = CARD_HEIGHT
_ICON_SIZE = ICON_SIZE
_ICON_MARGIN = ICON_MARGIN
_CAPSULE_WIDTH = CAPSULE_WIDTH
_CAPSULE_HEIGHT = CAPSULE_HEIGHT


# ---------------------------------------------------------------------------
# AppCard
# ---------------------------------------------------------------------------

class AppCard(BaseCard):
    """A single app row in the browse grid.

    Horizontal layout matching GNOME Software's style:
      [52×52 icon] [name (bold) / summary (dim)]
    """

    def __init__(
        self,
        entry: AppEntry,
        *,
        resolve_asset: Callable[[str], str] | None = None,
        asset_path: str | None = None,
        is_installed: bool = False,
        repo_uris: set[str] | None = None,
    ) -> None:
        # Resolve icon
        icon_path = asset_path
        if icon_path is None and resolve_asset and entry.icon:
            icon_path = resolve_asset(entry.icon)

        if icon_path and os.path.isfile(icon_path):
            pic = Gtk.Picture.new_for_filename(icon_path)
            pic.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
            img_area = FixedBox(ICON_SIZE, ICON_SIZE)
            img_area.set_margin_start(ICON_MARGIN)
            img_area.set_valign(Gtk.Align.CENTER)
            img_area.set_child(pic)
            icon_widget = img_area
        else:
            icon_widget = make_card_icon_from_name("application-x-executable")

        # Build summary subtitle with word wrap
        summary = entry.summary or ""

        super().__init__(name=entry.name, subtitle=summary, icon_widget=icon_widget)
        self.entry = entry
        self.repo_uris: set[str] = repo_uris or set()
        self._publish_overlay: Gtk.Box | None = None

        # Summary needs word wrap — replace the default single-line label
        if summary and self._subtitle_label is not None:
            self._subtitle_label.set_wrap(True)
            self._subtitle_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            self._subtitle_label.set_lines(2)

        if is_installed:
            check = Gtk.Image.new_from_icon_name("check-round-outline2-symbolic")
            check.set_pixel_size(16)
            check.set_halign(Gtk.Align.END)
            check.set_valign(Gtk.Align.START)
            check.set_margin_top(9)
            check.set_margin_end(9)
            check.add_css_class("success")
            self._overlay.add_overlay(check)

    def set_publishing(self, active: bool) -> None:
        """Show or hide a spinner overlay indicating a background publish."""
        if active and self._publish_overlay is None:
            _ensure_scrim_css()
            scrim = Gtk.Box()
            scrim.set_halign(Gtk.Align.FILL)
            scrim.set_valign(Gtk.Align.FILL)
            scrim.set_hexpand(True)
            scrim.set_vexpand(True)
            scrim.add_css_class("publish-scrim")
            spinner = Adw.Spinner()
            spinner.set_size_request(32, 32)
            spinner.set_halign(Gtk.Align.CENTER)
            spinner.set_valign(Gtk.Align.CENTER)
            scrim.append(spinner)
            self._overlay.add_overlay(scrim)
            self._publish_overlay = scrim
        elif not active and self._publish_overlay is not None:
            self._overlay.remove_overlay(self._publish_overlay)
            self._publish_overlay = None

    def matches(
        self,
        active_categories: set[str],
        search: str,
        active_repos: set[str] | None = None,
        active_genres: set[str] | None = None,
        active_platforms: set[str] | None = None,
    ) -> bool:
        """Return True if this card should be visible given the current filter."""
        if active_repos and not self.repo_uris & active_repos:
            return False
        if active_categories and self.entry.category not in active_categories:
            return False
        if active_genres and not (set(self.entry.genres) & active_genres):
            return False
        if active_platforms and self.entry.platform not in active_platforms:
            return False
        if search:
            needle = search.lower()
            if needle not in self.entry.name.lower() and needle not in self.entry.summary.lower():
                return False
        return True


# ---------------------------------------------------------------------------
# CapsuleCard — portrait cover art card
# ---------------------------------------------------------------------------

# Module-level CSS for the publish spinner scrim overlay.
_scrim_css_provider: Gtk.CssProvider | None = None


def _ensure_scrim_css() -> None:
    """Register the publish-scrim overlay CSS once."""
    global _scrim_css_provider
    if _scrim_css_provider is not None:
        return
    _scrim_css_provider = Gtk.CssProvider()
    _scrim_css_provider.load_from_string(
        ".publish-scrim {"
        "  background: alpha(@window_bg_color, 0.55);"
        "  border-radius: 12px;"
        "}"
    )
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        _scrim_css_provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


# Module-level CSS for the capsule name overlay gradient.
_capsule_css_provider: Gtk.CssProvider | None = None


def _ensure_capsule_css() -> None:
    """Register the capsule overlay CSS once."""
    global _capsule_css_provider
    if _capsule_css_provider is not None:
        return
    _capsule_css_provider = Gtk.CssProvider()
    _capsule_css_provider.load_from_string(
        ".capsule-name-overlay {"
        "  background: rgba(0, 0, 0, 0.55);"
        "  padding: 6px 10px;"
        "  border-radius: 0 0 12px 12px;"
        "}"
        ".capsule-name-label {"
        "  color: white;"
        "}"
    )
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        _capsule_css_provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )


class CapsuleCard(Gtk.FlowBoxChild):
    """A portrait cover-art card for the capsule display mode.

    Shows a 200×300 cover image with a gradient name overlay at the
    bottom.  Falls back to a centred icon + name when no cover is
    available.  Installed marker in the top-right corner.
    """

    def __init__(
        self,
        entry: AppEntry,
        *,
        resolve_asset: Callable[[str], str] | None = None,
        asset_path: str | None = None,
        is_installed: bool = False,
        repo_uris: set[str] | None = None,
    ) -> None:
        super().__init__()
        self.entry = entry
        self.repo_uris: set[str] = repo_uris or set()
        self.add_css_class("app-card-cell")

        set_margins(self, 6)
        _ensure_capsule_css()

        overlay = Gtk.Overlay()
        overlay.add_css_class("card")
        overlay.set_overflow(Gtk.Overflow.HIDDEN)

        # ── Base layer: cover image or icon fallback ─────────────────
        cover_path = asset_path
        if cover_path is None and resolve_asset and entry.cover:
            cover_path = resolve_asset(entry.cover)
        cover_shown = False
        if cover_path and os.path.isfile(cover_path):
            pic = Gtk.Picture.new_for_filename(cover_path)
            pic.set_content_fit(Gtk.ContentFit.COVER)
            img_box = _FixedBox(_CAPSULE_WIDTH, _CAPSULE_HEIGHT)
            img_box.set_child(pic)
            overlay.set_child(img_box)
            cover_shown = True

        if not cover_shown:
            # Fallback: dimmed platform icon + title on a card background.
            _platform_icons = {
                "windows": "grid-large-symbolic",
                "linux": "penguin-alt-symbolic",
                "dos": "floppy-symbolic",
            }
            fallback = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            fallback.set_valign(Gtk.Align.CENTER)
            fallback.set_halign(Gtk.Align.CENTER)

            icon = Gtk.Image.new_from_icon_name(
                _platform_icons.get(entry.platform, "grid-large-symbolic"),
            )
            icon.set_pixel_size(64)
            icon.set_halign(Gtk.Align.CENTER)
            icon.add_css_class("dim-label")
            fallback.append(icon)

            fb_label = Gtk.Label(label=entry.name)
            fb_label.add_css_class("heading")
            fb_label.set_halign(Gtk.Align.CENTER)
            fb_label.set_ellipsize(Pango.EllipsizeMode.END)
            fb_label.set_max_width_chars(16)
            fb_label.set_lines(2)
            fb_label.set_wrap(True)
            fb_label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            fallback.append(fb_label)

            fb_box = _FixedBox(_CAPSULE_WIDTH, _CAPSULE_HEIGHT, clip=False)
            fb_box.add_css_class("activatable")
            fb_box.set_child(fallback)
            overlay.set_child(fb_box)

        # ── Bottom overlay: name bar on hover ─────────────────────────
        name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        name_box.set_valign(Gtk.Align.END)
        name_box.add_css_class("capsule-name-overlay")
        name_box.set_visible(False)

        name_lbl = Gtk.Label(label=entry.name)
        name_lbl.add_css_class("heading")
        name_lbl.add_css_class("capsule-name-label")
        name_lbl.set_halign(Gtk.Align.CENTER)
        name_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        name_lbl.set_max_width_chars(20)
        name_box.append(name_lbl)
        overlay.add_overlay(name_box)

        hover = Gtk.EventControllerMotion()
        hover.connect("enter", lambda _c, _x, _y, b=name_box: b.set_visible(True))
        hover.connect("leave", lambda _c, b=name_box: b.set_visible(False))
        overlay.add_controller(hover)

        # ── Top-right overlay: installed checkmark ───────────────────
        if is_installed:
            check = Gtk.Image.new_from_icon_name("check-round-outline2-symbolic")
            check.set_pixel_size(16)
            check.set_halign(Gtk.Align.END)
            check.set_valign(Gtk.Align.START)
            check.set_margin_top(9)
            check.set_margin_end(9)
            check.add_css_class("success")
            overlay.add_overlay(check)

        self._overlay = overlay
        self._publish_overlay: Gtk.Box | None = None

        fixed = _FixedBox(_CAPSULE_WIDTH, _CAPSULE_HEIGHT, clip=False)
        fixed.set_child(overlay)
        self.set_child(fixed)

    def set_publishing(self, active: bool) -> None:
        """Show or hide a spinner overlay indicating a background publish."""
        if active and self._publish_overlay is None:
            _ensure_scrim_css()
            scrim = Gtk.Box()
            scrim.set_halign(Gtk.Align.FILL)
            scrim.set_valign(Gtk.Align.FILL)
            scrim.set_hexpand(True)
            scrim.set_vexpand(True)
            scrim.add_css_class("publish-scrim")
            spinner = Adw.Spinner()
            spinner.set_size_request(48, 48)
            spinner.set_halign(Gtk.Align.CENTER)
            spinner.set_valign(Gtk.Align.CENTER)
            scrim.append(spinner)
            self._overlay.add_overlay(scrim)
            self._publish_overlay = scrim
        elif not active and self._publish_overlay is not None:
            self._overlay.remove_overlay(self._publish_overlay)
            self._publish_overlay = None


    def matches(
        self,
        active_categories: set[str],
        search: str,
        active_repos: set[str] | None = None,
        active_genres: set[str] | None = None,
        active_platforms: set[str] | None = None,
    ) -> bool:
        """Return True if this card should be visible given the current filter."""
        if active_repos and not self.repo_uris & active_repos:
            return False
        if active_categories and self.entry.category not in active_categories:
            return False
        if active_genres and not (set(self.entry.genres) & active_genres):
            return False
        if active_platforms and self.entry.platform not in active_platforms:
            return False
        if search:
            needle = search.lower()
            if needle not in self.entry.name.lower() and needle not in self.entry.summary.lower():
                return False
        return True


# ---------------------------------------------------------------------------
# BrowseView
# ---------------------------------------------------------------------------

class BrowseView(Gtk.Box):
    """Main browse page: horizontal category strip + scrolling app grid."""

    __gsignals__ = {
        # Emitted when the user activates an app card.
        "app-selected": (GObject.SignalFlags.RUN_FIRST, None, (object,)),
    }

    def __init__(
        self,
        *,
        empty_title: str = "Empty Catalogue",
        empty_description: str = "The repository contains no apps.",
    ) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)

        self._empty_title = empty_title
        self._empty_description = empty_description
        self._display_mode: str = "card"  # "card" or "capsule"
        self._cards: list[AppCard | CapsuleCard] = []
        self._active_categories: set[str] = set()
        self._active_repos: set[str] = set()
        self._active_genres: set[str] = set()
        self._active_platforms: set[str] = set()
        self._search_text: str = ""
        self._publishing_ids: set[str] = set()

        # Stored so cards can be rebuilt on catalogue reload.
        self._entries: list[AppEntry] = []
        self._resolve_asset: Callable[[str], str] | None = None
        self._installed_ids: set[str] = set()
        # ── Content stack (grid / status page) ───────────────────────────
        self._stack = Gtk.Stack()
        self._stack.set_vexpand(True)
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_transition_duration(120)
        self.append(self._stack)

        # Grid page.
        grid_scroll = Gtk.ScrolledWindow()
        grid_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self._flow_box = make_app_grid(on_activated=self._on_card_activated)
        grid_scroll.set_child(self._flow_box)
        self._stack.add_named(grid_scroll, "grid")

        # Status / empty-state page.
        self._status = Adw.StatusPage()
        self._status.set_icon_name("package-x-generic-symbolic")
        self._stack.add_named(self._status, "status")

        # Loading spinner page.
        loading_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12,
            halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
            vexpand=True, hexpand=True,
        )
        spinner = Adw.Spinner()
        spinner.set_size_request(64, 64)
        loading_box.append(spinner)
        loading_lbl = Gtk.Label(label="Loading\u2026")
        loading_lbl.add_css_class("dim-label")
        loading_box.append(loading_lbl)
        self._stack.add_named(loading_box, "loading")

        # Generation counter for cancelling stale async rebuilds.
        self._rebuild_gen: int = 0

        # Start on the status page until a catalogue is loaded.
        self._show_status(self._empty_title, self._empty_description)

    # ── Public API ────────────────────────────────────────────────────────

    def load_entries(
        self,
        entries: list[AppEntry],
        resolve_asset: Callable[[str], str] | None = None,
        installed_ids: set[str] | None = None,
        entry_repo_uris: dict[str, set[str]] | None = None,
    ) -> None:
        """Populate the grid from a list of catalogue entries."""
        self._entries = entries
        self._resolve_asset = resolve_asset
        self._installed_ids = installed_ids or set()
        self._entry_repo_uris: dict[str, set[str]] = entry_repo_uris or {}
        self._rebuild_cards()

    def set_display_mode(self, mode: str) -> None:
        """Switch between ``"card"`` and ``"capsule"`` display modes."""
        if mode == self._display_mode:
            return
        self._display_mode = mode
        # Adjust FlowBox limits for the different card widths.
        if mode == "capsule":
            self._flow_box.set_min_children_per_line(2)
            self._flow_box.set_max_children_per_line(10)
        else:
            self._flow_box.set_min_children_per_line(2)
            self._flow_box.set_max_children_per_line(8)
        self._rebuild_cards()

    def _rebuild_cards(self) -> None:
        """Rebuild all cards from the stored entry/resolver state.

        Image assets are resolved on a background thread so the UI stays
        responsive.  Once all assets are cached, the cards are created in
        one batch on the main thread.
        """
        self._clear()
        self._rebuild_gen += 1
        gen = self._rebuild_gen

        if not self._entries:
            self._show_status(self._empty_title, self._empty_description)
            return

        card_cls = CapsuleCard if self._display_mode == "capsule" else AppCard
        resolve = self._resolve_asset
        installed_ids = self._installed_ids
        entry_repo_uris = self._entry_repo_uris
        sorted_entries = sorted(self._entries, key=lambda e: natural_sort_key(e.name))

        def _resolve_worker() -> None:
            """Background thread: pre-resolve assets so they're cached."""
            resolved: list[tuple[AppEntry, str | None]] = []
            for entry in sorted_entries:
                if self._rebuild_gen != gen:
                    return  # cancelled by a newer rebuild
                asset_rel = entry.icon if card_cls is AppCard else entry.cover
                path = None
                if resolve and asset_rel:
                    try:
                        path = resolve(asset_rel)
                    except Exception:
                        pass  # card constructor handles missing images
                resolved.append((entry, path))
            GLib.idle_add(_build_all, resolved)

        def _build_all(resolved: list[tuple[AppEntry, str | None]]) -> bool:
            if self._rebuild_gen != gen:
                return False  # stale
            publishing = self._publishing_ids
            for entry, asset_path in resolved:
                card = card_cls(
                    entry,
                    asset_path=asset_path,
                    is_installed=entry.id in installed_ids,
                    repo_uris=entry_repo_uris.get(entry.id, set()),
                )
                if entry.id in publishing:
                    card.set_publishing(True)
                self._cards.append(card)
                self._flow_box.append(card)
            self._apply_filter()
            return False

        thread = threading.Thread(target=_resolve_worker, daemon=True)
        thread.start()

    def show_error(self, title: str, description: str) -> None:
        """Display a full-page error / info message."""
        self._entries = []
        self._resolve_asset = None
        self._show_status(title, description)

    def show_loading(self) -> None:
        """Show a spinner page while the catalogue loads."""
        self._entries = []
        self._resolve_asset = None
        self._rebuild_gen += 1  # cancel any in-flight async rebuild
        self._stack.set_visible_child_name("loading")

    def _restore_status_icon(self) -> None:
        """Ensure the status page icon is set for non-loading states."""
        if not self._status.get_icon_name():
            self._status.set_icon_name("package-x-generic-symbolic")

    def set_search_text(self, text: str) -> None:
        self._search_text = text
        self._apply_filter()

    def set_active_categories(self, categories: set[str]) -> None:
        self._active_categories = categories
        self._apply_filter()

    def set_active_repos(self, repos: set[str]) -> None:
        self._active_repos = repos
        self._apply_filter()

    def set_active_genres(self, genres: set[str]) -> None:
        self._active_genres = genres
        self._apply_filter()

    def set_active_platforms(self, platforms: set[str]) -> None:
        self._active_platforms = platforms
        self._apply_filter()

    def set_publishing_ids(self, ids: set[str]) -> None:
        """Update the set of app IDs currently being published.

        Toggles spinner overlays on matching cards without a full rebuild.
        """
        self._publishing_ids = ids
        for card in self._cards:
            card.set_publishing(card.entry.id in ids)

    # ── Internals ─────────────────────────────────────────────────────────

    def _apply_filter(self) -> None:
        any_visible = False
        for card in self._cards:
            visible = card.matches(
                self._active_categories, self._search_text,
                self._active_repos, self._active_genres,
                self._active_platforms,
            )
            card.set_visible(visible)
            if visible:
                any_visible = True

        if not any_visible:
            if self._search_text:
                self._show_status(
                    "No Results",
                    f"No apps match \u201c{self._search_text}\u201d.",
                )
            else:
                self._show_status("No Apps Here", "No apps match the selected filters.")
        else:
            self._stack.set_visible_child_name("grid")

    def _show_status(self, title: str, description: str) -> None:
        self._restore_status_icon()
        self._status.set_title(title)
        self._status.set_description(description)
        self._stack.set_visible_child_name("status")

    def _clear(self) -> None:
        while (child := self._flow_box.get_first_child()) is not None:
            self._flow_box.remove(child)
        self._cards.clear()
        self._active_categories = set()
        self._active_repos = set()
        self._active_platforms = set()

    def _on_card_activated(self, _flow_box: Gtk.FlowBox, child: AppCard) -> None:
        log.debug("App selected: %s", child.entry.id)
        self.emit("app-selected", child.entry)



"""App detail view — shown when the user activates an app card."""

from __future__ import annotations

import logging
import os
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, Gtk, Pango

from cellar.models.app_entry import AppEntry

log = logging.getLogger(__name__)

_ICON_SIZE = 96


class DetailView(Gtk.Box):
    """Full-page detail view for a single app/game.

    Displayed inside an ``AdwNavigationPage`` pushed onto the window's
    ``AdwNavigationView`` — the back button is provided automatically.

    All data is read from the ``AppEntry``; no archive or network access
    happens at display time.  Asset images are loaded only if they resolve
    to a local file path; otherwise a generic placeholder is shown.

    Note: ``AdwToolbarView`` is a final GType and cannot be subclassed in
    Python, so this widget inherits ``Gtk.Box`` and embeds a toolbar view.
    """

    def __init__(
        self,
        entry: AppEntry,
        *,
        resolve_asset: Callable[[str], str] | None = None,
    ) -> None:
        super().__init__()
        self._entry = entry
        self._resolve = resolve_asset or (lambda rel: rel)
        toolbar = Adw.ToolbarView()
        self.append(toolbar)
        self._build(toolbar)

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------

    def _build(self, toolbar: Adw.ToolbarView) -> None:
        e = self._entry

        # ── Header bar ────────────────────────────────────────────────────
        header = Adw.HeaderBar()
        install_btn = Gtk.Button(label="Install")
        install_btn.add_css_class("suggested-action")
        install_btn.set_sensitive(False)
        install_btn.set_tooltip_text("Installer not yet available")
        header.pack_end(install_btn)
        toolbar.add_top_bar(header)

        # ── Scrollable body ───────────────────────────────────────────────
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar.set_content(scroll)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroll.set_child(outer)

        # Hero banner — full-width, outside the clamp.
        hero_widget = self._make_hero()
        if hero_widget:
            outer.append(hero_widget)

        # Everything else is width-clamped for readability.
        clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
        clamp.set_margin_bottom(32)
        outer.append(clamp)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        clamp.set_child(body)

        body.append(self._make_app_header())

        if e.description:
            body.append(self._make_description())

        screenshots = self._make_screenshots()
        if screenshots:
            body.append(screenshots)

        body.append(self._make_details_group())

        if e.built_with:
            body.append(self._make_components_group())

        body.append(self._make_package_group())

        if e.compatibility_notes:
            body.append(self._make_notes_group())

        if e.changelog:
            body.append(self._make_changelog_group())

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _make_hero(self) -> Gtk.Widget | None:
        if not self._entry.hero:
            return None
        path = self._resolve(self._entry.hero)
        if not os.path.isfile(path):
            return None
        pic = Gtk.Picture.new_for_filename(path)
        pic.set_content_fit(Gtk.ContentFit.COVER)
        pic.set_can_shrink(True)
        pic.set_size_request(-1, 220)
        return pic

    def _make_app_header(self) -> Gtk.Widget:
        e = self._entry
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=18,
            margin_start=18,
            margin_end=18,
            margin_top=18,
            margin_bottom=12,
        )

        icon = self._make_icon(e.icon, _ICON_SIZE)
        icon.set_valign(Gtk.Align.START)
        box.append(icon)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        meta.set_valign(Gtk.Align.CENTER)
        box.append(meta)

        name_lbl = Gtk.Label(label=e.name)
        name_lbl.add_css_class("title-1")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_wrap(True)
        meta.append(name_lbl)

        # "Developer · Publisher · Year" byline.
        byline_parts: list[str] = []
        if e.developer:
            byline_parts.append(e.developer)
        if e.publisher and e.publisher != e.developer:
            byline_parts.append(e.publisher)
        if e.release_year:
            byline_parts.append(str(e.release_year))
        if byline_parts:
            byline = Gtk.Label(label=" · ".join(byline_parts))
            byline.add_css_class("dim-label")
            byline.set_halign(Gtk.Align.START)
            byline.set_wrap(True)
            meta.append(byline)

        # Category + content-rating chips.
        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        chips.set_margin_top(4)
        for text in filter(None, [e.category, e.content_rating]):
            lbl = Gtk.Label(label=text)
            lbl.add_css_class("tag")
            chips.append(lbl)
        if chips.get_first_child():
            meta.append(chips)

        return box

    def _make_description(self) -> Gtk.Widget:
        lbl = Gtk.Label(label=self._entry.description)
        lbl.set_wrap(True)
        lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_xalign(0)
        lbl.set_margin_start(18)
        lbl.set_margin_end(18)
        lbl.set_margin_bottom(12)
        return lbl

    def _make_screenshots(self) -> Gtk.Widget | None:
        local_paths = [
            self._resolve(s)
            for s in self._entry.screenshots
            if os.path.isfile(self._resolve(s))
        ]
        if not local_paths:
            return None

        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        wrapper.set_margin_bottom(12)

        carousel = Adw.Carousel(allow_scroll_wheel=True, reveal_duration=200)
        wrapper.append(carousel)

        for path in local_paths:
            pic = Gtk.Picture.new_for_filename(path)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_can_shrink(True)
            pic.set_size_request(-1, 300)
            carousel.append(pic)

        dots = Adw.CarouselIndicatorDots(carousel=carousel)
        wrapper.append(dots)
        return wrapper

    def _make_details_group(self) -> Gtk.Widget:
        e = self._entry
        group = _group("Details")
        if e.developer:
            group.add(_info_row("Developer", e.developer))
        if e.publisher and e.publisher != e.developer:
            group.add(_info_row("Publisher", e.publisher))
        if e.release_year:
            group.add(_info_row("Released", str(e.release_year)))
        if e.languages:
            group.add(_info_row("Languages", ", ".join(e.languages)))
        if e.content_rating:
            group.add(_info_row("Content rating", e.content_rating))
        if e.tags:
            group.add(_info_row("Tags", ", ".join(e.tags)))
        if e.website:
            group.add(_link_row("Website", e.website))
        for store, url in (e.store_links or {}).items():
            group.add(_link_row(store.capitalize(), url))
        return group

    def _make_components_group(self) -> Gtk.Widget:
        bw = self._entry.built_with
        group = _group("Wine Components")
        group.add(_info_row("Runner", bw.runner))
        if bw.dxvk:
            group.add(_info_row("DXVK", bw.dxvk))
        if bw.vkd3d:
            group.add(_info_row("VKD3D", bw.vkd3d))
        return group

    def _make_package_group(self) -> Gtk.Widget:
        e = self._entry
        group = _group("Package")
        if e.archive_size:
            group.add(_info_row("Download size", _fmt_bytes(e.archive_size)))
        if e.install_size_estimate:
            group.add(_info_row("Install size", _fmt_bytes(e.install_size_estimate)))
        if e.update_strategy:
            label = (
                "Safe — preserves user data"
                if e.update_strategy == "safe"
                else "Full replacement"
            )
            group.add(_info_row("Update strategy", label))
        return group

    def _make_notes_group(self) -> Gtk.Widget:
        group = _group("Compatibility Notes")
        group.add(_text_row(self._entry.compatibility_notes))
        return group

    def _make_changelog_group(self) -> Gtk.Widget:
        group = _group("What's New")
        group.add(_text_row(self._entry.changelog))
        return group

    # ------------------------------------------------------------------
    # Asset helpers
    # ------------------------------------------------------------------

    def _make_icon(self, rel_path: str, size: int) -> Gtk.Image:
        if rel_path:
            path = self._resolve(rel_path)
            if os.path.isfile(path):
                try:
                    from gi.repository import Gdk
                    texture = Gdk.Texture.new_from_filename(path)
                    img = Gtk.Image.new_from_paintable(texture)
                    img.set_pixel_size(size)
                    return img
                except Exception:
                    pass
        img = Gtk.Image.new_from_icon_name("application-x-executable")
        img.set_pixel_size(size)
        return img


# ---------------------------------------------------------------------------
# Widget factories
# ---------------------------------------------------------------------------

def _group(title: str) -> Adw.PreferencesGroup:
    return Adw.PreferencesGroup(
        title=title,
        margin_start=12,
        margin_end=12,
        margin_top=6,
        margin_bottom=6,
    )


def _info_row(title: str, subtitle: str) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, subtitle=subtitle)
    row.set_subtitle_selectable(True)
    return row


def _link_row(title: str, url: str) -> Adw.ActionRow:
    row = Adw.ActionRow(title=title, subtitle=url)
    row.set_activatable(True)
    row.connect("activated", lambda _r: Gio.AppInfo.launch_default_for_uri(url, None))
    icon = Gtk.Image.new_from_icon_name("adw-external-link-symbolic")
    icon.set_valign(Gtk.Align.CENTER)
    row.add_suffix(icon)
    return row


def _text_row(text: str) -> Gtk.Label:
    lbl = Gtk.Label(label=text)
    lbl.set_wrap(True)
    lbl.set_xalign(0)
    lbl.set_margin_start(12)
    lbl.set_margin_end(12)
    lbl.set_margin_top(6)
    lbl.set_margin_bottom(6)
    return lbl


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} PB"

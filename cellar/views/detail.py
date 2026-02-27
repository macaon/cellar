"""App detail view — shown when the user activates an app card."""

from __future__ import annotations

import logging
import os
import threading
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gio, Gtk, Pango

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
        is_writable: bool = False,
        on_edit: Callable | None = None,
        bottles_installs: list | None = None,
        is_installed: bool = False,
        installed_record: dict | None = None,
        on_install_done: Callable | None = None,
        on_remove_done: Callable | None = None,
        on_update_done: Callable | None = None,
    ) -> None:
        super().__init__()
        self._entry = entry
        self._resolve = resolve_asset or (lambda rel: rel)
        self._is_writable = is_writable
        self._on_edit = on_edit
        self._bottles_installs = bottles_installs or []
        self._is_installed = is_installed
        self._installed_record = installed_record
        self._on_install_done = on_install_done
        self._on_remove_done = on_remove_done
        self._on_update_done = on_update_done
        self._has_update = (
            is_installed
            and installed_record is not None
            and installed_record.get("installed_version") != entry.version
            and bool(entry.archive)
        )
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

        # Install / Remove button (rightmost).
        self._install_btn = Gtk.Button()
        self._install_btn.connect("clicked", self._on_install_clicked)
        header.pack_end(self._install_btn)

        # Update button — visible only when an update is available.
        self._update_btn = Gtk.Button(label="Update")
        self._update_btn.add_css_class("suggested-action")
        self._update_btn.connect("clicked", self._on_update_clicked)
        header.pack_end(self._update_btn)

        self._update_install_button()

        if self._is_writable and self._on_edit:
            edit_btn = Gtk.Button(
                icon_name="document-edit-symbolic",
                tooltip_text="Edit catalogue entry",
            )
            edit_btn.connect("clicked", lambda _b: self._on_edit(e))
            header.pack_end(edit_btn)

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

        details = self._make_details_group()
        if details is not None:
            body.append(details)

        if e.built_with:
            body.append(self._make_components_group())

        body.append(self._make_package_group())

        if e.compatibility_notes:
            body.append(self._make_notes_group())

        if e.changelog:
            body.append(self._make_changelog_group())

    # ------------------------------------------------------------------
    # Install helpers
    # ------------------------------------------------------------------

    def _update_install_button(self) -> None:
        btn = self._install_btn
        for cls in ("suggested-action", "success", "destructive-action"):
            btn.remove_css_class(cls)
        if self._is_installed:
            btn.set_label("Remove")
            btn.add_css_class("destructive-action")
            btn.set_sensitive(True)
            btn.set_tooltip_text("")
        elif self._bottles_installs:
            btn.set_label("Install")
            btn.add_css_class("suggested-action")
            btn.set_sensitive(True)
            btn.set_tooltip_text("")
        else:
            btn.set_label("Install")
            btn.add_css_class("suggested-action")
            btn.set_sensitive(False)
            btn.set_tooltip_text("Bottles is not installed")
        self._update_btn.set_visible(self._has_update)

    def _on_install_clicked(self, _btn) -> None:
        if self._is_installed:
            self._on_remove_clicked()
            return
        archive_uri = self._resolve(self._entry.archive) if self._entry.archive else ""
        dialog = InstallProgressDialog(
            entry=self._entry,
            installs=self._bottles_installs,
            archive_uri=archive_uri,
            on_success=self._on_install_success,
        )
        dialog.present(self.get_root())

    def _on_install_success(self, bottle_name: str) -> None:
        self._is_installed = True
        self._update_install_button()
        if self._on_install_done:
            self._on_install_done(bottle_name)

    def _on_remove_clicked(self) -> None:
        bottle_name = (self._installed_record or {}).get("bottle_name", "")
        bottle_path = None
        for install in self._bottles_installs:
            candidate = install.data_path / bottle_name
            if candidate.is_dir():
                bottle_path = candidate
                break
        dialog = RemoveDialog(
            entry=self._entry,
            bottle_path=bottle_path,
            on_confirm=self._on_remove_confirmed,
        )
        dialog.present(self.get_root())

    def _on_update_clicked(self, _btn) -> None:
        from cellar.views.update_app import UpdateDialog

        bottle_name = (self._installed_record or {}).get("bottle_name", "")
        bottle_path = None
        for install in self._bottles_installs:
            candidate = install.data_path / bottle_name
            if candidate.is_dir():
                bottle_path = candidate
                break
        if bottle_path is None:
            log.error("Could not locate bottle directory for %s", self._entry.id)
            return

        archive_uri = self._resolve(self._entry.archive) if self._entry.archive else ""
        dialog = UpdateDialog(
            entry=self._entry,
            installed_record=self._installed_record or {},
            bottle_path=bottle_path,
            archive_uri=archive_uri,
            on_success=self._on_update_success,
        )
        dialog.present(self.get_root())

    def _on_update_success(self) -> None:
        self._has_update = False
        self._update_install_button()
        if self._on_update_done:
            self._on_update_done()

    def _on_remove_confirmed(self) -> None:
        import shutil
        from cellar.backend import database

        bottle_name = (self._installed_record or {}).get("bottle_name", "")
        for install in self._bottles_installs:
            candidate = install.data_path / bottle_name
            if candidate.is_dir():
                try:
                    shutil.rmtree(candidate)
                except Exception as exc:
                    log.error("Failed to remove bottle %s: %s", candidate, exc)
                break

        database.remove_installed(self._entry.id)
        self._is_installed = False
        self._installed_record = None
        self._update_install_button()
        if self._on_remove_done:
            self._on_remove_done()

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

        # Version + category + content-rating chips.
        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        chips.set_margin_top(4)
        if e.version:
            lbl = Gtk.Label(label=f"v{e.version}")
            lbl.add_css_class("tag")
            chips.append(lbl)
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

    def _make_details_group(self) -> Adw.PreferencesGroup | None:
        e = self._entry
        has_any = bool(
            e.developer
            or (e.publisher and e.publisher != e.developer)
            or e.release_year
            or e.languages
            or e.content_rating
            or e.tags
            or e.website
            or e.store_links
        )
        if not has_any:
            return None
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
# Install progress dialog
# ---------------------------------------------------------------------------

_VARIANT_LABELS = {
    "flatpak": "Bottles (Flatpak)",
    "native": "Bottles (Native)",
    "custom": "Bottles (Custom path)",
}


def _variant_label(variant: str) -> str:
    return _VARIANT_LABELS.get(variant, "Bottles")


def _short_path(path) -> str:
    """Return path as a string with the home directory replaced by ~."""
    return str(path).replace(os.path.expanduser("~"), "~", 1)


class InstallProgressDialog(Adw.Dialog):
    """Two-phase install dialog: confirmation → progress.

    Phase 1 (confirm): shows the detected Bottles installation(s).  If more
    than one is found the user can choose which to use.  Header has "Cancel"
    (start) and "Install" (end).

    Phase 2 (progress): background install with a progress bar and a body
    Cancel button.  The header buttons are hidden so the only affordance is
    the in-body Cancel.
    """

    def __init__(
        self,
        *,
        entry: AppEntry,
        installs: list,        # list[BottlesInstall]
        archive_uri: str,
        on_success: Callable[[str], None],
    ) -> None:
        super().__init__(title=f"Install {entry.name}", content_width=420)
        self._entry = entry
        self._installs = installs
        self._archive_uri = archive_uri
        self._on_success = on_success
        self._cancel_event = threading.Event()
        self._selected_install = installs[0] if installs else None

        self._build_ui()
        self.connect("closed", lambda _d: self._cancel_event.set())

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()

        self._header = Adw.HeaderBar()
        self._header.set_show_end_title_buttons(False)

        self._cancel_header_btn = Gtk.Button(label="Cancel")
        self._cancel_header_btn.connect("clicked", lambda _: self.close())
        self._header.pack_start(self._cancel_header_btn)

        self._install_header_btn = Gtk.Button(label="Install")
        self._install_header_btn.add_css_class("suggested-action")
        self._install_header_btn.connect("clicked", self._on_proceed_clicked)
        self._header.pack_end(self._install_header_btn)

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

        if len(self._installs) == 1:
            group = Adw.PreferencesGroup(title="Bottles Installation")
            install = self._installs[0]
            row = Adw.ActionRow(
                title=_variant_label(install.variant),
                subtitle=_short_path(install.data_path),
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("com.usebottles.bottles"))
            group.add(row)
        else:
            group = Adw.PreferencesGroup(
                title="Select Bottles Installation",
                description="Both a Flatpak and a native installation of Bottles were found.",
            )
            radio_group: Gtk.CheckButton | None = None
            for install in self._installs:
                row = Adw.ActionRow(
                    title=_variant_label(install.variant),
                    subtitle=_short_path(install.data_path),
                )
                radio = Gtk.CheckButton()
                radio.set_valign(Gtk.Align.CENTER)
                if radio_group is None:
                    radio_group = radio
                    radio.set_active(True)
                else:
                    radio.set_group(radio_group)
                radio.connect("toggled", self._on_radio_toggled, install)
                row.add_prefix(radio)
                row.set_activatable_widget(radio)
                group.add(row)

        page.add(group)
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

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_radio_toggled(self, btn: Gtk.CheckButton, install) -> None:
        if btn.get_active():
            self._selected_install = install

    def _on_proceed_clicked(self, _btn) -> None:
        self._stack.set_visible_child_name("progress")
        self._cancel_header_btn.set_visible(False)
        self._install_header_btn.set_visible(False)
        self._start_install()

    def _on_cancel_progress_clicked(self, _btn) -> None:
        self._cancel_event.set()
        self._phase_label.set_text("Cancelling…")
        self._cancel_body_btn.set_sensitive(False)

    # ── Install thread ────────────────────────────────────────────────────

    def _start_install(self) -> None:
        from cellar.backend.installer import InstallCancelled, install_app

        def _progress(phase: str, fraction: float) -> None:
            GLib.idle_add(self._phase_label.set_text, phase)
            GLib.idle_add(self._progress_bar.set_fraction, fraction)

        def _run() -> None:
            try:
                bottle_name = install_app(
                    self._entry,
                    self._archive_uri,
                    self._selected_install,
                    progress_cb=_progress,
                    cancel_event=self._cancel_event,
                )
                GLib.idle_add(self._on_done, bottle_name)
            except InstallCancelled:
                GLib.idle_add(self._on_cancelled)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_error, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _on_done(self, bottle_name: str) -> None:
        self.close()
        self._on_success(bottle_name)

    def _on_cancelled(self) -> None:
        self.close()

    def _on_error(self, message: str) -> None:
        self._cancel_body_btn.set_sensitive(False)
        alert = Adw.AlertDialog(heading="Install Failed", body=message)
        alert.add_response("ok", "OK")
        alert.connect("response", lambda _d, _r: self.close())
        alert.present(self)


# ---------------------------------------------------------------------------
# Remove confirmation dialog
# ---------------------------------------------------------------------------


class RemoveDialog(Adw.AlertDialog):
    """Confirmation dialog shown before removing an installed bottle."""

    def __init__(
        self,
        *,
        entry: AppEntry,
        bottle_path,          # pathlib.Path | None
        on_confirm: Callable,
    ) -> None:
        path_str = _short_path(bottle_path) if bottle_path else "unknown location"
        super().__init__(
            heading=f"Remove {entry.name}?",
            body=(
                f"The bottle at {path_str} will be permanently deleted. "
                "Any data stored inside the prefix — saved games, configuration "
                "files, and registry changes — will be lost."
            ),
        )
        self._on_confirm = on_confirm
        self.add_response("cancel", "Cancel")
        self.add_response("remove", "Remove")
        self.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        self.set_default_response("cancel")
        self.set_close_response("cancel")
        self.connect("response", self._on_response)

    def _on_response(self, _dialog, response: str) -> None:
        if response == "remove":
            self._on_confirm()


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

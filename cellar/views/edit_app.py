"""Edit-app dialog — lets the repo maintainer update or delete a catalogue entry.

Flow
----
1. Opened from the detail view's Edit button (writable repos only).
2. All form fields are pre-filled from the existing ``AppEntry``.
3. The user may update any metadata field or swap individual images.
4. On "Save Changes" a background thread calls ``update_in_repo()``.
5. The "Danger Zone" section exposes a "Delete Entry…" button which prompts
   the user to either delete or move the archive before removing the entry
   from the catalogue.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

import logging

from cellar.utils.async_work import run_in_background
from cellar.utils.progress import fmt_stats as _fmt_stats
from cellar.views.builder.media_panel import MediaPanel
from cellar.views.widgets import make_dialog_header, make_progress_page, set_margins

log = logging.getLogger(__name__)

_STRATEGIES = ["safe", "full"]
_STRATEGY_LABELS = ["Safe (preserve user data)", "Full (complete replacement)"]


class EditAppDialog(Adw.Dialog):
    """Dialog for editing or deleting an existing catalogue entry."""

    def __init__(
        self,
        *,
        entry,          # AppEntry
        repo,           # cellar.backend.repo.Repo
        on_done,        # callable() — called after a successful save
    ) -> None:
        super().__init__(title="Edit Catalogue Entry", content_width=1100, content_height=680)

        self._old_entry = entry
        self._repo = repo
        self._on_done = on_done
        self._cancel_event = threading.Event()

        # Screenshot dirty flag — True once grid has been touched
        self._screenshots_dirty: bool = False

        # Check whether the app is installed locally (determines entry-point editability)
        self._locally_installed: bool = self._check_locally_installed(entry)

        # Load category list from repo
        from cellar.backend.packager import BASE_CATEGORIES as _BASE_CATS
        try:
            self._categories = self._repo.fetch_categories()
        except Exception:
            self._categories = list(_BASE_CATS)

        self._build_ui()
        self._prefill()

    @staticmethod
    def _check_locally_installed(entry) -> bool:
        """Return True if the app has a local prefix/install we can browse."""
        try:
            if entry.platform == "linux":
                from cellar.backend.database import get_installed
                rec = get_installed(entry.id)
                return bool(rec and rec.get("install_path"))
            else:
                from cellar.backend.umu import prefixes_dir
                return (prefixes_dir() / entry.id / "drive_c").is_dir()
        except Exception:
            return False

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar_view, _hdr, self._save_btn = make_dialog_header(
            self,
            cancel_cb=self._on_cancel_clicked,
            action_label="Save Changes",
            action_cb=self._on_save_clicked,
            action_sensitive=False,
        )

        self._stack = Gtk.Stack()
        self._stack.add_named(self._build_form(), "form")
        self._stack.add_named(self._build_progress(), "progress")
        self._stack.add_named(self._build_spinner(), "spinner")
        self._stack.set_visible_child_name("form")

        toolbar_view.set_content(self._stack)
        self.set_child(toolbar_view)

    def _build_form(self) -> Gtk.Widget:
        # Single scroll wraps both panes — no nested scrolling
        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )

        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        scroll.set_child(hbox)

        # ── Left column: metadata (fixed width, never grows) ─────────────
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left_box.set_size_request(360, -1)
        left_box.set_hexpand(False)

        # Plain box instead of AdwPreferencesPage — the page has a built-in
        # ScrolledWindow which creates a second scrollbar when nested inside ours.
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        set_margins(page, 12)
        page.set_vexpand(True)
        page.set_hexpand(False)
        left_box.append(page)
        hbox.append(left_box)

        # Identity
        identity_group = Adw.PreferencesGroup()

        self._name_entry = Adw.EntryRow(title="Title *")
        self._name_entry.connect("changed", self._on_name_changed)
        steam_btn = Gtk.Button(icon_name="system-search-symbolic")
        steam_btn.add_css_class("flat")
        steam_btn.set_valign(Gtk.Align.CENTER)
        steam_btn.set_tooltip_text("Look up on Steam")
        steam_btn.connect("clicked", self._on_steam_lookup)
        self._name_entry.add_suffix(steam_btn)
        identity_group.add(self._name_entry)

        self._id_row = Adw.ActionRow(title="App ID", subtitle=self._old_entry.id)
        self._id_row.set_subtitle_selectable(True)
        identity_group.add(self._id_row)

        page.append(identity_group)

        # Details
        details_group = Adw.PreferencesGroup(title="Details")

        self._version_entry = Adw.EntryRow(title="Version")
        details_group.add(self._version_entry)

        self._category_row = Adw.ComboRow(title="Category")
        self._category_row.set_model(Gtk.StringList.new(self._categories))
        details_group.add(self._category_row)

        self._developer_entry = Adw.EntryRow(title="Developer")
        details_group.add(self._developer_entry)

        self._publisher_entry = Adw.EntryRow(title="Publisher")
        details_group.add(self._publisher_entry)

        self._year_entry = Adw.EntryRow(title="Release Year")
        details_group.add(self._year_entry)

        self._steam_appid_entry = Adw.EntryRow(title="Steam App ID")
        self._steam_appid_entry.set_tooltip_text(
            "Used to set GAMEID for protonfixes. Leave empty to use GAMEID=0."
        )
        details_group.add(self._steam_appid_entry)

        self._website_entry = Adw.EntryRow(title="Website")
        details_group.add(self._website_entry)

        self._genres_entry = Adw.EntryRow(title="Genres")
        self._genres_entry.set_tooltip_text("Comma-separated, e.g. Action, RPG")
        details_group.add(self._genres_entry)

        page.append(details_group)

        # Descriptions — Summary + Description editor in one unified card
        desc_group = Adw.PreferencesGroup(title="Descriptions")

        desc_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        desc_outer.add_css_class("card")

        # Summary row inside the card
        self._summary_entry = Adw.EntryRow(title="Summary")
        desc_outer.append(self._summary_entry)

        desc_outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Description header with formatting toolbar
        desc_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        desc_header.set_margin_top(8)
        desc_header.set_margin_bottom(4)
        desc_header.set_margin_start(12)
        desc_header.set_margin_end(6)
        desc_label = Gtk.Label(label="Description")
        desc_label.set_hexpand(True)
        desc_label.set_xalign(0)
        desc_header.append(desc_label)

        fmt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        bold_btn = Gtk.Button(label="B")
        bold_btn.add_css_class("flat")
        bold_btn.set_tooltip_text("Bold (<b>text</b>)")
        bold_btn.connect("clicked", lambda _: self._desc_fmt_wrap("b"))
        italic_btn = Gtk.Button(label="I")
        italic_btn.add_css_class("flat")
        italic_btn.set_tooltip_text("Italic (<i>text</i>)")
        italic_btn.connect("clicked", lambda _: self._desc_fmt_wrap("i"))
        h2_btn = Gtk.Button(label="H2")
        h2_btn.add_css_class("flat")
        h2_btn.set_tooltip_text("Heading (<h2>text</h2>)")
        h2_btn.connect("clicked", lambda _: self._desc_fmt_wrap("h2"))
        bullet_btn = Gtk.Button(icon_name="view-list-bullet-symbolic")
        bullet_btn.add_css_class("flat")
        bullet_btn.set_tooltip_text("Bullet list (<li>item</li>)")
        bullet_btn.connect("clicked", lambda _: self._desc_fmt_bullet())
        hr_btn = Gtk.Button(label="\u2014")
        hr_btn.add_css_class("flat")
        hr_btn.set_tooltip_text("Horizontal rule (<hr>)")
        hr_btn.connect("clicked", lambda _: self._desc_fmt_hr())
        fmt_box.append(bold_btn)
        fmt_box.append(italic_btn)
        fmt_box.append(h2_btn)
        fmt_box.append(bullet_btn)
        fmt_box.append(hr_btn)
        desc_header.append(fmt_box)
        desc_outer.append(desc_header)

        desc_outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self._desc_view = Gtk.TextView()
        self._desc_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._desc_view.set_margin_top(8)
        self._desc_view.set_margin_bottom(8)
        self._desc_view.set_margin_start(12)
        self._desc_view.set_margin_end(12)
        self._desc_view.set_size_request(-1, 100)
        desc_outer.append(self._desc_view)

        desc_group.add(desc_outer)
        page.append(desc_group)

        # Launch Settings
        launch_group = Adw.PreferencesGroup(title="Launch Settings")

        self._strategy_row = Adw.ComboRow(title="Update Strategy")
        strat_model = Gtk.StringList()
        for label in _STRATEGY_LABELS:
            strat_model.append(label)
        self._strategy_row.set_model(strat_model)
        launch_group.add(self._strategy_row)

        # Launch targets — dynamic list, one row per target
        self._launch_targets: list[dict] = []
        self._target_rows: list[Adw.ExpanderRow] = []
        self._targets_group = launch_group  # we'll add target rows here

        add_target_row = Adw.ActionRow(title="Add Launch Target\u2026")
        add_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", self._on_add_target_clicked)
        add_target_row.add_suffix(add_btn)
        add_target_row.set_activatable_widget(add_btn)
        self._add_target_row = add_target_row
        launch_group.add(add_target_row)

        page.append(launch_group)

        # ── Vertical separator ────────────────────────────────────────────
        hbox.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # ── Right column: media (via shared MediaPanel) ───────────────────
        self._media = MediaPanel(on_changed=self._on_screenshots_changed)
        hbox.append(self._media)

        # Wire steam appid changes to media panel
        self._steam_appid_entry.connect("changed", self._on_steam_appid_changed)

        return scroll

    def _build_progress(self) -> Gtk.Widget:
        box, self._progress_label, self._progress_bar, self._cancel_progress_btn = (
            make_progress_page("Saving changes\u2026", self._on_cancel_progress_clicked)
        )
        return box

    def _on_steam_appid_changed(self, _row) -> None:
        steam_txt = self._steam_appid_entry.get_text().strip()
        appid = int(steam_txt) if steam_txt.isdigit() else None
        self._media.set_steam_appid(appid)

    def _build_spinner(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(48)
        box.set_margin_bottom(48)
        box.set_margin_start(24)
        box.set_margin_end(24)

        spinner = Gtk.Spinner(spinning=True)
        spinner.set_size_request(32, 32)
        spinner.set_halign(Gtk.Align.CENTER)

        self._spinner_label = Gtk.Label(label="Deleting entry\u2026")
        self._spinner_label.add_css_class("dim-label")

        self._cancel_spinner_btn = Gtk.Button(label="Cancel")
        self._cancel_spinner_btn.set_halign(Gtk.Align.CENTER)
        self._cancel_spinner_btn.connect("clicked", self._on_cancel_progress_clicked)

        box.append(spinner)
        box.append(self._spinner_label)
        box.append(self._cancel_spinner_btn)
        return box

    # ── Pre-fill ──────────────────────────────────────────────────────────

    def _prefill(self) -> None:
        e = self._old_entry

        self._name_entry.set_text(e.name)
        self._version_entry.set_text(e.version)
        if e.category in self._categories:
            self._category_row.set_selected(self._categories.index(e.category))
        self._developer_entry.set_text(e.developer or "")
        self._publisher_entry.set_text(e.publisher or "")
        if e.release_year:
            self._year_entry.set_text(str(e.release_year))
        self._website_entry.set_text(e.website or "")
        self._genres_entry.set_text(", ".join(e.genres) if e.genres else "")
        self._summary_entry.set_text(e.summary or "")

        if e.description:
            self._desc_view.get_buffer().set_text(e.description)

        if e.steam_appid is not None:
            self._steam_appid_entry.set_text(str(e.steam_appid))

        for t in e.launch_targets:
            self._add_target_row_ui(dict(t))

        # Media panel — set subtitles first (sync), then load thumbnails async
        self._media.set_image_subtitles(e.icon, e.cover, e.logo, bool(e.hide_title))

        # Load image assets — peek cache first (no I/O), resolve missing ones in background.
        import os as _os
        peek = self._repo.peek_asset_cache

        def _peek_or_none(rel: str) -> str | None:
            p = peek(rel)
            return p if (p and _os.path.isfile(p)) else None

        # Single images: apply cached thumbnails synchronously
        for key, rel in [("icon", e.icon), ("cover", e.cover), ("logo", e.logo)]:
            if rel:
                cached = _peek_or_none(rel)
                if cached:
                    self._media.set_thumbnail(key, cached)

        # Screenshots: build list from cache; collect missing rels for background fetch
        _ss_rels = list(e.screenshots)
        _ss_cached: list[str] = []
        _ss_missing: list[tuple[int, str]] = []   # (original index, rel)
        for i, rel in enumerate(_ss_rels):
            cached = _peek_or_none(rel)
            if cached:
                _ss_cached.append(cached)
            else:
                _ss_missing.append((i, rel))

        if _ss_cached:
            _ss_source_urls = [e.screenshot_sources.get(r) for r in _ss_rels if _peek_or_none(r)]
            self._media.set_screenshots_local(_ss_cached, _ss_source_urls)

        # Resolve anything not already cached (and single images that weren't cached)
        _uncached_single = {
            k: rel for k, rel in [("icon", e.icon), ("cover", e.cover), ("logo", e.logo)]
            if rel and not _peek_or_none(rel)
        }

        if _uncached_single or _ss_missing:
            def _resolve_missing():
                singles = {}
                for key, rel in _uncached_single.items():
                    try:
                        singles[key] = self._repo.resolve_asset_uri(rel)
                    except Exception:
                        pass
                extra_ss: list[tuple[int, str]] = []
                for idx, rel in _ss_missing:
                    try:
                        p = self._repo.resolve_asset_uri(rel)
                        if p:
                            extra_ss.append((idx, p))
                    except Exception:
                        pass
                return singles, extra_ss

            def _on_missing_resolved(res):
                singles, extra_ss = res
                for key, path in singles.items():
                    self._media.set_thumbnail(key, path)
                if extra_ss:
                    merged = list(_ss_cached)
                    for _idx, path in extra_ss:
                        merged.append(path)
                    merged_rels = _ss_rels
                    merged_sources = [e.screenshot_sources.get(r) for r in merged_rels]
                    self._media.set_screenshots_local(merged, merged_sources)

            run_in_background(_resolve_missing, on_done=_on_missing_resolved)

        # Steam screenshot suggestions — fetch if steam_appid is set
        # (handled by set_steam_appid on the media panel via _on_steam_appid_changed)

        strategy = e.update_strategy or "safe"
        if strategy in _STRATEGIES:
            self._strategy_row.set_selected(_STRATEGIES.index(strategy))

        self._update_save_button()

    # ── Form validation ───────────────────────────────────────────────────

    def _get_category(self) -> str:
        idx = self._category_row.get_selected()
        return self._categories[idx] if 0 <= idx < len(self._categories) else ""

    def _on_name_changed(self, _entry) -> None:
        self._update_save_button()

    def _update_save_button(self) -> None:
        self._save_btn.set_sensitive(bool(self._name_entry.get_text().strip()))

    # ── Description formatting helpers ────────────────────────────────────

    def _desc_fmt_wrap(self, tag: str) -> None:
        buf = self._desc_view.get_buffer()
        buf.begin_user_action()
        if buf.get_has_selection():
            start, end = buf.get_selection_bounds()
            text = buf.get_text(start, end, False)
            buf.delete(start, end)
            buf.insert(buf.get_iter_at_mark(buf.get_insert()), f"<{tag}>{text}</{tag}>")
        else:
            buf.insert_at_cursor(f"<{tag}></{tag}>")
        buf.end_user_action()

    def _desc_fmt_bullet(self) -> None:
        buf = self._desc_view.get_buffer()
        buf.begin_user_action()
        if buf.get_has_selection():
            start, end = buf.get_selection_bounds()
            text = buf.get_text(start, end, False)
            buf.delete(start, end)
            buf.insert(buf.get_iter_at_mark(buf.get_insert()), f"<li>{text}</li>")
        else:
            buf.insert_at_cursor("<li></li>")
        buf.end_user_action()

    def _desc_fmt_hr(self) -> None:
        buf = self._desc_view.get_buffer()
        it = buf.get_iter_at_mark(buf.get_insert())
        it.set_line_offset(0)
        buf.begin_user_action()
        buf.insert(it, "<hr>\n")
        buf.end_user_action()

    # ── Screenshots changed ───────────────────────────────────────────────

    def _on_screenshots_changed(self) -> None:
        self._screenshots_dirty = True

    # ── Entry point browser ───────────────────────────────────────────────

    def _on_browse_target(self, _btn, idx: int) -> None:
        import os
        from cellar.backend.umu import prefixes_dir
        e = self._old_entry
        if e.platform == "linux":
            from cellar.backend.database import get_installed
            rec = get_installed(e.id)
            install_path = Path(rec["install_path"]) / e.id if rec and rec.get("install_path") else Path.home()
            browse_root = install_path
            title = "Select Executable"
        else:
            prefix = prefixes_dir() / e.id / "drive_c"
            browse_root = prefix if prefix.is_dir() else Path.home()
            title = "Select Executable"

        chooser = Gtk.FileChooserNative(
            title=title,
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Select",
        )
        from gi.repository import Gio
        chooser.set_current_folder(Gio.File.new_for_path(str(browse_root)))
        if e.platform != "linux":
            exe_filter = Gtk.FileFilter()
            exe_filter.set_name("Windows executables")
            for ext in ("exe", "msi", "bat", "cmd", "com", "lnk"):
                exe_filter.add_pattern(f"*.{ext}")
                exe_filter.add_pattern(f"*.{ext.upper()}")
            chooser.add_filter(exe_filter)
            all_filter = Gtk.FileFilter()
            all_filter.set_name("All files")
            all_filter.add_pattern("*")
            chooser.add_filter(all_filter)
        chooser.connect("response", self._on_target_path_chosen, chooser,
                        browse_root, e.platform, idx)
        chooser.show()
        self._ep_chooser = chooser  # keep reference alive

    def _on_target_path_chosen(self, _c, response, chooser, browse_root: Path,
                               platform: str, idx: int) -> None:
        import os
        if response != Gtk.ResponseType.ACCEPT:
            return
        abs_path = chooser.get_file().get_path()
        if platform == "linux":
            try:
                formatted = os.path.relpath(abs_path, str(browse_root))
            except ValueError:
                formatted = abs_path
        else:
            from cellar.utils.paths import to_win32_path
            formatted = to_win32_path(abs_path, str(browse_root))
        if idx < len(self._launch_targets):
            self._launch_targets[idx]["path"] = formatted
            self._target_rows[idx].set_subtitle(
                GLib.markup_escape_text(formatted)
            )

    # ── Steam lookup ──────────────────────────────────────────────────────

    def _on_steam_lookup(self, _btn) -> None:
        from cellar.views.steam_picker import SteamPickerDialog

        query = self._name_entry.get_text().strip()
        picker = SteamPickerDialog(query=query, on_picked=self._apply_steam_result)
        picker.present(self)

    def _apply_steam_result(self, result: dict) -> None:
        """Overwrite form fields from a Steam picker result."""
        if result.get("name"):
            self._name_entry.set_text(result["name"])
        if result.get("developer"):
            self._developer_entry.set_text(result["developer"])
        if result.get("publisher"):
            self._publisher_entry.set_text(result["publisher"])
        if result.get("year"):
            self._year_entry.set_text(str(result["year"]))
        if result.get("website"):
            self._website_entry.set_text(result["website"])
        if result.get("genres"):
            genres = result["genres"]
            if isinstance(genres, list):
                self._genres_entry.set_text(", ".join(genres))
            else:
                self._genres_entry.set_text(str(genres))
        if result.get("summary"):
            self._summary_entry.set_text(result["summary"])
            # Use the short summary for description too — the long Steam
            # description is too verbose for catalogue display.
            buf = self._desc_view.get_buffer()
            buf.set_text(result["summary"])
        if result.get("steam_appid"):
            self._steam_appid_entry.set_text(str(result["steam_appid"]))
        if result.get("category") and result["category"] in self._categories:
            self._category_row.set_selected(
                self._categories.index(result["category"])
            )
        if result.get("screenshots"):
            self._media.replace_steam_screenshots(result["screenshots"])

    # ── Save flow ─────────────────────────────────────────────────────────

    def _on_cancel_clicked(self, _btn) -> None:
        self.close()

    def _on_save_clicked(self, _btn) -> None:
        e = self._old_entry
        app_id = e.id
        name = self._name_entry.get_text().strip()
        version = self._version_entry.get_text().strip() or e.version
        category = self._get_category() or e.category
        summary = self._summary_entry.get_text().strip()
        desc_buf = self._desc_view.get_buffer()
        description = desc_buf.get_text(
            desc_buf.get_start_iter(), desc_buf.get_end_iter(), False
        ).strip()
        developer = self._developer_entry.get_text().strip()
        publisher = self._publisher_entry.get_text().strip()
        year_text = self._year_entry.get_text().strip()
        release_year = int(year_text) if year_text.isdigit() else None
        steam_appid_text = self._steam_appid_entry.get_text().strip()
        steam_appid = int(steam_appid_text) if steam_appid_text.isdigit() else None
        website = self._website_entry.get_text().strip()
        genres_text = self._genres_entry.get_text().strip()
        genres = tuple(g.strip() for g in genres_text.split(",") if g.strip()) if genres_text else ()
        strategy = _STRATEGIES[self._strategy_row.get_selected()]

        # Single images via media panel: None=keep existing, ""=clear, str=new file
        icon_path = self._media.get_icon_path() if self._media.icon_changed else None
        cover_path = self._media.get_cover_path() if self._media.cover_changed else None
        logo_path = self._media.get_logo_path() if self._media.logo_changed else None

        if icon_path is None:
            icon_rel = e.icon
        elif icon_path == "":
            icon_rel = ""
        else:
            ext = ".png" if Path(icon_path).suffix.lower() in (".ico", ".bmp") else Path(icon_path).suffix
            icon_rel = f"apps/{app_id}/icon{ext}"

        if cover_path is None:
            cover_rel = e.cover
        elif cover_path == "":
            cover_rel = ""
        else:
            cover_rel = f"apps/{app_id}/cover{Path(cover_path).suffix}"

        if logo_path is None:
            logo_rel = e.logo
        elif logo_path == "":
            logo_rel = ""
        else:
            logo_rel = f"apps/{app_id}/logo.png"

        # Screenshots resolved in the background thread (may need Steam downloads).
        screenshot_rels = e.screenshots
        _grid_items = self._media.screenshot_grid.get_items() if self._screenshots_dirty else None
        _excluded_locals = (
            self._media.screenshot_grid.get_excluded_local_items()
            if self._screenshots_dirty else []
        )

        from dataclasses import replace as _dc_replace

        new_entry = _dc_replace(
            e,
            name=name,
            version=version,
            category=category,
            summary=summary,
            description=description,
            developer=developer,
            publisher=publisher,
            release_year=release_year,
            icon=icon_rel,
            cover=cover_rel,
            logo=logo_rel,
            hide_title=self._media.get_hide_title(),
            screenshots=screenshot_rels,
            website=website,
            genres=genres,
            update_strategy=strategy,
            launch_targets=tuple(self._launch_targets),
            steam_appid=steam_appid,
        )

        images = {
            "icon": icon_path,      # None / "" / path
            "cover": cover_path,
            "logo": logo_path,
            "screenshots": None,          # filled in thread when dirty
        }

        self._cancel_event.clear()
        self._saved_entry = new_entry  # may be replaced in thread; read by _on_save_done
        self._stack.set_visible_child_name("progress")
        self._progress_bar.set_fraction(0.0)
        self._progress_label.set_text("Saving changes\u2026")

        repo_root = self._repo.writable_path()

        def _run():
            import tempfile as _tmp

            from cellar.backend.packager import (
                CancelledError,
                update_in_repo,
            )

            def _phase(label: str) -> None:
                GLib.idle_add(self._progress_label.set_text, label)
                GLib.idle_add(self._progress_bar.set_text, "")

            _last_stats_t = [0.0]

            def _stats(copied: int, total: int, speed: float) -> None:
                now = time.monotonic()
                if now - _last_stats_t[0] >= 0.1:
                    _last_stats_t[0] = now
                    GLib.idle_add(self._progress_bar.set_text, _fmt_stats(copied, total, speed))

            def _progress(fraction: float) -> None:
                GLib.idle_add(self._progress_bar.set_fraction, fraction)

            _run_entry = new_entry  # local alias; replaced below when screenshots are dirty
            _final_sources: list[str | None] = []

            if _grid_items is not None:
                log.debug("edit save: %d grid item(s) to process", len(_grid_items))
                _phase("Downloading screenshots\u2026")
                dl_dir = Path(_tmp.mkdtemp(prefix="cellar_ss_"))
                final_paths: list[str] = []
                final_sources: list[str | None] = []
                from cellar.utils.http import make_session as _make_session
                _session = _make_session()
                for _item in _grid_items:
                    if _item.local_path:
                        log.debug("  item local_path=%r", _item.local_path)
                        final_paths.append(_item.local_path)
                        final_sources.append(_item.source_url)
                    elif _item.full_url:
                        _fname = _item.full_url.split("/")[-1].split("?")[0] or "screenshot.jpg"
                        _dest = dl_dir / _fname
                        log.debug("  item full_url=%r -> downloading to %s", _item.full_url, _dest)
                        try:
                            _r = _session.get(_item.full_url, timeout=30)
                            _r.raise_for_status()
                            _dest.write_bytes(_r.content)
                            log.debug("    downloaded OK (%d bytes)", len(_r.content))
                            final_paths.append(str(_dest))
                            final_sources.append(_item.full_url)
                        except Exception as _exc:  # noqa: BLE001
                            log.warning("Screenshot download failed: %s", _exc)
                log.debug("edit save: final_paths=%s", final_paths)
                images["screenshots"] = final_paths
                # Placeholder screenshot rels — packager will replace with
                # content-hashed names and return the final entry.
                ss_rels = tuple(
                    f"apps/{app_id}/screenshots/ss_placeholder_{i:03d}{Path(p).suffix}"
                    for i, p in enumerate(final_paths)
                )
                # Build screenshot_sources keyed by placeholder paths so the
                # packager can remap them to the final hashed paths.
                ss_sources = {
                    rel: src
                    for rel, src in zip(ss_rels, final_sources)
                    if src
                }
                log.debug("edit save: ss_rels (placeholder)=%s", ss_rels)
                _run_entry = _dc_replace(new_entry, screenshots=ss_rels,
                                         screenshot_sources=ss_sources)
                _final_sources = final_sources
                self._saved_entry = _run_entry
            else:
                log.debug("edit save: screenshots not dirty, keeping existing rels=%s", e.screenshots)

            try:
                _final_entry = update_in_repo(
                    repo_root,
                    e,
                    _run_entry,
                    images,
                    progress_cb=_progress,
                    phase_cb=_phase,
                    stats_cb=_stats,
                    cancel_event=self._cancel_event,
                )
                if _final_entry is not None:
                    self._saved_entry = _final_entry
                log.debug("edit save: update_in_repo done")
                _saved = self._saved_entry
                if _excluded_locals:
                    new_rels_set = set(_saved.screenshots)
                    for _excl in _excluded_locals:
                        if not _excl.local_path:
                            continue
                        for old_rel in e.screenshots:
                            if str(repo_root / old_rel) == _excl.local_path:
                                if old_rel not in new_rels_set:
                                    try:
                                        (repo_root / old_rel).unlink(missing_ok=True)
                                        log.debug("edit save: deleted excluded screenshot %r", old_rel)
                                    except Exception:
                                        pass
                                break
                if _grid_items is not None:
                    # Evict old screenshot cache entries (old paths no longer valid)
                    for _rel in e.screenshots:
                        self._repo.evict_asset_cache(_rel)
                log.debug("edit save: saved_entry.screenshots=%s", self._saved_entry.screenshots)
                GLib.idle_add(self._on_save_done)
            except CancelledError:
                GLib.idle_add(self._on_save_cancelled)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_save_error, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    # ── Launch target management ─────────────────────────────────────────

    def _add_target_row_ui(self, target: dict) -> None:
        """Add a UI row for *target* and track it in ``_launch_targets``."""
        self._launch_targets.append(target)
        idx = len(self._launch_targets) - 1

        name = target.get("name", "Main")
        path = target.get("path", "")
        args = target.get("args", "")

        row = Adw.ExpanderRow(title=GLib.markup_escape_text(name))
        row.set_subtitle(GLib.markup_escape_text(path) if path else "Not set")
        row.set_subtitle_lines(1)

        # Browse button
        browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
        browse_btn.add_css_class("flat")
        browse_btn.set_valign(Gtk.Align.CENTER)
        browse_btn.set_sensitive(self._locally_installed)
        browse_btn.set_tooltip_text(
            "Browse for executable\u2026" if self._locally_installed
            else "Not installed locally"
        )
        browse_btn.connect("clicked", self._on_browse_target, idx)
        row.add_suffix(browse_btn)

        # Delete button
        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.connect("clicked", self._on_remove_target, idx)
        row.add_suffix(del_btn)

        # Name sub-row
        name_entry = Adw.EntryRow(title="Name")
        name_entry.set_text(name)
        name_entry.connect("changed", self._on_target_name_changed, idx, row)
        row.add_row(name_entry)

        # Args sub-row
        args_entry = Adw.EntryRow(title="Arguments")
        args_entry.set_text(args)
        args_entry.connect("changed", self._on_target_args_changed, idx)
        row.add_row(args_entry)

        self._target_rows.append(row)
        self._targets_group.remove(self._add_target_row)
        self._targets_group.add(row)
        self._targets_group.add(self._add_target_row)

    def _on_add_target_clicked(self, _btn) -> None:
        self._add_target_row_ui({"name": "New Target", "path": ""})

    def _on_remove_target(self, _btn, idx: int) -> None:
        if idx >= len(self._launch_targets):
            return
        row = self._target_rows[idx]
        self._targets_group.remove(row)
        self._launch_targets.pop(idx)
        self._target_rows.pop(idx)
        # Re-bind remaining rows to correct indices
        self._rebind_target_indices()

    def _rebind_target_indices(self) -> None:
        """Reconnect signal handlers after a row removal shifts indices."""
        old_targets = list(self._launch_targets)
        # Remove all existing rows
        for row in self._target_rows:
            self._targets_group.remove(row)
        self._launch_targets.clear()
        self._target_rows.clear()
        for t in old_targets:
            self._add_target_row_ui(t)

    def _on_target_name_changed(self, entry: Adw.EntryRow, idx: int,
                                row: Adw.ExpanderRow) -> None:
        if idx < len(self._launch_targets):
            self._launch_targets[idx]["name"] = entry.get_text().strip()
            row.set_title(GLib.markup_escape_text(entry.get_text().strip()))

    def _on_target_args_changed(self, entry: Adw.EntryRow, idx: int) -> None:
        if idx < len(self._launch_targets):
            text = entry.get_text().strip()
            if text:
                self._launch_targets[idx]["args"] = text
            else:
                self._launch_targets[idx].pop("args", None)

    def _on_cancel_progress_clicked(self, _btn) -> None:
        self._cancel_event.set()
        self._progress_label.set_text("Cancelling\u2026")
        self._cancel_progress_btn.set_sensitive(False)

    def _on_save_done(self) -> None:
        log.debug("_on_save_done: saved_entry.screenshots=%s", self._saved_entry.screenshots)
        self.close()
        self._on_done(self._saved_entry)

    def _on_save_cancelled(self) -> None:
        self._stack.set_visible_child_name("form")
        self._cancel_progress_btn.set_sensitive(True)

    def _on_save_error(self, message: str) -> None:
        self._stack.set_visible_child_name("form")
        self._cancel_progress_btn.set_sensitive(True)
        alert = Adw.AlertDialog(heading="Save Failed", body=message)
        alert.add_response("ok", "OK")
        alert.present(self)


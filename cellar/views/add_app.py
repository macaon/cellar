"""Add-app dialog — lets the user add a Bottles backup to a local Cellar repo.

Flow
----
1. User opens a ``.tar.gz`` Bottles backup via a file chooser in the main window.
2. This dialog opens, pre-filling what we can from ``bottle.yml`` inside the
   archive (Name, Runner, DXVK, VKD3D, Environment → category suggestion).
3. User completes the metadata form and optionally attaches images.
4. On "Add to Catalogue" the form is replaced by a progress view while a
   background thread copies the archive and writes the catalogue entry.
5. On success the dialog closes and the main window reloads the browse view.
"""

from __future__ import annotations

import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk


_CATEGORIES = ["Games", "Productivity", "Graphics", "Utility", "Other"]
_STRATEGIES = ["safe", "full"]
_STRATEGY_LABELS = ["Safe (preserve user data)", "Full (complete replacement)"]


class AddAppDialog(Adw.Dialog):
    """Dialog for adding a Bottles backup archive to a local Cellar repo."""

    def __init__(
        self,
        *,
        archive_path: str,
        repo,           # cellar.backend.repo.Repo
        on_done,        # callable()
    ) -> None:
        super().__init__(title="Add App to Catalogue", content_width=560)

        self._archive_path = archive_path
        self._repo = repo
        self._on_done = on_done
        self._cancel_event = threading.Event()

        # Image selections
        self._icon_path: str = ""
        self._cover_path: str = ""
        self._hero_path: str = ""
        self._screenshot_paths: list[str] = []

        # Track whether the user has manually edited the ID field
        self._id_user_edited = False

        self._build_ui()
        self._prefill()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Outer toolbar view so we get a header bar inside the dialog
        toolbar_view = Adw.ToolbarView()

        # Header bar
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        header.pack_start(cancel_btn)

        self._add_btn = Gtk.Button(label="Add to Catalogue")
        self._add_btn.add_css_class("suggested-action")
        self._add_btn.set_sensitive(False)
        self._add_btn.connect("clicked", self._on_add_clicked)
        header.pack_end(self._add_btn)

        toolbar_view.add_top_bar(header)

        # Stack: form vs progress
        self._stack = Gtk.Stack()
        self._stack.add_named(self._build_form(), "form")
        self._stack.add_named(self._build_progress(), "progress")
        self._stack.set_visible_child_name("form")

        toolbar_view.set_content(self._stack)
        self.set_child(toolbar_view)

    def _build_form(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                    vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(True)

        page = Adw.PreferencesPage()
        scroll.set_child(page)

        # ── Archive ───────────────────────────────────────────────────────
        archive_group = Adw.PreferencesGroup(title="Archive")
        self._archive_row = Adw.ActionRow(
            title="File",
            subtitle=Path(self._archive_path).name,
        )
        archive_group.add(self._archive_row)
        page.add(archive_group)

        # ── Identity ──────────────────────────────────────────────────────
        identity_group = Adw.PreferencesGroup(title="Identity")

        self._name_entry = Adw.EntryRow(title="Name *")
        self._name_entry.connect("changed", self._on_name_changed)
        identity_group.add(self._name_entry)

        self._id_entry = Adw.EntryRow(title="App ID")
        self._id_entry.connect("changed", self._on_id_changed)
        identity_group.add(self._id_entry)

        self._version_entry = Adw.EntryRow(title="Version")
        self._version_entry.set_text("1.0")
        identity_group.add(self._version_entry)

        page.add(identity_group)

        # ── Details ───────────────────────────────────────────────────────
        details_group = Adw.PreferencesGroup(title="Details")

        self._category_row = Adw.ComboRow(title="Category *")
        cat_model = Gtk.StringList()
        for c in _CATEGORIES:
            cat_model.append(c)
        self._category_row.set_model(cat_model)
        self._category_row.connect("notify::selected", self._on_field_changed)
        details_group.add(self._category_row)

        self._summary_entry = Adw.EntryRow(title="Summary")
        details_group.add(self._summary_entry)

        # Description (multi-line) in a card-styled frame
        desc_row = Adw.ActionRow(title="Description")
        desc_row.set_activatable(False)
        self._desc_view = Gtk.TextView()
        self._desc_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._desc_view.set_margin_top(6)
        self._desc_view.set_margin_bottom(6)
        self._desc_view.set_margin_start(6)
        self._desc_view.set_margin_end(6)
        self._desc_view.set_size_request(-1, 80)
        self._desc_view.add_css_class("monospace")
        desc_frame = Gtk.Frame()
        desc_frame.set_child(self._desc_view)
        desc_frame.set_margin_top(6)
        desc_frame.set_margin_bottom(6)
        desc_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        desc_box.append(desc_row)
        desc_box.append(desc_frame)
        desc_box.add_css_class("card")
        desc_wrapper = Adw.PreferencesGroup()
        desc_wrapper.add(desc_box)
        page.add(desc_wrapper)

        page.add(details_group)

        # ── Attribution ───────────────────────────────────────────────────
        attr_group = Adw.PreferencesGroup(title="Attribution")
        self._developer_entry = Adw.EntryRow(title="Developer")
        self._publisher_entry = Adw.EntryRow(title="Publisher")
        self._year_entry = Adw.EntryRow(title="Release Year")
        attr_group.add(self._developer_entry)
        attr_group.add(self._publisher_entry)
        attr_group.add(self._year_entry)
        page.add(attr_group)

        # ── Wine Components ───────────────────────────────────────────────
        wine_group = Adw.PreferencesGroup(title="Wine Components")

        self._runner_row = Adw.ActionRow(title="Runner")
        self._runner_row.set_subtitle("")
        self._runner_row.set_subtitle_selectable(True)
        wine_group.add(self._runner_row)

        self._dxvk_row = Adw.ActionRow(title="DXVK")
        self._dxvk_row.set_subtitle("")
        self._dxvk_row.set_subtitle_selectable(True)
        wine_group.add(self._dxvk_row)

        self._vkd3d_row = Adw.ActionRow(title="VKD3D")
        self._vkd3d_row.set_subtitle("")
        self._vkd3d_row.set_subtitle_selectable(True)
        wine_group.add(self._vkd3d_row)

        page.add(wine_group)

        # ── Images ────────────────────────────────────────────────────────
        images_group = Adw.PreferencesGroup(title="Images (optional)")

        self._icon_row = self._make_image_row("Icon", self._pick_icon)
        self._cover_row = self._make_image_row("Cover", self._pick_cover)
        self._hero_row = self._make_image_row("Hero", self._pick_hero)
        self._screenshots_row = self._make_image_row(
            "Screenshots", self._pick_screenshots, multi=True
        )

        images_group.add(self._icon_row)
        images_group.add(self._cover_row)
        images_group.add(self._hero_row)
        images_group.add(self._screenshots_row)
        page.add(images_group)

        # ── Install ───────────────────────────────────────────────────────
        install_group = Adw.PreferencesGroup(title="Install")

        self._strategy_row = Adw.ComboRow(title="Update Strategy")
        strat_model = Gtk.StringList()
        for label in _STRATEGY_LABELS:
            strat_model.append(label)
        self._strategy_row.set_model(strat_model)
        install_group.add(self._strategy_row)

        self._entry_point_entry = Adw.EntryRow(title="Entry Point (optional)")
        self._entry_point_entry.set_tooltip_text(
            "Relative path from drive_c to the main .exe, e.g. Program Files/App/app.exe"
        )
        install_group.add(self._entry_point_entry)

        page.add(install_group)

        return scroll

    def _make_image_row(self, label: str, handler, *, multi: bool = False) -> Adw.ActionRow:
        row = Adw.ActionRow(title=label)
        row.set_subtitle("No file selected")
        btn = Gtk.Button(label="Choose…")
        btn.set_valign(Gtk.Align.CENTER)
        btn.connect("clicked", handler)
        row.add_suffix(btn)
        return row

    def _build_progress(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(48)
        box.set_margin_bottom(48)
        box.set_margin_start(24)
        box.set_margin_end(24)

        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(True)
        self._progress_bar.set_fraction(0.0)

        self._progress_label = Gtk.Label(label="Copying archive…")
        self._progress_label.add_css_class("dim-label")

        self._cancel_progress_btn = Gtk.Button(label="Cancel")
        self._cancel_progress_btn.set_halign(Gtk.Align.CENTER)
        self._cancel_progress_btn.connect("clicked", self._on_cancel_progress_clicked)

        box.append(self._progress_label)
        box.append(self._progress_bar)
        box.append(self._cancel_progress_btn)
        return box

    # ── Pre-fill ──────────────────────────────────────────────────────────

    def _prefill(self) -> None:
        from cellar.backend.packager import read_bottle_yml

        yml = read_bottle_yml(self._archive_path)

        name = yml.get("Name", "")
        if name:
            self._name_entry.set_text(name)

        runner = yml.get("Runner", "")
        if runner:
            self._runner_row.set_subtitle(runner)

        dxvk = yml.get("DXVK", "")
        if dxvk:
            self._dxvk_row.set_subtitle(dxvk)

        vkd3d = yml.get("VKD3D", "")
        if vkd3d:
            self._vkd3d_row.set_subtitle(vkd3d)

        env = yml.get("Environment", "")
        if env.lower() == "game":
            self._category_row.set_selected(0)  # "Games"

    # ── Signal handlers — form fields ─────────────────────────────────────

    def _on_name_changed(self, _entry) -> None:
        name = self._name_entry.get_text().strip()
        if not self._id_user_edited:
            from cellar.backend.packager import slugify
            # Block the id_changed signal temporarily to avoid triggering
            # the "user edited" flag from our own programmatic update
            self._id_updating_programmatically = True
            self._id_entry.set_text(slugify(name) if name else "")
            self._id_updating_programmatically = False
        self._update_add_button()

    def _on_id_changed(self, _entry) -> None:
        if not getattr(self, "_id_updating_programmatically", False):
            # User typed in the ID field — lock it from auto-updates
            self._id_user_edited = bool(self._id_entry.get_text())
        self._update_add_button()

    def _on_field_changed(self, *_args) -> None:
        self._update_add_button()

    def _update_add_button(self) -> None:
        name_ok = bool(self._name_entry.get_text().strip())
        # Category always has a selection (index ≥ 0); just require name
        self._add_btn.set_sensitive(name_ok)

    # ── Image pickers ─────────────────────────────────────────────────────

    def _pick_image(self, title: str, multi: bool, callback) -> None:
        chooser = Gtk.FileChooserNative(
            title=title,
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            select_multiple=multi,
        )
        img_filter = Gtk.FileFilter()
        img_filter.set_name("Images (PNG, JPG)")
        img_filter.add_mime_type("image/png")
        img_filter.add_mime_type("image/jpeg")
        chooser.add_filter(img_filter)
        chooser.connect("response", callback, chooser)
        chooser.show()

    def _pick_icon(self, _btn) -> None:
        self._pick_image("Select Icon", False, self._on_icon_chosen)

    def _on_icon_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._icon_path = chooser.get_file().get_path()
            self._icon_row.set_subtitle(Path(self._icon_path).name)

    def _pick_cover(self, _btn) -> None:
        self._pick_image("Select Cover", False, self._on_cover_chosen)

    def _on_cover_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._cover_path = chooser.get_file().get_path()
            self._cover_row.set_subtitle(Path(self._cover_path).name)

    def _pick_hero(self, _btn) -> None:
        self._pick_image("Select Hero Banner", False, self._on_hero_chosen)

    def _on_hero_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            self._hero_path = chooser.get_file().get_path()
            self._hero_row.set_subtitle(Path(self._hero_path).name)

    def _pick_screenshots(self, _btn) -> None:
        self._pick_image("Select Screenshots", True, self._on_screenshots_chosen)

    def _on_screenshots_chosen(self, _chooser, response, chooser) -> None:
        if response == Gtk.ResponseType.ACCEPT:
            files = chooser.get_files()
            self._screenshot_paths = [files.get_item(i).get_path()
                                       for i in range(files.get_n_items())]
            count = len(self._screenshot_paths)
            self._screenshots_row.set_subtitle(
                f"{count} file{'s' if count != 1 else ''} selected"
            )

    # ── Add / cancel ──────────────────────────────────────────────────────

    def _on_cancel_clicked(self, _btn) -> None:
        self.close()

    def _on_add_clicked(self, _btn) -> None:
        name = self._name_entry.get_text().strip()
        app_id = self._id_entry.get_text().strip()
        version = self._version_entry.get_text().strip() or "1.0"
        category = _CATEGORIES[self._category_row.get_selected()]
        summary = self._summary_entry.get_text().strip()
        desc_buf = self._desc_view.get_buffer()
        description = desc_buf.get_text(
            desc_buf.get_start_iter(), desc_buf.get_end_iter(), False
        ).strip()
        developer = self._developer_entry.get_text().strip()
        publisher = self._publisher_entry.get_text().strip()
        year_text = self._year_entry.get_text().strip()
        release_year = int(year_text) if year_text.isdigit() else None
        runner = self._runner_row.get_subtitle() or ""
        dxvk = self._dxvk_row.get_subtitle() or ""
        vkd3d = self._vkd3d_row.get_subtitle() or ""
        strategy = _STRATEGIES[self._strategy_row.get_selected()]
        entry_point = self._entry_point_entry.get_text().strip()

        if not app_id:
            from cellar.backend.packager import slugify
            app_id = slugify(name)

        # Build archive filename: apps/<id>/<basename of source>
        archive_filename = Path(self._archive_path).name
        archive_rel = f"apps/{app_id}/{archive_filename}"

        # Build image relative paths (only for images the user picked)
        icon_rel = f"apps/{app_id}/icon{Path(self._icon_path).suffix}" if self._icon_path else ""
        cover_rel = f"apps/{app_id}/cover{Path(self._cover_path).suffix}" if self._cover_path else ""
        hero_rel = f"apps/{app_id}/hero{Path(self._hero_path).suffix}" if self._hero_path else ""
        screenshot_rels = tuple(
            f"apps/{app_id}/screenshots/{i + 1:02d}{Path(p).suffix}"
            for i, p in enumerate(self._screenshot_paths)
        )

        from cellar.models.app_entry import AppEntry, BuiltWith

        built_with = BuiltWith(runner=runner, dxvk=dxvk, vkd3d=vkd3d) if runner else None

        entry = AppEntry(
            id=app_id,
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
            hero=hero_rel,
            screenshots=screenshot_rels,
            archive=archive_rel,
            built_with=built_with,
            update_strategy=strategy,
            entry_point=entry_point,
        )

        images = {
            "icon": self._icon_path,
            "cover": self._cover_path,
            "hero": self._hero_path,
            "screenshots": self._screenshot_paths,
        }

        self._cancel_event.clear()
        self._stack.set_visible_child_name("progress")
        self._progress_bar.set_fraction(0.0)
        self._progress_label.set_text("Copying archive…")

        repo_root = self._repo.writable_path()

        def _run():
            from cellar.backend.packager import CancelledError, import_to_repo

            def _progress(fraction: float) -> bool:
                GLib.idle_add(self._progress_bar.set_fraction, fraction)
                if fraction >= 0.9:
                    GLib.idle_add(self._progress_label.set_text, "Writing catalogue…")
                return False

            try:
                import_to_repo(
                    repo_root,
                    entry,
                    self._archive_path,
                    images,
                    progress_cb=_progress,
                    cancel_event=self._cancel_event,
                )
                GLib.idle_add(self._on_import_done)
            except CancelledError:
                GLib.idle_add(self._on_import_cancelled)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_import_error, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _on_cancel_progress_clicked(self, _btn) -> None:
        self._cancel_event.set()
        self._progress_label.set_text("Cancelling…")
        self._cancel_progress_btn.set_sensitive(False)

    # ── Import result callbacks (called on main thread via GLib.idle_add) ─

    def _on_import_done(self) -> None:
        # Show a toast on the main window
        root = self.get_root()
        if hasattr(root, "_show_toast"):
            root._show_toast("App added to catalogue")
        self.close()
        self._on_done()

    def _on_import_cancelled(self) -> None:
        self._stack.set_visible_child_name("form")
        self._cancel_progress_btn.set_sensitive(True)

    def _on_import_error(self, message: str) -> None:
        self._stack.set_visible_child_name("form")
        self._cancel_progress_btn.set_sensitive(True)
        alert = Adw.AlertDialog(
            heading="Import Failed",
            body=message,
        )
        alert.add_response("ok", "OK")
        alert.present(self)

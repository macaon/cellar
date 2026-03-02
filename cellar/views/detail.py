"""App detail view — shown when the user activates an app card."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gio, Gtk, Pango

from cellar.models.app_entry import AppEntry
from cellar.utils.images import load_and_crop, load_and_fit, to_texture

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
        source_repos: list | None = None,
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
        self._source_repos = source_repos or []
        _first = self._source_repos[0] if self._source_repos else None
        self._resolve = _first.resolve_asset_uri if _first else (lambda rel: rel)
        self._peek = _first.peek_asset_cache if _first else (lambda _: "")
        self._token = _first.token if _first else None
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
        self._screenshot_paths: list[str] = []
        # Runner compatibility state
        self._installed_runners: list[str] = []
        self._runner_override: str | None = (
            (installed_record or {}).get("runner_override") if is_installed else None
        )
        self._runner_row: Adw.ActionRow | None = None
        self._runner_warning_icon: Gtk.Image | None = None
        self._runners_loaded: bool = False

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

        # Edit button (pencil) stays in the header bar; Install/Remove/Update
        # move to the app header row alongside the title.
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

        # Kick off async runner compatibility check.
        self._check_runners_async()

    # ------------------------------------------------------------------
    # Install helpers
    # ------------------------------------------------------------------

    def _update_install_button(self) -> None:
        btn = self._install_btn
        for cls in ("suggested-action", "success", "destructive-action"):
            btn.remove_css_class(cls)
        if self._is_installed:
            btn.set_label("Open")
            btn.add_css_class("suggested-action")
            btn.set_sensitive(True)
            btn.set_tooltip_text("")
            self._remove_btn.set_visible(True)
        elif self._bottles_installs:
            btn.set_label("Install")
            btn.add_css_class("suggested-action")
            btn.set_sensitive(True)
            btn.set_tooltip_text("")
            self._remove_btn.set_visible(False)
        else:
            btn.set_label("Install")
            btn.add_css_class("suggested-action")
            btn.set_sensitive(False)
            btn.set_tooltip_text("Bottles is not installed")
            self._remove_btn.set_visible(False)
        self._update_btn.set_visible(self._has_update)
        self._gear_btn.set_visible(self._is_installed)

    def _on_install_clicked(self, _btn) -> None:
        if self._is_installed:
            self._on_open_clicked()
            return
        bw = self._entry.built_with
        required_runner = (bw.runner if bw else "") or ""
        effective = self._runner_override or required_runner
        runner_to_install = ""
        if required_runner and self._runners_loaded and effective not in self._installed_runners:
            runner_to_install = effective
        self._proceed_to_install(runner_to_install=runner_to_install)

    def _resolve_runner_info(self, runner_name: str) -> dict | None:
        """Return download info dict for *runner_name*, or ``None`` if unavailable."""
        from cellar.backend.components import get_runner_info
        from cellar.backend.bottles import runners_dir

        if not self._bottles_installs:
            return None
        info = get_runner_info(runner_name)
        if not info:
            return None
        files = info.get("File") or []
        if not files:
            return None
        f = files[0]
        return {
            "url": f.get("url", ""),
            "checksum": f.get("file_checksum", "") or f.get("checksum", ""),
            "target_dir": runners_dir(self._bottles_installs[0]) / runner_name,
        }

    def _proceed_to_install(self, *, runner_to_install: str = "") -> None:
        archive_uri = self._resolve(self._entry.archive) if self._entry.archive else ""
        runner_info = self._resolve_runner_info(runner_to_install) if runner_to_install else None

        def _open_runner_manager(on_change):
            """Open RunnerManagerDialog from the Change button inside InstallProgressDialog."""
            self._open_runner_manager(
                required_runner=(self._entry.built_with.runner if self._entry.built_with else "") or "",
                on_confirm=on_change,
            )

        dialog = InstallProgressDialog(
            entry=self._entry,
            installs=self._bottles_installs,
            archive_uri=archive_uri,
            on_success=self._on_install_success,
            token=self._token,
            runner_to_install=runner_to_install,
            runner_info=runner_info,
            installed_runners=list(self._installed_runners),
            open_runner_manager=_open_runner_manager,
            resolve_runner_info=self._resolve_runner_info,
        )
        dialog.present(self.get_root())

    def _on_install_success(self, bottle_name: str) -> None:
        self._is_installed = True
        self._installed_record = {"bottle_name": bottle_name}
        self._update_install_button()
        if self._on_install_done:
            self._on_install_done(bottle_name)
        # Apply runner override when the user pre-selected a different runner.
        built_with_runner = (self._entry.built_with.runner if self._entry.built_with else "") or ""
        if self._runner_override and self._runner_override != built_with_runner and self._bottles_installs:
            from cellar.backend.bottles import BottlesError, set_bottle_runner
            from cellar.backend import database
            install = self._bottles_installs[0]
            try:
                set_bottle_runner(install, bottle_name, self._runner_override)
                database.set_runner_override(self._entry.id, self._runner_override)
            except BottlesError as exc:
                log.error("Failed to apply runner override after install: %s", exc)

    def _on_open_clicked(self) -> None:
        from cellar.backend.bottles import launch_bottle, list_bottle_programs
        bottle_name = (self._installed_record or {}).get("bottle_name", "")
        if not bottle_name or not self._bottles_installs:
            return
        # Prefer the install that actually has the bottle directory.
        install = self._bottles_installs[0]
        for inst in self._bottles_installs:
            if (inst.data_path / bottle_name).is_dir():
                install = inst
                break
        programs = list_bottle_programs(install, bottle_name)
        if not programs:
            # Nothing registered — fall back to catalogue entry_point or GUI.
            launch_bottle(install, bottle_name, self._entry.entry_point or None)
        elif len(programs) == 1:
            launch_bottle(install, bottle_name, program=programs[0])
        else:
            LaunchProgramDialog(
                bottle_name=bottle_name,
                install=install,
                programs=programs,
            ).present(self.get_root())

    def _on_remove_clicked(self) -> None:
        bottle_name = (self._installed_record or {}).get("bottle_name", "")
        bottle_path = None
        if bottle_name:
            for install in self._bottles_installs:
                candidate = install.data_path / bottle_name
                if candidate.is_dir() and candidate != install.data_path:
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
            token=self._token,
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
        if bottle_name:
            for install in self._bottles_installs:
                candidate = install.data_path / bottle_name
                if candidate.is_dir():
                    # Safety: never delete the Bottles data root itself.
                    if candidate == install.data_path:
                        log.error("Refusing to delete Bottles data root %s", candidate)
                        break
                    try:
                        shutil.rmtree(candidate)
                    except Exception as exc:
                        log.error("Failed to remove bottle %s: %s", candidate, exc)
                    break
        else:
            log.error("No bottle_name for %s; skipping filesystem removal", self._entry.id)

        database.remove_installed(self._entry.id)
        from cellar.utils.desktop import remove_desktop_entry
        remove_desktop_entry(self._entry.id)
        self._is_installed = False
        self._installed_record = None
        self._update_install_button()
        if self._on_remove_done:
            self._on_remove_done()

    # ------------------------------------------------------------------
    # Desktop shortcut (gear menu)
    # ------------------------------------------------------------------

    def _setup_gear_actions(self) -> None:
        ag = Gio.SimpleActionGroup()

        create_act = Gio.SimpleAction.new("create-shortcut", None)
        create_act.connect("activate", self._on_create_shortcut)
        ag.add_action(create_act)

        remove_act = Gio.SimpleAction.new("remove-shortcut", None)
        remove_act.connect("activate", self._on_remove_shortcut)
        ag.add_action(remove_act)

        self.insert_action_group("detail", ag)
        self._refresh_gear_menu()

    def _refresh_gear_menu(self) -> None:
        from cellar.utils.desktop import has_desktop_entry

        menu = Gio.Menu()
        if has_desktop_entry(self._entry.id):
            menu.append("Remove Desktop Shortcut", "detail.remove-shortcut")
        else:
            menu.append("Create Desktop Shortcut", "detail.create-shortcut")
        self._gear_btn.set_menu_model(menu)

    def _on_create_shortcut(self, _action, _param) -> None:
        from cellar.backend.bottles import _bottle_display_name, list_bottle_programs
        from cellar.utils.desktop import create_desktop_entry

        bottle_name = (self._installed_record or {}).get("bottle_name", "")
        if not bottle_name or not self._bottles_installs:
            return

        install = self._bottles_installs[0]
        for inst in self._bottles_installs:
            if (inst.data_path / bottle_name).is_dir():
                install = inst
                break

        # bottles-cli -b expects the Name from bottle.yml, not the directory name.
        display_name = _bottle_display_name(install, bottle_name)

        programs = list_bottle_programs(install, bottle_name)
        program_name = programs[0]["name"] if programs else None

        icon_source: str | None = None
        if self._entry.icon:
            resolved = self._resolve(self._entry.icon)
            if resolved and Path(resolved).is_file():
                icon_source = resolved

        try:
            create_desktop_entry(
                entry=self._entry,
                bottle_name=display_name,
                program_name=program_name,
                is_flatpak=(install.variant == "flatpak"),
                icon_source=icon_source,
            )
            self._refresh_gear_menu()
            self._add_toast(f"Shortcut created for {self._entry.name}")
        except Exception as exc:
            log.error("Failed to create desktop entry: %s", exc)
            self._add_toast("Failed to create shortcut")

    def _on_remove_shortcut(self, _action, _param) -> None:
        from cellar.utils.desktop import remove_desktop_entry

        remove_desktop_entry(self._entry.id)
        self._refresh_gear_menu()
        self._add_toast(f"Shortcut removed for {self._entry.name}")

    def _add_toast(self, message: str) -> None:
        root = self.get_root()
        if hasattr(root, "toast_overlay"):
            root.toast_overlay.add_toast(Adw.Toast(title=message))

    # ------------------------------------------------------------------
    # Runner compatibility
    # ------------------------------------------------------------------

    def _check_runners_async(self) -> None:
        """Start a background thread that lists installed runners and updates the banner."""
        if not self._bottles_installs:
            return
        bw = self._entry.built_with
        if not bw or not bw.runner:
            return
        install = self._bottles_installs[0]

        def _run() -> None:
            from cellar.backend.bottles import BottlesError, list_runners
            try:
                runners = list_runners(install)
            except BottlesError:
                runners = []
            GLib.idle_add(self._on_runners_loaded, runners)

        threading.Thread(target=_run, daemon=True).start()

    def _on_runners_loaded(self, runners: list[str]) -> None:
        self._installed_runners = runners
        self._runners_loaded = True
        required = (self._entry.built_with.runner if self._entry.built_with else "") or ""
        if not required or not self._runner_warning_icon:
            return
        effective = self._runner_override or required
        missing = effective not in runners
        self._runner_warning_icon.set_visible(missing)
        if missing:
            self._runner_warning_icon.set_tooltip_text(
                f"Runner \u201c{effective}\u201d is not installed"
            )

    def _on_change_runner_clicked(self, _btn) -> None:
        self._open_runner_manager()

    def _open_install_runner_dialog(
        self,
        runner_name: str | None = None,
        *,
        on_done: Callable[[str], None] | None = None,
    ) -> None:
        """Open InstallRunnerDialog for *runner_name*.

        When *runner_name* is ``None``, falls back to the entry's built-with
        runner.  The optional *on_done* callback is invoked (in addition to the
        internal bookkeeping) once the runner is successfully installed.
        """
        from cellar.backend.components import get_runner_info
        from cellar.backend.bottles import runners_dir
        from cellar.views.install_runner import InstallRunnerDialog

        bw = self._entry.built_with
        effective_name = runner_name or (bw.runner if bw else "") or ""
        if not effective_name or not self._bottles_installs:
            return

        info = get_runner_info(effective_name)
        if not info:
            return
        files = info.get("File") or []
        if not files:
            return
        file_info = files[0]
        url = file_info.get("url", "")
        checksum = file_info.get("file_checksum", "") or file_info.get("checksum", "")
        target_dir = runners_dir(self._bottles_installs[0]) / effective_name

        def _on_done_internal(rname: str) -> None:
            if rname not in self._installed_runners:
                self._installed_runners = sorted(self._installed_runners + [rname])
            if self._runner_warning_icon:
                self._runner_warning_icon.set_visible(False)
            if on_done:
                on_done(rname)

        InstallRunnerDialog(
            runner_name=effective_name,
            url=url,
            checksum=checksum,
            target_dir=target_dir,
            on_done=_on_done_internal,
        ).present(self.get_root())

    def _open_runner_manager(
        self,
        required_runner: str = "",
        on_confirm: Callable[[str], None] | None = None,
    ) -> None:
        """Open :class:`RunnerManagerDialog` for selecting or managing runners."""
        from cellar.backend.components import is_available, list_runners_by_category
        from cellar.backend.bottles import get_runners_in_use, runners_dir

        bw = self._entry.built_with
        built_with_runner = (bw.runner if bw else "") or ""
        current = self._runner_override or built_with_runner

        runners_in_use: set[str] = set()
        rdir = None
        if self._bottles_installs:
            install = self._bottles_installs[0]
            runners_in_use = get_runners_in_use(install)
            rdir = runners_dir(install)

        runners_by_cat: dict[str, list[str]] = {}
        if is_available():
            runners_by_cat = list_runners_by_category()

        effective_on_confirm = on_confirm if on_confirm is not None else self._on_runner_selected

        def _on_install(runner_name: str) -> None:
            self._open_install_runner_dialog(runner_name, on_done=effective_on_confirm)

        RunnerManagerDialog(
            installed_runners=self._installed_runners,
            runners_in_use=runners_in_use,
            runners_by_category=runners_by_cat,
            runners_dir=rdir,
            current_runner=current,
            required_runner=required_runner or built_with_runner,
            on_confirm=effective_on_confirm,
            on_install_runner=_on_install,
        ).present(self.get_root())

    def _on_runner_selected(self, runner_name: str) -> None:
        self._runner_override = runner_name
        if self._runner_row:
            self._runner_row.set_subtitle(runner_name)
        if self._runner_warning_icon and runner_name in self._installed_runners:
            self._runner_warning_icon.set_visible(False)
        # If already installed, apply runner change immediately via bottle.yml.
        if self._is_installed and self._installed_record and self._bottles_installs:
            from cellar.backend.bottles import BottlesError, set_bottle_runner
            from cellar.backend import database
            bottle_name = self._installed_record.get("bottle_name", "")
            if bottle_name:
                install = self._bottles_installs[0]
                try:
                    set_bottle_runner(install, bottle_name, runner_name)
                    database.set_runner_override(self._entry.id, runner_name)
                except BottlesError as exc:
                    log.error("Failed to set runner: %s", exc)

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _make_hero(self) -> Gtk.Widget | None:
        if not self._entry.hero:
            return None

        clamp = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        clamp.set_overflow(Gtk.Overflow.HIDDEN)
        clamp.set_size_request(-1, 220)
        clamp.set_vexpand(False)

        def _attach_pic(path: str) -> None:
            pic = Gtk.Picture.new_for_filename(path)
            pic.set_content_fit(Gtk.ContentFit.COVER)
            pic.set_can_shrink(True)
            pic.set_halign(Gtk.Align.FILL)
            pic.set_valign(Gtk.Align.CENTER)
            clamp.append(pic)

        # Fast path: already on disk — attach synchronously, no layout shift.
        cached = self._peek(self._entry.hero)
        if cached and os.path.isfile(cached):
            _attach_pic(cached)
            return clamp

        # Slow path: needs a network fetch — resolve on a background thread.
        hero_path = self._entry.hero

        def _worker() -> None:
            path = self._resolve(hero_path)
            GLib.idle_add(_on_resolved, path)

        def _on_resolved(path: str) -> bool:
            if os.path.isfile(path):
                _attach_pic(path)
            else:
                clamp.set_visible(False)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=_worker, daemon=True).start()
        return clamp

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

        icon = self._make_icon(e.icon, _ICON_SIZE, cover_fallback=e.cover)
        icon.set_valign(Gtk.Align.START)
        box.append(icon)

        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        meta.set_valign(Gtk.Align.CENTER)
        meta.set_hexpand(True)
        box.append(meta)

        name_lbl = Gtk.Label(label=e.name)
        name_lbl.add_css_class("title-1")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_wrap(True)
        meta.append(name_lbl)

        # "Developer · Publisher · Year · Version" byline.
        byline_parts: list[str] = []
        if e.developer:
            byline_parts.append(e.developer)
        if e.publisher and e.publisher != e.developer:
            byline_parts.append(e.publisher)
        if e.release_year:
            byline_parts.append(str(e.release_year))
        if e.version:
            byline_parts.append(e.version)
        if e.archive_size:
            byline_parts.append(_fmt_bytes(e.archive_size))
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

        # Right column: action row + update + source selector.
        right = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            valign=Gtk.Align.CENTER,
            halign=Gtk.Align.END,
        )
        box.append(right)

        # Action row: primary button (Install / Open) + trash (visible when installed).
        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        right.append(action_row)

        self._install_btn = Gtk.Button()
        self._install_btn.set_size_request(105, 34)
        self._install_btn.connect("clicked", self._on_install_clicked)
        action_row.append(self._install_btn)

        self._gear_btn = Gtk.MenuButton(icon_name="emblem-system-symbolic")
        self._gear_btn.set_size_request(34, 34)
        self._gear_btn.set_tooltip_text("Options")
        self._gear_btn.set_visible(False)
        action_row.append(self._gear_btn)

        self._remove_btn = Gtk.Button(icon_name="user-trash-symbolic")
        self._remove_btn.set_size_request(34, 34)
        self._remove_btn.add_css_class("destructive-action")
        self._remove_btn.set_tooltip_text("Uninstall")
        self._remove_btn.connect("clicked", lambda _b: self._on_remove_clicked())
        self._remove_btn.set_visible(False)
        action_row.append(self._remove_btn)

        self._update_btn = Gtk.Button(label="Update")
        self._update_btn.set_size_request(105, 34)
        self._update_btn.add_css_class("suggested-action")
        self._update_btn.connect("clicked", self._on_update_clicked)
        right.append(self._update_btn)

        source_widget = self._make_source_selector()
        if source_widget:
            right.append(source_widget)

        self._update_install_button()
        self._setup_gear_actions()

        return box

    def _make_source_selector(self) -> Gtk.Widget | None:
        """Return a GNOME-Software-style source MenuButton, or None for ≤1 repo."""
        if len(self._source_repos) < 2:
            return None

        # Label + arrow inside the button, matching GNOME Software's layout.
        self._source_label = Gtk.Label(label=self._source_repos[0].name)
        self._source_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._source_label.set_hexpand(True)
        self._source_label.set_xalign(0)

        arrow = Gtk.Image.new_from_icon_name("pan-down-symbolic")

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        btn_box.append(self._source_label)
        btn_box.append(arrow)

        # Popover with radio rows — one per repo.
        pop_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=2,
            margin_top=6,
            margin_bottom=6,
            margin_start=6,
            margin_end=6,
        )
        radio_group: Gtk.CheckButton | None = None
        for idx, repo in enumerate(self._source_repos):
            radio = Gtk.CheckButton(label=repo.name)
            if radio_group is None:
                radio_group = radio
                radio.set_active(True)
            else:
                radio.set_group(radio_group)
            radio.connect("toggled", self._on_source_radio_toggled, idx)
            pop_box.append(radio)

        popover = Gtk.Popover()
        popover.set_child(pop_box)

        menu_btn = Gtk.MenuButton(popover=popover)
        menu_btn.set_child(btn_box)
        menu_btn.set_size_request(105, 34)

        self._source_popover = popover
        return menu_btn

    def _on_source_radio_toggled(self, radio: Gtk.CheckButton, idx: int) -> None:
        if not radio.get_active():
            return
        repo = self._source_repos[idx]
        self._resolve = repo.resolve_asset_uri
        self._token = repo.token
        self._source_label.set_label(repo.name)
        self._source_popover.popdown()

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
        if not self._entry.screenshots:
            return None

        screenshots = list(self._entry.screenshots)

        # Fast path: peek the cache for every screenshot.  If all are already
        # on disk, build the carousel synchronously — no placeholders, no shift.
        cached_paths = []
        for s in screenshots:
            p = self._peek(s)
            if p and os.path.isfile(p):
                cached_paths.append(p)
            else:
                cached_paths = []
                break

        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        wrapper.set_margin_bottom(12)

        if cached_paths:
            self._screenshot_paths = cached_paths
            self._populate_screenshots(wrapper, cached_paths)
            return wrapper

        # Slow path: some images need fetching — resolve async and fill in later.
        wrapper.set_visible(False)

        def _worker() -> None:
            resolved = []
            for s in screenshots:
                path = self._resolve(s)
                if os.path.isfile(path):
                    resolved.append(path)
            GLib.idle_add(_on_resolved, resolved)

        def _on_resolved(paths: list[str]) -> bool:
            self._screenshot_paths = paths
            if paths:
                self._populate_screenshots(wrapper, paths)
                wrapper.set_visible(True)
            return GLib.SOURCE_REMOVE

        threading.Thread(target=_worker, daemon=True).start()
        return wrapper

    def _populate_screenshots(self, wrapper: Gtk.Box, paths: list[str]) -> None:
        """Build and append carousel content into *wrapper*."""
        carousel = Adw.Carousel(
            allow_scroll_wheel=False, reveal_duration=200, spacing=12,
        )
        multiple = len(paths) > 1
        pointer_cursor = Gdk.Cursor.new_from_name("pointer")

        for idx, path in enumerate(paths):
            pic = Gtk.Picture.new_for_filename(path)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_can_shrink(True)
            pic.set_size_request(-1, 300)
            pic.set_cursor(pointer_cursor)
            click = Gtk.GestureClick()
            click.connect("released", self._on_screenshot_clicked, idx)
            pic.add_controller(click)
            carousel.append(pic)

        overlay = Gtk.Overlay(child=carousel)
        wrapper.append(overlay)

        if multiple:
            prev_btn = Gtk.Button(icon_name="go-previous-symbolic")
            prev_btn.add_css_class("osd")
            prev_btn.add_css_class("circular")
            prev_btn.add_css_class("screenshot-nav")
            prev_btn.set_halign(Gtk.Align.START)
            prev_btn.set_valign(Gtk.Align.CENTER)
            prev_btn.set_margin_start(12)
            prev_btn.set_opacity(0)
            prev_btn.set_can_target(False)
            prev_btn.connect("clicked", lambda _b: carousel.scroll_to(
                carousel.get_nth_page(max(0, round(carousel.get_position()) - 1)),
                True,
            ))
            overlay.add_overlay(prev_btn)

            next_btn = Gtk.Button(icon_name="go-next-symbolic")
            next_btn.add_css_class("osd")
            next_btn.add_css_class("circular")
            next_btn.add_css_class("screenshot-nav")
            next_btn.set_halign(Gtk.Align.END)
            next_btn.set_valign(Gtk.Align.CENTER)
            next_btn.set_margin_end(12)
            next_btn.set_opacity(0)
            next_btn.set_can_target(False)
            next_btn.connect("clicked", lambda _b: carousel.scroll_to(
                carousel.get_nth_page(min(
                    carousel.get_n_pages() - 1,
                    round(carousel.get_position()) + 1,
                )),
                True,
            ))
            overlay.add_overlay(next_btn)

            def _update_arrow_visibility(*_args) -> None:
                page = round(carousel.get_position())
                prev_btn.set_opacity(0 if page == 0 else 1)
                prev_btn.set_can_target(page != 0)
                last = carousel.get_n_pages() - 1
                next_btn.set_opacity(0 if page >= last else 1)
                next_btn.set_can_target(page < last)

            carousel.connect("page-changed", _update_arrow_visibility)

            motion = Gtk.EventControllerMotion()
            motion.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
            motion.connect("enter", lambda *_: _update_arrow_visibility())
            motion.connect("leave", lambda *_: (
                prev_btn.set_opacity(0),
                prev_btn.set_can_target(False),
                next_btn.set_opacity(0),
                next_btn.set_can_target(False),
            ))
            overlay.add_controller(motion)

        wrapper.append(Adw.CarouselIndicatorDots(carousel=carousel))

    def _on_screenshot_clicked(self, _gesture, _n, _x, _y, index: int) -> None:
        dialog = ScreenshotDialog(self._screenshot_paths, index)
        dialog.present(self.get_root())

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

        if bw.runner:
            runner_subtitle = self._runner_override or bw.runner
            runner_row = Adw.ActionRow(title="Runner", subtitle=runner_subtitle)
            runner_row.set_subtitle_selectable(True)
            warning_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
            warning_icon.set_valign(Gtk.Align.CENTER)
            warning_icon.set_visible(False)
            warning_icon.add_css_class("warning")
            runner_row.add_suffix(warning_icon)
            self._runner_warning_icon = warning_icon
            if self._is_installed:
                # Change button only makes sense once the bottle exists on disk.
                change_btn = Gtk.Button(label="Change")
                change_btn.add_css_class("suggested-action")
                change_btn.set_valign(Gtk.Align.CENTER)
                change_btn.connect("clicked", self._on_change_runner_clicked)
                runner_row.add_suffix(change_btn)
            self._runner_row = runner_row
            group.add(runner_row)
        else:
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

    def _make_icon(self, rel_path: str, size: int, *, cover_fallback: str = "") -> Gtk.Widget:
        # Fast path: if the image is already on disk, decode and return immediately.
        for path_arg, is_cover in ((rel_path, False), (cover_fallback, True)):
            if not path_arg:
                continue
            cached = self._peek(path_arg)
            if cached and os.path.isfile(cached):
                png_bytes = (
                    load_and_crop(cached, size, size)
                    if is_cover
                    else load_and_fit(cached, size)
                )
                if png_bytes is not None:
                    if is_cover:
                        w: Gtk.Widget = Gtk.Picture.new_for_paintable(to_texture(png_bytes))
                        w.set_size_request(size, size)
                        w.set_content_fit(Gtk.ContentFit.FILL)
                        w.add_css_class("icon-dropshadow")
                    else:
                        w = Gtk.Image.new_from_paintable(to_texture(png_bytes))
                        w.set_pixel_size(size)
                    return w

        # Slow path: needs a network fetch — use a placeholder stack and swap async.
        placeholder = Gtk.Image.new_from_icon_name("application-x-executable")
        placeholder.set_pixel_size(size)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(150)
        stack.add_named(placeholder, "placeholder")
        stack.set_size_request(size, size)

        icon_path = rel_path
        fallback_path = cover_fallback

        def _worker() -> None:
            if icon_path:
                path = self._resolve(icon_path)
                if os.path.isfile(path):
                    png_bytes = load_and_fit(path, size)
                    if png_bytes is not None:
                        GLib.idle_add(_on_loaded, png_bytes, False)
                        return
            if fallback_path:
                cover = self._resolve(fallback_path)
                if os.path.isfile(cover):
                    png_bytes = load_and_crop(cover, size, size)
                    if png_bytes is not None:
                        GLib.idle_add(_on_loaded, png_bytes, True)
                        return

        def _on_loaded(png_bytes: bytes, is_cover: bool) -> bool:
            texture = to_texture(png_bytes)
            if is_cover:
                real: Gtk.Widget = Gtk.Picture.new_for_paintable(texture)
                real.set_size_request(size, size)
                real.set_content_fit(Gtk.ContentFit.FILL)
                real.add_css_class("icon-dropshadow")
            else:
                real = Gtk.Image.new_from_paintable(texture)
                real.set_pixel_size(size)
            stack.add_named(real, "real")
            stack.set_visible_child_name("real")
            return GLib.SOURCE_REMOVE

        threading.Thread(target=_worker, daemon=True).start()
        return stack


# ---------------------------------------------------------------------------
# Program picker dialog (shown when a bottle has multiple External_Programs)
# ---------------------------------------------------------------------------


class LaunchProgramDialog(Adw.Dialog):
    """Let the user pick which program inside a bottle to launch."""

    def __init__(
        self,
        *,
        bottle_name: str,
        install,          # BottlesInstall
        programs: list[dict],
    ) -> None:
        super().__init__(title="Open", content_width=360)
        self._bottle_name = bottle_name
        self._install = install
        self._programs = programs
        self._selected: dict | None = programs[0] if programs else None
        self._build_ui()

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        open_btn = Gtk.Button(label="Open")
        open_btn.add_css_class("suggested-action")
        open_btn.connect("clicked", self._on_open_clicked)
        header.pack_end(open_btn)

        toolbar.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        scroll.set_propagate_natural_height(True)

        page = Adw.PreferencesPage()
        scroll.set_child(page)

        group = Adw.PreferencesGroup(title="Select Program")
        page.add(group)

        radio_group: Gtk.CheckButton | None = None
        for program in self._programs:
            name = program.get("name") or program.get("executable") or "Unknown"
            exe = program.get("executable") or ""
            row = Adw.ActionRow(title=name, subtitle=exe)
            radio = Gtk.CheckButton()
            radio.set_valign(Gtk.Align.CENTER)
            if radio_group is None:
                radio_group = radio
                radio.set_active(True)
            else:
                radio.set_group(radio_group)
            radio.connect("toggled", self._on_radio_toggled, program)
            row.add_prefix(radio)
            row.set_activatable_widget(radio)
            group.add(row)

        toolbar.set_content(scroll)
        self.set_child(toolbar)

    def _on_radio_toggled(self, btn: Gtk.CheckButton, program: dict) -> None:
        if btn.get_active():
            self._selected = program

    def _on_open_clicked(self, _btn) -> None:
        from cellar.backend.bottles import launch_bottle
        if self._selected:
            launch_bottle(self._install, self._bottle_name, program=self._selected)
        self.close()


# ---------------------------------------------------------------------------
# Runner manager dialog
# ---------------------------------------------------------------------------


class RunnerManagerDialog(Adw.Dialog):
    """Browse, download, delete and (optionally) select a runner.

    Runners are grouped into collapsible :class:`Adw.ExpanderRow` sections by
    family (Soda, Caffe, Wine GE, …).  Each runner row shows state-based
    suffix icons:

    * **Installed + in use** — ``folder-open-symbolic`` only (cannot delete
      while a bottle depends on it).
    * **Installed + not in use** — ``folder-open-symbolic`` + ``user-trash-symbolic``.
    * **Not installed** — ``folder-arrow-down-symbolic`` (download).

    When *on_confirm* is supplied the dialog acts as a picker: every installed
    runner row gets a radio-button prefix and a *Select* header button confirms
    the choice.
    """

    def __init__(
        self,
        *,
        installed_runners: list[str],
        runners_in_use: set[str],
        runners_by_category: dict[str, list[str]],
        runners_dir: Path | None,
        current_runner: str = "",
        required_runner: str = "",
        on_confirm: Callable[[str], None] | None = None,
        on_install_runner: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(title="Runners", content_width=500, content_height=500)
        self._installed = set(installed_runners)
        self._runners_in_use = runners_in_use
        self._runners_by_category = runners_by_category
        self._runners_dir = runners_dir
        self._current_runner = current_runner
        self._required_runner = required_runner
        self._on_confirm = on_confirm
        self._on_install_runner = on_install_runner
        self._selected = required_runner or current_runner or ""
        self._radio_group_anchor: Gtk.CheckButton | None = None
        self._select_btn: Gtk.Button | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        from cellar.backend.components import _classify_runner, _version_sort_key, family_display_order, get_family_info

        toolbar = Adw.ToolbarView()

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        if self._on_confirm:
            self._select_btn = Gtk.Button(label="Select")
            self._select_btn.add_css_class("suggested-action")
            self._select_btn.connect("clicked", self._on_select_clicked)
            self._select_btn.set_sensitive(bool(self._selected))
            header.pack_end(self._select_btn)

        toolbar.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        scroll.set_vexpand(True)

        page = Adw.PreferencesPage()
        scroll.set_child(page)
        toolbar.set_content(scroll)
        self.set_child(toolbar)

        # ── Determine family membership ────────────────────────────────────
        # Map each indexed runner name → its family dir_name.
        runner_to_family: dict[str, str] = {}
        for dir_name, names in self._runners_by_category.items():
            for rname in names:
                if rname not in runner_to_family:
                    runner_to_family[rname] = dir_name

        # Classify installed runners not in the index by name prefix so
        # they appear in the correct family (e.g. sys-wine-11.0 → wine).
        for r in self._installed:
            if r not in runner_to_family:
                runner_to_family[r] = _classify_runner(r, "wine")

        # All families that have at least one runner (installed or indexed).
        active_families: set[str] = set(self._runners_by_category.keys())
        for r in self._installed:
            fam = runner_to_family.get(r)
            if fam:
                active_families.add(fam)

        display_order = family_display_order()
        explicit = [f for f in display_order if f in active_families]
        rest = sorted(f for f in active_families if f not in display_order and f != "other")
        ordered: list[str] = explicit + rest
        if "other" in active_families:
            ordered.append("other")

        # ── Family groups (collapsible) ────────────────────────────────────
        if ordered:
            families_grp = Adw.PreferencesGroup()
            page.add(families_grp)

            for dir_name in ordered:
                display_name, description = get_family_info(dir_name)
                expander = Adw.ExpanderRow(title=display_name)
                if description:
                    expander.set_subtitle(description)

                index_runners: set[str] = set(self._runners_by_category.get(dir_name, []))
                installed_in_family: set[str] = {
                    r for r in self._installed if runner_to_family.get(r) == dir_name
                }
                all_runners = sorted(index_runners | installed_in_family, key=_version_sort_key)

                # Auto-expand if the family contains the current or required runner.
                should_expand = (
                    bool(self._current_runner and self._current_runner in (index_runners | installed_in_family))
                    or bool(self._required_runner and self._required_runner in (index_runners | installed_in_family))
                )
                expander.set_expanded(should_expand)

                for runner in all_runners:
                    expander.add_row(self._make_runner_row(runner, is_installed=runner in self._installed))

                families_grp.add(expander)

        # Placeholder when nothing is available at all.
        if not ordered:
            grp = Adw.PreferencesGroup()
            page.add(grp)
            grp.add(Adw.ActionRow(
                title="No runners found",
                subtitle="Install runners in Bottles or sync the component index.",
            ))

    def _make_runner_row(self, runner: str, *, is_installed: bool) -> Adw.ActionRow:
        is_current = runner == self._current_runner
        is_required = runner == self._required_runner and runner != self._current_runner
        subtitle_parts: list[str] = []
        if is_current:
            subtitle_parts.append("current")
        if is_required:
            subtitle_parts.append("required")

        row = Adw.ActionRow(title=runner, subtitle=" · ".join(subtitle_parts))

        # Radio prefix for selection mode.
        if self._on_confirm:
            radio = Gtk.CheckButton()
            radio.set_valign(Gtk.Align.CENTER)
            if self._radio_group_anchor is None:
                self._radio_group_anchor = radio
            else:
                radio.set_group(self._radio_group_anchor)
            if runner == self._selected:
                radio.set_active(True)
            radio.connect("toggled", self._on_radio_toggled, runner)
            row.set_activatable_widget(radio)
            row.add_prefix(radio)

        # Suffix buttons.
        if is_installed:
            in_use = runner in self._runners_in_use
            folder_btn = Gtk.Button(icon_name="folder-open-symbolic")
            folder_btn.add_css_class("flat")
            folder_btn.set_valign(Gtk.Align.CENTER)
            folder_btn.set_tooltip_text("Open runner folder")
            folder_btn.set_sensitive(self._runners_dir is not None)
            folder_btn.connect("clicked", self._on_open_folder, runner)
            row.add_suffix(folder_btn)

            if not in_use:
                trash_btn = Gtk.Button(icon_name="user-trash-symbolic")
                trash_btn.add_css_class("flat")
                trash_btn.set_valign(Gtk.Align.CENTER)
                trash_btn.set_tooltip_text("Delete runner")
                trash_btn.set_sensitive(self._runners_dir is not None)
                trash_btn.connect("clicked", self._on_delete, runner)
                row.add_suffix(trash_btn)
        elif not self._on_confirm:
            dl_btn = Gtk.Button(icon_name="folder-download-symbolic")
            dl_btn.add_css_class("flat")
            dl_btn.set_valign(Gtk.Align.CENTER)
            dl_btn.set_tooltip_text("Download runner")
            dl_btn.connect("clicked", self._on_download, runner)
            row.add_suffix(dl_btn)

        return row

    def _on_radio_toggled(self, btn: Gtk.CheckButton, runner: str) -> None:
        if btn.get_active():
            self._selected = runner
            if self._select_btn:
                self._select_btn.set_sensitive(True)

    def _on_select_clicked(self, _btn) -> None:
        if not self._selected or not self._on_confirm:
            self.close()
            return
        if self._selected not in self._installed:
            self.close()
            if self._on_install_runner:
                self._on_install_runner(self._selected)
            return
        self._on_confirm(self._selected)
        self.close()

    def _on_open_folder(self, _btn, runner: str) -> None:
        if self._runners_dir is None:
            return
        path = self._runners_dir / runner
        if path.is_dir():
            Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)

    def _on_delete(self, _btn, runner: str) -> None:
        dialog = Adw.AlertDialog(
            heading=f"Delete {runner}?",
            body="This runner will be permanently deleted from disk.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_confirmed, runner)
        dialog.present(self)

    def _on_delete_confirmed(self, _dialog, response: str, runner: str) -> None:
        if response != "delete" or self._runners_dir is None:
            return
        import shutil
        path = self._runners_dir / runner

        def _do_delete() -> None:
            try:
                shutil.rmtree(path)
            except Exception as exc:  # noqa: BLE001
                log.error("Failed to delete runner %s: %s", runner, exc)
            GLib.idle_add(self.close)

        threading.Thread(target=_do_delete, daemon=True).start()

    def _on_download(self, _btn, runner: str) -> None:
        self.close()
        if self._on_install_runner:
            self._on_install_runner(runner)


# ---------------------------------------------------------------------------
# Install progress dialog
# ---------------------------------------------------------------------------


def _trunc_filename(name: str, max_chars: int = 40) -> str:
    """Middle-truncate *name* so it fits in the progress bar without resizing."""
    if len(name) <= max_chars:
        return name
    half = (max_chars - 1) // 2
    return f"{name[:half]}\u2026{name[-(max_chars - half - 1):]}"


def _fmt_dl_stats(downloaded: int, total: int, speed: float) -> str:
    """Format download progress as e.g. '2.6 MB / 349 MB (1.3 MB/s)'."""
    def _sz(n: int) -> str:
        if n < 1024:
            return f"{n} B"
        if n < 1024 ** 2:
            return f"{n / 1024:.1f} KB"
        if n < 1024 ** 3:
            return f"{n / 1024 ** 2:.1f} MB"
        return f"{n / 1024 ** 3:.2f} GB"

    size_str = f"{_sz(downloaded)} / {_sz(total)}" if total > 0 else _sz(downloaded)
    speed_str = f"{_sz(int(speed))}/s" if speed > 0 else "…"
    return f"{size_str} ({speed_str})"


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
        token: str | None = None,
        runner_to_install: str = "",
        runner_info: dict | None = None,
        installed_runners: list[str] | None = None,
        open_runner_manager: Callable | None = None,
        resolve_runner_info: Callable | None = None,
    ) -> None:
        super().__init__(title=f"Install {entry.name}", content_width=360)
        self._entry = entry
        self._installs = installs
        self._archive_uri = archive_uri
        self._on_success = on_success
        self._token = token
        self._cancel_event = threading.Event()
        self._selected_install = installs[0] if installs else None
        self._runner_to_install = runner_to_install
        self._runner_info = runner_info
        self._installed_runners = installed_runners or []
        self._open_runner_manager = open_runner_manager
        self._resolve_runner_info = resolve_runner_info
        self._runner_row: Adw.ActionRow | None = None

        # Determine whether we need to show the confirm page at all.
        self._needs_confirm = bool(runner_to_install) or len(installs) > 1

        self._build_ui()
        self.connect("closed", lambda _d: self._cancel_event.set())

        # Auto-proceed: skip confirm page when nothing to confirm.
        if not self._needs_confirm:
            GLib.idle_add(self._on_proceed_clicked, None)

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

        # ── Runner group (shown when a runner needs downloading) ──────────
        if self._runner_to_install:
            runner_group = Adw.PreferencesGroup(title="Runner")
            row = Adw.ActionRow(
                title=self._runner_to_install,
                subtitle="Will be downloaded",
            )
            row.add_prefix(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))

            change_btn = Gtk.Button(label="Change")
            change_btn.add_css_class("suggested-action")
            change_btn.set_valign(Gtk.Align.CENTER)
            change_btn.connect("clicked", self._on_runner_change_clicked)
            row.add_suffix(change_btn)

            runner_group.add(row)
            page.add(runner_group)
            self._runner_row = row

        # ── Bottles group ─────────────────────────────────────────────────
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

    def _on_runner_change_clicked(self, _btn) -> None:
        """Open RunnerManagerDialog from the Change button."""
        if not self._open_runner_manager:
            return

        def _on_change(runner_name: str) -> None:
            if runner_name in self._installed_runners:
                # Already installed — no download needed.
                self._runner_to_install = ""
                self._runner_info = None
                if self._runner_row:
                    self._runner_row.set_title(runner_name)
                    self._runner_row.set_subtitle("Installed")
            else:
                # Different uninstalled runner — resolve new download info.
                self._runner_to_install = runner_name
                if self._resolve_runner_info:
                    self._runner_info = self._resolve_runner_info(runner_name)
                if self._runner_row:
                    self._runner_row.set_title(runner_name)
                    self._runner_row.set_subtitle("Will be downloaded")

        self._open_runner_manager(_on_change)

    def _build_progress_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(24)
        box.set_margin_end(24)

        self._phase_label = Gtk.Label(label="Downloading", xalign=0)
        self._phase_label.add_css_class("dim-label")
        box.append(self._phase_label)

        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(True)
        self._progress_bar.set_fraction(0.0)
        # Prevent the bar from requesting extra width to fit text — the dialog
        # width is fixed by content_width; the bar fills that space.
        self._progress_bar.set_size_request(0, -1)
        box.append(self._progress_bar)

        self._cancel_body_btn = Gtk.Button(label="Cancel")
        self._cancel_body_btn.set_halign(Gtk.Align.CENTER)
        self._cancel_body_btn.set_margin_top(6)
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

        self._pulse_id: int | None = None

        def _set_phase(label: str) -> None:
            GLib.idle_add(self._on_phase_change, label)

        def _dl_progress(fraction: float) -> None:
            GLib.idle_add(self._progress_bar.set_fraction, fraction)

        def _dl_stats(downloaded: int, total: int, speed: float) -> None:
            GLib.idle_add(self._progress_bar.set_text, _fmt_dl_stats(downloaded, total, speed))

        def _verify_progress(fraction: float) -> None:
            GLib.idle_add(self._progress_bar.set_fraction, fraction)

        def _inst_progress(fraction: float) -> None:
            GLib.idle_add(self._progress_bar.set_fraction, fraction)

        _last_name_t: list[float] = [0.0]

        def _extract_name(filename: str) -> None:
            now = time.monotonic()
            if now - _last_name_t[0] >= 0.08:
                _last_name_t[0] = now
                GLib.idle_add(self._progress_bar.set_text, _trunc_filename(filename))

        def _run() -> None:
            try:
                # ── Runner download phase (if needed) ─────────────────────
                if self._runner_to_install and self._runner_info:
                    from cellar.views.install_runner import _Cancelled, _download_and_extract_runner

                    _set_phase("Downloading runner\u2026")

                    def _runner_progress(fraction: float) -> None:
                        GLib.idle_add(self._progress_bar.set_fraction, fraction)

                    def _runner_stats(downloaded: int, total: int, speed: float) -> None:
                        GLib.idle_add(self._progress_bar.set_text, _fmt_dl_stats(downloaded, total, speed))

                    def _runner_phase(text: str) -> None:
                        GLib.idle_add(self._on_phase_change, text)

                    _last_runner_name_t: list[float] = [0.0]

                    def _runner_name(filename: str) -> None:
                        now = time.monotonic()
                        if now - _last_runner_name_t[0] >= 0.08:
                            _last_runner_name_t[0] = now
                            GLib.idle_add(self._progress_bar.set_text, _trunc_filename(filename))

                    try:
                        _download_and_extract_runner(
                            url=self._runner_info["url"],
                            checksum=self._runner_info["checksum"],
                            target_dir=self._runner_info["target_dir"],
                            progress_cb=_runner_progress,
                            stats_cb=_runner_stats,
                            phase_cb=_runner_phase,
                            name_cb=_runner_name,
                            cancel_event=self._cancel_event,
                        )
                    except _Cancelled:
                        from cellar.backend.installer import InstallCancelled as _IC
                        raise _IC("Runner download cancelled")

                    if self._cancel_event.is_set():
                        raise InstallCancelled("Runner download cancelled")

                # ── App install phase ─────────────────────────────────────
                _set_phase("Downloading package\u2026")
                bottle_name = install_app(
                    self._entry,
                    self._archive_uri,
                    self._selected_install,
                    download_cb=_dl_progress,
                    download_stats_cb=_dl_stats,
                    install_cb=_inst_progress,
                    extract_name_cb=_extract_name,
                    phase_cb=_set_phase,
                    verify_cb=_verify_progress,
                    cancel_event=self._cancel_event,
                    token=self._token,
                )
                GLib.idle_add(self._on_done, bottle_name)
            except InstallCancelled:
                GLib.idle_add(self._on_cancelled)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_error, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _on_phase_change(self, label: str) -> None:
        """Reset bar and update label on phase transition (runs on UI thread)."""
        # Stop any active pulse
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self._phase_label.set_text(label)
        if "Copying" in label:
            # Indeterminate pulse for copytree
            self._progress_bar.set_fraction(0.0)
            self._progress_bar.set_show_text(False)
            self._pulse_id = GLib.timeout_add(80, self._do_pulse)
        else:
            self._progress_bar.set_fraction(0.0)
            self._progress_bar.set_show_text(True)
            # Clear any download stats text from the previous phase.
            self._progress_bar.set_text("")

    def _do_pulse(self) -> bool:
        self._progress_bar.pulse()
        return True  # keep calling

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
# Screenshot fullscreen dialog
# ---------------------------------------------------------------------------


class ScreenshotDialog(Adw.Dialog):
    """Fullscreen screenshot viewer with its own carousel and arrow navigation."""

    def __init__(self, paths: list[str], start_index: int = 0) -> None:
        super().__init__(content_width=1000, content_height=700)
        self._paths = paths
        self._start_index = start_index
        self._build_ui()

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        toolbar.set_content(body)

        carousel = Adw.Carousel(allow_scroll_wheel=True, reveal_duration=200, spacing=12)
        for path in self._paths:
            pic = Gtk.Picture.new_for_filename(path)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_can_shrink(True)
            pic.set_vexpand(True)
            carousel.append(pic)

        overlay = Gtk.Overlay(child=carousel, vexpand=True)
        body.append(overlay)

        multiple = len(self._paths) > 1

        if multiple:
            prev_btn = Gtk.Button(icon_name="go-previous-symbolic")
            prev_btn.add_css_class("osd")
            prev_btn.add_css_class("circular")
            prev_btn.set_halign(Gtk.Align.START)
            prev_btn.set_valign(Gtk.Align.CENTER)
            prev_btn.set_margin_start(12)
            prev_btn.connect("clicked", lambda _b: carousel.scroll_to(
                carousel.get_nth_page(max(0, round(carousel.get_position()) - 1)),
                True,
            ))
            overlay.add_overlay(prev_btn)

            next_btn = Gtk.Button(icon_name="go-next-symbolic")
            next_btn.add_css_class("osd")
            next_btn.add_css_class("circular")
            next_btn.set_halign(Gtk.Align.END)
            next_btn.set_valign(Gtk.Align.CENTER)
            next_btn.set_margin_end(12)
            next_btn.connect("clicked", lambda _b: carousel.scroll_to(
                carousel.get_nth_page(min(
                    carousel.get_n_pages() - 1,
                    round(carousel.get_position()) + 1,
                )),
                True,
            ))
            overlay.add_overlay(next_btn)

            def _update_arrows(*_args) -> None:
                page = round(carousel.get_position())
                prev_btn.set_visible(page > 0)
                next_btn.set_visible(page < carousel.get_n_pages() - 1)

            carousel.connect("page-changed", _update_arrows)

            dots = Adw.CarouselIndicatorDots(carousel=carousel)
            body.append(dots)

        # Scroll to the clicked screenshot once the carousel is realized.
        if self._start_index > 0:
            def _scroll_to_start(*_args) -> None:
                carousel.scroll_to(
                    carousel.get_nth_page(self._start_index), False,
                )

            carousel.connect("realize", _scroll_to_start)

        if multiple:
            # Update arrow visibility after initial scroll.
            carousel.connect("realize", lambda *_: GLib.idle_add(_update_arrows))

        self.set_child(toolbar)


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

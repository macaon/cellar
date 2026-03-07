"""App detail view — shown when the user activates an app card."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gio, Gtk, Pango

from cellar.models.app_entry import AppEntry
from cellar.utils.images import load_and_crop, load_and_fit, load_logo, to_texture
from cellar.views.widgets import make_progress_page
from cellar.utils.paths import short_path as _short_path
from cellar.utils.async_work import run_in_background
from cellar.utils.progress import fmt_stats as _fmt_dl_stats, trunc_middle as _trunc_filename

log = logging.getLogger(__name__)

_ICON_SIZE = 96


def _html_to_pango(text: str) -> str:
    """Convert a limited HTML subset to Pango markup for display.

    Supported tags: <b>, <i>, <h1>, <h2>, <h3>, <li>, <hr>, <br>, <p>, <ul>.
    All other tags and their content are stripped; bare text is XML-escaped.
    """
    # Split on tags, preserving them as tokens
    tokens = re.split(r"(<[^>]+>)", text)
    out: list[str] = []
    skip_until: str | None = None  # set when inside an unsupported container

    for tok in tokens:
        if not tok:
            continue
        if tok.startswith("<"):
            low = tok.lower().strip("<>").split()[0].lstrip("/")
            closing = tok.lstrip("<").startswith("/")
            if skip_until:
                if closing and low == skip_until:
                    skip_until = None
                continue
            if low == "b":
                out.append("</b>" if closing else "<b>")
            elif low == "i":
                out.append("</i>" if closing else "<i>")
            elif low in ("h1", "h2", "h3"):
                out.append("</b></big>\n" if closing else "\n<big><b>")
            elif low == "li":
                if not closing:
                    out.append("\n\u2022\u00a0")
            elif low == "hr":
                out.append("\n\u2015\u2015\u2015\u2015\u2015\u2015\u2015\u2015\n")
            elif low in ("br",):
                out.append("\n")
            elif low == "p":
                if closing:
                    out.append("\n")
            elif low in ("ul", "ol", "li", "div", "span"):
                pass  # silently skip structural tags
            else:
                # Unknown tag — skip it (don't try to emit it as Pango)
                pass
        else:
            out.append(GLib.markup_escape_text(tok))

    result = "".join(out).strip()
    # Collapse 3+ consecutive newlines to 2
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result


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
        self._is_installed = is_installed
        self._installed_record = installed_record
        self._on_install_done = on_install_done
        self._on_remove_done = on_remove_done
        self._on_update_done = on_update_done
        _cat_crc = entry.archive_crc32 or ""
        _stored_crc = (installed_record or {}).get("archive_crc32") or ""
        self._has_update = (
            is_installed
            and bool(_cat_crc and _stored_crc and _cat_crc != _stored_crc)
        )
        self._screenshot_paths: list[str] = []
        # Runner compatibility state
        self._installed_runners: list[str] = []
        self._runner_override: str | None = (
            (installed_record or {}).get("runner_override") if is_installed else None
        )
        self._runner_label: Gtk.Label | None = None
        self._runner_warning_icon: Gtk.Image | None = None
        self._runners_loaded: bool = False
        # Base image resolution — populated once by _resolve_base_async.
        # Observers registered before resolution completes are called on idle.
        self._base_sz: int = 0
        self._base_installed: bool | None = None  # None = not yet resolved
        self._base_resolve_cbs: list[Callable] = []

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
        header.set_title_widget(Gtk.Label())  # no centred title — mimics GNOME Software

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

        # App header (width-clamped).
        header_clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
        header_clamp.set_child(self._make_app_header())
        outer.append(header_clamp)

        # Separator between header and screenshots/content (always shown).
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Screenshots band — full-width, flanked by a second separator.
        screenshots_widget = self._make_screenshots()
        if screenshots_widget:
            outer.append(screenshots_widget)
            second_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            second_sep.set_visible(screenshots_widget.get_visible())
            outer.append(second_sep)

            def _on_screenshots_visible(widget, _pspec):
                second_sep.set_visible(widget.get_visible())

            screenshots_widget.connect("notify::visible", _on_screenshots_visible)

        # Content (width-clamped).
        content_clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
        content_clamp.set_margin_bottom(32)
        outer.append(content_clamp)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=50)
        content_box.set_margin_top(18)
        content_box.set_margin_start(18)
        content_box.set_margin_end(18)
        content_clamp.set_child(content_box)

        if e.description:
            content_box.append(self._make_description())

        content_box.append(self._make_info_cards())

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
            self._install_btn_label.set_label("Open")
            btn.add_css_class("suggested-action")
            btn.set_sensitive(True)
            btn.set_tooltip_text("")
            self._remove_btn.set_visible(True)
            self._update_indicator.set_visible(self._has_update)
            self._update_indicator.set_tooltip_text(
                "Update available — see Options menu" if self._has_update else ""
            )
        else:
            self._install_btn_label.set_label("Install")
            btn.add_css_class("suggested-action")
            btn.set_sensitive(True)
            btn.set_tooltip_text("")
            self._remove_btn.set_visible(False)
            self._update_indicator.set_visible(False)
        self._gear_btn.set_visible(self._is_installed)
        if self._is_installed:
            self._refresh_gear_menu()

    def _on_install_clicked(self, _btn) -> None:
        if self._is_installed:
            self._on_open_clicked()
            return
        if self._entry.platform == "linux":
            self._proceed_to_install()
            return
        bw = self._entry.built_with
        required_runner = (bw.runner if bw else "") or ""
        effective = self._runner_override or required_runner
        runner_to_install = ""
        if required_runner and self._runners_loaded and effective not in self._installed_runners:
            runner_to_install = effective
        # Always show confirm page for Windows apps (runner selection)
        self._proceed_to_install(runner_to_install=runner_to_install)

    def _resolve_runner_info(self, runner_name: str) -> dict | None:
        """Return download info dict for *runner_name*, or ``None`` if unavailable."""
        from cellar.backend import runners
        from cellar.backend.umu import runners_dir

        info = runners.get_release_info(runner_name)
        if not info:
            return None
        return {
            "url": info.get("url", ""),
            "checksum": info.get("checksum", ""),
            "target_dir": runners_dir() / runner_name,
        }

    def _proceed_to_install(self, *, runner_to_install: str = "") -> None:
        archive_uri = self._resolve(self._entry.archive) if self._entry.archive else ""
        runner_info = self._resolve_runner_info(runner_to_install) if runner_to_install else None

        # Resolve base archive for delta installs.
        runner = self._get_base_runner()
        base_entry, base_archive_uri = self._find_base_entry(runner) if runner else (None, "")

        def _open_runner_manager(on_change):
            """Open RunnerManagerDialog from the Change button inside InstallProgressDialog."""
            self._open_runner_manager(
                required_runner=(self._entry.built_with.runner if self._entry.built_with else "") or "",
                on_confirm=on_change,
            )

        dialog = InstallProgressDialog(
            entry=self._entry,
            archive_uri=archive_uri,
            on_success=self._on_install_success,
            token=self._token,
            runner_to_install=runner_to_install,
            runner_info=runner_info,
            installed_runners=list(self._installed_runners),
            open_runner_manager=_open_runner_manager,
            resolve_runner_info=self._resolve_runner_info,
            base_entry=base_entry,
            base_archive_uri=base_archive_uri,
        )
        dialog.present(self.get_root())

    def _on_install_success(self, prefix_dir: str, install_path: str = "", runner: str = "", install_size: int = 0) -> None:
        self._is_installed = True
        self._installed_record = {"prefix_dir": prefix_dir, "install_path": install_path, "install_size": install_size}
        self._update_install_button()
        if self._on_install_done:
            self._on_install_done(prefix_dir, install_path, runner, install_size)
        # Persist runner override when the user pre-selected a different runner.
        built_with_runner = (self._entry.built_with.runner if self._entry.built_with else "") or ""
        if self._runner_override and self._runner_override != built_with_runner:
            from cellar.backend import database
            database.set_runner_override(self._entry.id, self._runner_override)

    def _on_open_clicked(self) -> None:
        if self._entry.platform == "linux":
            self._launch_linux_app()
            return
        from cellar.backend.umu import launch_app
        from cellar.backend import database
        rec = self._installed_record or {}
        runner_name = rec.get("runner_override") or rec.get("runner") or ""
        if not runner_name:
            db_rec = database.get_installed(self._entry.id) or {}
            runner_name = db_rec.get("runner_override") or db_rec.get("runner") or ""
        launch_app(
            app_id=self._entry.id,
            entry_point=self._entry.entry_point or "",
            runner_name=runner_name,
            steam_appid=self._entry.steam_appid,
        )

    def _launch_linux_app(self) -> None:
        """Launch a native Linux app by executing its entry_point directly."""
        import subprocess as _sp
        if not self._entry.entry_point:
            return
        from cellar.backend.umu import native_dir
        exe = native_dir() / self._entry.id / self._entry.entry_point
        _sp.Popen([str(exe)], start_new_session=True)

    def _on_remove_clicked(self) -> None:
        prefix_path = None
        if self._entry.platform == "linux":
            from cellar.backend.umu import native_dir
            candidate = native_dir() / self._entry.id
            if candidate.is_dir():
                prefix_path = candidate
        else:
            from cellar.backend.umu import prefixes_dir
            candidate = prefixes_dir() / self._entry.id
            if candidate.is_dir():
                prefix_path = candidate
        dialog = RemoveDialog(
            entry=self._entry,
            prefix_path=prefix_path,
            on_confirm=self._on_remove_confirmed,
        )
        dialog.present(self.get_root())

    def _on_update_clicked(self, _btn) -> None:
        from cellar.views.update_app import UpdateDialog
        from cellar.backend.umu import prefixes_dir

        prefix_path = prefixes_dir() / self._entry.id
        if not prefix_path.is_dir():
            log.error("Could not locate prefix directory for %s", self._entry.id)
            return

        archive_uri = self._resolve(self._entry.archive) if self._entry.archive else ""

        # Resolve base archive for delta updates.
        runner = self._get_base_runner()
        base_entry, base_archive_uri = self._find_base_entry(runner) if runner else (None, "")

        dialog = UpdateDialog(
            entry=self._entry,
            installed_record=self._installed_record or {},
            prefix_path=prefix_path,
            archive_uri=archive_uri,
            on_success=self._on_update_success,
            base_entry=base_entry,
            base_archive_uri=base_archive_uri,
            token=self._token,
        )
        dialog.present(self.get_root())

    def _on_update_success(self, install_size: int = 0) -> None:
        self._has_update = False
        if self._installed_record is not None and install_size:
            self._installed_record = {**self._installed_record, "install_size": install_size}
        self._update_install_button()
        if self._on_update_done:
            self._on_update_done(install_size)

    def _on_remove_confirmed(self) -> None:
        import shutil
        from cellar.backend import database

        if self._entry.platform == "linux":
            from cellar.backend.umu import native_dir
            candidate = native_dir() / self._entry.id
            if candidate.is_dir():
                try:
                    shutil.rmtree(candidate)
                except Exception as exc:
                    log.error("Failed to remove app dir %s: %s", candidate, exc)
        else:
            from cellar.backend.umu import prefixes_dir
            candidate = prefixes_dir() / self._entry.id
            if candidate.is_dir():
                try:
                    shutil.rmtree(candidate)
                except Exception as exc:
                    log.error("Failed to remove prefix %s: %s", candidate, exc)
            else:
                log.warning("Prefix %s not found on disk; cleaning up DB only", candidate)

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

        update_act = Gio.SimpleAction.new("update", None)
        update_act.connect("activate", lambda *_: self._on_update_clicked(None))
        ag.add_action(update_act)

        open_folder_act = Gio.SimpleAction.new("open-folder", None)
        open_folder_act.connect("activate", self._on_open_folder_action)
        ag.add_action(open_folder_act)

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
        if self._has_update:
            menu.append("Update", "detail.update")
        if has_desktop_entry(self._entry.id):
            menu.append("Remove Desktop Shortcut", "detail.remove-shortcut")
        else:
            menu.append("Create Desktop Shortcut", "detail.create-shortcut")
        menu.append("Open Install Folder", "detail.open-folder")
        self._gear_btn.set_menu_model(menu)

    def _on_open_folder_action(self, _action, _param) -> None:
        folder = self._get_install_folder()
        if folder:
            Gio.AppInfo.launch_default_for_uri(f"file://{folder}", None)


    def _get_install_folder(self) -> str | None:
        """Return the install folder path for the current entry, or None."""
        if self._entry.platform == "linux":
            from cellar.backend.umu import native_dir
            p = native_dir() / self._entry.id
            return str(p) if p.is_dir() else None
        # Windows app — prefix is at umu prefixes_dir / app_id
        from cellar.backend.umu import prefixes_dir
        p = prefixes_dir() / self._entry.id
        return str(p) if p.is_dir() else None

    def _on_create_shortcut(self, _action, _param) -> None:
        from cellar.utils.desktop import create_desktop_entry

        icon_source: str | None = None
        if self._entry.icon:
            resolved = self._resolve(self._entry.icon)
            if resolved and Path(resolved).is_file():
                icon_source = resolved

        if self._entry.platform == "linux":
            from cellar.backend.umu import native_dir
            try:
                create_desktop_entry(
                    entry=self._entry,
                    bottle_name=self._entry.id,
                    icon_source=icon_source,
                    install_path=str(native_dir()),
                )
                self._refresh_gear_menu()
                self._add_toast(f"Shortcut created for {self._entry.name}")
            except Exception as exc:
                log.error("Failed to create desktop entry: %s", exc)
                self._add_toast("Failed to create shortcut")
            return

        # Windows app — umu desktop shortcut
        try:
            create_desktop_entry(
                entry=self._entry,
                bottle_name=self._entry.id,
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
        bw = self._entry.built_with
        if not bw or not bw.runner:
            return

        def _work():
            from cellar.backend import runners
            return runners.installed_runners()

        run_in_background(_work, on_done=self._on_runners_loaded)

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
        from cellar.backend import runners
        from cellar.backend.umu import runners_dir
        from cellar.views.install_runner import InstallRunnerDialog

        bw = self._entry.built_with
        effective_name = runner_name or (bw.runner if bw else "") or ""
        if not effective_name:
            return

        info = runners.get_release_info(effective_name)
        if not info:
            return
        url = info.get("url", "")
        checksum = info.get("checksum", "")
        target_dir = runners_dir() / effective_name

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
        from cellar.backend import runners
        from cellar.backend.umu import runners_dir

        bw = self._entry.built_with
        built_with_runner = (bw.runner if bw else "") or ""
        current = self._runner_override or built_with_runner

        available_releases = runners.fetch_releases()
        rdir = runners_dir()

        effective_on_confirm = on_confirm if on_confirm is not None else self._on_runner_selected

        def _on_install(runner_name: str) -> None:
            self._open_install_runner_dialog(runner_name, on_done=effective_on_confirm)

        RunnerManagerDialog(
            installed_runners=self._installed_runners,
            available_releases=available_releases,
            runners_dir=rdir,
            current_runner=current,
            required_runner=required_runner or built_with_runner,
            on_confirm=effective_on_confirm,
            on_install_runner=_on_install,
        ).present(self.get_root())

    def _on_runner_selected(self, runner_name: str) -> None:
        self._runner_override = runner_name
        if self._runner_label:
            self._runner_label.set_label(runner_name)
        if self._runner_warning_icon and runner_name in self._installed_runners:
            self._runner_warning_icon.set_visible(False)
        # Persist the runner override to the database.
        if self._is_installed:
            from cellar.backend import database
            database.set_runner_override(self._entry.id, runner_name)

    # ------------------------------------------------------------------
    # Section builders
    # ------------------------------------------------------------------

    def _make_app_header(self) -> Gtk.Widget:
        e = self._entry
        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=18,
            margin_start=18,
            margin_end=18,
            margin_top=18,
            margin_bottom=18,
        )

        dev_parts: list[str] = []
        if e.developer:
            dev_parts.append(e.developer)
        if e.publisher and e.publisher != e.developer:
            dev_parts.append(e.publisher)

        # Logo column: logo image, with developer credit below when title is hidden.
        # Falls back to a square icon when no logo is set.
        dev_below_logo = e.logo and e.hide_title
        if e.logo:
            logo_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            logo_col.set_halign(Gtk.Align.START)
            logo_col.set_valign(Gtk.Align.CENTER)
            logo = self._make_logo_widget(e.logo, _ICON_SIZE)
            logo.set_halign(Gtk.Align.CENTER)
            logo_col.append(logo)
            if dev_below_logo and dev_parts:
                dev_lbl = Gtk.Label(label=" · ".join(dev_parts))
                dev_lbl.add_css_class("dim-label")
                dev_lbl.add_css_class("caption")
                dev_lbl.set_halign(Gtk.Align.CENTER)
                dev_lbl.set_wrap(True)
                logo_col.append(dev_lbl)
            box.append(logo_col)
        else:
            icon = self._make_icon(e.icon, _ICON_SIZE)
            icon.set_valign(Gtk.Align.CENTER)
            box.append(icon)

        # Meta column: name + developer (when title is visible).
        meta = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        meta.set_hexpand(True)
        meta.set_valign(Gtk.Align.CENTER)
        box.append(meta)

        if not (e.hide_title and e.logo):
            name_lbl = Gtk.Label(label=e.name)
            name_lbl.add_css_class("title-1")
            name_lbl.set_halign(Gtk.Align.START)
            name_lbl.set_wrap(True)
            meta.append(name_lbl)

        if not dev_below_logo and dev_parts:
            dev_lbl = Gtk.Label(label=" · ".join(dev_parts))
            dev_lbl.add_css_class("dim-label")
            dev_lbl.set_halign(Gtk.Align.START)
            meta.append(dev_lbl)

        # Right column: action buttons + update + repo button.
        right = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            valign=Gtk.Align.CENTER,
            halign=Gtk.Align.END,
        )
        box.append(right)

        # Action row: Install/Open + Gear + Remove.
        action_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        right.append(action_row)

        self._install_btn = Gtk.Button()
        self._install_btn.set_size_request(105, 34)
        self._install_btn.connect("clicked", self._on_install_clicked)
        # Inner box: warning icon (update indicator) + label.
        _btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4,
                           halign=Gtk.Align.CENTER)
        self._update_indicator = Gtk.Image.new_from_icon_name("software-update-urgent-symbolic")
        self._update_indicator.set_visible(False)
        _btn_box.append(self._update_indicator)
        self._install_btn_label = Gtk.Label()
        _btn_box.append(self._install_btn_label)
        self._install_btn.set_child(_btn_box)
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

        right.append(self._make_repo_button())

        self._update_install_button()
        self._setup_gear_actions()

        return box

    def _make_repo_button(self) -> Gtk.Widget:
        """Return a flat MenuButton showing the current source repo.

        Single repo: popover shows name (heading) + URI (dim-label, selectable).
        Multiple repos: popover shows radio buttons for source selection.
        Always shown (even with a single repo), so the user knows the source.
        """
        first = self._source_repos[0] if self._source_repos else None

        self._source_label = Gtk.Label(
            label=first.name if first else "No source",
        )
        self._source_label.add_css_class("dim-label")
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_weight_new(Pango.Weight.NORMAL))
        self._source_label.set_attributes(attrs)

        btn_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=4,
            halign=Gtk.Align.CENTER,
        )
        btn_box.append(self._source_label)
        btn_box.append(Gtk.Image.new_from_icon_name("pan-down-symbolic"))

        if len(self._source_repos) <= 1:
            # Single-repo popover: show name + URI info.
            pop_box = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL,
                spacing=4,
                margin_top=12,
                margin_bottom=12,
                margin_start=12,
                margin_end=12,
            )
            if first:
                name_lbl = Gtk.Label(label=first.name)
                name_lbl.add_css_class("heading")
                name_lbl.set_xalign(0)
                pop_box.append(name_lbl)

                uri_lbl = Gtk.Label(label=getattr(first, "uri", ""))
                uri_lbl.add_css_class("dim-label")
                uri_lbl.set_xalign(0)
                uri_lbl.set_selectable(True)
                uri_lbl.set_wrap(True)
                pop_box.append(uri_lbl)
        else:
            # Multi-repo popover: radio buttons for source selection.
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
        self._source_popover = popover

        menu_btn = Gtk.MenuButton(popover=popover)
        menu_btn.set_child(btn_box)
        menu_btn.add_css_class("flat")
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
        e = self._entry
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        if e.description:
            lbl = Gtk.Label()
            lbl.set_markup(_html_to_pango(e.description))
            lbl.set_wrap(True)
            lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
            lbl.set_xalign(0)
            box.append(lbl)

        return box

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
        wrapper.add_css_class("screenshots-band")

        if cached_paths:
            self._screenshot_paths = cached_paths
            self._populate_screenshots(wrapper, cached_paths)
            return wrapper

        # Slow path: some images need fetching — resolve async and fill in later.
        wrapper.set_visible(False)

        def _work():
            resolved = []
            for s in screenshots:
                path = self._resolve(s)
                if os.path.isfile(path):
                    resolved.append(path)
            return resolved

        def _on_resolved(paths: list[str]) -> None:
            self._screenshot_paths = paths
            if paths:
                self._populate_screenshots(wrapper, paths)
                wrapper.set_visible(True)

        run_in_background(_work, on_done=_on_resolved)
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
            pic.set_overflow(Gtk.Overflow.HIDDEN)
            pic.set_margin_start(8)
            pic.set_margin_end(8)
            pic.set_margin_top(4)
            pic.set_margin_bottom(10)
            pic.add_css_class("screenshot-pic")
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

    def _make_info_cards(self) -> Gtk.Box:
        """Return a horizontal row of info cards (download, install, wine, category)."""
        e = self._entry

        # Outer container acts as the card — rounded corners on the four outer
        # corners only.  Individual cells are plain boxes separated by a 1px
        # dark strip matching the window background, mimicking GNOME Software.
        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        outer.add_css_class("card")
        _count = [0]

        def _sep() -> Gtk.Widget:
            s = Gtk.Box()
            s.add_css_class("info-card-sep")
            s.set_size_request(1, -1)
            return s

        def _add(widget: Gtk.Widget) -> None:
            if _count[0] > 0:
                outer.append(_sep())
            outer.append(widget)
            _count[0] += 1

        def _make_interactive(card: Gtk.Box, on_click: Callable) -> None:
            """Attach hover highlight and click handler to a cell."""
            card.add_css_class("info-cell-interactive")
            card.set_cursor(Gdk.Cursor.new_from_name("pointer"))
            motion = Gtk.EventControllerMotion()
            motion.connect("enter", lambda *_: card.add_css_class("hovered"))
            motion.connect("leave", lambda *_: card.remove_css_class("hovered"))
            card.add_controller(motion)
            click = Gtk.GestureClick()
            click.connect("released", lambda _g, _n, _x, _y: on_click())
            card.add_controller(click)

        def _simple_card(icon_name: str, value: str, label: str) -> tuple[Gtk.Box, Gtk.Label]:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("info-cell")
            card.set_hexpand(True)

            icon = Gtk.Image.new_from_icon_name(icon_name)
            icon.set_pixel_size(24)
            icon.set_halign(Gtk.Align.CENTER)
            card.append(icon)

            val_lbl = Gtk.Label(label=value)
            val_lbl.add_css_class("heading")
            val_lbl.set_halign(Gtk.Align.CENTER)
            val_lbl.set_wrap(True)
            card.append(val_lbl)

            sub_lbl = Gtk.Label(label=label)
            sub_lbl.add_css_class("dim-label")
            sub_lbl.add_css_class("caption")
            sub_lbl.set_halign(Gtk.Align.CENTER)
            card.append(sub_lbl)

            return card, val_lbl

        if self._is_installed:
            stored_size = (self._installed_record or {}).get("install_size") or 0
            if stored_size:
                _add(_simple_card("drive-harddisk-symbolic", _fmt_bytes(stored_size), "Install size")[0])
        else:
            if e.archive_size > 0:
                dl_card, dl_val_lbl = _simple_card(
                    "folder-download-symbolic", _fmt_bytes(e.archive_size), "Download",
                )
                _make_interactive(dl_card, self._show_download_dialog)
                _add(dl_card)
                self._resolve_base_async(dl_val_lbl)

            if e.install_size_estimate > 0:
                _add(_simple_card(
                    "drive-harddisk-symbolic",
                    _fmt_bytes(e.install_size_estimate),
                    "Disk space",
                )[0])

        if e.platform == "linux":
            _add(_simple_card("penguin-alt-symbolic", "Native", "Linux")[0])
        elif e.built_with:
            wine_card = self._make_wine_card()
            if e.built_with.dxvk or e.built_with.vkd3d:
                _make_interactive(wine_card, self._show_wine_dialog)
            _add(wine_card)

        if e.version:
            _add(_simple_card("software-update-available-symbolic", e.version, "Version")[0])

        if e.release_year:
            _add(_simple_card("x-office-calendar-symbolic", str(e.release_year), "Released")[0])

        if e.category:
            _add(_simple_card(e.category_icon or "tag-symbolic", e.category, "Category")[0])

        first = outer.get_first_child()
        last = outer.get_last_child()
        if first:
            first.add_css_class("info-cell-first")
        if last and last is not first:
            last.add_css_class("info-cell-last")

        return outer

    def _make_wine_card(self) -> Gtk.Box:
        """Return a card showing Wine runner and a change button."""
        bw = self._entry.built_with
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("info-cell")
        card.set_hexpand(True)

        icon = Gtk.Image.new_from_icon_name("system-run-symbolic")
        icon.set_pixel_size(24)
        icon.set_halign(Gtk.Align.CENTER)
        card.append(icon)

        runner_name = self._runner_override or (bw.runner if bw else "") or ""
        runner_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        runner_row.set_halign(Gtk.Align.CENTER)
        card.append(runner_row)

        self._runner_label = Gtk.Label(label=runner_name)
        self._runner_label.add_css_class("heading")
        runner_row.append(self._runner_label)

        self._runner_warning_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        self._runner_warning_icon.add_css_class("warning")
        self._runner_warning_icon.set_visible(False)
        runner_row.append(self._runner_warning_icon)

        bottom_lbl = Gtk.Label(label="Wine")
        bottom_lbl.add_css_class("dim-label")
        bottom_lbl.add_css_class("caption")
        bottom_lbl.set_halign(Gtk.Align.CENTER)
        card.append(bottom_lbl)

        return card

    def _get_base_runner(self) -> str:
        """Return the effective base runner key for this entry."""
        e = self._entry
        return e.base_runner or (e.built_with.runner if e.built_with else "")

    def _find_base_entry(self, runner: str) -> tuple[object | None, str]:
        """Return (BaseEntry, archive_uri) for *runner*, or (None, "") if not found."""
        for repo in self._source_repos:
            try:
                bases = repo.fetch_bases()
                if runner in bases:
                    entry = bases[runner]
                    return entry, repo.resolve_asset_uri(entry.archive)
            except Exception:
                pass
        return None, ""

    def _resolve_base_async(self, val_lbl: Gtk.Label) -> None:
        """Resolve base image size + installed status once; cache on self for the dialog."""
        base_runner = self._get_base_runner()
        if not base_runner:
            return
        app_size = self._entry.archive_size

        def _work():
            from cellar.backend.base_store import is_base_installed
            installed = is_base_installed(base_runner)
            base_entry, _ = self._find_base_entry(base_runner)
            base_sz = base_entry.archive_size if base_entry else 0
            return installed, base_sz

        def _apply(result) -> None:
            installed, base_sz = result
            self._base_installed = installed
            self._base_sz = base_sz
            if not installed and base_sz:
                val_lbl.set_label(_fmt_bytes(app_size + base_sz))
            for cb in self._base_resolve_cbs:
                cb(installed, base_sz)
            self._base_resolve_cbs.clear()

        run_in_background(_work, on_done=_apply)

    def _show_download_dialog(self) -> None:
        """Show a download size breakdown: header pill + per-component rows."""
        e = self._entry
        base_runner = self._get_base_runner()

        def _pill(text: str, *, large: bool = False) -> Gtk.Label:
            lbl = Gtk.Label(label=text)
            lbl.add_css_class("download-pill")
            if large:
                lbl.add_css_class("download-pill-large")
            return lbl

        def _row(pill_lbl: Gtk.Label, title: str, subtitle: str) -> Adw.ActionRow:
            wrap = Gtk.Box(valign=Gtk.Align.FILL, margin_end=6)
            pill_lbl.set_valign(Gtk.Align.CENTER)
            wrap.append(pill_lbl)
            r = Adw.ActionRow(title=title, subtitle=subtitle)
            r.add_prefix(wrap)
            return r

        # ── Header: total size pill ──────────────────────────────────
        # Compute initial value: if base is already resolved and missing, add its size.
        if base_runner and self._base_installed is not None:
            _total_sz = e.archive_size + (0 if self._base_installed else self._base_sz)
            _initial_total = _fmt_bytes(_total_sz) if _total_sz else _fmt_bytes(e.archive_size)
        elif base_runner:
            _initial_total = "…"
        else:
            _initial_total = _fmt_bytes(e.archive_size) if e.archive_size else "Unknown"
        total_pill = _pill(_initial_total, large=True)
        total_pill.set_halign(Gtk.Align.CENTER)
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        header.set_halign(Gtk.Align.CENTER)
        header.append(total_pill)
        ds_lbl = Gtk.Label(label="Download Size")
        ds_lbl.add_css_class("heading")
        header.append(ds_lbl)

        # ── Per-component rows ───────────────────────────────────────
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")

        app_pill = _pill(_fmt_bytes(e.archive_size) if e.archive_size else "Unknown")
        listbox.append(_row(app_pill, e.name, "The app itself"))

        base_pill: Gtk.Label | None = None
        base_action_row: Adw.ActionRow | None = None
        if base_runner:
            base_pill = _pill("…")
            base_action_row = _row(base_pill, "Base Image", "…")
            listbox.append(base_action_row)

        # ── Layout ──────────────────────────────────────────────────
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        content.append(header)
        content.append(listbox)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(content)

        dlg = Adw.Dialog(title="Download", content_width=380)
        dlg.set_child(toolbar)
        dlg.present(self)

        # ── Populate base row from cached resolution ─────────────────
        if base_runner and base_pill is not None and base_action_row is not None:
            if self._base_installed is not None:
                # Already resolved — total_pill was pre-computed above; just fill the row.
                sz = self._base_sz
                base_pill.set_label(_fmt_bytes(sz) if sz else "Unknown")
                base_action_row.set_subtitle(_base_status_subtitle(self._base_installed))
            else:
                # Resolution not yet done (rare — dialog opened within ms of page load).
                _p, _r, _t, _app = base_pill, base_action_row, total_pill, e.archive_size

                def _on_resolved(installed: bool, base_sz: int) -> None:
                    total = _app + (0 if installed else base_sz)
                    _p.set_label(_fmt_bytes(base_sz) if base_sz else "Unknown")
                    _r.set_subtitle(_base_status_subtitle(installed))
                    _t.set_label(_fmt_bytes(total) if total else _fmt_bytes(_app))

                self._base_resolve_cbs.append(_on_resolved)

    def _show_wine_dialog(self) -> None:
        """Show Wine component details: runner, DXVK, VKD3D."""
        bw = self._entry.built_with
        if not bw:
            return

        runner_name = self._runner_override or bw.runner or ""

        def _value_row(title: str, value: str) -> Adw.ActionRow:
            r = Adw.ActionRow(title=title)
            lbl = Gtk.Label(label=value)
            lbl.add_css_class("dim-label")
            lbl.set_valign(Gtk.Align.CENTER)
            r.add_suffix(lbl)
            return r

        # ── Runner listbox ────────────────────────────────────────────
        runner_lbl = Gtk.Label(label="Runner")
        runner_lbl.add_css_class("heading")
        runner_lbl.set_halign(Gtk.Align.START)

        runner_listbox = Gtk.ListBox()
        runner_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        runner_listbox.add_css_class("boxed-list")

        runner_row = Adw.ActionRow(title=runner_name or "—")
        _dlg_ref: list[Adw.Dialog] = []
        if self._is_installed and self._entry.lock_runner:
            lock_icon = Gtk.Image.new_from_icon_name("changes-prevent-symbolic")
            lock_icon.add_css_class("dim-label")
            lock_icon.set_valign(Gtk.Align.CENTER)
            runner_row.add_suffix(lock_icon)
        elif self._is_installed and not self._entry.lock_runner:
            change_btn = Gtk.Button(label="Change")
            change_btn.add_css_class("suggested-action")
            change_btn.add_css_class("flat")
            change_btn.set_valign(Gtk.Align.CENTER)
            def _on_change(_b, _ref=_dlg_ref):
                if _ref:
                    _ref[0].close()
                self._on_change_runner_clicked(_b)
            change_btn.connect("clicked", _on_change)
            runner_row.add_suffix(change_btn)
        runner_listbox.append(runner_row)

        # ── Component rows ───────────────────────────────────────────
        components_lbl = Gtk.Label(label="Components")
        components_lbl.add_css_class("heading")
        components_lbl.set_halign(Gtk.Align.START)

        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")

        if bw.dxvk:
            listbox.append(_value_row("DXVK", bw.dxvk))
        if bw.vkd3d:
            listbox.append(_value_row("VKD3D", bw.vkd3d))

        # ── Layout ───────────────────────────────────────────────────
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(18)
        content.set_margin_end(18)
        content.append(runner_lbl)
        content.append(runner_listbox)
        if bw.dxvk or bw.vkd3d:
            content.append(components_lbl)
            content.append(listbox)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(content)

        dlg = Adw.Dialog(title="Wine", content_width=340)
        _dlg_ref.append(dlg)
        dlg.set_child(toolbar)
        dlg.present(self)

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

        def _work():
            if icon_path:
                path = self._resolve(icon_path)
                if os.path.isfile(path):
                    png_bytes = load_and_fit(path, size)
                    if png_bytes is not None:
                        return png_bytes, False
            if fallback_path:
                cover = self._resolve(fallback_path)
                if os.path.isfile(cover):
                    png_bytes = load_and_crop(cover, size, size)
                    if png_bytes is not None:
                        return png_bytes, True
            return None

        def _on_loaded(result) -> None:
            if result is None:
                return
            png_bytes, is_cover = result
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

        run_in_background(_work, on_done=_on_loaded)
        return stack

    def _make_logo_widget(self, rel_path: str, target_height: int) -> Gtk.Widget:
        """Return a widget displaying the transparent logo at *target_height* px tall.

        Crops to tight non-transparent bounds via :func:`load_logo`, so the
        result always has the natural width of the logo content.  Fast path if
        cached; otherwise loads on a background thread and swaps in with a
        crossfade.
        """
        def _build_picture(png_bytes: bytes) -> Gtk.Picture:
            texture = to_texture(png_bytes)
            pic = Gtk.Picture.new_for_paintable(texture)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            pic.set_halign(Gtk.Align.START)
            # Pin both dimensions explicitly so GTK never squashes the widget
            # when a sibling has hexpand=True.
            pic.set_size_request(texture.get_width(), texture.get_height())
            pic.add_css_class("logo-pic")
            return pic

        cached = self._peek(rel_path)
        if cached and os.path.isfile(cached):
            png_bytes = load_logo(cached, target_height)
            if png_bytes is not None:
                return _build_picture(png_bytes)

        # Slow path: invisible placeholder, swap in after background load.
        placeholder = Gtk.Box()
        placeholder.set_size_request(target_height * 2, target_height)

        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(150)
        stack.add_named(placeholder, "placeholder")
        stack.set_halign(Gtk.Align.START)

        logo_path = rel_path

        def _work():
            path = self._resolve(logo_path)
            if os.path.isfile(path):
                return load_logo(path, target_height)
            return None

        def _on_loaded(png_bytes) -> None:
            if png_bytes is None:
                return
            pic = _build_picture(png_bytes)
            stack.add_named(pic, "real")
            stack.set_visible_child_name("real")

        run_in_background(_work, on_done=_on_loaded)
        return stack


# ---------------------------------------------------------------------------
# Runner manager dialog
# ---------------------------------------------------------------------------


class RunnerManagerDialog(Adw.Dialog):
    """Browse, download, delete and (optionally) select a GE-Proton runner.

    Each runner row shows state-based suffix icons:

    * **Installed** — ``folder-open-symbolic`` + ``user-trash-symbolic``.
    * **Not installed** — ``folder-download-symbolic`` (download).

    When *on_confirm* is supplied the dialog acts as a picker: every installed
    runner row gets a radio-button prefix and a *Select* header button confirms
    the choice.
    """

    def __init__(
        self,
        *,
        installed_runners: list[str],
        available_releases: list[dict],
        runners_dir: Path | None,
        current_runner: str = "",
        required_runner: str = "",
        on_confirm: Callable[[str], None] | None = None,
        on_install_runner: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(title="GE-Proton Runners", content_width=500, content_height=500)
        self._installed = set(installed_runners)
        self._available_releases = available_releases
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

        # Build the flat list: available releases + any installed not in releases.
        release_names = [r.get("name", "") for r in self._available_releases]
        extra_installed = sorted(
            (r for r in self._installed if r not in release_names),
            reverse=True,
        )
        all_runners = release_names + extra_installed

        grp = Adw.PreferencesGroup()
        page.add(grp)

        if all_runners:
            for runner in all_runners:
                grp.add(self._make_runner_row(runner, is_installed=runner in self._installed))
        else:
            grp.add(Adw.ActionRow(
                title="No runners found",
                subtitle="Check your internet connection and try again.",
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
            folder_btn = Gtk.Button(icon_name="folder-open-symbolic")
            folder_btn.add_css_class("flat")
            folder_btn.set_valign(Gtk.Align.CENTER)
            folder_btn.set_tooltip_text("Open runner folder")
            folder_btn.set_sensitive(self._runners_dir is not None)
            folder_btn.connect("clicked", self._on_open_folder, runner)
            row.add_suffix(folder_btn)

            trash_btn = Gtk.Button(icon_name="user-trash-symbolic")
            trash_btn.add_css_class("flat")
            trash_btn.set_valign(Gtk.Align.CENTER)
            trash_btn.set_tooltip_text("Delete runner")
            trash_btn.set_sensitive(self._runners_dir is not None)
            trash_btn.connect("clicked", self._on_delete, runner)
            row.add_suffix(trash_btn)
        else:
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

        def _work():
            shutil.rmtree(path)

        def _on_err(msg: str) -> None:
            log.error("Failed to delete runner %s: %s", runner, msg)
            self.close()

        run_in_background(_work, on_done=lambda _: self.close(), on_error=_on_err)

    def _on_download(self, _btn, runner: str) -> None:
        self.close()
        if self._on_install_runner:
            self._on_install_runner(runner)


# ---------------------------------------------------------------------------
# Install progress dialog
# ---------------------------------------------------------------------------





class InstallProgressDialog(Adw.Dialog):
    """Two-phase install dialog: confirmation → progress.

    Phase 1 (confirm): shown for Linux apps (install path) and when a runner
    needs downloading.  Skipped automatically for simple Windows installs.

    Phase 2 (progress): background install with a progress bar and a body
    Cancel button.
    """

    def __init__(
        self,
        *,
        entry: AppEntry,
        archive_uri: str,
        on_success: Callable,  # (prefix_dir: str, install_path: str, runner: str, install_size: int) -> None
        token: str | None = None,
        runner_to_install: str = "",
        runner_info: dict | None = None,
        installed_runners: list[str] | None = None,
        open_runner_manager: Callable | None = None,
        resolve_runner_info: Callable | None = None,
        base_entry=None,            # BaseEntry | None — for delta installs
        base_archive_uri: str = "", # resolved URI for the base archive
    ) -> None:
        super().__init__(title=f"Install {entry.name}", content_width=360)
        self._entry = entry
        self._archive_uri = archive_uri
        self._on_success = on_success
        self._token = token
        self._cancel_event = threading.Event()
        self._runner_to_install = runner_to_install
        self._runner_info = runner_info
        self._installed_runners = installed_runners or []
        self._open_runner_manager = open_runner_manager
        self._resolve_runner_info = resolve_runner_info
        self._runner_row: Adw.ActionRow | None = None
        self._base_entry = base_entry
        self._base_archive_uri = base_archive_uri
        # Determine whether we need to show the confirm page at all.
        self._needs_confirm = bool(runner_to_install)

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
        box, self._phase_label, self._progress_bar, self._cancel_body_btn = (
            make_progress_page("Downloading", self._on_cancel_progress_clicked)
        )
        return box

    # ── Signal handlers ───────────────────────────────────────────────────

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
        if self._entry.platform == "linux":
            self._start_linux_install()
            return
        from cellar.backend.installer import InstallCancelled, install_app

        self._pulse_id: int | None = None

        def _set_phase(label: str) -> None:
            GLib.idle_add(self._on_phase_change, label)

        def _dl_progress(fraction: float) -> None:
            GLib.idle_add(self._progress_bar.set_fraction, fraction)

        def _dl_stats(downloaded: int, total: int, speed: float) -> None:
            GLib.idle_add(self._progress_bar.set_text, _fmt_dl_stats(downloaded, total, speed))

        def _inst_progress(fraction: float) -> None:
            GLib.idle_add(self._progress_bar.set_fraction, fraction)

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

                # ── App install phase (includes base download if needed) ───
                _set_phase("Downloading & extracting\u2026")
                effective_runner = self._runner_to_install or (
                    self._installed_runners[0] if self._installed_runners else ""
                )
                prefix_dir = install_app(
                    self._entry,
                    self._archive_uri,
                    base_entry=self._base_entry,
                    base_archive_uri=self._base_archive_uri,
                    download_cb=_dl_progress,
                    download_stats_cb=_dl_stats,
                    install_cb=_inst_progress,
                    phase_cb=_set_phase,
                    cancel_event=self._cancel_event,
                    token=self._token,
                )
                from cellar.backend.umu import prefixes_dir as _prefixes_dir
                from cellar.utils.paths import dir_size_bytes as _dir_size
                _install_size = _dir_size(_prefixes_dir() / prefix_dir)
                GLib.idle_add(self._on_done, prefix_dir, str(_prefixes_dir()), effective_runner, _install_size)
            except InstallCancelled:
                GLib.idle_add(self._on_cancelled)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_error, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _start_linux_install(self) -> None:
        """Background install for Linux native apps."""
        from cellar.backend.installer import InstallCancelled, install_linux_app

        self._pulse_id: int | None = None

        def _set_phase(label: str) -> None:
            GLib.idle_add(self._on_phase_change, label)

        def _dl_progress(fraction: float) -> None:
            GLib.idle_add(self._progress_bar.set_fraction, fraction)

        def _dl_stats(downloaded: int, total: int, speed: float) -> None:
            GLib.idle_add(self._progress_bar.set_text, _fmt_dl_stats(downloaded, total, speed))

        def _inst_progress(fraction: float) -> None:
            GLib.idle_add(self._progress_bar.set_fraction, fraction)

        def _run() -> None:
            try:
                _app_id, install_dest = install_linux_app(
                    self._entry,
                    self._archive_uri,
                    download_cb=_dl_progress,
                    download_stats_cb=_dl_stats,
                    install_cb=_inst_progress,
                    phase_cb=_set_phase,
                    cancel_event=self._cancel_event,
                    token=self._token,
                )
                from cellar.utils.paths import dir_size_bytes as _dir_size
                _install_size = _dir_size(install_dest)
                GLib.idle_add(self._on_done, self._entry.id, str(install_dest.parent), "", _install_size)
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
        if "Copying" in label or "Applying delta" in label or "Installing" in label:
            # Indeterminate pulse for copytree / rsync delta / Linux copy
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

    def _on_done(self, prefix_dir: str, install_path: str = "", runner: str = "", install_size: int = 0) -> None:
        self.close()
        self._on_success(prefix_dir, install_path, runner, install_size)

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
    """Confirmation dialog shown before removing an installed app."""

    def __init__(
        self,
        *,
        entry: AppEntry,
        prefix_path,          # pathlib.Path | None
        on_confirm: Callable,
    ) -> None:
        path_str = _short_path(prefix_path) if prefix_path else "unknown location"
        super().__init__(
            heading=f"Remove {entry.name}?",
            body=(
                f"The directory at {path_str} will be permanently deleted. "
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

def _base_status_subtitle(installed: bool) -> str:
    return "Already present on your system" if installed else "Will also be downloaded"


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1000
    return f"{n:.1f} PB"

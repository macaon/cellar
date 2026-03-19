"""App detail view — shown when the user activates an app card."""

from __future__ import annotations

import logging
import os
import re
import threading
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from cellar.models.app_entry import AppEntry
from cellar.utils.async_work import run_in_background
from cellar.utils.paths import short_path as _short_path
from cellar.utils.progress import fmt_stats as _fmt_dl_stats
from cellar.utils.progress import user_facing_error
from cellar.views.widgets import make_progress_page, set_margins

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
        is_installed: bool = False,
        installed_record: dict | None = None,
        install_queue=None,
        on_remove_done: Callable | None = None,
        on_update_done: Callable | None = None,
        on_genre_filter: Callable[[str], None] | None = None,
        is_offline: bool = False,
    ) -> None:
        super().__init__()
        self._entry = entry
        self._source_repos = source_repos or []
        self._on_genre_filter = on_genre_filter
        self._is_offline = is_offline
        # Prefer an online repo for asset resolution; fall back to first repo.
        _online = next((r for r in self._source_repos if not r.is_offline), None)
        _first = _online or (self._source_repos[0] if self._source_repos else None)
        self._resolve = _first.resolve_asset_uri if _first else (lambda rel: rel)
        self._peek = _first.peek_asset_cache if _first else (lambda _: "")
        self._token = _first.token if _first else None
        self._ssh_identity = _first.ssh_identity if _first else None
        self._is_installed = is_installed
        self._installed_record = installed_record
        self._install_queue = install_queue
        self._on_remove_done = on_remove_done
        self._on_update_done = on_update_done
        _cat_crc = entry.archive_crc32 or ""
        _stored_crc = (installed_record or {}).get("archive_crc32") or ""
        self._has_update = (
            is_installed
            and bool(_cat_crc and _stored_crc and _cat_crc != _stored_crc)
        )
        self._screenshot_paths: list[str] = []
        self._resolved_runner: str = ""
        self._runner_label: Gtk.Label | None = None
        self._base_warning_icon: Gtk.Image | None = None
        # Base image resolution — populated once by _resolve_base_async.
        # Observers registered before resolution completes are called on idle.
        self._base_sz: int = 0
        self._base_installed: bool | None = None  # None = not yet resolved
        self._runner_sz: int = 0
        self._runner_installed: bool | None = None  # None = not yet resolved
        self._base_resolve_cbs: list[Callable] = []
        self._metadata_loaded = not entry.is_partial

        self._toolbar = Adw.ToolbarView()
        self.append(self._toolbar)
        self._build(self._toolbar)

        if entry.is_partial:
            self._fetch_full_metadata()

    # ------------------------------------------------------------------
    # Layout construction
    # ------------------------------------------------------------------

    def _build(self, toolbar: Adw.ToolbarView) -> None:
        # ── Header bar ────────────────────────────────────────────────────
        self._header_bar = Adw.HeaderBar()
        self._header_bar.set_title_widget(Gtk.Label())  # no centred title
        toolbar.add_top_bar(self._header_bar)

        if self._entry.is_partial:
            # Show a spinner until full metadata is ready — avoids layout
            # jumps from rendering partial data then swapping in the rest.
            loading = Gtk.Box(
                orientation=Gtk.Orientation.VERTICAL, spacing=12,
                halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER,
                vexpand=True, hexpand=True,
            )
            spinner = Adw.Spinner()
            spinner.set_size_request(64, 64)
            loading.append(spinner)
            lbl = Gtk.Label(label="Loading App Details")
            lbl.add_css_class("dim-label")
            loading.append(lbl)
            toolbar.set_content(loading)
        else:
            self._build_content()

    def _build_content(self) -> None:
        """Build the full detail page (header + body).

        Called immediately for non-partial entries, or after metadata fetch
        completes for partial ones.
        """
        toolbar = self._toolbar

        # ── Scrollable body ───────────────────────────────────────────────
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar.set_content(scroll)

        self._outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._outer.set_valign(Gtk.Align.START)
        scroll.set_child(self._outer)

        # App header (width-clamped).
        header_clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
        header_clamp.set_child(self._make_app_header())
        self._outer.append(header_clamp)

        # Separator between header and screenshots/content (always shown).
        self._outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self._build_detail_body()

    def _build_detail_body(self) -> None:
        """Build the screenshots band, description, and info cards."""
        e = self._entry

        # Screenshots band — full-width, flanked by a second separator.
        screenshots_widget = self._make_screenshots()
        if screenshots_widget:
            self._outer.append(screenshots_widget)
            second_sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            second_sep.set_visible(screenshots_widget.get_visible())
            self._outer.append(second_sep)

            def _on_screenshots_visible(widget, _pspec):
                second_sep.set_visible(widget.get_visible())

            screenshots_widget.connect("notify::visible", _on_screenshots_visible)

        # Content (width-clamped).
        content_clamp = Adw.Clamp(maximum_size=860, tightening_threshold=600)
        content_clamp.set_margin_bottom(32)
        self._outer.append(content_clamp)

        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=50)
        content_box.set_margin_top(18)
        content_box.set_margin_start(18)
        content_box.set_margin_end(18)
        content_clamp.set_child(content_box)

        if e.description:
            content_box.append(self._make_description())

        self._content_box = content_box
        self._info_cards = self._make_info_cards()
        content_box.append(self._info_cards)

        # Bottom spacer absorbs extra vertical space so the content stays
        # at its natural size when the page is shorter than the viewport
        # (e.g. no screenshots).
        spacer = Gtk.Box()
        spacer.set_vexpand(True)
        self._outer.append(spacer)

    # ------------------------------------------------------------------
    # Lazy metadata loading
    # ------------------------------------------------------------------

    def _fetch_full_metadata(self) -> None:
        """Fetch ``apps/<id>/metadata.json`` in a background thread."""
        app_id = self._entry.id

        def _work() -> AppEntry | None:
            for repo in self._source_repos:
                try:
                    return repo.fetch_app_metadata(app_id)
                except Exception as exc:
                    log.debug("metadata fetch from %s failed: %s", repo.uri, exc)
            return None

        def _done(result: AppEntry | None) -> None:
            if result is not None:
                self._on_metadata_loaded(result)
            else:
                log.warning("Could not fetch metadata for %s from any repo", app_id)
                # Build the view with partial data so the user can still
                # see name/icon/category and attempt install (which fetches
                # metadata on demand).
                self._metadata_loaded = True
                self._build_content()

        run_in_background(_work, on_done=_done)

    def _on_metadata_loaded(self, full_entry: AppEntry) -> None:
        """Replace the partial entry with full metadata, pre-resolve assets,
        then build the view so nothing reflows after it appears."""
        self._entry = full_entry
        self._metadata_loaded = True
        self._prefetch_then_build()

    def _prefetch_then_build(self) -> None:
        """Pre-resolve assets in the background, then build the view.

        Downloads icon/logo/screenshots and resolves base-image metadata
        while the spinner is still showing so the view appears fully
        populated with no reflow.
        """
        e = self._entry

        def _work():
            # Pre-cache icon, logo, and screenshots so _make_icon / _make_logo
            # and _make_screenshots hit the fast (synchronous) path.
            for rel in filter(None, [e.icon, e.logo, e.cover]):
                try:
                    self._resolve(rel)
                except Exception:
                    pass
            for s in (e.screenshots or []):
                try:
                    self._resolve(s)
                except Exception:
                    pass

            # Resolve base image / runner info so _make_info_cards has
            # real data instead of "…" placeholders.
            base_image = e.base_image
            if base_image:
                from cellar.backend.base_store import is_base_installed
                from cellar.backend.umu import resolve_runner_path
                installed = is_base_installed(base_image)
                base_entry, _ = self._find_base_entry(base_image)
                base_sz = base_entry.archive_size if base_entry else 0
                runner = base_entry.runner if base_entry else ""
                runner_installed = bool(resolve_runner_path(runner)) if runner else True
                runner_entry, _ = self._find_runner_entry(runner) if runner else (None, "")
                runner_sz = runner_entry.archive_size if runner_entry else 0
                return installed, base_sz, runner, runner_installed, runner_sz
            return None

        def _done(result) -> None:
            if result is not None:
                installed, base_sz, runner, runner_installed, runner_sz = result
                self._base_installed = installed
                self._base_sz = base_sz
                self._runner_installed = runner_installed
                self._runner_sz = runner_sz
                if runner:
                    self._resolved_runner = runner
            self._build_content()

        run_in_background(_work, on_done=_done)

    def _ensure_metadata(self) -> AppEntry:
        """Return the full entry, fetching synchronously if still partial.

        Called from the install thread when the user clicks Install before
        the background metadata fetch completes.
        """
        if not self._entry.is_partial:
            return self._entry
        app_id = self._entry.id
        for repo in self._source_repos:
            try:
                full = repo.fetch_app_metadata(app_id)
                GLib.idle_add(self._on_metadata_loaded, full)
                return full
            except Exception:
                continue
        return self._entry

    # ------------------------------------------------------------------
    # Install helpers
    # ------------------------------------------------------------------

    def _is_install_pending(self) -> bool:
        """Return True if this app is actively installing or queued."""
        if self._install_queue is None:
            return False
        return self._install_queue.is_pending(self._entry.id)

    def _update_install_button(self) -> None:
        btn = self._install_btn
        for cls in ("suggested-action", "success", "destructive-action"):
            btn.remove_css_class(cls)
        # Hide spinner button by default.
        self._spinner_btn.set_visible(False)
        if self._is_installed:
            self._install_btn_label.set_label("Open")
            btn.add_css_class("suggested-action")
            btn.set_sensitive(True)
            btn.set_tooltip_text("")
            self._update_indicator.set_visible(self._has_update and not self._is_offline)
            self._update_indicator.set_tooltip_text(
                "Update available — see Options menu"
                if (self._has_update and not self._is_offline) else ""
            )
        elif self._is_install_pending():
            self._install_btn_label.set_label("Cancel")
            btn.add_css_class("destructive-action")
            btn.set_sensitive(True)
            btn.set_tooltip_text("")
            self._update_indicator.set_visible(False)
            self._spinner_btn.set_visible(True)
        elif self._is_offline:
            self._install_btn_label.set_label("Unavailable")
            btn.set_sensitive(False)
            btn.set_tooltip_text("Repository is offline")
            self._update_indicator.set_visible(False)
        else:
            self._install_btn_label.set_label("Install")
            btn.add_css_class("suggested-action")
            btn.set_sensitive(True)
            btn.set_tooltip_text("")
            self._update_indicator.set_visible(False)
        self._gear_btn.set_visible(self._is_installed)
        if self._is_installed:
            self._refresh_gear_menu()

    def _update_install_progress(self, queue) -> None:
        """Update the spinner button tooltip with live stats.

        Called by the window on every progress/stats callback.
        """
        if not hasattr(self, "_spinner_btn"):
            return
        if not queue.is_active(self._entry.id):
            return
        stats = queue.active_stats_text
        phase = queue.active_phase
        if stats:
            self._spinner_btn.set_tooltip_text(f"{phase}\n{stats}" if phase else stats)
        elif phase:
            self._spinner_btn.set_tooltip_text(phase)

    def _on_spinner_btn_clicked(self, _btn) -> None:
        """Show a progress popover with live download stats."""
        if self._install_queue is None:
            return
        self._show_progress_popover()

    def _show_progress_popover(self) -> None:
        """Show a popover anchored to the spinner button with live download progress."""
        q = self._install_queue
        popover = Gtk.Popover()
        popover.set_parent(self._spinner_btn)
        popover.set_position(Gtk.PositionType.BOTTOM)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        phase_label = Gtk.Label()
        phase_label.set_halign(Gtk.Align.START)
        phase_label.add_css_class("heading")
        box.append(phase_label)

        progress_bar = Gtk.ProgressBar()
        progress_bar.set_show_text(False)
        progress_bar.set_size_request(240, -1)
        box.append(progress_bar)

        stats_label = Gtk.Label()
        stats_label.set_halign(Gtk.Align.START)
        stats_label.add_css_class("dim-label")
        stats_label.add_css_class("caption")
        box.append(stats_label)

        popover.set_child(box)

        def _update() -> bool:
            if not popover.is_visible():
                return False
            phase = q.active_phase or "Installing…"
            phase_label.set_label(phase)
            progress_bar.set_fraction(q.active_fraction)
            stats = q.active_stats_text
            stats_label.set_label(stats if stats else "")
            stats_label.set_visible(bool(stats))
            return True  # keep updating

        _update()
        GLib.timeout_add(250, _update)
        popover.popup()

    def _on_install_clicked(self, _btn) -> None:
        if self._is_installed:
            self._on_open_clicked()
            return
        # Cancel if already installing/queued.
        if self._is_install_pending():
            self._install_queue.cancel(self._entry.id)
            self._update_install_button()
            return
        if len(self._source_repos) > 1:
            self._pick_source_then_install()
            return
        self._proceed_to_install()

    def _pick_source_then_install(self) -> None:
        """Show a dialog to choose the source repo, then proceed to install."""
        online_repos = [(i, r) for i, r in enumerate(self._source_repos) if not r.is_offline]
        if not online_repos:
            return
        if len(online_repos) == 1:
            _, repo = online_repos[0]
            self._resolve = repo.resolve_asset_uri
            self._token = repo.token
            self._ssh_identity = repo.ssh_identity
            self._proceed_to_install()
            return

        dialog = Adw.AlertDialog(
            heading="Choose Source",
            body="This app is available from multiple repositories.",
        )
        for idx, repo in online_repos:
            dialog.add_response(str(idx), repo.name)
        dialog.set_default_response(str(online_repos[0][0]))
        dialog.set_close_response("close")
        dialog.add_response("close", "Cancel")

        def _on_response(_dlg, response):
            if response == "close":
                return
            idx = int(response)
            repo = self._source_repos[idx]
            self._resolve = repo.resolve_asset_uri
            self._token = repo.token
            self._ssh_identity = repo.ssh_identity
            self._proceed_to_install()

        dialog.connect("response", _on_response)
        dialog.present(self.get_root())

    def _proceed_to_install(self) -> None:
        # Ensure full metadata is available (archive, launch_targets, etc.).
        if self._entry.is_partial:
            entry = self._ensure_metadata()
            if entry.is_partial:
                log.error("Cannot install %s: metadata unavailable", self._entry.id)
                return
        else:
            entry = self._entry
        # Update self._entry so _get_base_image and other helpers see full data.
        self._entry = entry
        archive_uri = self._resolve(entry.archive) if entry.archive else ""

        # Resolve base archive for delta installs.
        base_image = entry.base_image
        base_entry, base_archive_uri = (
            self._find_base_entry(base_image) if base_image else (None, "")
        )

        # Resolve runner archive (runner binary required by umu-launcher).
        runner_name = base_entry.runner if base_entry else ""
        runner_entry, runner_archive_uri = (
            self._find_runner_entry(runner_name) if runner_name else (None, "")
        )

        if self._install_queue is not None:
            from cellar.backend.install_queue import InstallJob

            job = InstallJob(
                app_id=entry.id,
                app_name=entry.name,
                entry=entry,
                archive_uri=archive_uri,
                platform=entry.platform,
                token=self._token,
                ssh_identity=self._ssh_identity,
                base_entry=base_entry,
                base_archive_uri=base_archive_uri,
                runner_entry=runner_entry,
                runner_archive_uri=runner_archive_uri,
            )
            # Register the entry for DB write on completion.
            window = self.get_root()
            if hasattr(window, "_pending_entries"):
                window._pending_entries[entry.id] = entry
            self._install_queue.enqueue(job)
            self._update_install_button()
            return

        # Fallback: legacy dialog (should not be reached).
        dialog = InstallProgressDialog(
            entry=entry,
            archive_uri=archive_uri,
            on_success=self._on_install_success,
            token=self._token,
            ssh_identity=self._ssh_identity,
            base_entry=base_entry,
            base_archive_uri=base_archive_uri,
            runner_entry=runner_entry,
            runner_archive_uri=runner_archive_uri,
        )
        dialog.present(self.get_root())

    def _on_install_success(
        self, prefix_dir: str, install_path: str = "", runner: str = "",
        install_size: int = 0, delta_size: int = 0,
    ) -> None:
        """Legacy callback for InstallProgressDialog fallback path."""
        self._is_installed = True
        self._installed_record = {
            "prefix_dir": prefix_dir,
            "install_path": install_path,
            "install_size": install_size,
            "delta_size": delta_size,
            "runner": runner,
        }
        self._resolved_runner = runner
        if self._runner_label:
            self._runner_label.set_label(runner)
        self._update_install_button()
        self._rebuild_info_cards()

    def _effective_launch_targets(self) -> list[dict]:
        """Return the launch targets to use, honouring local overrides."""
        from cellar.backend import database  # noqa: PLC0415
        overrides = database.get_launch_overrides(self._entry.id)
        return overrides.get("launch_targets") or list(self._entry.launch_targets)

    def _on_open_clicked(self) -> None:
        targets = self._effective_launch_targets()
        if not targets:
            return
        if len(targets) == 1:
            self._launch_target(targets[0])
            return
        # Multiple targets — let the user pick.
        dialog = Adw.AlertDialog(
            heading="Select Launch Target",
            body="Choose which target to launch:",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.set_response_appearance("cancel", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        for i, t in enumerate(targets):
            dialog.add_response(str(i), t.get("name", t.get("path", "")))
        dialog.connect("response", self._on_launch_target_chosen)
        dialog.present(self.get_root())

    def _on_launch_target_chosen(self, _dialog, response: str) -> None:
        if response == "cancel":
            return
        try:
            idx = int(response)
        except ValueError:
            return
        targets = self._effective_launch_targets()
        if 0 <= idx < len(targets):
            self._launch_target(targets[idx])

    def _launch_target(self, target: dict) -> None:
        entry_path = target.get("path", "")
        entry_args = target.get("args", "")
        if self._entry.platform == "dos":
            self._launch_dos_target(entry_path, entry_args)
            return
        if self._entry.platform == "linux":
            self._launch_linux_target(entry_path, entry_args)
            return
        from cellar.backend.umu import launch_app_monitored, merge_launch_params  # noqa: PLC0415
        from cellar.views.builder.progress import ProgressDialog  # noqa: PLC0415
        from cellar.backend import database  # noqa: PLC0415

        overrides = database.get_launch_overrides(self._entry.id)
        params = merge_launch_params(
            self._entry, overrides, installed_runner=self._resolved_runner
        )
        runner_name = params["runner"]
        if not runner_name:
            base_entry, _ = self._find_base_entry(self._entry.base_image)
            if base_entry:
                runner_name = base_entry.runner

        extra_env = _parse_launch_env(target.get("env", ""))
        from cellar.backend.umu import dll_overrides, proton_compat_env  # noqa: PLC0415
        from cellar.backend.config import load_audio_driver  # noqa: PLC0415
        audio = params["audio_driver"]
        if audio == "auto":
            audio = load_audio_driver()
        dll_overrides_str = dll_overrides(
            dxvk=params["dxvk"], vkd3d=params["vkd3d"],
            audio_driver=audio,
        )
        if dll_overrides_str:
            extra_env["WINEDLLOVERRIDES"] = dll_overrides_str
        extra_env.update(proton_compat_env(
            dxvk=params["dxvk"], vkd3d=params["vkd3d"],
        ))
        if params["debug"]:
            extra_env["PROTON_LOG"] = "1"

        progress = ProgressDialog(label="Launching\u2026")
        progress.set_can_close(True)

        def _on_line(line: str) -> None:
            if "Downloading" in line:
                label = "Downloading runtime\u2026"
            elif "SHA256 is OK" in line:
                label = "Verifying runtime\u2026"
            elif "mtree is OK" in line or "is up to date" in line:
                label = "Runtime ready"
            elif "Setting up Unified Launcher" in line:
                label = "Setting up\u2026"
            elif "pressure-vessel" in line:
                label = "Starting container\u2026"
            elif "Using steamrt3" in line or "Using SteamLinuxRuntime" in line:
                label = "Starting runtime\u2026"
            elif "fsync:" in line or "esync:" in line:
                label = "Configuring Wine\u2026"
            elif "wine: configuration" in line:
                label = "Initializing prefix\u2026"
            elif "Proton:" in line:
                label = "Starting Proton\u2026"
            elif line.startswith("[pid]"):
                label = "App started"
            else:
                return
            GLib.idle_add(progress.set_label, label)

        def _work() -> None:
            try:
                launch_app_monitored(
                    app_id=self._entry.id,
                    entry_point=entry_path,
                    runner_name=runner_name,
                    steam_appid=params["steam_appid"],
                    launch_args=entry_args,
                    extra_env=extra_env or None,
                    line_cb=_on_line,
                    direct_proton=params["direct_proton"],
                )
            except Exception as exc:
                log.warning("Launch failed: %s", exc)
            GLib.idle_add(progress.force_close)

        threading.Thread(target=_work, daemon=True).start()
        progress.present(self.get_root())

    def _launch_linux_target(self, entry_path: str, entry_args: str) -> None:
        """Launch a native Linux app target with PID-based launch detection."""
        import subprocess as _sp
        if not entry_path:
            return
        import shlex as _shlex

        from cellar.backend.umu import (  # noqa: PLC0415
            is_cellar_sandboxed, native_dir, dos_dir, monitor_process_tree,
        )
        from cellar.views.builder.progress import ProgressDialog  # noqa: PLC0415

        base = dos_dir() if self._entry.platform == "dos" else native_dir()
        exe = base / self._entry.id / entry_path
        cmd = [str(exe)]
        if entry_args:
            cmd += _shlex.split(entry_args)
        if is_cellar_sandboxed():
            cmd = ["flatpak-spawn", "--host"] + cmd

        progress = ProgressDialog(label="Launching\u2026")
        progress.set_can_close(True)

        def _on_line(line: str) -> None:
            if line.startswith("[pid]"):
                GLib.idle_add(progress.set_label, "App started")

        def _work() -> None:
            _sp.Popen(cmd, cwd=str(exe.parent), start_new_session=True)
            launch_event = threading.Event()
            monitor_process_tree(entry_path, launch_event, _on_line)
            GLib.idle_add(progress.force_close)

        threading.Thread(target=_work, daemon=True).start()
        progress.present(self.get_root())

    def _launch_dos_target(self, entry_path: str, entry_args: str) -> None:
        """Launch a DOS game via the bundled DOSBox Staging binary."""
        import subprocess as _sp

        from cellar.backend.dosbox import build_dos_launch_cmd
        from cellar.backend.umu import dos_dir, monitor_process_tree
        from cellar.views.builder.progress import ProgressDialog

        game_dir = dos_dir() / self._entry.id
        dosbox_bin = game_dir / "dosbox" / "dosbox"
        if not dosbox_bin.is_file():
            self._add_toast("DOSBox Staging binary not found in package")
            return

        cmd, tmp_conf = build_dos_launch_cmd(game_dir, entry_path, entry_args)

        progress = ProgressDialog(label="Launching\u2026")
        progress.set_can_close(True)

        def _on_line(line: str) -> None:
            if line.startswith("[pid]"):
                GLib.idle_add(progress.set_label, "Game started")

        def _work() -> None:
            _sp.Popen(cmd, cwd=str(game_dir), start_new_session=True)
            launch_event = threading.Event()
            monitor_process_tree("dosbox", launch_event, _on_line)
            if tmp_conf and tmp_conf.is_file():
                tmp_conf.unlink(missing_ok=True)
            GLib.idle_add(progress.force_close)

        threading.Thread(target=_work, daemon=True).start()
        progress.present(self.get_root())

    def _on_remove_clicked(self) -> None:
        prefix_path = None
        if self._entry.platform == "linux":
            from cellar.backend.umu import native_dir
            candidate = native_dir() / self._entry.id
            if candidate.is_dir():
                prefix_path = candidate
        elif self._entry.platform == "dos":
            from cellar.backend.umu import dos_dir
            candidate = dos_dir() / self._entry.id
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
        from cellar.backend import database
        from cellar.backend.umu import native_dir, prefixes_dir
        from cellar.views.update_app import UpdateDialog

        if self._entry.is_partial:
            self._entry = self._ensure_metadata()
            if self._entry.is_partial:
                log.error("Cannot update %s: metadata unavailable", self._entry.id)
                return

        if self._entry.platform == "linux":
            prefix_path = native_dir() / self._entry.id
        elif self._entry.platform == "dos":
            from cellar.backend.umu import dos_dir
            prefix_path = dos_dir() / self._entry.id
        else:
            prefix_path = prefixes_dir() / self._entry.id
        if not prefix_path.is_dir():
            log.error("Could not locate prefix directory for %s", self._entry.id)
            return

        archive_uri = self._resolve(self._entry.archive) if self._entry.archive else ""

        # Resolve base archive for delta updates.
        runner = self._get_base_image()
        base_entry, base_archive_uri = self._find_base_entry(runner) if runner else (None, "")

        # Re-fetch from DB so the installed version is current, not a
        # stale copy from when the DetailView was first constructed.
        rec = database.get_installed(self._entry.id) or self._installed_record or {}

        dialog = UpdateDialog(
            entry=self._entry,
            installed_record=rec,
            prefix_path=prefix_path,
            archive_uri=archive_uri,
            on_success=self._on_update_success,
            base_entry=base_entry,
            base_archive_uri=base_archive_uri,
            token=self._token,
        )
        dialog.present(self.get_root())

    def _on_update_success(self, install_size: int = 0, delta_size: int = 0) -> None:
        self._has_update = False
        if self._installed_record is not None and install_size:
            self._installed_record = {
                **self._installed_record,
                "install_size": install_size,
                "delta_size": delta_size,
            }
        self._update_install_button()
        self._rebuild_info_cards()
        if self._on_update_done:
            self._on_update_done(install_size, delta_size)

    def _on_remove_confirmed(self) -> None:
        import shutil

        from cellar.backend import database

        if self._entry.platform in ("linux", "dos"):
            if self._entry.platform == "dos":
                from cellar.backend.umu import dos_dir
                candidate = dos_dir() / self._entry.id
            else:
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

        # Capture runner and repo_source before clearing the record.
        removed_runner = self._installed_record.get("runner") if self._installed_record else None
        removed_repo = (
            self._installed_record.get("repo_source") if self._installed_record else None
        )

        database.remove_installed(self._entry.id)
        from cellar.utils.desktop import remove_desktop_entry
        remove_desktop_entry(self._entry.id)

        # Clean up orphaned runner, base image, and stale repo caches.
        if removed_runner or removed_repo:
            remaining = database.get_all_installed()

            if removed_runner and not any(r.get("runner") == removed_runner for r in remaining):
                has_writable = any(r.is_writable for r in self._source_repos)
                if has_writable:
                    self._ask_remove_runner_base(removed_runner)
                else:
                    self._do_remove_runner_base(removed_runner)

            if removed_repo and not any(r.get("repo_source") == removed_repo for r in remaining):
                from cellar.backend.repo import Repo
                Repo.clear_catalogue_cache(removed_repo)
                Repo.clear_asset_cache(removed_repo)
                log.info("Cleared stale caches for repo %s", removed_repo)

        self._is_installed = False
        self._installed_record = None
        self._update_install_button()
        self._rebuild_info_cards()
        if self._on_remove_done:
            self._on_remove_done()

    def _do_remove_runner_base(self, runner: str) -> None:
        """Silently remove an orphaned runner and its base image."""
        from cellar.backend import base_store
        from cellar.backend import runners as _runners
        try:
            _runners.remove_runner(runner)
            base_store.remove_base(runner)
            log.info("Cleaned up orphaned runner and base: %s", runner)
        except Exception as exc:
            log.warning("Failed to clean up runner/base %s: %s", runner, exc)

    def _ask_remove_runner_base(self, runner: str) -> None:
        """Ask the maintainer whether to keep or remove an orphaned runner/base."""
        dlg = Adw.AlertDialog(
            heading="Keep runner and base image?",
            body=(
                f"No installed apps use \u201c{runner}\u201d anymore. "
                "Keep it for future package building, or remove it to free disk space?"
            ),
        )
        dlg.add_response("remove", "Remove")
        dlg.add_response("keep", "Keep")
        dlg.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("keep")

        def _on_response(_dlg, response):
            if response == "remove":
                self._do_remove_runner_base(runner)

        dlg.connect("response", _on_response)
        dlg.present(self.get_root())

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

        manage_act = Gio.SimpleAction.new("manage-shortcuts", None)
        manage_act.connect("activate", self._on_manage_shortcuts)
        ag.add_action(manage_act)

        launch_params_act = Gio.SimpleAction.new("launch-params", None)
        launch_params_act.connect("activate", lambda *_: self._on_launch_params_clicked())
        ag.add_action(launch_params_act)

        dosbox_config_act = Gio.SimpleAction.new("dosbox-config", None)
        dosbox_config_act.connect("activate", lambda *_: self._on_dosbox_config_clicked())
        ag.add_action(dosbox_config_act)

        backup_user_act = Gio.SimpleAction.new("backup-user-files", None)
        backup_user_act.connect("activate", lambda *_: self._on_backup_user_files())
        ag.add_action(backup_user_act)

        import_user_act = Gio.SimpleAction.new("import-user-files", None)
        import_user_act.connect("activate", lambda *_: self._on_import_user_files())
        ag.add_action(import_user_act)

        uninstall_act = Gio.SimpleAction.new("uninstall", None)
        uninstall_act.connect("activate", lambda *_: self._on_remove_clicked())
        ag.add_action(uninstall_act)

        self._gear_btn.insert_action_group("detail", ag)
        self._refresh_gear_menu()

    def _refresh_gear_menu(self) -> None:
        from cellar.utils.desktop import has_desktop_entry

        main_section = Gio.Menu()
        if self._has_update and not self._is_offline:
            main_section.append("Update", "detail.update")

        main_section.append("Launch Parameters\u2026", "detail.launch-params")

        targets = self._entry.launch_targets
        if len(targets) > 1:
            main_section.append("Desktop Shortcuts\u2026", "detail.manage-shortcuts")
        elif has_desktop_entry(self._entry.id):
            main_section.append("Remove Desktop Shortcut", "detail.remove-shortcut")
        else:
            main_section.append("Create Desktop Shortcut", "detail.create-shortcut")

        if self._entry.platform == "dos":
            main_section.append("DOSBox Configuration\u2026", "detail.dosbox-config")

        main_section.append("Open Install Folder", "detail.open-folder")
        if self._entry.platform in ("windows", "dos"):
            main_section.append("Backup User Files\u2026", "detail.backup-user-files")
            main_section.append("Import User Files\u2026", "detail.import-user-files")

        danger_section = Gio.Menu()
        danger_section.append("Uninstall\u2026", "detail.uninstall")

        menu = Gio.Menu()
        menu.append_section(None, main_section)
        menu.append_section(None, danger_section)
        self._gear_btn.set_menu_model(menu)

    def _on_open_folder_action(self, _action, _param) -> None:
        folder = self._get_install_folder()
        if folder:
            Gio.AppInfo.launch_default_for_uri(f"file://{folder}", None)

    # ------------------------------------------------------------------
    # Backup user files (gear menu)
    # ------------------------------------------------------------------

    def _on_backup_user_files(self) -> None:
        """Scan for user-modified files, then offer to export as .tar.zst."""
        folder = self._get_install_folder()
        if not folder:
            self._add_toast("Install folder not found")
            return
        prefix_path = Path(folder)

        # Scan in background to avoid blocking the UI.
        def _scan():
            from cellar.backend.manifest import scan_user_files  # noqa: PLC0415
            return scan_user_files(prefix_path)

        def _on_scanned(modified, user_created):
            all_files = modified + user_created
            if not all_files:
                dlg = Adw.AlertDialog(
                    heading="No User Files Found",
                    body=(
                        "No files have been modified or created since "
                        "installation. There is nothing to back up."
                    ),
                )
                dlg.add_response("ok", "OK")
                dlg.present(self.get_root())
                return
            self._show_backup_file_chooser(prefix_path, len(all_files))

        def _do_scan():
            try:
                modified, user_created = _scan()
                GLib.idle_add(_on_scanned, modified, user_created)
            except Exception as exc:
                GLib.idle_add(self._add_toast, f"Scan failed: {exc}")

        threading.Thread(target=_do_scan, daemon=True).start()

    def _show_backup_file_chooser(self, prefix_path: Path, file_count: int) -> None:
        """Show a save dialog for the user-file backup archive."""
        import datetime  # noqa: PLC0415

        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        suggested = f"{self._entry.id}-userfiles-{stamp}.tar.zst"

        chooser = Gtk.FileChooserNative(
            title="Backup User Files",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.SAVE,
        )
        chooser.set_current_name(suggested)

        f = Gtk.FileFilter()
        f.set_name("Compressed archive (*.tar.zst)")
        f.add_pattern("*.tar.zst")
        chooser.add_filter(f)

        chooser.connect(
            "response",
            self._on_backup_dest_chosen, chooser, prefix_path, file_count,
        )
        chooser.show()

    def _on_backup_dest_chosen(
        self, _chooser, response, chooser, prefix_path: Path, file_count: int,
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        dest_path = Path(chooser.get_file().get_path())

        # Show a progress dialog and run the backup in a background thread.
        dlg = Adw.Dialog(
            title="Backup User Files",
            content_width=340,
            content_height=180,
        )
        cancel_event = threading.Event()
        dlg.connect("closed", lambda _d: cancel_event.set())

        box, phase_label, progress_bar, cancel_btn = make_progress_page(
            "Preparing\u2026",
            lambda _btn: (cancel_event.set(), phase_label.set_text("Cancelling\u2026")),
        )
        dlg.set_child(box)
        dlg.present(self.get_root())

        def _progress(frac):
            GLib.idle_add(progress_bar.set_fraction, frac)

        def _stats(done, total, speed):
            GLib.idle_add(
                progress_bar.set_text,
                _fmt_dl_stats(done, total, speed),
            )

        def _phase(label):
            GLib.idle_add(phase_label.set_text, label)

        def _run():
            from cellar.backend.updater import (  # noqa: PLC0415
                UpdateCancelled,
                backup_user_files,
            )
            try:
                count = backup_user_files(
                    prefix_path,
                    dest_path,
                    app_id=self._entry.id,
                    progress_cb=_progress,
                    stats_cb=_stats,
                    phase_cb=_phase,
                    cancel_event=cancel_event,
                )
                GLib.idle_add(_on_done, count, None)
            except UpdateCancelled:
                GLib.idle_add(_on_done, 0, None)
            except Exception as exc:
                GLib.idle_add(_on_done, 0, exc)

        def _on_done(count, error):
            dlg.close()
            if error:
                self._add_toast(f"Backup failed: {error}")
            elif cancel_event.is_set():
                self._add_toast("Backup cancelled")
            else:
                self._add_toast(f"Backed up {count} file{'s' if count != 1 else ''}")

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # Import user files (gear menu)
    # ------------------------------------------------------------------

    def _on_import_user_files(self) -> None:
        """Show a file chooser, then restore a user-file backup archive."""
        chooser = Gtk.FileChooserNative(
            title="Import User Files",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
        )
        f = Gtk.FileFilter()
        f.set_name("Compressed archive (*.tar.zst)")
        f.add_pattern("*.tar.zst")
        chooser.add_filter(f)
        chooser.connect("response", self._on_import_file_chosen, chooser)
        chooser.show()

    def _on_import_file_chosen(self, _chooser, response, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        archive_path = Path(chooser.get_file().get_path())

        dlg = Adw.Dialog(
            title="Import User Files",
            content_width=340,
            content_height=180,
        )
        cancel_event = threading.Event()
        dlg.connect("closed", lambda _d: cancel_event.set())

        box, phase_label, progress_bar, cancel_btn = make_progress_page(
            "Preparing\u2026",
            lambda _btn: (cancel_event.set(), phase_label.set_text("Cancelling\u2026")),
        )
        dlg.set_child(box)
        dlg.present(self.get_root())

        def _progress(frac):
            GLib.idle_add(progress_bar.set_fraction, frac)

        def _stats(done, total, speed):
            GLib.idle_add(
                progress_bar.set_text,
                _fmt_dl_stats(done, total, speed),
            )

        def _phase(label):
            GLib.idle_add(phase_label.set_text, label)

        def _run():
            from cellar.backend.updater import (  # noqa: PLC0415
                UpdateCancelled,
                UpdateError,
                import_user_files,
            )
            try:
                app_id, count = import_user_files(
                    archive_path,
                    progress_cb=_progress,
                    stats_cb=_stats,
                    phase_cb=_phase,
                    cancel_event=cancel_event,
                )
                GLib.idle_add(_on_done, app_id, count, None)
            except UpdateCancelled:
                GLib.idle_add(_on_done, None, 0, None)
            except UpdateError as exc:
                GLib.idle_add(_on_done, None, 0, exc)
            except Exception as exc:
                GLib.idle_add(_on_done, None, 0, exc)

        def _on_done(app_id, count, error):
            dlg.close()
            if error:
                self._add_toast(f"Import failed: {error}")
            elif cancel_event.is_set():
                self._add_toast("Import cancelled")
            else:
                self._add_toast(f"Imported {count} file{'s' if count != 1 else ''}")

        threading.Thread(target=_run, daemon=True).start()

    def _on_dosbox_config_clicked(self) -> None:
        """Show DOSBox settings dialog for installed DOS games."""
        install_folder = self._get_install_folder()
        if not install_folder:
            return
        install_path = Path(install_folder)
        config_dir = install_path / "config"
        if not config_dir.is_dir():
            self._add_toast("No DOSBox config folder found")
            return

        from cellar.views.dosbox_settings import DosboxSettingsDialog
        DosboxSettingsDialog(
            config_dir=config_dir,
            assets_dir=install_path / "assets",
            allow_assets=True,
        ).present(self.get_root())

    def _on_launch_params_clicked(self) -> None:
        from cellar.views.launch_params import LaunchParamsDialog  # noqa: PLC0415
        LaunchParamsDialog(
            self._entry,
            on_saved=self._refresh_gear_menu,
        ).present(self)


    def _get_install_folder(self) -> str | None:
        """Return the install folder path for the current entry, or None."""
        if self._entry.platform == "linux":
            from cellar.backend.umu import native_dir
            p = native_dir() / self._entry.id
            return str(p) if p.is_dir() else None
        if self._entry.platform == "dos":
            from cellar.backend.umu import dos_dir
            p = dos_dir() / self._entry.id
            return str(p) if p.is_dir() else None
        # Windows app — prefix is at umu prefixes_dir / app_id
        from cellar.backend.umu import prefixes_dir
        p = prefixes_dir() / self._entry.id
        return str(p) if p.is_dir() else None

    def _resolve_icon_source(self) -> str | None:
        if self._entry.icon:
            resolved = self._resolve(self._entry.icon)
            if resolved and Path(resolved).is_file():
                return resolved
        return None

    def _create_shortcut_for_target(
        self, target: dict | None = None, target_idx: int = 0,
    ) -> None:
        from cellar.utils.desktop import create_desktop_entry

        icon_source = self._resolve_icon_source()
        kwargs: dict = dict(
            entry=self._entry,
            icon_source=icon_source,
            target=target,
            target_idx=target_idx,
        )
        if self._entry.platform == "linux":
            from cellar.backend.umu import native_dir
            kwargs["install_path"] = str(native_dir())
        elif self._entry.platform == "dos":
            from cellar.backend.umu import dos_dir
            kwargs["install_path"] = str(dos_dir())
        try:
            create_desktop_entry(**kwargs)
        except Exception as exc:
            log.error("Failed to create desktop entry: %s", exc)
            self._add_toast("Failed to create shortcut")
            return
        name = (target or {}).get("name", self._entry.name)
        self._add_toast(f"Shortcut created for {name}")

    def _on_create_shortcut(self, _action, _param) -> None:
        self._create_shortcut_for_target()
        self._refresh_gear_menu()

    def _on_remove_shortcut(self, _action, _param) -> None:
        from cellar.utils.desktop import remove_desktop_entry

        remove_desktop_entry(self._entry.id)
        self._refresh_gear_menu()
        self._add_toast(f"Shortcut removed for {self._entry.name}")

    def _on_manage_shortcuts(self, _action, _param) -> None:
        from cellar.utils.desktop import has_desktop_entry

        targets = self._entry.launch_targets
        if not targets:
            return

        dialog = Adw.AlertDialog(
            heading="Desktop Shortcuts",
            body=f"Toggle desktop shortcuts for {self._entry.name}.",
        )
        dialog.add_response("done", "Done")
        dialog.set_close_response("done")

        listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        listbox.add_css_class("boxed-list")
        listbox.set_margin_start(0)
        listbox.set_margin_end(0)

        for i, t in enumerate(targets):
            name = t.get("name", t.get("path", f"Target {i}"))
            row = Adw.SwitchRow(title=name)
            row.set_active(has_desktop_entry(self._entry.id, i))
            row.connect("notify::active", self._on_shortcut_switch_toggled, i, t)
            listbox.append(row)

        dialog.set_extra_child(listbox)
        dialog.present(self.get_root())

    def _on_shortcut_switch_toggled(
        self, row: Adw.SwitchRow, _pspec, target_idx: int, target: dict,
    ) -> None:
        from cellar.utils.desktop import remove_desktop_entry

        if row.get_active():
            self._create_shortcut_for_target(target=target, target_idx=target_idx)
        else:
            remove_desktop_entry(self._entry.id, target_idx=target_idx)
            name = target.get("name", self._entry.name)
            self._add_toast(f"Shortcut removed for {name}")

    def _add_toast(self, message: str) -> None:
        root = self.get_root()
        if hasattr(root, "toast_overlay"):
            root.toast_overlay.add_toast(Adw.Toast(title=message))

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
        meta.set_valign(Gtk.Align.CENTER)

        name_lbl = Gtk.Label(label=" " if (e.hide_title and e.logo) else e.name)
        if not (e.hide_title and e.logo):
            name_lbl.add_css_class("title-1")
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_wrap(True)
        meta.append(name_lbl)

        if not dev_below_logo and dev_parts:
            dev_lbl = Gtk.Label(label=" · ".join(dev_parts))
            dev_lbl.add_css_class("dim-label")
            dev_lbl.set_halign(Gtk.Align.START)
            meta.append(dev_lbl)

        meta.set_hexpand(True)
        box.append(meta)

        # Right column: action buttons.
        action_row = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=6,
            valign=Gtk.Align.CENTER,
            halign=Gtk.Align.END,
        )
        box.append(action_row)

        # Spinner button — visible only during active/queued installs.
        self._spinner_btn = Gtk.Button()
        self._spinner_btn.set_child(Adw.Spinner())
        self._spinner_btn.add_css_class("flat")
        self._spinner_btn.set_visible(False)
        self._spinner_btn.set_tooltip_text("Show download progress")
        self._spinner_btn.connect("clicked", self._on_spinner_btn_clicked)
        action_row.append(self._spinner_btn)

        self._install_btn = Gtk.Button()
        self._install_btn.connect("clicked", self._on_install_clicked)
        self._install_btn.add_css_class("install-btn")

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
        self._gear_btn.set_tooltip_text("Options")
        self._gear_btn.set_visible(False)
        action_row.append(self._gear_btn)

        self._update_install_button()
        self._setup_gear_actions()

        return box

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
            log.debug("_make_screenshots: no screenshots on entry, returning None")
            return None

        screenshots = list(self._entry.screenshots)
        log.debug("_make_screenshots: %d screenshot(s): %s", len(screenshots), screenshots)

        # Fast path: peek the cache for every screenshot.  If all are already
        # on disk, build the carousel synchronously — no placeholders, no shift.
        cached_paths = []
        for s in screenshots:
            p = self._peek(s)
            exists = bool(p) and os.path.isfile(p)
            log.debug("  peek %r -> %r  isfile=%s", s, p, exists)
            if exists:
                cached_paths.append(p)
            else:
                cached_paths = []
                break

        wrapper = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        wrapper.add_css_class("screenshots-band")

        if cached_paths:
            log.debug("_make_screenshots: fast path — all %d cached", len(cached_paths))
            self._screenshot_paths = cached_paths
            pages = [self._make_screenshot_pic(p, i) for i, p in enumerate(cached_paths)]
            self._populate_screenshots(wrapper, pages)
            return wrapper

        log.debug("_make_screenshots: slow path — starting per-slot downloads")

        # Slow path: build the carousel immediately with fixed-height placeholder
        # slots so the layout reserves the correct space — no jump when images load.
        slots: list[Gtk.Box] = []
        for _ in screenshots:
            slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            slot.set_size_request(-1, 300)
            slot.set_margin_start(8)
            slot.set_margin_end(8)
            slot.set_margin_top(4)
            slot.set_margin_bottom(10)
            spinner = Adw.Spinner()
            spinner.set_halign(Gtk.Align.CENTER)
            spinner.set_valign(Gtk.Align.CENTER)
            slot.append(spinner)
            slots.append(slot)

        self._populate_screenshots(wrapper, slots)

        # Pre-allocate; each per-slot task fills its own index when resolved.
        self._screenshot_paths = [""] * len(screenshots)

        for slot_idx, (s, slot) in enumerate(zip(screenshots, slots)):

            def _work(s=s, idx=slot_idx) -> str:
                path = self._resolve(s)
                result = path if os.path.isfile(path) else ""
                log.debug("  slot %d: resolve(%r) -> %r  isfile=%s", idx, s, path, bool(result))
                return result

            def _on_slot_done(path: str, slot=slot, idx=slot_idx) -> None:
                log.debug("  slot %d: _on_slot_done path=%r", idx, path)
                if not path:
                    log.debug("  slot %d: empty path — leaving spinner", idx)
                    return  # leave spinner; download failed
                # Remove spinner
                child = slot.get_first_child()
                while child:
                    nxt = child.get_next_sibling()
                    slot.remove(child)
                    child = nxt
                # Insert real picture (margins are already on the slot)
                pic = Gtk.Picture.new_for_filename(path)
                pic.set_content_fit(Gtk.ContentFit.CONTAIN)
                pic.set_can_shrink(True)
                pic.set_overflow(Gtk.Overflow.HIDDEN)
                pic.add_css_class("screenshot-pic")
                click = Gtk.GestureClick()
                click.connect("released", self._on_screenshot_clicked, idx)
                pic.add_controller(click)
                slot.append(pic)
                self._screenshot_paths[idx] = path
                log.debug("  slot %d: picture inserted for %r", idx, path)

            run_in_background(_work, on_done=_on_slot_done)

        return wrapper

    def _make_screenshot_pic(self, path: str, idx: int) -> Gtk.Picture:
        """Build a single carousel page widget for a screenshot at *path*."""
        pic = Gtk.Picture.new_for_filename(path)
        pic.set_content_fit(Gtk.ContentFit.CONTAIN)
        pic.set_can_shrink(True)
        pic.set_size_request(-1, 300)
        pic.set_cursor(Gdk.Cursor.new_from_name("pointer"))
        pic.set_overflow(Gtk.Overflow.HIDDEN)
        pic.set_margin_start(8)
        pic.set_margin_end(8)
        pic.set_margin_top(4)
        pic.set_margin_bottom(10)
        pic.add_css_class("screenshot-pic")
        click = Gtk.GestureClick()
        click.connect("released", self._on_screenshot_clicked, idx)
        pic.add_controller(click)
        return pic

    def _populate_screenshots(self, wrapper: Gtk.Box, pages: list[Gtk.Widget]) -> None:
        """Build and append carousel content into *wrapper* from pre-built page widgets."""
        carousel = Adw.Carousel(
            allow_scroll_wheel=False, reveal_duration=200, spacing=12,
        )
        multiple = len(pages) > 1

        for page in pages:
            carousel.append(page)

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
        # Filter to only loaded paths (some may still be downloading).
        # Re-map start index so the correct image opens in the dialog.
        loaded = [(i, p) for i, p in enumerate(self._screenshot_paths) if p]
        paths = [p for _, p in loaded]
        start = next((pos for pos, (i, _) in enumerate(loaded) if i == index), 0)
        dialog = ScreenshotDialog(paths, start)
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
            delta_size = (self._installed_record or {}).get("delta_size") or 0
            if stored_size:
                display_size = stored_size
                # On CoW filesystems, delta apps share base files via reflinks —
                # show the delta-only size which reflects actual unique disk usage.
                if delta_size:
                    from cellar.utils.paths import is_cow_filesystem as _is_cow
                    _rec = self._installed_record or {}
                    _install_path = _rec.get("install_path", "")
                    if not _install_path:
                        from cellar.backend.umu import prefixes_dir as _prefixes_dir
                        _install_path = str(_prefixes_dir())
                    if _is_cow(_install_path):
                        display_size = delta_size
                _add(_simple_card(
                    "drive-harddisk-symbolic", _fmt_bytes(display_size), "Install size",
                )[0])
        else:
            if e.archive_size > 0:
                # Use pre-resolved total when available.
                dl_total = e.archive_size
                if self._base_installed is not None:
                    if not self._base_installed:
                        dl_total += self._base_sz
                    if not self._runner_installed:
                        dl_total += self._runner_sz
                dl_card, dl_val_lbl = _simple_card(
                    "folder-download-symbolic", _fmt_bytes(dl_total), "Download",
                )
                _make_interactive(dl_card, self._show_download_dialog)
                _add(dl_card)
                self._resolve_base_async(dl_val_lbl)


        if e.platform == "linux":
            _add(_simple_card("penguin-alt-symbolic", "Native", "Linux")[0])
        elif e.platform == "dos":
            _add(_simple_card("floppy-symbolic", "DOSBox", "DOS")[0])
        else:
            _add(self._make_wine_card())
            self._resolve_base_async()

        if e.version:
            _add(_simple_card("software-update-available-symbolic", e.version, "Version")[0])

        if e.release_year:
            _add(_simple_card("x-office-calendar-symbolic", str(e.release_year), "Released")[0])

        if e.category:
            cat_card = _simple_card(e.category_icon or "tag-symbolic", e.category, "Category")[0]
            if e.genres:
                _make_interactive(cat_card, lambda: self._show_genre_popover(cat_card))
            _add(cat_card)

        first = outer.get_first_child()
        last = outer.get_last_child()
        if first:
            first.add_css_class("info-cell-first")
        if last and last is not first:
            last.add_css_class("info-cell-last")

        return outer

    def _rebuild_info_cards(self) -> None:
        """Replace the info cards row in-place (e.g. after install/remove)."""
        new_cards = self._make_info_cards()
        parent = self._info_cards.get_parent()
        if parent is not None:
            prev = self._info_cards.get_prev_sibling()
            parent.remove(self._info_cards)
            if prev is not None:
                parent.insert_child_after(new_cards, prev)
            else:
                parent.prepend(new_cards)
        self._info_cards = new_cards

    def _show_genre_popover(self, anchor: Gtk.Widget) -> None:
        """Show a popover with genre pills anchored to *anchor*."""
        popover = Gtk.Popover()
        popover.set_parent(anchor)
        popover.set_position(Gtk.PositionType.TOP)

        flow = Gtk.FlowBox()
        flow.set_homogeneous(False)
        flow.set_max_children_per_line(4)
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_margin_top(4)
        flow.set_margin_bottom(4)
        flow.set_margin_start(4)
        flow.set_margin_end(4)
        flow.set_row_spacing(4)
        flow.set_column_spacing(4)

        for genre in self._entry.genres:
            pill = Gtk.Button(label=genre)
            pill.add_css_class("pill")
            pill.add_css_class("genre-pill")
            if self._on_genre_filter:
                pill.connect("clicked", self._on_genre_pill_clicked, genre, popover)
            else:
                pill.set_sensitive(False)
            flow.insert(pill, -1)

        popover.set_child(flow)
        popover.popup()

    def _on_genre_pill_clicked(self, _btn: Gtk.Button, genre: str, popover: Gtk.Popover) -> None:
        popover.popdown()
        if self._on_genre_filter:
            self._on_genre_filter(genre)

    def _make_wine_card(self) -> Gtk.Box:
        """Return a card showing the Wine base image."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("info-cell")
        card.set_hexpand(True)

        icon = Gtk.Image.new_from_icon_name("system-run-symbolic")
        icon.set_pixel_size(24)
        icon.set_halign(Gtk.Align.CENTER)
        card.append(icon)

        runner_name = self._resolved_runner or ("…" if self._entry.base_image else "")
        runner_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        runner_row.set_halign(Gtk.Align.CENTER)
        card.append(runner_row)

        self._runner_label = Gtk.Label(label=runner_name)
        self._runner_label.add_css_class("heading")
        runner_row.append(self._runner_label)

        self._base_warning_icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        self._base_warning_icon.add_css_class("warning")
        self._base_warning_icon.set_visible(False)
        runner_row.append(self._base_warning_icon)

        bottom_lbl = Gtk.Label(label="Wine")
        bottom_lbl.add_css_class("dim-label")
        bottom_lbl.add_css_class("caption")
        bottom_lbl.set_halign(Gtk.Align.CENTER)
        card.append(bottom_lbl)

        return card

    def _get_base_image(self) -> str:
        """Return the base image key for this entry."""
        return self._entry.base_image

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

    def _find_runner_entry(self, runner_name: str) -> tuple[object | None, str]:
        """Return (RunnerEntry, archive_uri) for *runner_name*, or (None, "") if not found."""
        for repo in self._source_repos:
            try:
                runners = repo.fetch_runners()
                if runner_name in runners:
                    entry = runners[runner_name]
                    return entry, repo.resolve_asset_uri(entry.archive)
            except Exception:
                pass
        return None, ""

    def _resolve_base_async(self, val_lbl: Gtk.Label | None = None) -> None:
        """Resolve base image size + installed status once; cache on self for the dialog."""
        base_image = self._get_base_image()
        if not base_image:
            return

        # Already resolved (e.g. by _prefetch_then_build) — apply immediately.
        if self._base_installed is not None:
            if val_lbl is not None:
                app_size = self._entry.archive_size
                total = app_size
                if not self._base_installed:
                    total += self._base_sz
                if not self._runner_installed:
                    total += self._runner_sz
                if total != app_size:
                    val_lbl.set_label(_fmt_bytes(total))
            if not self._base_installed and self._base_warning_icon:
                self._base_warning_icon.set_visible(True)
                self._base_warning_icon.set_tooltip_text(
                    f"Base image \u201c{base_image}\u201d is not installed"
                )
            if self._resolved_runner and self._runner_label:
                self._runner_label.set_label(self._resolved_runner)
            for cb in self._base_resolve_cbs:
                cb(self._base_installed, self._base_sz,
                   self._runner_installed, self._runner_sz)
            self._base_resolve_cbs.clear()
            return

        app_size = self._entry.archive_size

        def _work():
            from cellar.backend.base_store import is_base_installed
            from cellar.backend.umu import resolve_runner_path
            installed = is_base_installed(base_image)
            base_entry, _ = self._find_base_entry(base_image)
            base_sz = base_entry.archive_size if base_entry else 0
            runner = base_entry.runner if base_entry else ""
            runner_installed = bool(resolve_runner_path(runner)) if runner else True
            runner_entry, _ = self._find_runner_entry(runner) if runner else (None, "")
            runner_sz = runner_entry.archive_size if runner_entry else 0
            return installed, base_sz, runner, runner_installed, runner_sz

        def _apply(result) -> None:
            installed, base_sz, runner, runner_installed, runner_sz = result
            self._base_installed = installed
            self._base_sz = base_sz
            self._runner_installed = runner_installed
            self._runner_sz = runner_sz
            if runner:
                self._resolved_runner = runner
                if self._runner_label:
                    self._runner_label.set_label(runner)
            if val_lbl is not None:
                total = app_size
                if not installed:
                    total += base_sz
                if not runner_installed:
                    total += runner_sz
                if total != app_size:
                    val_lbl.set_label(_fmt_bytes(total))
            if not installed and self._base_warning_icon:
                self._base_warning_icon.set_visible(True)
                self._base_warning_icon.set_tooltip_text(
                    f"Base image \u201c{base_image}\u201d is not installed"
                )
            for cb in self._base_resolve_cbs:
                cb(installed, base_sz, runner_installed, runner_sz)
            self._base_resolve_cbs.clear()

        run_in_background(_work, on_done=_apply)

    def _show_download_dialog(self) -> None:
        """Show a download size breakdown: header pill + per-component rows."""
        e = self._entry
        base_image = self._get_base_image()

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
        # Compute initial value: if base+runner are already resolved, sum what's missing.
        if not base_image:
            _initial_total = _fmt_bytes(e.archive_size) if e.archive_size else "Unknown"
        elif self._base_installed is not None and self._runner_installed is not None:
            _total_sz = (e.archive_size
                         + (0 if self._base_installed else self._base_sz)
                         + (0 if self._runner_installed else self._runner_sz))
            _initial_total = _fmt_bytes(_total_sz) if _total_sz else _fmt_bytes(e.archive_size)
        else:
            _initial_total = "…"
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

        if base_image:
            base_pill = _pill("…")
            base_action_row = _row(base_pill, "Base Image", "…")
            listbox.append(base_action_row)
            runner_pill = _pill("…")
            runner_action_row = _row(runner_pill, "Runner", "…")
            listbox.append(runner_action_row)

        # ── Layout ──────────────────────────────────────────────────
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        set_margins(content, 18)
        content.append(header)
        content.append(listbox)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(content)

        dlg = Adw.Dialog(title="Download", content_width=380)
        dlg.set_child(toolbar)
        dlg.present(self)

        # ── Populate base + runner rows from cached resolution ────────
        if base_image and self._base_installed is not None and self._runner_installed is not None:
            # Already resolved — total_pill was pre-computed above; just fill the rows.
            base_pill.set_label(_fmt_bytes(self._base_sz) if self._base_sz else "Unknown")
            base_action_row.set_subtitle(_base_status_subtitle(self._base_installed))
            runner_pill.set_label(_fmt_bytes(self._runner_sz) if self._runner_sz else "Unknown")
            runner_action_row.set_subtitle(_base_status_subtitle(self._runner_installed))
        elif base_image:
            # Resolution not yet done (rare — dialog opened within ms of page load).
            _bp, _br = base_pill, base_action_row
            _rp, _rr = runner_pill, runner_action_row
            _t, _app = total_pill, e.archive_size

            def _on_resolved(
                installed: bool, base_sz: int, runner_installed: bool, runner_sz: int
            ) -> None:
                total = (
                    _app + (0 if installed else base_sz) + (0 if runner_installed else runner_sz)
                )
                _bp.set_label(_fmt_bytes(base_sz) if base_sz else "Unknown")
                _br.set_subtitle(_base_status_subtitle(installed))
                _rp.set_label(_fmt_bytes(runner_sz) if runner_sz else "Unknown")
                _rr.set_subtitle(_base_status_subtitle(runner_installed))
                _t.set_label(_fmt_bytes(total) if total else _fmt_bytes(_app))

            self._base_resolve_cbs.append(_on_resolved)

    def _show_wine_dialog(self) -> None:
        """Show Wine runner details."""
        runner_name = self._resolved_runner

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
        if self._is_installed and self._entry.lock_runner:
            lock_icon = Gtk.Image.new_from_icon_name("changes-prevent-symbolic")
            lock_icon.add_css_class("dim-label")
            lock_icon.set_valign(Gtk.Align.CENTER)
            runner_row.add_suffix(lock_icon)
        runner_listbox.append(runner_row)

        # ── Layout ───────────────────────────────────────────────────
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        set_margins(content, 18)
        content.append(runner_lbl)
        content.append(runner_listbox)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(Adw.HeaderBar())
        toolbar.set_content(content)

        dlg = Adw.Dialog(title="Wine", content_width=340)
        dlg.set_child(toolbar)
        dlg.present(self)

    # ------------------------------------------------------------------
    # Asset helpers
    # ------------------------------------------------------------------

    def _make_icon(self, rel_path: str, size: int, *, cover_fallback: str = "") -> Gtk.Widget:
        def _build_widget(path: str, is_cover: bool) -> Gtk.Widget:
            if is_cover:
                pic = Gtk.Picture.new_for_filename(path)
                pic.set_size_request(size, size)
                pic.set_content_fit(Gtk.ContentFit.COVER)
                pic.add_css_class("icon-dropshadow")
                return pic
            img = Gtk.Image.new_from_file(path)
            img.set_pixel_size(size)
            return img

        # Fast path: if the image is already on disk, load natively.
        for path_arg, is_cover in ((rel_path, False), (cover_fallback, True)):
            if not path_arg:
                continue
            cached = self._peek(path_arg)
            if cached and os.path.isfile(cached):
                return _build_widget(cached, is_cover)

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
            for rp in (icon_path, fallback_path):
                if rp:
                    path = self._resolve(rp)
                    if os.path.isfile(path):
                        return path, rp == fallback_path
            return None

        def _on_loaded(result) -> None:
            if result is None:
                return
            path, is_cover = result
            real = _build_widget(path, is_cover)
            stack.add_named(real, "real")
            stack.set_visible_child_name("real")

        run_in_background(_work, on_done=_on_loaded)
        return stack

    def _make_logo_widget(self, rel_path: str, target_height: int) -> Gtk.Widget:
        """Return a widget displaying the transparent logo at *target_height* px tall.

        Logos are bbox-cropped at import time, so the file already contains
        only the visible content.  GTK handles scaling natively.
        """
        def _build_picture(path: str) -> Gtk.Picture:
            pic = Gtk.Picture.new_for_filename(path)
            pic.set_content_fit(Gtk.ContentFit.SCALE_DOWN)
            pic.set_halign(Gtk.Align.START)
            pic.set_can_shrink(False)
            pic.set_size_request(-1, target_height)
            pic.add_css_class("logo-pic")
            return pic

        cached = self._peek(rel_path)
        if cached and os.path.isfile(cached):
            return _build_picture(cached)

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
                return path
            return None

        def _on_loaded(path) -> None:
            if path is None:
                return
            pic = _build_picture(path)
            stack.add_named(pic, "real")
            stack.set_visible_child_name("real")

        run_in_background(_work, on_done=_on_loaded)
        return stack


# ---------------------------------------------------------------------------
# Install progress dialog
# ---------------------------------------------------------------------------


class InstallProgressDialog(Adw.Dialog):
    """Install progress dialog — downloads base (if needed) + app archive.

    Shows a progress bar and Cancel button.  Starts automatically on present.
    """

    def __init__(
        self,
        *,
        entry: AppEntry,
        archive_uri: str,
        on_success: Callable,  # (prefix_dir, install_path, runner, install_size, delta_size) -> None
        token: str | None = None,
        ssh_identity: str | None = None,
        base_entry=None,            # BaseEntry | None — for delta installs
        base_archive_uri: str = "", # resolved URI for the base archive
        runner_entry=None,            # RunnerEntry | None
        runner_archive_uri: str = "", # resolved URI for the runner archive
    ) -> None:
        super().__init__(title=f"Install {entry.name}", content_width=360)
        self._entry = entry
        self._archive_uri = archive_uri
        self._on_success = on_success
        self._token = token
        self._ssh_identity = ssh_identity
        self._cancel_event = threading.Event()
        self._base_entry = base_entry
        self._base_archive_uri = base_archive_uri
        self._runner_entry = runner_entry
        self._runner_archive_uri = runner_archive_uri

        self._build_ui()
        self.connect("closed", lambda _d: self._cancel_event.set())

        GLib.idle_add(self._start_install)

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()

        self._header = Adw.HeaderBar()
        self._header.set_show_end_title_buttons(False)
        self._header.set_visible(False)

        toolbar_view.add_top_bar(self._header)
        toolbar_view.set_content(self._build_progress_page())
        self.set_child(toolbar_view)

    def _build_progress_page(self) -> Gtk.Widget:
        box, self._phase_label, self._progress_bar, self._cancel_body_btn = (
            make_progress_page("Downloading", self._on_cancel_progress_clicked)
        )
        return box

    def _on_cancel_progress_clicked(self, _btn) -> None:
        self._cancel_event.set()
        self._phase_label.set_text("Cancelling…")
        self._cancel_body_btn.set_sensitive(False)

    # ── Install thread ────────────────────────────────────────────────────

    def _start_install(self) -> None:
        if self._entry.platform in ("linux", "dos"):
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
                prefix_dir = install_app(
                    self._entry,
                    self._archive_uri,
                    base_entry=self._base_entry,
                    base_archive_uri=self._base_archive_uri,
                    runner_entry=self._runner_entry,
                    runner_archive_uri=self._runner_archive_uri,
                    download_cb=_dl_progress,
                    download_stats_cb=_dl_stats,
                    install_cb=_inst_progress,
                    phase_cb=_set_phase,
                    cancel_event=self._cancel_event,
                    token=self._token,
                    ssh_identity=self._ssh_identity,
                )
                from cellar.backend.umu import prefixes_dir as _prefixes_dir
                from cellar.utils.paths import dir_size_bytes as _dir_size
                _install_size = _dir_size(_prefixes_dir() / prefix_dir)
                GLib.idle_add(
                    self._on_done, prefix_dir, str(_prefixes_dir()), "",
                    _install_size, self._entry.delta_size,
                )
            except InstallCancelled:
                GLib.idle_add(self._on_cancelled)
            except Exception as exc:  # noqa: BLE001
                log.error("Install failed: %s", exc, exc_info=True)
                GLib.idle_add(self._on_error, user_facing_error(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _start_linux_install(self) -> None:
        """Background install for Linux native and DOS apps."""
        from cellar.backend.installer import InstallCancelled, install_dos_app, install_linux_app
        _installer = install_dos_app if self._entry.platform == "dos" else install_linux_app

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
                _app_id, install_dest = _installer(
                    self._entry,
                    self._archive_uri,
                    download_cb=_dl_progress,
                    download_stats_cb=_dl_stats,
                    install_cb=_inst_progress,
                    phase_cb=_set_phase,
                    cancel_event=self._cancel_event,
                    token=self._token,
                    ssh_identity=self._ssh_identity,
                )
                from cellar.utils.paths import dir_size_bytes as _dir_size
                _install_size = _dir_size(install_dest)
                GLib.idle_add(
                    self._on_done, self._entry.id, str(install_dest.parent), "",
                    _install_size, 0,
                )
            except InstallCancelled:
                GLib.idle_add(self._on_cancelled)
            except Exception as exc:  # noqa: BLE001
                log.error("Install failed: %s", exc, exc_info=True)
                GLib.idle_add(self._on_error, user_facing_error(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _on_phase_change(self, label: str) -> None:
        """Reset bar and update label on phase transition (runs on UI thread)."""
        # Stop any active pulse
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self._phase_label.set_text(label)
        if (
            "Copying" in label or "Applying delta" in label
            or "Installing" in label or "Initialising" in label
        ):
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

    def _on_done(
        self, prefix_dir: str, install_path: str = "", runner: str = "",
        install_size: int = 0, delta_size: int = 0,
    ) -> None:
        self.close()
        self._on_success(prefix_dir, install_path, runner, install_size, delta_size)

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

def _parse_launch_env(env_str: str) -> dict[str, str]:
    """Parse a launch-environment string into a ``{KEY: value}`` dict.

    Accepts Steam-style launch option strings copied from ProtonDB, e.g.::

        PROTON_USE_WINED3D=1 PROTON_NO_ESYNC=1 %command%

    Rules:
    - Tokens matching ``KEY=VALUE`` (identifier before ``=``) are kept.
    - ``%command%`` and any other non-``KEY=VALUE`` tokens are silently dropped.
    - Duplicate keys: last value wins.
    """
    import shlex
    result: dict[str, str] = {}
    try:
        tokens = shlex.split(env_str)
    except ValueError:
        tokens = env_str.split()
    for tok in tokens:
        if tok == "%command%":
            continue
        eq = tok.find("=")
        if eq <= 0:
            continue
        key = tok[:eq]
        # Key must look like an env-var identifier (letters, digits, underscores)
        if not key.replace("_", "").isalnum():
            continue
        result[key] = tok[eq + 1:]
    return result


def _base_status_subtitle(installed: bool) -> str:
    return "Already present on your system" if installed else "Will also be downloaded"


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if n < 1000:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1000
    return f"{n:.1f} PB"

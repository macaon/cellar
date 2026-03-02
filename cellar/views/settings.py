"""Settings / Preferences dialog."""

from __future__ import annotations

import json
import logging
import threading
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.backend.config import certs_dir, load_repos, save_repos

log = logging.getLogger(__name__)

# Human-readable labels for bottle.yml Windows: values.
_WIN_VER_LABELS: dict[str, str] = {
    "win10": "Windows 10",
    "win11": "Windows 11",
    "win7": "Windows 7",
    "win8": "Windows 8",
    "win81": "Windows 8.1",
    "winxp": "Windows XP",
    "win2k": "Windows 2000",
}


class SettingsDialog(Adw.PreferencesDialog):
    """Application preferences window.

    Shown via the hamburger menu → Preferences.  Currently exposes repo
    management; more groups will be added as later phases land.
    """

    def __init__(
        self,
        *,
        on_repos_changed: Callable[[], None] | None = None,
        writable_repos: list | None = None,
        **kwargs,
    ):
        super().__init__(title="Preferences", content_width=560, content_height=500, **kwargs)
        self._on_repos_changed = on_repos_changed
        self._writable_repos: list = writable_repos or []
        self._repo_rows: list[Adw.PreferencesRow] = []
        self._delta_rows: list[Adw.PreferencesRow] = []

        # ── Page: General ─────────────────────────────────────────────────
        page = Adw.PreferencesPage(
            title="General",
            icon_name="preferences-other-symbolic",
        )
        self.add(page)

        # ── Group: Repositories ───────────────────────────────────────────
        self._repo_group = Adw.PreferencesGroup(title="Repositories")
        page.add(self._repo_group)

        add_btn = Gtk.Button(label="Add")
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._on_add_clicked)
        self._repo_group.set_header_suffix(add_btn)

        # ── Group: Access Control ─────────────────────────────────────────
        access_group = Adw.PreferencesGroup(
            title="Access Control",
            description=(
                "Restrict access to an HTTP(S) repository you host by "
                "requiring a bearer token. Generate one here, add it to "
                "your web server, then share the URL and token with anyone "
                "who should have access. See the README for nginx and Caddy "
                "configuration examples."
            ),
        )
        page.add(access_group)

        gen_row = Adw.ActionRow(
            title="Generate access token",
            subtitle="Creates a random token to use in your web server config",
        )
        gen_btn = Gtk.Button(
            label="Generate",
            valign=Gtk.Align.CENTER,
        )
        gen_btn.add_css_class("suggested-action")
        gen_btn.connect("clicked", self._on_generate_token)
        gen_row.add_suffix(gen_btn)
        access_group.add(gen_row)

        # ── Group: Delta Base Images ───────────────────────────────────────
        self._delta_group = Adw.PreferencesGroup(title="Delta Base Images")
        page.add(self._delta_group)

        add_base_btn = Gtk.Button(label="Add")
        add_base_btn.add_css_class("suggested-action")
        add_base_btn.connect("clicked", self._on_upload_base_clicked)
        self._delta_group.set_header_suffix(add_base_btn)

        self._rebuild_repo_rows()
        self._rebuild_delta_rows()

    # ------------------------------------------------------------------
    # Repo list management
    # ------------------------------------------------------------------

    def _rebuild_repo_rows(self) -> None:
        """Sync the visible rows with the on-disk repo list."""
        for row in self._repo_rows:
            self._repo_group.remove(row)
        self._repo_rows.clear()

        for repo_cfg in load_repos():
            row = self._make_repo_row(repo_cfg)
            self._repo_rows.append(row)
            self._repo_group.add(row)

    def _make_repo_row(self, repo_cfg: dict) -> Adw.ActionRow:
        name = repo_cfg.get("name") or ""
        uri = repo_cfg["uri"]
        ca_cert = repo_cfg.get("ca_cert") or ""
        ssl_verify = repo_cfg.get("ssl_verify", True)
        token = repo_cfg.get("token") or ""

        title = name if name else uri
        subtitle = uri if name else ""

        row = Adw.ActionRow(title=title, subtitle=subtitle)

        if token:
            icon = Gtk.Image.new_from_icon_name("channel-secure-symbolic")
            icon.set_tooltip_text("Bearer token configured")
            row.add_prefix(icon)
        elif ca_cert:
            icon = Gtk.Image.new_from_icon_name("security-high-symbolic")
            icon.set_tooltip_text(f"CA certificate: {Path(ca_cert).name}")
            row.add_prefix(icon)
        elif not ssl_verify:
            icon = Gtk.Image.new_from_icon_name("security-low-symbolic")
            icon.set_tooltip_text("SSL verification disabled")
            row.add_prefix(icon)

        edit_btn = Gtk.Button(
            icon_name="document-edit-symbolic",
            valign=Gtk.Align.CENTER,
            has_frame=False,
            tooltip_text="Edit repository",
        )
        edit_btn.connect("clicked", self._on_edit_repo, repo_cfg)
        row.add_suffix(edit_btn)

        del_btn = Gtk.Button(
            icon_name="user-trash-symbolic",
            valign=Gtk.Align.CENTER,
            has_frame=False,
            tooltip_text="Remove repository",
        )
        del_btn.add_css_class("destructive-action")
        del_btn.connect("clicked", self._on_delete_repo, uri)
        row.add_suffix(del_btn)

        return row

    # ------------------------------------------------------------------
    # Delta Base Images
    # ------------------------------------------------------------------

    def _rebuild_delta_rows(self) -> None:
        """Sync visible base-image rows with the local database."""
        for row in self._delta_rows:
            self._delta_group.remove(row)
        self._delta_rows.clear()

        from cellar.backend import database

        bases = database.get_all_installed_bases()
        if not bases:
            empty = Adw.ActionRow(title="No base images installed")
            empty.set_sensitive(False)
            self._delta_rows.append(empty)
            self._delta_group.add(empty)
            return

        for rec in bases:
            row = self._make_base_row(rec)
            self._delta_rows.append(row)
            self._delta_group.add(row)

    def _make_base_row(self, rec: dict) -> Adw.ActionRow:
        win_ver = rec["win_ver"]
        label = _WIN_VER_LABELS.get(win_ver, win_ver)
        installed_at = rec.get("installed_at", "")
        if installed_at:
            try:
                dt = datetime.fromisoformat(installed_at)
                installed_at = dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
        subtitle = f"{win_ver} · installed {installed_at}" if installed_at else win_ver

        row = Adw.ActionRow(title=label, subtitle=subtitle)

        del_btn = Gtk.Button(
            icon_name="user-trash-symbolic",
            valign=Gtk.Align.CENTER,
            has_frame=False,
            tooltip_text="Remove base image",
        )
        del_btn.add_css_class("destructive-action")
        del_btn.connect("clicked", self._on_remove_base, win_ver, label)
        row.add_suffix(del_btn)
        return row

    def _on_upload_base_clicked(self, _btn: Gtk.Button) -> None:
        chooser = Gtk.FileChooserNative(
            title="Select Base Image Archive",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
        )
        f = Gtk.FileFilter()
        f.set_name("Bottles backup archives (*.tar.gz)")
        f.add_pattern("*.tar.gz")
        chooser.add_filter(f)
        chooser.connect("response", self._on_base_archive_chosen, chooser)
        chooser.show()

    def _on_base_archive_chosen(
        self, _chooser, response: int, chooser: Gtk.FileChooserNative
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        path = chooser.get_file().get_path()
        if not path:
            return
        dialog = UploadBaseDialog(
            archive_path=path,
            writable_repos=self._writable_repos,
            on_done=self._rebuild_delta_rows,
        )
        dialog.present(self)

    def _on_remove_base(self, _btn: Gtk.Button, win_ver: str, label: str) -> None:
        alert = Adw.AlertDialog(
            heading=f"Remove {label} Base?",
            body=(
                f"The {label} base image will be removed from this machine. "
                "Apps that use this base can still be installed — the base "
                "will be downloaded again automatically."
            ),
        )
        alert.add_response("cancel", "Cancel")
        alert.add_response("remove", "Remove")
        alert.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        alert.connect("response", self._on_remove_base_confirmed, win_ver)
        alert.present(self)

    def _on_remove_base_confirmed(self, _alert, response: str, win_ver: str) -> None:
        if response != "remove":
            return
        from cellar.backend import base_store
        base_store.remove_base(win_ver)
        self._rebuild_delta_rows()

    # ------------------------------------------------------------------
    # Add / Edit handlers
    # ------------------------------------------------------------------

    def _on_add_clicked(self, _btn: Gtk.Button) -> None:
        def _save(cfg: dict) -> None:
            repos = load_repos()
            repos.append(cfg)
            save_repos(repos)
            self._rebuild_repo_rows()
            if self._on_repos_changed:
                self._on_repos_changed()

        dialog = AddEditRepoDialog(on_save=_save)
        dialog.present(self)

    def _on_edit_repo(self, _btn: Gtk.Button, repo_cfg: dict) -> None:
        old_uri = repo_cfg["uri"]

        def _save(cfg: dict) -> None:
            repos = load_repos()
            for i, r in enumerate(repos):
                if r["uri"] == old_uri:
                    repos[i] = cfg
                    break
            save_repos(repos)
            self._rebuild_repo_rows()
            if self._on_repos_changed:
                self._on_repos_changed()

        dialog = AddEditRepoDialog(on_save=_save, existing=repo_cfg)
        dialog.present(self)

    # ------------------------------------------------------------------
    # Delete handler
    # ------------------------------------------------------------------

    def _on_delete_repo(self, _btn: Gtk.Button, uri: str) -> None:
        repos = [r for r in load_repos() if r["uri"] != uri]
        save_repos(repos)
        self._rebuild_repo_rows()
        if self._on_repos_changed:
            self._on_repos_changed()

    # ------------------------------------------------------------------
    # Access Control
    # ------------------------------------------------------------------

    def _on_generate_token(self, _btn: Gtk.Button) -> None:
        """Generate a token and display it in a dialog for copying."""
        import secrets
        token = secrets.token_hex(32)
        label = Gtk.Label(
            label=token,
            wrap=True,
            wrap_mode=2,        # WORD_CHAR
            selectable=True,
            xalign=0,
            css_classes=["monospace"],
            margin_top=8,
        )
        dialog = Adw.AlertDialog(
            heading="Generated Token",
            body=(
                "Add this token to your web server configuration, then share "
                "it with anyone who should have access to your repository."
            ),
            extra_child=label,
        )
        dialog.add_response("copy", "Copy to Clipboard")
        dialog.add_response("ok", "Done")
        dialog.set_default_response("copy")
        dialog.set_response_appearance("copy", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", lambda _d, r: self.get_clipboard().set(token) if r == "copy" else None)
        dialog.present(self)

    def _alert(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)


# ---------------------------------------------------------------------------
# Upload Base Image dialog
# ---------------------------------------------------------------------------


def _fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def _fmt_ul_stats(copied: int, total: int, speed: float) -> str:
    size_str = f"{_fmt_size(copied)} / {_fmt_size(total)}" if total > 0 else _fmt_size(copied)
    speed_str = f"{_fmt_size(int(speed))}/s" if speed > 0 else "\u2026"
    return f"{size_str} ({speed_str})"


class UploadBaseDialog(Adw.Dialog):
    """Two-phase dialog: scan archive → confirm/upload base image.

    Phase 1 (scan): reads bottle.yml in a background thread, shows a
    progress bar, then switches to the form page.
    Phase 2 (upload): installs the base locally and optionally copies
    the archive to a writable repository.
    """

    def __init__(
        self,
        *,
        archive_path: str,
        writable_repos: list,   # list[cellar.backend.repo.Repo]
        on_done: Callable[[], None],
    ) -> None:
        super().__init__(title="Upload Base Image", content_width=480)
        self._archive_path = archive_path
        self._writable_repos = writable_repos
        self._on_done = on_done
        self._win_ver = ""
        self._cancel_event = threading.Event()
        self._pulse_id: int | None = None
        self._build_ui()
        self._pulse_id = GLib.timeout_add(80, self._do_pulse)
        threading.Thread(target=self._scan, daemon=True).start()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        self._cancel_btn = Gtk.Button(label="Cancel")
        self._cancel_btn.connect("clicked", self._on_cancel)
        header.pack_start(self._cancel_btn)

        self._upload_btn = Gtk.Button(label="Upload")
        self._upload_btn.add_css_class("suggested-action")
        self._upload_btn.set_sensitive(False)
        self._upload_btn.set_visible(False)
        self._upload_btn.connect("clicked", self._on_upload_clicked)
        header.pack_end(self._upload_btn)

        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()
        self._stack.add_named(self._build_scan_page(), "scan")
        self._stack.add_named(self._build_form_page(), "form")
        self._stack.add_named(self._build_progress_page(), "progress")
        self._stack.set_visible_child_name("scan")

        toolbar.set_content(self._stack)
        self.set_child(toolbar)

    def _build_scan_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(48)
        box.set_margin_bottom(48)
        box.set_margin_start(24)
        box.set_margin_end(24)
        box.append(Gtk.Label(label="Reading archive\u2026", css_classes=["dim-label"]))
        self._scan_bar = Gtk.ProgressBar()
        self._scan_bar.set_pulse_step(0.05)
        box.append(self._scan_bar)
        return box

    def _do_pulse(self) -> bool:
        self._scan_bar.pulse()
        return True  # keep firing

    def _build_form_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        scroll.set_propagate_natural_height(True)
        page = Adw.PreferencesPage()
        scroll.set_child(page)

        group = Adw.PreferencesGroup()
        page.add(group)

        self._win_ver_row = Adw.ActionRow(title="Windows version")
        self._win_ver_row.set_subtitle("")
        group.add(self._win_ver_row)

        self._already_installed_row = Adw.ActionRow(
            title="Replaces existing base",
            subtitle="A base for this Windows version is already installed locally",
        )
        icon = Gtk.Image.new_from_icon_name("dialog-warning-symbolic")
        icon.set_icon_size(Gtk.IconSize.NORMAL)
        self._already_installed_row.add_prefix(icon)
        self._already_installed_row.set_visible(False)
        group.add(self._already_installed_row)

        # Repository upload group — only shown when writable repos exist
        if self._writable_repos:
            repo_group = Adw.PreferencesGroup(title="Repository")
            page.add(repo_group)

            self._upload_repo_row = Adw.SwitchRow(
                title="Upload archive to repository",
                subtitle="Clients will download the base automatically when needed",
                active=True,
            )
            self._upload_repo_row.connect("notify::active", self._on_upload_repo_toggled)
            repo_group.add(self._upload_repo_row)

            if len(self._writable_repos) > 1:
                self._repo_row = Adw.ComboRow(title="Repository")
                model = Gtk.StringList()
                for r in self._writable_repos:
                    model.append(r.name or r.uri)
                self._repo_row.set_model(model)
                repo_group.add(self._repo_row)
            else:
                self._repo_row = None
                single_row = Adw.ActionRow(
                    title=self._writable_repos[0].name or self._writable_repos[0].uri,
                )
                single_row.set_sensitive(False)
                repo_group.add(single_row)
        else:
            self._upload_repo_row = None
            self._repo_row = None

        return scroll

    def _build_progress_page(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(24)
        box.set_margin_end(24)
        self._progress_label = Gtk.Label(label="Installing\u2026", xalign=0,
                                         css_classes=["dim-label"])
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(True)
        self._cancel_progress_btn = Gtk.Button(label="Cancel")
        self._cancel_progress_btn.set_halign(Gtk.Align.CENTER)
        self._cancel_progress_btn.set_margin_top(6)
        self._cancel_progress_btn.connect("clicked", self._on_cancel_progress)
        box.append(self._progress_label)
        box.append(self._progress_bar)
        box.append(self._cancel_progress_btn)
        return box

    # ── Scan phase ───────────────────────────────────────────────────────

    def _scan(self) -> None:
        from cellar.backend.packager import read_bottle_yml

        yml = read_bottle_yml(self._archive_path)
        win_ver = yml.get("Windows", "")

        def _apply() -> None:
            if self._pulse_id is not None:
                GLib.source_remove(self._pulse_id)
                self._pulse_id = None
            if not win_ver:
                self._show_scan_error()
                return
            self._win_ver = win_ver
            label = _WIN_VER_LABELS.get(win_ver, win_ver)
            self._win_ver_row.set_subtitle(f"{label} ({win_ver})")

            from cellar.backend.base_store import is_base_installed
            self._already_installed_row.set_visible(is_base_installed(win_ver))

            self._upload_btn.set_sensitive(True)
            self._upload_btn.set_visible(True)
            self._stack.set_visible_child_name("form")

        GLib.idle_add(_apply)

    def _show_scan_error(self) -> None:
        """Replace content with an error status page when win_ver is undetectable."""
        status = Adw.StatusPage(
            title="Cannot Detect Windows Version",
            description=(
                "No Windows: field was found in bottle.yml. "
                "Make sure this is a valid Bottles backup archive."
            ),
            icon_name="dialog-error-symbolic",
        )
        self._stack.add_named(status, "error")
        self._stack.set_visible_child_name("error")

    # ── Upload phase ─────────────────────────────────────────────────────

    def _on_upload_repo_toggled(self, row, _param) -> None:
        if self._repo_row:
            self._repo_row.set_sensitive(row.get_active())

    def _on_upload_clicked(self, _btn) -> None:
        self.set_content_height(200)
        self._cancel_btn.set_visible(False)
        self._upload_btn.set_visible(False)
        self._stack.set_visible_child_name("progress")
        self._progress_bar.set_fraction(0.0)
        self._cancel_event.clear()

        do_repo_upload = bool(
            self._upload_repo_row and self._upload_repo_row.get_active()
        )
        if do_repo_upload and self._repo_row:
            repo = self._writable_repos[self._repo_row.get_selected()]
        elif do_repo_upload and self._writable_repos:
            repo = self._writable_repos[0]
        else:
            repo = None

        win_ver = self._win_ver
        archive_path = self._archive_path

        def _run() -> None:
            try:
                self._do_upload(win_ver, archive_path, repo)
                GLib.idle_add(self._on_upload_done)
            except _Cancelled:
                GLib.idle_add(self._on_upload_cancelled)
            except Exception as exc:
                GLib.idle_add(self._on_upload_error, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _do_upload(self, win_ver: str, archive_path: str, repo) -> None:
        """Install locally, then optionally copy to repo. Runs on background thread."""
        from cellar.backend import base_store

        # Phase 1: install locally
        phase_frac = 0.5 if repo else 1.0

        def _local_prog(f: float) -> None:
            if self._cancel_event.is_set():
                raise _Cancelled
            GLib.idle_add(self._progress_label.set_text, "Installing base image locally\u2026")
            GLib.idle_add(self._progress_bar.set_fraction, f * phase_frac)

        _local_prog(0.0)
        try:
            base_store.install_base(archive_path, win_ver, progress_cb=_local_prog)
        except _Cancelled:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to install base locally: {exc}") from exc

        if self._cancel_event.is_set():
            raise _Cancelled

        if not repo:
            GLib.idle_add(self._progress_bar.set_fraction, 1.0)
            return

        # Phase 2: upload archive to repo
        GLib.idle_add(self._progress_label.set_text, "Uploading to repository\u2026")
        GLib.idle_add(self._progress_bar.set_text, "")

        from cellar.backend.packager import upsert_base

        repo_root = repo.writable_path()
        bases_dir = repo_root / "bases"
        bases_dir.mkdir(parents=True, exist_ok=True)
        dest = bases_dir / f"{win_ver}-base.tar.gz"
        archive_rel = f"bases/{win_ver}-base.tar.gz"

        src_size = Path(archive_path).stat().st_size
        chunk = 1 * 1024 * 1024
        copied = 0
        crc = 0
        start = time.monotonic()

        try:
            with open(archive_path, "rb") as src_f, open(dest, "wb") as dst_f:
                while True:
                    if self._cancel_event.is_set():
                        dst_f.close()
                        dest.unlink(missing_ok=True)
                        raise _Cancelled
                    buf = src_f.read(chunk)
                    if not buf:
                        break
                    dst_f.write(buf)
                    crc = zlib.crc32(buf, crc)
                    copied += len(buf)
                    elapsed = time.monotonic() - start
                    speed = copied / elapsed if elapsed > 0.1 else 0.0
                    GLib.idle_add(
                        self._progress_bar.set_text,
                        _fmt_ul_stats(copied, src_size, speed),
                    )
                    GLib.idle_add(
                        self._progress_bar.set_fraction,
                        0.5 + min(copied / src_size * 0.5, 0.5),
                    )
        except _Cancelled:
            raise
        except OSError as exc:
            dest.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to copy archive to repository: {exc}") from exc

        crc32_hex = format(crc & 0xFFFFFFFF, "08x")
        upsert_base(repo_root, win_ver, archive_rel, crc32_hex, src_size)
        GLib.idle_add(self._progress_bar.set_fraction, 1.0)

    # ── Result callbacks ─────────────────────────────────────────────────

    def _on_upload_done(self) -> None:
        self._on_done()
        self.close()

    def _on_upload_cancelled(self) -> None:
        self.set_content_height(-1)
        self._cancel_btn.set_visible(True)
        self._upload_btn.set_visible(True)
        self._cancel_progress_btn.set_sensitive(True)
        self._stack.set_visible_child_name("form")

    def _on_upload_error(self, message: str) -> None:
        self.set_content_height(-1)
        self._cancel_btn.set_visible(True)
        self._upload_btn.set_visible(True)
        self._cancel_progress_btn.set_sensitive(True)
        self._stack.set_visible_child_name("form")
        alert = Adw.AlertDialog(heading="Upload Failed", body=message)
        alert.add_response("ok", "OK")
        alert.present(self)

    def _on_cancel(self, _btn) -> None:
        self.close()

    def _on_cancel_progress(self, _btn) -> None:
        self._cancel_event.set()
        self._progress_label.set_text("Cancelling\u2026")
        self._cancel_progress_btn.set_sensitive(False)


class _Cancelled(Exception):
    """Internal signal used to abort the upload thread."""


# ---------------------------------------------------------------------------
# Add / Edit Repository dialog
# ---------------------------------------------------------------------------


class AddEditRepoDialog(Adw.Dialog):
    """Single dialog for adding a new repository or editing an existing one.

    Pass ``existing=None`` for "Add" mode, or ``existing=<repo-cfg-dict>``
    for "Edit" mode.  ``on_save`` is called with the validated repo config
    dict on success; closing the dialog without saving does nothing.
    """

    def __init__(
        self,
        *,
        on_save: Callable[[dict], None],
        existing: dict | None = None,
    ) -> None:
        mode = "Edit Repository" if existing else "Add Repository"
        super().__init__(title=mode, content_width=480)
        self._on_save = on_save
        self._existing = existing
        self._ca_cert_path: str | None = None
        if existing and existing.get("ca_cert"):
            self._ca_cert_path = existing["ca_cert"]
        self._build_ui()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        save_label = "Save" if self._existing else "Add"
        self._save_btn = Gtk.Button(label=save_label)
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", self._on_save_clicked)
        header.pack_end(self._save_btn)

        toolbar.add_top_bar(header)

        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        scroll.set_propagate_natural_height(True)

        page = Adw.PreferencesPage()
        scroll.set_child(page)

        group = Adw.PreferencesGroup()
        page.add(group)

        # Name
        self._name_row = Adw.EntryRow(title="Name")
        if self._existing:
            self._name_row.set_text(self._existing.get("name") or "")
        group.add(self._name_row)

        # URI (required)
        self._uri_row = Adw.EntryRow(title="URI *")
        self._uri_row.connect("entry-activated", lambda _: self._on_save_clicked(None))
        if self._existing:
            self._uri_row.set_text(self._existing.get("uri") or "")
        group.add(self._uri_row)

        # Access token (password-masked)
        self._token_row = Adw.PasswordEntryRow(title="Access token")
        if self._existing:
            self._token_row.set_text(self._existing.get("token") or "")
        group.add(self._token_row)

        # SSL verification toggle
        self._ssl_row = Adw.SwitchRow(title="Verify SSL certificate")
        ssl_active = True
        if self._existing:
            ssl_active = self._existing.get("ssl_verify", True)
        self._ssl_row.set_active(ssl_active)
        self._ssl_row.connect("notify::active", self._on_ssl_toggled)
        group.add(self._ssl_row)

        # CA certificate selector (hidden when ssl_verify is off)
        self._ca_row = Adw.ActionRow(title="CA Certificate")
        ca_subtitle = (
            Path(self._ca_cert_path).name
            if self._ca_cert_path
            else "No certificate selected"
        )
        self._ca_row.set_subtitle(ca_subtitle)
        select_btn = Gtk.Button(label="Select…", valign=Gtk.Align.CENTER)
        select_btn.connect("clicked", self._on_select_ca_cert)
        self._ca_row.add_suffix(select_btn)
        self._ca_row.set_visible(ssl_active)
        group.add(self._ca_row)

        toolbar.set_content(scroll)
        self.set_child(toolbar)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_ssl_toggled(self, row: Adw.SwitchRow, _param) -> None:
        self._ca_row.set_visible(row.get_active())

    def _on_select_ca_cert(self, _btn: Gtk.Button) -> None:
        root = self.get_root()
        chooser = Gtk.FileChooserNative(
            title="Select CA Certificate",
            transient_for=root if isinstance(root, Gtk.Window) else None,
            action=Gtk.FileChooserAction.OPEN,
        )
        f = Gtk.FileFilter()
        f.set_name("Certificate files (*.crt, *.pem, *.cer)")
        f.add_pattern("*.crt")
        f.add_pattern("*.pem")
        f.add_pattern("*.cer")
        chooser.add_filter(f)
        chooser.connect("response", self._on_ca_cert_chosen, chooser)
        chooser.show()

    def _on_ca_cert_chosen(
        self, _chooser, response: int, chooser: Gtk.FileChooserNative
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        path = chooser.get_file().get_path()
        if path:
            self._ca_cert_path = path
            self._ca_row.set_subtitle(Path(path).name)

    def _on_save_clicked(self, _btn) -> None:
        uri = self._uri_row.get_text().strip()
        if not uri:
            self._alert("URI Required", "Please enter a repository URI.")
            return

        name = self._name_row.get_text().strip()
        token = self._token_row.get_text().strip() or None
        ssl_verify = self._ssl_row.get_active()

        # Duplicate check — only if URI is new.
        old_uri = self._existing.get("uri") if self._existing else None
        if uri != old_uri and any(r["uri"] == uri for r in load_repos()):
            self._alert("Already Added", "This repository is already in the list.")
            return

        # Local path that doesn't exist → offer to initialise.
        if _is_local_uri(uri):
            parsed = urlparse(uri)
            local_path = Path(parsed.path if parsed.path else uri).expanduser()
            if not local_path.is_dir():
                self._ask_init(uri, name, token, ssl_verify)
                return

        # Resolve CA cert path for the connection attempt.
        ca_cert_path, ca_cert_name = self._resolve_ca_cert(ssl_verify)

        root = self.get_root()
        mount_op = Gtk.MountOperation(
            parent=root if isinstance(root, Gtk.Window) else None
        )

        from cellar.backend.repo import Repo, RepoError

        try:
            repo = Repo(
                uri,
                name,
                mount_op=mount_op,
                ssl_verify=ssl_verify,
                ca_cert=ca_cert_path,
                token=token,
            )
        except RepoError as exc:
            self._alert("Invalid Repository", str(exc))
            return

        try:
            repo.fetch_catalogue()
        except RepoError as exc:
            err = str(exc)
            if _looks_like_missing(err):
                if repo.is_writable:
                    self._ask_init(uri, name, token, ssl_verify)
                else:
                    self._alert(
                        "No Catalogue Found",
                        f"No catalogue.json was found at:\n\n{uri}\n\n"
                        "HTTP repositories are read-only — the catalogue must "
                        "already exist on the server.",
                    )
            elif _looks_like_auth_error(err):
                if token:
                    self._alert(
                        "Authentication Failed",
                        "The token was rejected. Check that it matches your "
                        "web server configuration.",
                    )
                else:
                    self._alert(
                        "Authentication Required",
                        "This repository requires a bearer token. "
                        "Enter it in the Access token field and try again.",
                    )
            elif _looks_like_forbidden_error(err):
                self._alert(
                    "Access Denied",
                    "The server returned 403 Forbidden. "
                    "If this repository uses bearer token authentication, "
                    "check that the token is correct.\n\n"
                    "If you manage the server, verify the web server "
                    "configuration — see the README for a working nginx "
                    "example.",
                )
            elif _looks_like_ssl_error(err):
                self._alert(
                    "SSL Certificate Error",
                    f"The server at {uri} presented a certificate that could not "
                    "be verified. Provide your CA certificate file using the "
                    "CA Certificate field above, or disable SSL verification.",
                )
            else:
                self._alert("Could Not Connect", err)
            return

        # Copy CA cert into certs_dir if it's a newly selected file.
        if ca_cert_path and ca_cert_name:
            import shutil
            src = Path(ca_cert_path)
            dest = certs_dir() / ca_cert_name
            if not dest.exists():
                shutil.copy2(src, dest)

        self._finish_save(uri, name, token, ssl_verify, ca_cert_name)

    # ------------------------------------------------------------------
    # Init flow (catalogue missing on a writable repo)
    # ------------------------------------------------------------------

    def _ask_init(
        self, uri: str, name: str, token: str | None, ssl_verify: bool
    ) -> None:
        if _is_local_uri(uri):
            body = (
                "No catalogue.json was found at this location. "
                "Initialise a new empty repository here?"
            )
        else:
            body = (
                "No catalogue.json was found at this location. "
                "Initialise a new empty repository here?\n\n"
                "The directory will be created on the server if it "
                "does not exist yet."
            )
        dialog = Adw.AlertDialog(heading="No Catalogue Found", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("init", "Initialise")
        dialog.set_response_appearance("init", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_init_response, uri, name, token, ssl_verify)
        dialog.present(self)

    def _on_init_response(
        self,
        _dialog,
        response: str,
        uri: str,
        name: str,
        token: str | None,
        ssl_verify: bool,
    ) -> None:
        if response != "init":
            return
        scheme = urlparse(uri).scheme.lower()
        if _is_local_uri(uri):
            self._init_local_repo(uri, name, token, ssl_verify)
        elif scheme in ("smb", "nfs"):
            self._init_gio_repo(uri, name, token, ssl_verify)
        elif scheme == "ssh":
            self._init_ssh_repo(uri, name, token, ssl_verify)
        else:
            self._alert(
                "Not Supported",
                f"Initialising {scheme!r} repositories is not supported.",
            )

    def _init_local_repo(
        self, uri: str, name: str, token: str | None, ssl_verify: bool
    ) -> None:
        parsed = urlparse(uri)
        target = Path(parsed.path if parsed.path else uri).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
            (target / "catalogue.json").write_text(
                json.dumps(_empty_catalogue(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info("Initialised new repo at %s", target)
        except OSError as exc:
            self._alert("Could Not Initialise", str(exc))
            return
        self._finish_save(uri, name, token, ssl_verify, None)

    def _init_gio_repo(
        self, uri: str, name: str, token: str | None, ssl_verify: bool
    ) -> None:
        from cellar.utils.gio_io import gio_makedirs, gio_write_bytes

        root = self.get_root()
        mount_op = Gtk.MountOperation(
            parent=root if isinstance(root, Gtk.Window) else None
        )
        data = json.dumps(_empty_catalogue(), indent=2, ensure_ascii=False).encode()
        catalogue_uri = uri.rstrip("/") + "/catalogue.json"
        try:
            gio_makedirs(uri, mount_op=mount_op)
            gio_write_bytes(catalogue_uri, data, mount_op=mount_op)
            log.info("Initialised new GIO repo at %s", uri)
        except OSError as exc:
            self._alert("Could Not Initialise", str(exc))
            return
        self._finish_save(uri, name, token, ssl_verify, None)

    def _init_ssh_repo(
        self, uri: str, name: str, token: str | None, ssl_verify: bool
    ) -> None:
        import shlex
        import subprocess

        parsed = urlparse(uri)
        if not parsed.hostname:
            self._alert("Invalid URI", f"No hostname in SSH URI: {uri!r}")
            return

        dest = (
            f"{parsed.username}@{parsed.hostname}"
            if parsed.username
            else parsed.hostname
        )
        base_args = [
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        ]
        if parsed.port:
            base_args += ["-p", str(parsed.port)]
        base_args.append(dest)

        path = parsed.path or "/"
        try:
            result = subprocess.run(
                base_args + ["mkdir", "-p", path],
                capture_output=True, timeout=30, check=False,
            )
        except FileNotFoundError:
            self._alert("Could Not Initialise", "ssh not found; install an OpenSSH client.")
            return
        except subprocess.TimeoutExpired:
            self._alert("Could Not Initialise", "SSH connection timed out.")
            return
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            self._alert(
                "Could Not Initialise",
                f"Could not create directory: {stderr or 'SSH error'}",
            )
            return

        cat_path = path.rstrip("/") + "/catalogue.json"
        data = json.dumps(_empty_catalogue(), indent=2, ensure_ascii=False).encode()
        try:
            result = subprocess.run(
                base_args + [f"cat > {shlex.quote(cat_path)}"],
                input=data, capture_output=True, timeout=30, check=False,
            )
        except subprocess.TimeoutExpired:
            self._alert("Could Not Initialise", "SSH connection timed out while writing.")
            return
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            self._alert(
                "Could Not Initialise",
                f"Could not write catalogue.json: {stderr or 'SSH error'}",
            )
            return

        log.info("Initialised new SSH repo at %s", uri)
        self._finish_save(uri, name, token, ssl_verify, None)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_ca_cert(self, ssl_verify: bool) -> tuple[str | None, str | None]:
        """Return (full path, filename) for the current CA cert selection."""
        if not self._ca_cert_path or not ssl_verify:
            return None, None
        p = Path(self._ca_cert_path)
        if p.is_absolute() and p.exists():
            return str(p), p.name
        # Existing cert stored by filename only.
        resolved = certs_dir() / self._ca_cert_path
        if resolved.exists():
            return str(resolved), self._ca_cert_path
        return None, None

    def _finish_save(
        self,
        uri: str,
        name: str,
        token: str | None,
        ssl_verify: bool,
        ca_cert_name: str | None,
    ) -> None:
        cfg: dict = {"uri": uri, "name": name}
        if token:
            cfg["token"] = token
        if not ssl_verify:
            cfg["ssl_verify"] = False
        if ca_cert_name and ssl_verify:
            cfg["ca_cert"] = ca_cert_name
        self._on_save(cfg)
        self.close()

    def _alert(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("ok", "OK")
        dialog.present(self)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _empty_catalogue() -> dict:
    return {
        "cellar_version": 1,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "apps": [],
    }


def _is_local_uri(uri: str) -> bool:
    return urlparse(uri).scheme.lower() in ("", "file")


def _looks_like_missing(err: str) -> bool:
    """Heuristic: does this look like a missing file rather than an auth/network error?

    Mount failures (e.g. "Failed to mount Windows share: No such file or
    directory") also contain "no such file", so explicitly exclude them.
    """
    low = err.lower()
    if "mount" in low:
        return False
    return any(kw in low for kw in ("not found", "does not exist", "no such file"))


def _looks_like_ssl_error(err: str) -> bool:
    """Heuristic: does this look like an SSL certificate verification failure?"""
    low = err.lower()
    return any(kw in low for kw in ("ssl", "certificate", "cert_verify", "handshake"))


def _looks_like_auth_error(err: str) -> bool:
    """Heuristic: does this look like a 401 Unauthorized response?"""
    return "HTTP 401" in err


def _looks_like_forbidden_error(err: str) -> bool:
    """Heuristic: does this look like a 403 Forbidden response?"""
    return "HTTP 403" in err

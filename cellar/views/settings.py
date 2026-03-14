"""Settings / Preferences dialog."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib, Gtk

from cellar.backend.config import (
    certs_dir,
    clear_password,
    load_repos,
    load_ssh_password,
    save_repos,
)

log = logging.getLogger(__name__)


class SettingsDialog(Adw.PreferencesDialog):
    """Application preferences window.

    Shown via the hamburger menu → Preferences.  Currently exposes repo
    management; more groups will be added as later phases land.
    """

    def __init__(
        self,
        *,
        on_repos_changed: Callable[[], None] | None = None,
        **kwargs,
    ):
        super().__init__(title="Preferences", content_width=560, content_height=500, **kwargs)
        self._on_repos_changed = on_repos_changed
        self._repo_rows: list[Adw.PreferencesRow] = []

        # ── Page: General ─────────────────────────────────────────────────
        page = Adw.PreferencesPage(
            title="General",
            icon_name="preferences-other-symbolic",
        )
        self.add(page)

        # ── Group: Repositories ───────────────────────────────────────────
        self._repo_group = Adw.PreferencesGroup(title="Repositories")
        add_btn = Gtk.Button(
            icon_name="list-add-symbolic",
            valign=Gtk.Align.CENTER,
            tooltip_text="Add Repository",
        )
        add_btn.add_css_class("flat")
        add_btn.connect("clicked", self._on_add_clicked)
        self._repo_group.set_header_suffix(add_btn)
        page.add(self._repo_group)

        # ── Group: SteamGridDB ─────────────────────────────────────────
        self._build_sgdb_group(page)

        # ── Group: Install Location ───────────────────────────────────────
        self._build_install_location_group(page)

        # ── Group: Wine ────────────────────────────────────────────────────
        self._build_wine_group(page)

        self._rebuild_repo_rows()

    # ------------------------------------------------------------------
    # SteamGridDB
    # ------------------------------------------------------------------

    def _build_sgdb_group(self, page: Adw.PreferencesPage) -> None:
        from cellar.backend.config import load_sgdb_key

        group = Adw.PreferencesGroup(
            title="SteamGridDB",
            description=(
                "API key for downloading high-res icons from SteamGridDB. "
                "Get a free key at steamgriddb.com/profile/preferences/api."
            ),
        )
        page.add(group)

        self._sgdb_row = Adw.PasswordEntryRow(title="API Key")
        self._sgdb_saved = load_sgdb_key()
        self._sgdb_row.set_text(self._sgdb_saved)
        group.add(self._sgdb_row)

        # Status indicator — spinner while validating, icon for result
        self._sgdb_spinner = Adw.Spinner()
        self._sgdb_spinner.set_visible(False)
        self._sgdb_spinner.set_valign(Gtk.Align.CENTER)
        self._sgdb_row.add_suffix(self._sgdb_spinner)

        self._sgdb_status = Gtk.Image(visible=bool(self._sgdb_saved))
        self._sgdb_status.set_from_icon_name("object-select-symbolic")
        if self._sgdb_saved:
            self._sgdb_status.add_css_class("success")
        self._sgdb_status.set_valign(Gtk.Align.CENTER)
        self._sgdb_row.add_suffix(self._sgdb_status)

        self._sgdb_debounce_id = 0
        self._sgdb_row.connect("changed", self._on_sgdb_key_edited)

    def _on_sgdb_key_edited(self, _row) -> None:
        if self._sgdb_debounce_id:
            GLib.source_remove(self._sgdb_debounce_id)
            self._sgdb_debounce_id = 0

        key = self._sgdb_row.get_text().strip()

        # Empty → immediately clear
        if not key:
            from cellar.backend.config import save_sgdb_key
            save_sgdb_key("")
            self._sgdb_saved = ""
            self._sgdb_spinner.set_visible(False)
            self._sgdb_spinner.set_visible(False)
            self._sgdb_status.set_visible(False)
            return

        # No change from saved value
        if key == self._sgdb_saved:
            return

        self._sgdb_debounce_id = GLib.timeout_add(500, self._validate_sgdb_key)

    def _validate_sgdb_key(self) -> int:
        self._sgdb_debounce_id = 0
        key = self._sgdb_row.get_text().strip()
        if not key or key == self._sgdb_saved:
            return GLib.SOURCE_REMOVE

        # Show spinner
        self._sgdb_status.set_visible(False)
        self._sgdb_spinner.set_visible(True)
        self._sgdb_spinner.set_visible(True)

        from cellar.utils.async_work import run_in_background

        def _validate():
            from cellar.utils.http import make_session
            s = make_session()
            r = s.get(
                "https://www.steamgriddb.com/api/v2/grids/steam/220",
                headers={"Authorization": f"Bearer {key}"},
                timeout=10,
            )
            return r.status_code == 200

        def _done(valid):
            self._sgdb_spinner.set_visible(False)
            self._sgdb_spinner.set_visible(False)

            # Guard against stale callback
            current = self._sgdb_row.get_text().strip()
            if current != key:
                return

            # Remove old CSS classes
            self._sgdb_status.remove_css_class("success")
            self._sgdb_status.remove_css_class("error")

            if valid:
                from cellar.backend.config import save_sgdb_key
                save_sgdb_key(key)
                self._sgdb_saved = key
                self._sgdb_status.set_from_icon_name("object-select-symbolic")
                self._sgdb_status.add_css_class("success")
                self.add_toast(Adw.Toast(title="API key saved"))
            else:
                self._sgdb_status.set_from_icon_name("dialog-error-symbolic")
                self._sgdb_status.add_css_class("error")
                self.add_toast(Adw.Toast(title="Invalid API key"))
            self._sgdb_status.set_visible(True)

        def _error(_msg):
            self._sgdb_spinner.set_visible(False)
            self._sgdb_spinner.set_visible(False)
            self._sgdb_status.remove_css_class("success")
            self._sgdb_status.remove_css_class("error")
            self._sgdb_status.set_from_icon_name("dialog-error-symbolic")
            self._sgdb_status.add_css_class("error")
            self._sgdb_status.set_visible(True)
            self.add_toast(Adw.Toast(title="Could not reach SteamGridDB"))

        run_in_background(_validate, on_done=_done, on_error=_error)
        return GLib.SOURCE_REMOVE

    # ------------------------------------------------------------------
    # Install location
    # ------------------------------------------------------------------

    def _build_install_location_group(self, page: Adw.PreferencesPage) -> None:
        from cellar.backend.config import install_data_dir

        group = Adw.PreferencesGroup(
            title="Install Location",
            description=(
                "Where Cellar stores prefixes, native apps, and base images. "
                "A \u2018Cellar\u2019 subfolder is created at the chosen location. "
                "Existing installs are moved to the new location automatically."
            ),
        )
        page.add(group)

        self._install_location_row = Adw.ActionRow(
            title="Location",
            subtitle=str(install_data_dir()),
        )

        browse_btn = Gtk.Button(
            icon_name="folder-open-symbolic",
            valign=Gtk.Align.CENTER,
            has_frame=False,
            tooltip_text="Choose install location",
        )
        browse_btn.connect("clicked", self._on_install_location_browse)
        self._install_location_row.add_suffix(browse_btn)

        reset_btn = Gtk.Button(
            icon_name="edit-undo-symbolic",
            valign=Gtk.Align.CENTER,
            has_frame=False,
            tooltip_text="Reset to default location",
        )
        reset_btn.connect("clicked", self._on_install_location_reset)
        self._install_location_row.add_suffix(reset_btn)

        group.add(self._install_location_row)

    def _on_install_location_browse(self, _btn) -> None:
        from cellar.backend.config import load_install_base

        chooser = Gtk.FileChooserNative(
            title="Select Install Base Folder",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.SELECT_FOLDER,
        )
        current = load_install_base()
        start = current if current else str(Path.home())
        chooser.set_current_folder(Gio.File.new_for_path(start))
        chooser.connect("response", self._on_install_location_response, chooser)
        chooser.show()

    def _on_install_location_response(self, _chooser, response, chooser) -> None:
        from cellar.backend.config import install_data_dir, save_install_base

        if response == Gtk.ResponseType.ACCEPT:
            f = chooser.get_file()
            if f:
                old_dir = install_data_dir()
                save_install_base(f.get_path())
                new_dir = install_data_dir()
                self._install_location_row.set_subtitle(str(new_dir))
                self._migrate_and_reload(old_dir, new_dir)

    def _on_install_location_reset(self, _btn) -> None:
        from cellar.backend.config import install_data_dir, save_install_base

        old_dir = install_data_dir()
        save_install_base("")
        new_dir = install_data_dir()
        self._install_location_row.set_subtitle(str(new_dir))
        self._migrate_and_reload(old_dir, new_dir)

    def _migrate_and_reload(self, old_dir: Path, new_dir: Path) -> None:
        """Move install data from *old_dir* to *new_dir* then reload catalogue."""
        from cellar.utils.async_work import run_in_background

        if old_dir == new_dir:
            if self._on_repos_changed:
                self._on_repos_changed()
            return

        has_data = any(
            (old_dir / sub).is_dir() and any((old_dir / sub).iterdir())
            for sub in ("prefixes", "native", "bases")
        )
        if not has_data:
            if self._on_repos_changed:
                self._on_repos_changed()
            return

        spinner = Adw.Spinner()
        spinner.set_margin_top(8)
        progress_dialog = Adw.AlertDialog(
            heading="Moving Install Data",
            body=f"Moving installs to {new_dir}\u2026",
            extra_child=spinner,
        )
        progress_dialog.present(self)

        def _finish(_result: object) -> None:
            progress_dialog.close()
            if self._on_repos_changed:
                self._on_repos_changed()

        run_in_background(
            work=lambda: _move_install_data(old_dir, new_dir),
            on_done=_finish,
        )

    # ------------------------------------------------------------------
    # Wine
    # ------------------------------------------------------------------

    _AUDIO_LABELS = ("Auto (let Proton decide)", "PulseAudio", "ALSA", "OSS")
    _AUDIO_VALUES = ("auto", "pulseaudio", "alsa", "oss")

    def _build_wine_group(self, page: Adw.PreferencesPage) -> None:
        from cellar.backend.config import load_audio_driver  # noqa: PLC0415

        group = Adw.PreferencesGroup(
            title="Wine",
            description="Default settings for Windows applications.",
        )
        page.add(group)

        self._audio_row = Adw.ComboRow(
            title="Audio Driver",
            subtitle="Default audio backend for Wine/Proton apps",
        )
        model = Gtk.StringList()
        for label in self._AUDIO_LABELS:
            model.append(label)
        self._audio_row.set_model(model)

        current = load_audio_driver()
        if current in self._AUDIO_VALUES:
            self._audio_row.set_selected(self._AUDIO_VALUES.index(current))

        self._audio_row.connect("notify::selected", self._on_audio_driver_changed)
        group.add(self._audio_row)

    def _on_audio_driver_changed(self, row, _pspec) -> None:
        from cellar.backend.config import save_audio_driver  # noqa: PLC0415

        idx = row.get_selected()
        if 0 <= idx < len(self._AUDIO_VALUES):
            save_audio_driver(self._AUDIO_VALUES[idx])

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
        smb_username = repo_cfg.get("smb_username") or ""

        title = name if name else uri
        subtitle = uri if name else ""

        row = Adw.ActionRow(title=title, subtitle=subtitle)

        if smb_username:
            from cellar.backend.config import load_smb_password
            has_pw = bool(load_smb_password(uri))
            icon = Gtk.Image.new_from_icon_name(
                "channel-secure-symbolic" if has_pw else "dialog-password-symbolic"
            )
            icon.set_tooltip_text(
                f"SMB: {smb_username}" + (" (password stored)" if has_pw else " (no password)")
            )
            row.add_prefix(icon)
        elif uri.startswith("sftp://"):
            from urllib.parse import urlparse as _urlparse

            from cellar.backend.config import load_ssh_password
            ssh_user = _urlparse(uri).username or ""
            has_pw = bool(load_ssh_password(uri))
            ssh_identity = repo_cfg.get("ssh_identity") or ""
            if ssh_identity:
                icon = Gtk.Image.new_from_icon_name("channel-secure-symbolic")
                icon.set_tooltip_text(
                    f"SFTP: {ssh_user} (key: {Path(ssh_identity).name})"
                    if ssh_user else f"SFTP key: {Path(ssh_identity).name}"
                )
            else:
                icon = Gtk.Image.new_from_icon_name(
                    "channel-secure-symbolic" if has_pw else "dialog-password-symbolic"
                )
                icon.set_tooltip_text(
                    f"SFTP: {ssh_user}" + (" (password stored)" if has_pw else " (agent/config)")
                    if ssh_user
                    else ("SFTP (password stored)" if has_pw else "SFTP (agent/config)")
                )
            row.add_prefix(icon)
        elif token:
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

        enabled = repo_cfg.get("enabled", True)

        switch = Gtk.Switch(
            active=enabled,
            valign=Gtk.Align.CENTER,
            tooltip_text="Enable or disable this repository",
        )
        switch.connect("notify::active", self._on_repo_enabled_toggled, uri)
        row.add_suffix(switch)

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

        if not enabled:
            row.add_css_class("dim-label")

        return row

    def _on_repo_enabled_toggled(self, switch: Gtk.Switch, _pspec, uri: str) -> None:
        enabled = switch.get_active()
        repos = [
            {**r, "enabled": enabled} if r["uri"] == uri else r
            for r in load_repos()
        ]
        save_repos(repos)
        self._rebuild_repo_rows()
        if self._on_repos_changed:
            self._on_repos_changed()

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
        clear_password(uri)
        from cellar.backend.repo import Repo
        Repo.clear_catalogue_cache(uri)
        Repo.clear_asset_cache(uri)
        self._rebuild_repo_rows()
        if self._on_repos_changed:
            self._on_repos_changed()



# ---------------------------------------------------------------------------
# Add / Edit Repository dialog
# ---------------------------------------------------------------------------

_SCHEME_PREFIXES = ("", "http://", "https://", "smb://", "sftp://")


def _split_uri(uri: str) -> tuple[int, str]:
    """Return (scheme_index, path_portion) for an existing URI string."""
    for idx, prefix in enumerate(_SCHEME_PREFIXES):
        if prefix and uri.startswith(prefix):
            return idx, uri[len(prefix):]
    # Bare UNC path //server/share → SMB
    if uri.startswith("//") and "://" not in uri:
        return 3, uri[2:]
    return 0, uri  # LOCAL / bare path


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
        super().__init__(title=mode, content_width=480, follows_content_size=True)
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
            vscrollbar_policy=Gtk.PolicyType.NEVER,
        )
        scroll.set_propagate_natural_height(True)

        page = Adw.PreferencesPage(width_request=480)
        scroll.set_child(page)

        group = Adw.PreferencesGroup()
        page.add(group)

        # Name
        self._name_row = Adw.EntryRow(title="Name")
        if self._existing:
            self._name_row.set_text(self._existing.get("name") or "")
        group.add(self._name_row)

        # Scheme selector (suffix) + path entry
        _existing_uri = self._existing.get("uri", "") if self._existing else ""
        _scheme_idx, _path_text = _split_uri(_existing_uri)
        _initial_text = _SCHEME_PREFIXES[_scheme_idx] + _path_text

        scheme_model = Gtk.StringList()
        for label in ("Local", "HTTP", "HTTPS", "SMB", "SFTP"):
            scheme_model.append(label)
        self._scheme_dropdown = Gtk.DropDown(model=scheme_model, valign=Gtk.Align.CENTER)
        self._scheme_dropdown.add_css_class("flat")
        self._scheme_dropdown.set_selected(_scheme_idx)
        self._scheme_dropdown.connect("notify::selected", self._on_scheme_changed)

        sep = Gtk.Separator(
            orientation=Gtk.Orientation.VERTICAL, margin_top=8, margin_bottom=8
        )
        self._path_row = Adw.EntryRow(title="Path" if _scheme_idx == 0 else "Host / Path")
        self._path_row.set_text(_initial_text)
        self._path_row.connect("entry-activated", lambda _: self._on_save_clicked(None))
        self._path_row.connect("changed", self._on_path_changed)
        self._path_row.add_prefix(sep)
        self._path_row.add_prefix(self._scheme_dropdown)
        self._updating = False
        _inner = self._path_row.get_delegate()
        if _inner:
            _inner.connect("delete-text", self._on_delete_text)
            _inner.connect("notify::cursor-position", self._on_cursor_moved)
        group.add(self._path_row)

        # SMB credentials group (shown only when URI scheme is smb://)
        self._smb_group = Adw.PreferencesGroup(
            title="SMB Credentials",
            description="Optional — leave blank for anonymous/guest access.",
        )
        page.add(self._smb_group)

        self._smb_user_row = Adw.EntryRow(title="Username")
        if self._existing:
            self._smb_user_row.set_text(self._existing.get("smb_username") or "")
        self._smb_group.add(self._smb_user_row)

        self._smb_pass_row = Adw.PasswordEntryRow(title="Password")
        if self._existing:
            from cellar.backend.config import load_smb_password
            stored_pw = load_smb_password(self._existing.get("uri", "")) or ""
            self._smb_pass_row.set_text(stored_pw)
        self._smb_group.add(self._smb_pass_row)

        self._smb_group.set_visible(_scheme_idx == 3)

        # SFTP credentials group (shown only when URI scheme is sftp://)
        self._ssh_group = Adw.PreferencesGroup(
            title="SFTP Credentials",
            description=(
                "Uses SFTP for file transfer. The path in the URI must be "
                "the SFTP path, which may differ from the SSH shell path."
            ),
        )
        page.add(self._ssh_group)

        self._ssh_user_row = Adw.EntryRow(title="Username")
        if self._existing:
            self._ssh_user_row.set_text(self._existing.get("ssh_username") or "")
        self._ssh_group.add(self._ssh_user_row)

        self._ssh_pass_row = Adw.PasswordEntryRow(title="Password")
        if self._existing:
            stored_pw = load_ssh_password(self._existing.get("uri", "")) or ""
            self._ssh_pass_row.set_text(stored_pw)
        self._ssh_group.add(self._ssh_pass_row)

        self._ssh_group.set_visible(_scheme_idx == 4)

        # HTTP / HTTPS group
        http_group = Adw.PreferencesGroup(title="HTTP Authentication")
        page.add(http_group)

        # Access token (password-masked)
        self._token_row = Adw.PasswordEntryRow(title="Bearer token")
        if self._existing:
            self._token_row.set_text(self._existing.get("token") or "")
        http_group.add(self._token_row)

        # SSL verification toggle
        self._ssl_row = Adw.SwitchRow(title="Verify SSL certificate")
        ssl_active = True
        if self._existing:
            ssl_active = self._existing.get("ssl_verify", True)
        self._ssl_row.set_active(ssl_active)
        self._ssl_row.set_visible(_scheme_idx == 2)
        self._ssl_row.connect("notify::active", self._on_ssl_toggled)
        http_group.add(self._ssl_row)

        # CA certificate selector (hidden when ssl_verify is off or scheme is HTTP)
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
        self._ca_row.set_visible(_scheme_idx == 2 and ssl_active)
        http_group.add(self._ca_row)

        http_group.set_visible(_scheme_idx in (1, 2))
        self._http_group = http_group

        toolbar.set_content(scroll)
        self.set_child(toolbar)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_scheme_changed(self, _widget, _param=None) -> None:
        """Update the text field scheme prefix and toggle credential groups."""
        if self._updating:
            return
        idx = self._scheme_dropdown.get_selected()
        new_prefix = _SCHEME_PREFIXES[idx]
        self._smb_group.set_visible(idx == 3)
        self._ssh_group.set_visible(idx == 4)
        self._http_group.set_visible(idx in (1, 2))
        self._ssl_row.set_visible(idx == 2)
        self._ca_row.set_visible(idx == 2 and self._ssl_row.get_active())
        self._path_row.set_title("Path" if idx == 0 else "Host / Path")
        # Replace the scheme portion of the current text.
        text = self._path_row.get_text()
        path = text
        for prefix in _SCHEME_PREFIXES:
            if prefix and path.startswith(prefix):
                path = path[len(prefix):]
                break
        self._updating = True
        new_text = new_prefix + path
        self._path_row.set_text(new_text)
        self._path_row.set_position(len(new_text))
        self._updating = False

    def _on_path_changed(self, _entry) -> None:
        """Strip any extra scheme prefix the user typed or pasted after the current one."""
        if self._updating:
            return
        text = self._path_row.get_text()
        current_prefix = _SCHEME_PREFIXES[self._scheme_dropdown.get_selected()]
        after = text[len(current_prefix):] if text.startswith(current_prefix) else text
        for prefix in _SCHEME_PREFIXES:
            if prefix and after.startswith(prefix):
                self._updating = True
                new_text = current_prefix + after[len(prefix):]
                self._path_row.set_text(new_text)
                self._path_row.set_position(len(new_text))
                self._updating = False
                return

    def _on_delete_text(self, text_widget, start_pos: int, end_pos: int) -> None:
        """Block deletions that would erase part of the locked scheme prefix."""
        if self._updating:
            return
        prefix_len = len(_SCHEME_PREFIXES[self._scheme_dropdown.get_selected()])
        if prefix_len > 0 and start_pos < prefix_len:
            text_widget.stop_emission_by_name("delete-text")

    def _on_cursor_moved(self, text_widget, _param) -> None:
        """Keep cursor at or after the locked scheme prefix."""
        prefix_len = len(_SCHEME_PREFIXES[self._scheme_dropdown.get_selected()])
        if prefix_len > 0 and text_widget.get_position() < prefix_len:
            GLib.idle_add(text_widget.set_position, prefix_len)

    def _full_uri(self) -> str:
        return self._path_row.get_text().strip()

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
        uri = self._full_uri()
        prefix = _SCHEME_PREFIXES[self._scheme_dropdown.get_selected()]
        if not uri[len(prefix):].strip():
            self._alert("Path Required", "Please enter a repository path or host.")
            return

        name = self._name_row.get_text().strip()
        scheme = urlparse(uri).scheme.lower()
        is_smb = scheme == "smb"
        is_ssh = scheme == "sftp"

        token = self._token_row.get_text().strip() or None if scheme in ("http", "https") else None
        ssl_verify = self._ssl_row.get_active() if scheme == "https" else True
        smb_username = self._smb_user_row.get_text().strip() or None if is_smb else None
        smb_password = self._smb_pass_row.get_text() or None if is_smb else None
        ssh_username = self._ssh_user_row.get_text().strip() or None if is_ssh else None
        ssh_password = self._ssh_pass_row.get_text() or None if is_ssh else None

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
                self._ask_init(
                    uri, name, token, ssl_verify,
                    smb_username, smb_password, ssh_username, ssh_password,
                )
                return

        # Resolve CA cert path for the connection attempt.
        ca_cert_path, ca_cert_name = self._resolve_ca_cert(ssl_verify)

        from cellar.backend.repo import Repo, RepoError

        try:
            repo = Repo(
                uri,
                name,
                ssl_verify=ssl_verify,
                ca_cert=ca_cert_path,
                token=token,
                smb_username=smb_username,
                smb_password=smb_password,
            )
        except RepoError as exc:
            err = str(exc)
            if is_smb and _looks_like_smb_auth_error(err):
                self._alert(
                    "SMB Authentication Failed",
                    "Could not authenticate with the SMB server.\n\n"
                    "Enter your username and password in the SMB Credentials "
                    "section above and try again.\n\n"
                    f"Details: {err}",
                )
            else:
                self._alert("Invalid Repository", err)
            return

        try:
            repo.fetch_catalogue(use_cache=False)
        except RepoError as exc:
            err = str(exc)
            if _looks_like_missing(err):
                if repo.is_writable:
                    self._ask_init(
                        uri, name, token, ssl_verify,
                        smb_username, smb_password, ssh_username, ssh_password,
                    )
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
            elif is_smb and _looks_like_smb_auth_error(err):
                cred_hint = (
                    "Enter your username and password in the SMB Credentials section above."
                    if not smb_username
                    else "Check that your username and password are correct."
                )
                self._alert(
                    "SMB Access Denied",
                    f"Access to {uri} was denied by the SMB server.\n\n{cred_hint}",
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

        self._finish_save(
            uri, name, token, ssl_verify, ca_cert_name,
            smb_username, smb_password, ssh_username, ssh_password,
        )

    # ------------------------------------------------------------------
    # Init flow (catalogue missing on a writable repo)
    # ------------------------------------------------------------------

    def _ask_init(
        self,
        uri: str,
        name: str,
        token: str | None,
        ssl_verify: bool,
        smb_username: str | None = None,
        smb_password: str | None = None,
        ssh_username: str | None = None,
        ssh_password: str | None = None,
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
        dialog.connect(
            "response", self._on_init_response,
            uri, name, token, ssl_verify, smb_username, smb_password, ssh_username, ssh_password,
        )
        dialog.present(self)

    def _on_init_response(
        self,
        _dialog,
        response: str,
        uri: str,
        name: str,
        token: str | None,
        ssl_verify: bool,
        smb_username: str | None = None,
        smb_password: str | None = None,
        ssh_username: str | None = None,
        ssh_password: str | None = None,
    ) -> None:
        if response != "init":
            return
        scheme = urlparse(uri).scheme.lower()
        _args = (uri, name, token, ssl_verify,
                 smb_username, smb_password, ssh_username, ssh_password)
        if _is_local_uri(uri):
            self._init_local_repo(*_args)
        elif scheme == "smb":
            self._init_smb_repo(*_args)
        elif scheme == "sftp":
            self._init_ssh_repo(*_args)
        else:
            self._alert(
                "Not Supported",
                f"Initialising {scheme!r} repositories is not supported.",
            )

    def _init_local_repo(
        self,
        uri: str,
        name: str,
        token: str | None,
        ssl_verify: bool,
        smb_username: str | None = None,
        smb_password: str | None = None,
        ssh_username: str | None = None,
        ssh_password: str | None = None,
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
        self._finish_save(
            uri, name, token, ssl_verify, None,
            smb_username, smb_password, ssh_username, ssh_password,
        )

    def _init_smb_repo(
        self,
        uri: str,
        name: str,
        token: str | None,
        ssl_verify: bool,
        smb_username: str | None = None,
        smb_password: str | None = None,
        ssh_username: str | None = None,
        ssh_password: str | None = None,
    ) -> None:
        from cellar.utils.smb import smb_uri_to_unc

        unc = smb_uri_to_unc(uri)
        cat_unc = unc.rstrip("/") + "/catalogue.json"
        data = json.dumps(_empty_catalogue(), indent=2, ensure_ascii=False).encode()
        server = urlparse(uri).hostname or ""
        try:
            import smbclient  # type: ignore[import]
            kwargs: dict = {}
            if smb_username:
                kwargs["username"] = smb_username
            if smb_password:
                kwargs["password"] = smb_password
            smbclient.register_session(server, **kwargs)
            smbclient.makedirs(unc, exist_ok=True)
            with smbclient.open_file(cat_unc, mode="wb") as f:
                f.write(data)
            log.info("Initialised new SMB repo at %s", uri)
        except ImportError:
            self._alert(
                "Could Not Initialise",
                "smbprotocol is not installed. Install it with: pip install smbprotocol",
            )
            return
        except Exception as exc:
            err = str(exc)
            if _looks_like_smb_auth_error(err):
                cred_hint = (
                    "Enter your username and password in the SMB Credentials section."
                    if not smb_username
                    else "Check that your username and password are correct."
                )
                self._alert(
                    "Could Not Initialise",
                    f"SMB access was denied while creating the repository.\n\n{cred_hint}",
                )
            else:
                self._alert("Could Not Initialise", err)
            return
        self._finish_save(
            uri, name, token, ssl_verify, None,
            smb_username, smb_password, ssh_username, ssh_password,
        )

    def _init_ssh_repo(
        self,
        uri: str,
        name: str,
        token: str | None,
        ssl_verify: bool,
        smb_username: str | None = None,
        smb_password: str | None = None,
        ssh_username: str | None = None,
        ssh_password: str | None = None,
    ) -> None:
        from cellar.utils.ssh import SshPath

        parsed = urlparse(uri)
        if not parsed.hostname:
            self._alert("Invalid URI", f"No hostname in SFTP URI: {uri!r}")
            return

        user = ssh_username or parsed.username or None
        root = SshPath(
            parsed.hostname,
            parsed.path or "/",
            user=user,
            port=parsed.port,
            password=ssh_password,
        )
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._alert("Could Not Initialise", str(exc))
            return

        cat_path = root / "catalogue.json"
        data = json.dumps(_empty_catalogue(), indent=2, ensure_ascii=False)
        try:
            cat_path.write_text(data)
        except OSError as exc:
            self._alert("Could Not Initialise", f"Could not write catalogue.json: {exc}")
            return

        log.info("Initialised new SFTP repo at %s", uri)
        self._finish_save(
            uri, name, token, ssl_verify, None,
            smb_username, smb_password, ssh_username, ssh_password,
        )

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
        smb_username: str | None = None,
        smb_password: str | None = None,
        ssh_username: str | None = None,
        ssh_password: str | None = None,
    ) -> None:
        if smb_password:
            from cellar.backend.config import save_smb_password
            save_smb_password(uri, smb_password)
        if ssh_password:
            from cellar.backend.config import save_ssh_password
            save_ssh_password(uri, ssh_password)

        cfg: dict = {"uri": uri, "name": name}
        if smb_username:
            cfg["smb_username"] = smb_username
        if ssh_username:
            cfg["ssh_username"] = ssh_username
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

def _move_install_data(old_dir: Path, new_dir: Path) -> None:
    """Move prefixes, native, and bases from *old_dir* to *new_dir*.

    Each item is moved individually so that items already present at the
    destination are skipped rather than overwritten.  The DB ``install_path``
    values are updated after all moves complete.
    """
    import shutil

    from cellar.backend import database

    for subdir in ("prefixes", "native", "bases"):
        src_root = old_dir / subdir
        if not src_root.is_dir():
            continue
        dst_root = new_dir / subdir
        dst_root.mkdir(parents=True, exist_ok=True)
        for item in list(src_root.iterdir()):
            dst_item = dst_root / item.name
            if dst_item.exists():
                log.warning("Skipping %s — already exists at destination", item.name)
                continue
            try:
                shutil.move(str(item), dst_item)
            except Exception:
                log.exception("Failed to move %s", item)

    database.update_install_paths(str(old_dir), str(new_dir))


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
    return any(kw in low for kw in (
        "not found", "does not exist", "no such file",
        "cannot find", "path not found", "object name not found",
        "object path not found",
    ))


def _looks_like_smb_auth_error(err: str) -> bool:
    """Heuristic: does this look like an SMB authentication or access-denied failure?"""
    low = err.lower()
    return any(kw in low for kw in (
        "access denied", "access_denied", "permission denied",
        "logon failure", "wrong password", "bad password",
        "status_access_denied", "status_logon_failure",
        "authentication", "0xc000006d", "0xc0000022", "0xc000006e",
    ))


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

"""Settings / Preferences dialog."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse


import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.backend.config import (
    certs_dir,
    clear_igdb_creds,
    load_igdb_creds,
    load_repos,
    save_igdb_creds,
    save_repos,
)
from cellar.utils.progress import fmt_stats as _fmt_ul_stats

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
        page.add(self._repo_group)

        # ── Group: Access Control ─────────────────────────────────────────
        access_group = Adw.PreferencesGroup(title="Access Control")
        page.add(access_group)

        gen_row = Adw.ActionRow(
            title="Generate access token",
            subtitle="Creates a random token to use in your web server config",
        )
        gen_btn = Gtk.Button(label="Generate", valign=Gtk.Align.CENTER)
        gen_btn.add_css_class("suggested-action")
        gen_btn.connect("clicked", self._on_generate_token)
        gen_row.add_suffix(gen_btn)
        access_group.add(gen_row)

        # ── Group: umu-launcher ───────────────────────────────────────────
        self._build_umu_group(page)

        # ── Group: Installed Base Images ──────────────────────────────────
        self._build_bases_group(page)

        # ── Group: IGDB Integration ────────────────────────────────────────
        self._build_igdb_group(page)

        self._rebuild_repo_rows()

    # ------------------------------------------------------------------
    # umu-launcher
    # ------------------------------------------------------------------

    def _build_umu_group(self, page: Adw.PreferencesPage) -> None:
        from cellar.backend.config import load_umu_path, save_umu_path
        from cellar.backend.umu import detect_umu

        umu_group = Adw.PreferencesGroup(
            title="umu-launcher",
            description="Path to umu-run binary. Leave empty to auto-detect.",
        )
        page.add(umu_group)

        detected = detect_umu(None) or ""
        hint = f"e.g. {detected}" if detected else "leave empty to auto-detect"
        self._umu_path_row = Adw.EntryRow(title=f"umu-run path ({hint})")
        current = load_umu_path() or ""
        if current:
            self._umu_path_row.set_text(current)
        umu_group.add(self._umu_path_row)

        save_row = Adw.ActionRow(title="Save umu-run path")
        save_btn = Gtk.Button(label="Save")
        save_btn.add_css_class("suggested-action")
        save_btn.set_valign(Gtk.Align.CENTER)
        save_btn.connect("clicked", self._on_umu_save_clicked)
        save_row.add_suffix(save_btn)
        umu_group.add(save_row)

    def _on_umu_save_clicked(self, _btn) -> None:
        from cellar.backend.config import save_umu_path
        path = self._umu_path_row.get_text().strip() or None
        save_umu_path(path)

    # ------------------------------------------------------------------
    # Installed Base Images
    # ------------------------------------------------------------------

    def _build_bases_group(self, page: Adw.PreferencesPage) -> None:
        self._bases_page = page
        self._bases_group = Adw.PreferencesGroup(
            title="Installed Base Images",
            description=(
                "Base images are shared by delta packages. Removing one will "
                "require a re-download the next time a dependent title is launched."
            ),
        )
        page.add(self._bases_group)
        self._bases_rows: list[Adw.ActionRow] = []
        self._rebuild_bases_rows()

    def _rebuild_bases_rows(self) -> None:
        from cellar.backend.database import get_all_installed_bases

        for row in self._bases_rows:
            self._bases_group.remove(row)
        self._bases_rows.clear()

        bases = get_all_installed_bases()
        if not bases:
            row = Adw.ActionRow(title="No base images installed")
            row.set_sensitive(False)
            self._bases_rows.append(row)
            self._bases_group.add(row)
            return

        for rec in bases:
            runner = rec["runner"]
            installed_at = (rec.get("installed_at") or "")[:10]
            row = Adw.ActionRow(title=runner, subtitle=f"Installed {installed_at}" if installed_at else "")
            del_btn = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                has_frame=False,
                tooltip_text="Remove base image",
                css_classes=["destructive-action"],
            )
            del_btn.connect("clicked", self._on_delete_base, runner)
            row.add_suffix(del_btn)
            self._bases_rows.append(row)
            self._bases_group.add(row)

    def _on_delete_base(self, _btn: Gtk.Button, runner: str) -> None:
        dialog = Adw.AlertDialog(
            heading="Remove Base Image?",
            body=f"The base image for \"{runner}\" will be deleted from local storage. "
                 "If a delta package that depends on it is launched, it will be downloaded again.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_delete_base_confirmed, runner)
        dialog.present(self)

    def _on_delete_base_confirmed(self, _dialog, response: str, runner: str) -> None:
        if response != "remove":
            return
        from cellar.backend.base_store import remove_base
        remove_base(runner)
        self._rebuild_bases_rows()

    # ------------------------------------------------------------------
    # IGDB Integration
    # ------------------------------------------------------------------

    def _build_igdb_group(self, page: Adw.PreferencesPage) -> None:
        igdb_group = Adw.PreferencesGroup(
            title="IGDB Integration",
            description="Look up game metadata when adding packages. "
            "Requires a free Twitch Developer application.",
        )
        page.add(igdb_group)

        # Entry rows — shown when no credentials are saved.
        self._igdb_id_row = Adw.EntryRow(title="Twitch Client ID")
        igdb_group.add(self._igdb_id_row)

        self._igdb_secret_row = Adw.PasswordEntryRow(title="Client Secret")
        igdb_group.add(self._igdb_secret_row)

        # Save row — shown with the entry rows.
        self._igdb_save_row = Adw.ActionRow()
        self._igdb_save_btn = Gtk.Button(
            label="Save", valign=Gtk.Align.CENTER, css_classes=["suggested-action"]
        )
        self._igdb_save_btn.connect("clicked", self._on_igdb_save)
        self._igdb_save_row.add_suffix(self._igdb_save_btn)
        igdb_group.add(self._igdb_save_row)

        # Configured row — shown when credentials are stored; hides the entry rows.
        self._igdb_configured_row = Adw.ActionRow(title="IGDB credentials")
        ok_icon = Gtk.Image.new_from_icon_name("emblem-ok-symbolic")
        ok_icon.set_pixel_size(16)
        ok_icon.set_valign(Gtk.Align.CENTER)
        self._igdb_configured_row.add_prefix(ok_icon)

        igdb_edit_btn = Gtk.Button(
            icon_name="document-edit-symbolic",
            valign=Gtk.Align.CENTER,
            has_frame=False,
            tooltip_text="Edit credentials",
        )
        igdb_edit_btn.connect("clicked", self._on_igdb_edit)
        self._igdb_configured_row.add_suffix(igdb_edit_btn)

        igdb_del_btn = Gtk.Button(
            icon_name="user-trash-symbolic",
            valign=Gtk.Align.CENTER,
            has_frame=False,
            tooltip_text="Remove credentials",
            css_classes=["destructive-action"],
        )
        igdb_del_btn.connect("clicked", self._on_igdb_clear)
        self._igdb_configured_row.add_suffix(igdb_del_btn)

        igdb_group.add(self._igdb_configured_row)

        self._igdb_refresh_status()

    def _igdb_refresh_status(self) -> None:
        creds = load_igdb_creds()
        configured = creds is not None
        self._igdb_id_row.set_visible(not configured)
        self._igdb_secret_row.set_visible(not configured)
        self._igdb_save_row.set_visible(not configured)
        if configured:
            self._igdb_configured_row.set_subtitle(creds["client_id"])
        self._igdb_configured_row.set_visible(configured)

    def _on_igdb_save(self, _btn) -> None:
        client_id = self._igdb_id_row.get_text().strip()
        secret = self._igdb_secret_row.get_text().strip()
        if not client_id or not secret:
            self._alert("Missing Credentials", "Enter both Client ID and Client Secret.")
            return

        self._igdb_save_btn.set_label("Saving\u2026")
        self._igdb_save_btn.set_sensitive(False)

        def _run() -> None:
            try:
                from cellar.backend.igdb import IGDBClient  # noqa: PLC0415

                client = IGDBClient(client_id, secret)
                client._ensure_token()  # noqa: SLF001 — intentional verification call
                GLib.idle_add(self._on_igdb_save_ok, client_id, secret)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_igdb_save_fail, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _on_igdb_save_ok(self, client_id: str, secret: str) -> None:
        save_igdb_creds(client_id, secret)
        self._igdb_secret_row.set_text("")
        self._igdb_save_btn.set_label("Save")
        self._igdb_save_btn.set_sensitive(True)
        self._igdb_refresh_status()

    def _on_igdb_save_fail(self, message: str) -> None:
        self._igdb_save_btn.set_label("Save")
        self._igdb_save_btn.set_sensitive(True)
        self._alert("Verification Failed", message)

    def _on_igdb_edit(self, _btn) -> None:
        creds = load_igdb_creds()
        if creds:
            self._igdb_id_row.set_text(creds["client_id"])
        self._igdb_id_row.set_visible(True)
        self._igdb_secret_row.set_visible(True)
        self._igdb_save_row.set_visible(True)
        self._igdb_configured_row.set_visible(False)

    def _on_igdb_clear(self, _btn) -> None:
        clear_igdb_creds()
        self._igdb_id_row.set_text("")
        self._igdb_secret_row.set_text("")
        self._igdb_refresh_status()

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

        # "Add Repository" row always appears last — same style as Access Control.
        add_row = Adw.ActionRow(title="Add Repository")
        add_btn = Gtk.Button(label="Add", valign=Gtk.Align.CENTER)
        add_btn.add_css_class("suggested-action")
        add_btn.connect("clicked", self._on_add_clicked)
        add_row.add_suffix(add_btn)
        add_row.set_activatable_widget(add_btn)
        self._repo_rows.append(add_row)
        self._repo_group.add(add_row)

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
        self._uri_row.connect("changed", self._on_uri_changed)
        if self._existing:
            self._uri_row.set_text(self._existing.get("uri") or "")
        group.add(self._uri_row)

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

        # Show SMB group only for smb:// URIs (or bare UNC paths starting with //).
        _existing_uri = self._existing.get("uri", "") if self._existing else ""
        is_smb = (
            urlparse(_existing_uri).scheme.lower() == "smb"
            or (_existing_uri.startswith("//") and "://" not in _existing_uri)
        )
        self._smb_group.set_visible(is_smb)

        # HTTP-only group
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
        self._ssl_row.connect("notify::active", self._on_ssl_toggled)
        http_group.add(self._ssl_row)

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
        http_group.add(self._ca_row)

        # Show HTTP group only for non-SMB URIs
        http_group.set_visible(not is_smb)
        self._http_group = http_group

        toolbar.set_content(scroll)
        self.set_child(toolbar)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------

    def _on_uri_changed(self, _entry) -> None:
        """Show/hide SMB and HTTP credential groups based on current URI scheme."""
        text = self._uri_row.get_text().strip()
        scheme = urlparse(text).scheme.lower()
        # UNC paths (//server/share/…) are SMB even without the smb: prefix.
        is_smb = scheme == "smb" or (text.startswith("//") and "://" not in text)
        self._smb_group.set_visible(is_smb)
        self._http_group.set_visible(not is_smb)

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

        # Auto-convert UNC paths (//server/share/path) to smb:// URIs.
        if uri.startswith("//") and "://" not in uri:
            uri = "smb:" + uri
            self._uri_row.set_text(uri)

        name = self._name_row.get_text().strip()
        scheme = urlparse(uri).scheme.lower()
        is_smb = scheme == "smb"

        token = self._token_row.get_text().strip() or None if not is_smb else None
        ssl_verify = self._ssl_row.get_active() if not is_smb else True
        smb_username = self._smb_user_row.get_text().strip() or None if is_smb else None
        smb_password = self._smb_pass_row.get_text() or None if is_smb else None

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
                self._ask_init(uri, name, token, ssl_verify, smb_username, smb_password)
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
            repo.fetch_catalogue()
        except RepoError as exc:
            err = str(exc)
            if _looks_like_missing(err):
                if repo.is_writable:
                    self._ask_init(uri, name, token, ssl_verify, smb_username, smb_password)
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

        self._finish_save(uri, name, token, ssl_verify, ca_cert_name, smb_username, smb_password)

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
            uri, name, token, ssl_verify, smb_username, smb_password,
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
    ) -> None:
        if response != "init":
            return
        scheme = urlparse(uri).scheme.lower()
        if _is_local_uri(uri):
            self._init_local_repo(uri, name, token, ssl_verify, smb_username, smb_password)
        elif scheme == "smb":
            self._init_smb_repo(uri, name, token, ssl_verify, smb_username, smb_password)
        elif scheme == "ssh":
            self._init_ssh_repo(uri, name, token, ssl_verify, smb_username, smb_password)
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
        self._finish_save(uri, name, token, ssl_verify, None, smb_username, smb_password)

    def _init_smb_repo(
        self,
        uri: str,
        name: str,
        token: str | None,
        ssl_verify: bool,
        smb_username: str | None = None,
        smb_password: str | None = None,
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
        self._finish_save(uri, name, token, ssl_verify, None, smb_username, smb_password)

    def _init_ssh_repo(
        self,
        uri: str,
        name: str,
        token: str | None,
        ssl_verify: bool,
        smb_username: str | None = None,
        smb_password: str | None = None,
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
        self._finish_save(uri, name, token, ssl_verify, None, smb_username, smb_password)

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
    ) -> None:
        # Persist SMB password to keyring before saving config.
        if smb_password:
            from cellar.backend.config import save_smb_password
            save_smb_password(uri, smb_password)
        elif smb_username is None:
            # Clearing credentials: remove stored password if URI changed.
            pass

        cfg: dict = {"uri": uri, "name": name}
        if smb_username:
            cfg["smb_username"] = smb_username
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

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
from gi.repository import Adw, Gtk

from cellar.backend.config import certs_dir, load_repos, save_repos

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
        self._repo_group = Adw.PreferencesGroup(
            title="Repositories",
            description=(
                "Sources to browse and install apps from. "
                "HTTP(S) sources are read-only."
            ),
        )
        page.add(self._repo_group)

        # URI entry row — the Add button triggers the full add flow.
        self._add_row = Adw.EntryRow(title="Repository URI")
        add_btn = Gtk.Button(
            icon_name="list-add-symbolic",
            valign=Gtk.Align.CENTER,
            has_frame=False,
            tooltip_text="Add repository",
        )
        add_btn.connect("clicked", lambda _b: self._on_add_activated(self._add_row))
        self._add_row.add_suffix(add_btn)
        self._add_row.connect("entry-activated", self._on_add_activated)

        # Optional token row — sits below the URI row, always visible.
        self._add_token_row = Adw.EntryRow(title="Access token (optional)")

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

        self._rebuild_repo_rows()

    # ------------------------------------------------------------------
    # Repo list management
    # ------------------------------------------------------------------

    def _rebuild_repo_rows(self) -> None:
        """Sync the visible rows with the on-disk repo list."""
        for w in (self._add_row, self._add_token_row):
            if w.get_parent() is not None:
                self._repo_group.remove(w)

        for row in self._repo_rows:
            self._repo_group.remove(row)
        self._repo_rows.clear()

        for repo_cfg in load_repos():
            row = self._make_repo_row(repo_cfg)
            self._repo_rows.append(row)
            self._repo_group.add(row)

        # Add rows are always last.
        self._repo_group.add(self._add_row)
        self._repo_group.add(self._add_token_row)

    def _make_repo_row(self, repo_cfg: dict) -> Adw.EntryRow:
        uri = repo_cfg["uri"]
        ca_cert = repo_cfg.get("ca_cert") or ""
        ssl_verify = repo_cfg.get("ssl_verify", True)
        token = repo_cfg.get("token") or ""

        row = Adw.EntryRow(title="Repository URI", text=uri)
        row.connect("entry-activated", self._on_repo_uri_activated, uri)

        if token:
            icon = Gtk.Image.new_from_icon_name("channel-secure-symbolic")
            icon.set_tooltip_text("Bearer token configured")
            row.add_prefix(icon)
            change_btn = Gtk.Button(
                label="Token…",
                valign=Gtk.Align.CENTER,
                has_frame=False,
            )
            change_btn.connect("clicked", self._on_change_token, uri)
            row.add_suffix(change_btn)
        elif ca_cert:
            icon = Gtk.Image.new_from_icon_name("security-high-symbolic")
            icon.set_tooltip_text(f"CA certificate: {Path(ca_cert).name}")
            row.add_prefix(icon)
        elif not ssl_verify:
            icon = Gtk.Image.new_from_icon_name("security-low-symbolic")
            icon.set_tooltip_text("SSL verification disabled")
            row.add_prefix(icon)

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
    # "Add" flow
    # ------------------------------------------------------------------

    def _on_add_activated(self, entry_row: Adw.EntryRow) -> None:
        uri = entry_row.get_text().strip()
        if not uri:
            return

        token = self._add_token_row.get_text().strip() or None

        # Duplicate check.
        if any(r["uri"] == uri for r in load_repos()):
            self._alert("Already Added", "This repository is already in the list.")
            return

        from cellar.backend.repo import Repo, RepoError

        # For a local path that doesn't exist yet, skip straight to init.
        if _is_local_uri(uri):
            parsed = urlparse(uri)
            local_path = Path(parsed.path if parsed.path else uri).expanduser()
            if not local_path.is_dir():
                self._ask_init(uri)
                return

        # Validate scheme / create fetcher.
        # Pass a MountOperation so that SMB/NFS shares can be mounted (and
        # credential dialogs shown) when the user first adds them.
        # Adw.PreferencesDialog is not a GtkWindow, so walk up to the root.
        root = self.get_root()
        mount_op = Gtk.MountOperation(
            parent=root if isinstance(root, Gtk.Window) else None
        )
        try:
            repo = Repo(uri, mount_op=mount_op, ssl_verify=True, token=token)
        except RepoError as exc:
            self._alert("Invalid Repository", str(exc))
            return

        # Try to fetch the catalogue.
        # TODO: run this off the main thread once async support lands.
        try:
            repo.fetch_catalogue()
        except RepoError as exc:
            err = str(exc)
            if _looks_like_missing(err):
                if repo.is_writable:
                    self._ask_init(uri)
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
                self._ask_ssl_options(uri, entry_row)
            else:
                self._alert("Could Not Connect", err)
            return

        self._commit_add(uri, token=token)
        entry_row.set_text("")
        self._add_token_row.set_text("")

    def _ask_init(self, uri: str) -> None:
        if _is_local_uri(uri):
            body = (
                "No catalogue.json was found at this location. "
                "Initialise a new empty repository here?"
            )
        else:
            body = (
                "No catalogue.json was found at this location. "
                "Initialise a new empty repository here?\n\n"
                "The directory will be created on the server if it does not exist yet."
            )
        dialog = Adw.AlertDialog(heading="No Catalogue Found", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("init", "Initialise")
        dialog.set_response_appearance("init", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_init_response, uri)
        dialog.present(self)

    def _on_init_response(self, _dialog, response: str, uri: str) -> None:
        if response != "init":
            return

        scheme = urlparse(uri).scheme.lower()

        if _is_local_uri(uri):
            self._init_local_repo(uri)
        elif scheme in ("smb", "nfs"):
            self._init_gio_repo(uri)
        elif scheme == "ssh":
            self._init_ssh_repo(uri)
        else:
            self._alert(
                "Not Supported",
                f"Initialising {scheme!r} repositories is not supported.",
            )

    def _init_local_repo(self, uri: str) -> None:
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
        self._commit_add(uri)
        self._add_row.set_text("")

    def _init_gio_repo(self, uri: str) -> None:
        """Create an empty catalogue.json at an SMB or NFS URI via GIO."""
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
        self._commit_add(uri)
        self._add_row.set_text("")

    def _init_ssh_repo(self, uri: str) -> None:
        """Create an empty catalogue.json at an SSH URI via subprocess."""
        import shlex
        import subprocess

        parsed = urlparse(uri)
        if not parsed.hostname:
            self._alert("Invalid URI", f"No hostname in SSH URI: {uri!r}")
            return

        dest = f"{parsed.username}@{parsed.hostname}" if parsed.username else parsed.hostname
        base_args = [
            "ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
        ]
        if parsed.port:
            base_args += ["-p", str(parsed.port)]
        base_args.append(dest)

        path = parsed.path or "/"

        # Create directory tree on the remote.
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
            self._alert("Could Not Initialise", f"Could not create directory: {stderr or 'SSH error'}")
            return

        # Write catalogue.json via stdin.
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
            self._alert("Could Not Initialise", f"Could not write catalogue.json: {stderr or 'SSH error'}")
            return

        log.info("Initialised new SSH repo at %s", uri)
        self._commit_add(uri)
        self._add_row.set_text("")

    # ------------------------------------------------------------------
    # "Edit URI" flow
    # ------------------------------------------------------------------

    def _on_repo_uri_activated(self, row: Adw.EntryRow, old_uri: str) -> None:
        new_uri = row.get_text().strip()
        if not new_uri or new_uri == old_uri:
            return
        repos = load_repos()
        for r in repos:
            if r["uri"] == old_uri:
                r["uri"] = new_uri
                break
        save_repos(repos)
        self._rebuild_repo_rows()
        if self._on_repos_changed:
            self._on_repos_changed()

    # ------------------------------------------------------------------
    # "Delete" flow
    # ------------------------------------------------------------------

    def _on_delete_repo(self, _btn: Gtk.Button, uri: str) -> None:
        repos = [r for r in load_repos() if r["uri"] != uri]
        save_repos(repos)
        self._rebuild_repo_rows()
        if self._on_repos_changed:
            self._on_repos_changed()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _ask_ssl_options(self, uri: str, entry_row: Adw.EntryRow) -> None:
        dialog = Adw.AlertDialog(
            heading="SSL Certificate Error",
            body=(
                f"The server at {uri} presented a certificate that could not be "
                "verified. This is common for self-signed certificates or a "
                "private certificate authority.\n\n"
                "You can provide your CA certificate file (.crt / .pem) so Cellar "
                "can verify the server securely, or disable verification entirely "
                "(not recommended outside a trusted local network)."
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ca_cert", "Add CA Certificate…")
        dialog.add_response("skip", "Disable Verification")
        dialog.set_response_appearance("ca_cert", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_response_appearance("skip", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_ssl_options_response, uri, entry_row)
        dialog.present(self)

    def _on_ssl_options_response(
        self, _dialog, response: str, uri: str, entry_row: Adw.EntryRow
    ) -> None:
        if response == "ca_cert":
            self._pick_ca_cert(uri, entry_row)
        elif response == "skip":
            self._commit_add(uri, ssl_verify=False)
            entry_row.set_text("")

    def _pick_ca_cert(self, uri: str, entry_row: Adw.EntryRow) -> None:
        chooser = Gtk.FileChooserNative(
            title="Select CA Certificate",
            transient_for=self.get_root()
            if isinstance(self.get_root(), Gtk.Window)
            else None,
            action=Gtk.FileChooserAction.OPEN,
        )
        f = Gtk.FileFilter()
        f.set_name("Certificate files (*.crt, *.pem, *.cer)")
        f.add_pattern("*.crt")
        f.add_pattern("*.pem")
        f.add_pattern("*.cer")
        chooser.add_filter(f)
        chooser.connect("response", self._on_ca_cert_chosen, uri, entry_row, chooser)
        chooser.show()

    def _on_ca_cert_chosen(
        self,
        _chooser,
        response: int,
        uri: str,
        entry_row: Adw.EntryRow,
        chooser: Gtk.FileChooserNative,
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        src_path = chooser.get_file().get_path()
        if not src_path:
            return
        # Validate before copying: try connecting with the cert as-is.
        from cellar.backend.repo import Repo, RepoError
        try:
            Repo(uri, ca_cert=src_path).fetch_catalogue()
        except RepoError as exc:
            err = str(exc)
            if _looks_like_ssl_error(err):
                self._alert(
                    "Certificate Not Accepted",
                    f"The server still could not be verified using "
                    f"{Path(src_path).name}.\n\n{err}",
                )
            else:
                self._alert("Could Not Connect", err)
            return
        # Copy cert into the Cellar data directory so the repo config
        # stays valid even if the source file is moved or deleted.
        import shutil
        src = Path(src_path)
        dest = certs_dir() / src.name
        if not dest.exists():
            shutil.copy2(src, dest)
        self._commit_add(uri, ca_cert=dest.name)
        entry_row.set_text("")

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

    def _on_change_token(self, _btn: Gtk.Button, uri: str) -> None:
        """Show a dialog to replace the bearer token for an existing repo."""
        box, token_entry = self._make_token_box()
        dialog = Adw.AlertDialog(
            heading="Change Access Token",
            body="Enter a new bearer token for this repository.",
            extra_child=box,
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("save", "Save")
        dialog.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_change_token_response, uri, token_entry)
        dialog.present(self)

    def _on_change_token_response(
        self, _dialog, response: str, uri: str, token_entry
    ) -> None:
        if response != "save":
            return
        token = token_entry.get_text().strip()
        if not token:
            return
        repos = load_repos()
        for r in repos:
            if r["uri"] == uri:
                r["token"] = token
                break
        save_repos(repos)
        self._rebuild_repo_rows()
        if self._on_repos_changed:
            self._on_repos_changed()

    def _make_token_box(self) -> tuple[Gtk.Box, Gtk.Entry]:
        """Return a (box, entry) pair with a token field and a Generate button."""
        import secrets

        box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=6, margin_top=8
        )
        token_entry = Gtk.Entry(
            hexpand=True,
            placeholder_text="Paste or generate a token",
        )
        gen_btn = Gtk.Button(label="Generate")

        def _on_generate(_b: Gtk.Button) -> None:
            t = secrets.token_hex(32)
            token_entry.set_text(t)
            token_entry.get_clipboard().set(t)

        gen_btn.connect("clicked", _on_generate)
        box.append(token_entry)
        box.append(gen_btn)
        return box, token_entry

    def _commit_add(
        self,
        uri: str,
        *,
        ssl_verify: bool = True,
        ca_cert: str | None = None,
        token: str | None = None,
    ) -> None:
        """Persist the new repo and refresh both the list and the main window."""
        repos = load_repos()
        entry: dict = {"uri": uri, "name": ""}
        if ca_cert:
            entry["ca_cert"] = ca_cert
        elif not ssl_verify:
            entry["ssl_verify"] = False
        if token:
            entry["token"] = token
        repos.append(entry)
        save_repos(repos)
        self._rebuild_repo_rows()
        if self._on_repos_changed:
            self._on_repos_changed()

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

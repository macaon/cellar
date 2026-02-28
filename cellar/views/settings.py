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

from cellar.backend.config import load_repos, save_repos

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
        super().__init__(title="Preferences", **kwargs)
        self._on_repos_changed = on_repos_changed
        self._repo_rows: list[Adw.ActionRow] = []

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

        # Entry row for adding a new repo — always sits at the bottom.
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

        self._rebuild_repo_rows()

    # ------------------------------------------------------------------
    # Repo list management
    # ------------------------------------------------------------------

    def _rebuild_repo_rows(self) -> None:
        """Sync the visible rows with the on-disk repo list."""
        # Pull add_row out of the group (it might not be there yet on first call).
        if self._add_row.get_parent() is not None:
            self._repo_group.remove(self._add_row)

        for row in self._repo_rows:
            self._repo_group.remove(row)
        self._repo_rows.clear()

        for repo_cfg in load_repos():
            row = self._make_repo_row(repo_cfg)
            self._repo_rows.append(row)
            self._repo_group.add(row)

        # Add-row is always last.
        self._repo_group.add(self._add_row)

    def _make_repo_row(self, repo_cfg: dict) -> Adw.ActionRow:
        uri = repo_cfg["uri"]
        name = repo_cfg.get("name") or ""
        ca_cert = repo_cfg.get("ca_cert") or ""
        ssl_verify = repo_cfg.get("ssl_verify", True)
        subtitle_parts = [uri] if name else []
        if ca_cert:
            subtitle_parts.append(f"CA: {Path(ca_cert).name}")
        elif not ssl_verify:
            subtitle_parts.append("SSL verification disabled")
        row = Adw.ActionRow(
            title=name or uri,
            subtitle=" · ".join(subtitle_parts),
        )
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
            repo = Repo(uri, mount_op=mount_op, ssl_verify=True)
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
            elif _looks_like_ssl_error(err):
                self._ask_ssl_options(uri, entry_row)
            else:
                self._alert("Could Not Connect", err)
            return

        self._commit_add(uri)
        entry_row.set_text("")

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
        ca_path = chooser.get_file().get_path()
        if not ca_path:
            return
        # Validate: try connecting with this CA cert before saving.
        from cellar.backend.repo import Repo, RepoError
        try:
            Repo(uri, ca_cert=ca_path).fetch_catalogue()
        except RepoError as exc:
            err = str(exc)
            if _looks_like_ssl_error(err):
                self._alert(
                    "Certificate Not Accepted",
                    f"The server still could not be verified using {ca_path}.\n\n{err}",
                )
            else:
                self._alert("Could Not Connect", err)
            return
        self._commit_add(uri, ca_cert=ca_path)
        entry_row.set_text("")

    def _commit_add(
        self, uri: str, *, ssl_verify: bool = True, ca_cert: str | None = None
    ) -> None:
        """Persist the new repo and refresh both the list and the main window."""
        repos = load_repos()
        entry: dict = {"uri": uri, "name": ""}
        if ca_cert:
            entry["ca_cert"] = ca_cert
        elif not ssl_verify:
            entry["ssl_verify"] = False
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

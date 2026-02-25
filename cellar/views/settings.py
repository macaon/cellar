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

    def __init__(self, *, on_repos_changed: Callable[[], None] | None = None, **kwargs):
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
        row = Adw.ActionRow(
            title=name or uri,
            subtitle=uri if name else "",
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
        try:
            repo = Repo(uri)
        except RepoError as exc:
            self._alert("Invalid Repository", str(exc))
            return

        # Try to fetch the catalogue.
        # TODO: run this off the main thread once async support lands.
        try:
            repo.fetch_catalogue()
        except RepoError as exc:
            err = str(exc)
            if _looks_like_missing(err) and repo.is_writable:
                self._ask_init(uri)
            elif not repo.is_writable:
                self._alert(
                    "No Catalogue Found",
                    f"No catalogue.json was found at:\n\n{uri}\n\n"
                    "HTTP repositories are read-only — the catalogue must "
                    "already exist on the server.",
                )
            else:
                self._alert("Could Not Connect", err)
            return

        self._commit_add(uri)
        entry_row.set_text("")

    def _ask_init(self, uri: str) -> None:
        dialog = Adw.AlertDialog(
            heading="No Catalogue Found",
            body=(
                "No catalogue.json was found at this location. "
                "Initialise a new empty repository here?"
            ),
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("init", "Initialise")
        dialog.set_response_appearance("init", Adw.ResponseAppearance.SUGGESTED)
        dialog.connect("response", self._on_init_response, uri)
        dialog.present(self)

    def _on_init_response(self, _dialog, response: str, uri: str) -> None:
        if response != "init":
            return

        if not _is_local_uri(uri):
            self._alert(
                "Not Yet Supported",
                "Initialising repositories over SSH, SMB, or NFS is not yet "
                "supported.\n\nCreate a catalogue.json at the remote location "
                "manually, then add it here.",
            )
            return

        parsed = urlparse(uri)
        target = Path(parsed.path if parsed.path else uri).expanduser()
        try:
            target.mkdir(parents=True, exist_ok=True)
            catalogue = {
                "cellar_version": 1,
                "generated_at": datetime.now(timezone.utc).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                ),
                "apps": [],
            }
            (target / "catalogue.json").write_text(
                json.dumps(catalogue, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info("Initialised new repo at %s", target)
        except OSError as exc:
            self._alert("Could Not Initialise", str(exc))
            return

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

    def _commit_add(self, uri: str) -> None:
        """Persist the new repo and refresh both the list and the main window."""
        repos = load_repos()
        repos.append({"uri": uri, "name": ""})
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

def _is_local_uri(uri: str) -> bool:
    return urlparse(uri).scheme.lower() in ("", "file")


def _looks_like_missing(err: str) -> bool:
    """Heuristic: does this look like a missing file rather than an auth/network error?"""
    low = err.lower()
    return any(kw in low for kw in ("not found", "does not exist", "no such file"))

"""Catalogue Entries dialog — browse, import for editing, and delete catalogue content."""

from __future__ import annotations

import html
import logging
import shutil
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.backend.project import Project, load_projects, save_project
from cellar.utils.async_work import run_in_background
from cellar.views.builder.progress import ProgressDialog
from cellar.views.widgets import make_loading_stack

log = logging.getLogger(__name__)


class CatalogueEntriesDialog(Adw.Dialog):
    """List catalogue entries from all repos with per-row import and delete actions.

    The download button imports the archive as a builder Project for editing.
    The trash button permanently removes the entry from the repo (writable
    repos only).  Both actions work for app entries and base images.
    """

    def __init__(
        self,
        repos: list,
        on_imported: Callable,
        on_catalogue_changed: Callable | None = None,
    ) -> None:
        super().__init__(title="Catalogue Entries", content_width=560)
        self._repos = repos
        self._on_imported = on_imported
        self._on_catalogue_changed = on_catalogue_changed
        self._entries: list[tuple] = []  # (item, repo, kind) — kind = "app" | "base"

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        self._stack, self._list_box = make_loading_stack("Fetching catalogue\u2026")
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        run_in_background(self._fetch_entries)

    def _fetch_entries(self) -> None:
        apps: list[tuple] = []
        bases: list[tuple] = []
        seen_bases: set[str] = set()
        for repo in self._repos:
            try:
                for entry in repo.fetch_catalogue():
                    if entry.archive:
                        apps.append((entry, repo, "app"))
            except Exception as exc:
                log.warning("Could not fetch catalogue from %s: %s", repo.uri, exc)
            try:
                for runner, base_entry in repo.fetch_bases().items():
                    if runner not in seen_bases:
                        seen_bases.add(runner)
                        bases.append((base_entry, repo, "base"))
            except Exception as exc:
                log.warning("Could not fetch bases from %s: %s", repo.uri, exc)
        apps.sort(key=lambda t: t[0].name.lower())
        bases.sort(key=lambda t: t[0].runner.lower())
        GLib.idle_add(self._populate, apps + bases)

    def _populate(self, results: list[tuple]) -> None:
        self._entries = results
        for idx, (item, repo, kind) in enumerate(results):
            size_mb = (item.archive_size or 0) / 1_000_000
            size_str = f"{size_mb:.0f} MB" if size_mb else ""
            repo_name = repo.name or repo.uri
            if kind == "base":
                title = item.runner
                subtitle_parts = [size_str, repo_name]
            else:
                title = item.name
                subtitle_parts = [item.version or "", size_str, repo_name]
            subtitle = "  \u00b7  ".join(p for p in subtitle_parts if p)
            row = Adw.ActionRow(title=html.escape(title), subtitle=html.escape(subtitle))

            if kind == "base":
                chip = Gtk.Label(label="Base Image")
                chip.add_css_class("tag")
                chip.set_valign(Gtk.Align.CENTER)
                row.add_suffix(chip)

            dl_btn = Gtk.Button(
                icon_name="folder-download-symbolic",
                valign=Gtk.Align.CENTER,
                has_frame=False,
                tooltip_text="Import for editing",
            )
            dl_btn.connect("clicked", self._on_download_clicked, idx)
            row.add_suffix(dl_btn)

            del_btn = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                has_frame=False,
                tooltip_text="Remove from repo" if repo.is_writable else "Read-only repo",
                sensitive=repo.is_writable,
            )
            if repo.is_writable:
                del_btn.add_css_class("destructive-action")
            del_btn.connect("clicked", self._on_delete_clicked, idx)
            row.add_suffix(del_btn)

            self._list_box.append(row)

        if not results:
            empty_row = Adw.ActionRow(
                title="No entries found",
                subtitle="Add a repository with published apps.",
            )
            self._list_box.append(empty_row)

        self._stack.set_visible_child_name("list")

    # ------------------------------------------------------------------
    # Download / import for editing
    # ------------------------------------------------------------------

    def _on_download_clicked(self, _btn, idx: int) -> None:
        if not (0 <= idx < len(self._entries)):
            return
        item, repo, kind = self._entries[idx]
        if kind == "base":
            self._start_import_base(item, repo)
        else:
            self._start_import_app(item, repo)

    def _start_import_base(self, base_entry, repo) -> None:
        root = self.get_root()
        self.close()

        progress = ProgressDialog(label=f"Downloading {base_entry.runner}\u2026")
        progress.present(root)

        def _work():
            import tempfile
            from cellar.backend.installer import (  # noqa: PLC2701
                _build_source,
                _find_bottle_dir,
                _stream_and_extract,
            )
            from cellar.backend.packager import slugify

            archive_uri = repo.resolve_asset_uri(base_entry.archive)
            chunks, total = _build_source(
                archive_uri,
                expected_size=base_entry.archive_size,
                token=repo.token,
                ssl_verify=repo.ssl_verify,
                ca_cert=repo.ca_cert,
            )

            with tempfile.TemporaryDirectory(prefix="cellar-base-import-") as tmp_str:
                tmp = Path(tmp_str)
                extract_dir = tmp / "extracted"
                extract_dir.mkdir()

                _stream_and_extract(
                    chunks, total,
                    is_zst=archive_uri.endswith(".tar.zst"),
                    dest=extract_dir,
                    expected_crc32=base_entry.archive_crc32,
                    cancel_event=None,
                    progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                    stats_cb=None,
                    name_cb=None,
                )

                bottle_src = _find_bottle_dir(extract_dir)

                slug = slugify(base_entry.runner)
                existing = {p.slug for p in load_projects()}
                base_slug, i = slug, 2
                while slug in existing:
                    slug = f"{base_slug}-{i}"
                    i += 1

                project = Project(
                    name=base_entry.runner,
                    slug=slug,
                    project_type="base",
                    runner=base_entry.runner,
                    initialized=True,
                )

                GLib.idle_add(progress.set_label, "Copying prefix\u2026")
                GLib.idle_add(progress.set_fraction, 0.0)
                project.prefix_path.mkdir(parents=True, exist_ok=True)
                for src in bottle_src.rglob("*"):
                    rel = src.relative_to(bottle_src)
                    dst = project.prefix_path / rel
                    if src.is_dir():
                        dst.mkdir(parents=True, exist_ok=True)
                    elif src.is_file():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)

                save_project(project)
                return project

        def _done(project) -> None:
            progress.force_close()
            self._on_imported(project)

        def _error(msg: str) -> None:
            progress.force_close()
            log.error("Base import failed: %s", msg)
            err = Adw.AlertDialog(heading="Import failed", body=msg)
            err.add_response("ok", "OK")
            err.present(root)

        run_in_background(_work, on_done=_done, on_error=_error)

    def _start_import_app(self, entry, repo) -> None:
        root = self.get_root()
        self.close()

        progress = ProgressDialog(label="Downloading\u2026")
        progress.present(root)

        def _work():
            import tempfile
            from cellar.backend.installer import (
                _build_source,        # noqa: PLC2701
                _find_bottle_dir,     # noqa: PLC2701
                _stream_and_extract,  # noqa: PLC2701
            )
            archive_uri = repo.resolve_asset_uri(entry.archive)
            chunks, total = _build_source(
                archive_uri,
                expected_size=entry.archive_size,
                token=repo.token,
                ssl_verify=repo.ssl_verify,
                ca_cert=repo.ca_cert,
            )

            with tempfile.TemporaryDirectory(prefix="cellar-import-") as tmp_str:
                tmp = Path(tmp_str)
                extract_dir = tmp / "extracted"
                extract_dir.mkdir()

                _stream_and_extract(
                    chunks, total,
                    is_zst=archive_uri.endswith(".tar.zst"),
                    dest=extract_dir,
                    expected_crc32=entry.archive_crc32,
                    cancel_event=None,
                    progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                    stats_cb=None,
                    name_cb=None,
                )

                bottle_src = _find_bottle_dir(extract_dir)

                from cellar.backend.packager import slugify
                slug = slugify(entry.id)
                existing = {p.slug for p in load_projects()}
                base_slug, i = slug, 2
                while slug in existing:
                    slug = f"{base_slug}-{i}"
                    i += 1

                _ep = entry.entry_point or ""
                project = Project(
                    name=entry.name,
                    slug=slug,
                    project_type="app",
                    runner=entry.built_with.runner if entry.built_with else "",
                    entry_points=[{"name": "Main", "path": _ep}] if _ep else [],
                    steam_appid=entry.steam_appid,
                    initialized=True,
                    origin_app_id=entry.id,
                )

                GLib.idle_add(progress.set_label, "Copying prefix\u2026")
                project.prefix_path.mkdir(parents=True, exist_ok=True)
                for src in bottle_src.rglob("*"):
                    rel = src.relative_to(bottle_src)
                    dst = project.prefix_path / rel
                    if src.is_dir():
                        dst.mkdir(parents=True, exist_ok=True)
                    elif src.is_file():
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst)

                save_project(project)
                return project

        def _done(project) -> None:
            progress.force_close()
            self._on_imported(project)

        def _error(msg: str) -> None:
            progress.force_close()
            log.error("Import failed: %s", msg)
            err = Adw.AlertDialog(heading="Import failed", body=msg)
            err.add_response("ok", "OK")
            err.present(root)

        run_in_background(_work, on_done=_done, on_error=_error)

    # ------------------------------------------------------------------
    # Delete from repo
    # ------------------------------------------------------------------

    def _on_delete_clicked(self, _btn, idx: int) -> None:
        if not (0 <= idx < len(self._entries)):
            return
        item, repo, kind = self._entries[idx]
        name = item.runner if kind == "base" else item.name
        dialog = Adw.AlertDialog(
            heading="Remove from Repo?",
            body=f"\u201c{name}\u201d will be permanently deleted from the repository.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.connect("response", self._on_delete_confirmed, idx)
        dialog.present(self)

    def _on_delete_confirmed(self, _dialog, response: str, idx: int) -> None:
        if response != "remove":
            return
        item, repo, kind = self._entries[idx]

        def _work():
            from cellar.backend import packager
            repo_root = repo.writable_path()
            if kind == "base":
                packager.remove_base(repo_root, item.runner)
            else:
                packager.remove_from_repo(repo_root, item)

        def _done(_result) -> None:
            self._entries.pop(idx)
            row = self._list_box.get_row_at_index(idx)
            if row:
                self._list_box.remove(row)
            if self._on_catalogue_changed:
                self._on_catalogue_changed()

        def _error(msg: str) -> None:
            log.error("Delete failed: %s", msg)
            err = Adw.AlertDialog(heading="Delete failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

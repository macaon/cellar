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
        self._list_box.set_header_func(self._section_header_func)
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        run_in_background(self._fetch_entries)

    def _fetch_entries(self) -> None:
        from types import SimpleNamespace

        apps: list[tuple] = []
        bases: list[tuple] = []
        runners: list[tuple] = []
        seen_bases: set[str] = set()
        seen_runners: set[str] = set()
        for repo in self._repos:
            try:
                for entry in repo.fetch_catalogue():
                    if entry.archive:
                        apps.append((entry, repo, "app"))
            except Exception as exc:
                log.warning("Could not fetch catalogue from %s: %s", repo.uri, exc)
            try:
                for name, base_entry in repo.fetch_bases().items():
                    if name not in seen_bases:
                        seen_bases.add(name)
                        bases.append((base_entry, repo, "base"))
                    if base_entry.runner_archive and base_entry.runner not in seen_runners:
                        seen_runners.add(base_entry.runner)
                        runner_item = SimpleNamespace(
                            name=base_entry.runner,
                            archive=base_entry.runner_archive,
                            archive_size=base_entry.runner_archive_size,
                            archive_crc32=base_entry.runner_archive_crc32,
                        )
                        runners.append((runner_item, repo, "runner"))
            except Exception as exc:
                log.warning("Could not fetch bases from %s: %s", repo.uri, exc)
        apps.sort(key=lambda t: t[0].name.lower())
        bases.sort(key=lambda t: t[0].name.lower())
        runners.sort(key=lambda t: t[0].name.lower())
        GLib.idle_add(self._populate, apps + bases + runners)

    def _populate(self, results: list[tuple]) -> None:
        self._entries = results

        # Build set of (repo_uri, runner) pairs that have at least one dependent app.
        # Used to disable the delete button on base images that apps rely on.
        depended: set[tuple[str, str]] = set()
        for item, repo, kind in results:
            if kind == "app" and item.base_runner:
                depended.add((repo.uri, item.base_runner))

        for idx, (item, repo, kind) in enumerate(results):
            size_mb = (item.archive_size or 0) / 1_000_000
            size_str = f"{size_mb:.0f} MB" if size_mb else ""
            repo_name = repo.name or repo.uri
            if kind == "app":
                title = item.name
                base_str = f"Base: {item.base_runner}" if item.base_runner else ""
                subtitle_parts = [item.version or "", base_str, size_str, repo_name]
            elif kind == "base":
                title = item.name
                runner_str = f"Runner: {item.runner}" if item.runner != item.name else ""
                subtitle_parts = [runner_str, size_str, repo_name]
            else:  # runner
                title = item.name
                subtitle_parts = [size_str, repo_name]
            subtitle = "  \u00b7  ".join(p for p in subtitle_parts if p)
            row = Adw.ActionRow(title=html.escape(title), subtitle=html.escape(subtitle))

            if kind == "base":
                chip = Gtk.Label(label="Base Image")
                chip.add_css_class("tag")
                chip.set_valign(Gtk.Align.CENTER)
                row.add_suffix(chip)
            elif kind == "runner":
                chip = Gtk.Label(label="Runner")
                chip.add_css_class("tag")
                chip.set_valign(Gtk.Align.CENTER)
                row.add_suffix(chip)

            if kind == "runner":
                dl_btn = Gtk.Button(
                    icon_name="folder-download-symbolic",
                    valign=Gtk.Align.CENTER,
                    has_frame=False,
                    tooltip_text="Install runner locally",
                )
            else:
                dl_btn = Gtk.Button(
                    icon_name="folder-download-symbolic",
                    valign=Gtk.Align.CENTER,
                    has_frame=False,
                    tooltip_text="Import for editing",
                )
            dl_btn.connect("clicked", self._on_download_clicked, idx)
            row.add_suffix(dl_btn)

            if kind == "base":
                is_depended = (repo.uri, item.runner) in depended
                can_delete = repo.is_writable and not is_depended
                if is_depended:
                    del_tooltip = "Apps in this repo depend on this base"
                elif not repo.is_writable:
                    del_tooltip = "Read-only repo"
                else:
                    del_tooltip = "Remove from repo"
            else:
                can_delete = repo.is_writable
                del_tooltip = "Remove from repo" if repo.is_writable else "Read-only repo"

            del_btn = Gtk.Button(
                icon_name="user-trash-symbolic",
                valign=Gtk.Align.CENTER,
                has_frame=False,
                tooltip_text=del_tooltip,
                sensitive=can_delete,
            )
            if can_delete:
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

    def _section_header_func(
        self, row: Gtk.ListBoxRow, before: Gtk.ListBoxRow | None
    ) -> None:
        idx = row.get_index()
        if not (0 <= idx < len(self._entries)):
            row.set_header(None)
            return
        _, _, kind = self._entries[idx]
        prev_kind: str | None = None
        if before is not None:
            prev_idx = before.get_index()
            if 0 <= prev_idx < len(self._entries):
                _, _, prev_kind = self._entries[prev_idx]

        if prev_kind is None and kind == "app":
            row.set_header(self._make_section_label("Apps"))
        elif (prev_kind is None and kind == "base") or (prev_kind == "app" and kind == "base"):
            row.set_header(self._make_section_label("Base Images"))
        elif prev_kind != "runner" and kind == "runner":
            row.set_header(self._make_section_label("Runners"))
        else:
            row.set_header(None)

    @staticmethod
    def _make_section_label(text: str) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        label.add_css_class("heading")
        label.add_css_class("dim-label")
        label.set_margin_top(16)
        label.set_margin_bottom(4)
        label.set_margin_start(12)
        label.set_margin_end(12)
        return label

    # ------------------------------------------------------------------
    # Download / import for editing
    # ------------------------------------------------------------------

    def _on_download_clicked(self, _btn, idx: int) -> None:
        if not (0 <= idx < len(self._entries)):
            return
        item, repo, kind = self._entries[idx]
        if kind == "base":
            self._start_import_base(item, repo)
        elif kind == "runner":
            self._start_import_runner(item, repo)
        else:
            self._start_import_app(item, repo)

    def _start_import_base(self, base_entry, repo) -> None:
        root = self.get_root()
        self.close()

        progress = ProgressDialog(label=f"Downloading {base_entry.name}\u2026")
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

                slug = slugify(base_entry.name)
                existing = {p.slug for p in load_projects()}
                base_slug, i = slug, 2
                while slug in existing:
                    slug = f"{base_slug}-{i}"
                    i += 1

                project = Project(
                    name=base_entry.name,
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

    def _start_import_runner(self, runner_item, repo) -> None:
        root = self.get_root()
        self.close()

        progress = ProgressDialog(label=f"Downloading {runner_item.name}\u2026")
        progress.present(root)

        def _work():
            import tempfile
            from cellar.backend.installer import (  # noqa: PLC2701
                _build_source,
                _stream_and_extract,
            )
            from cellar.backend.umu import runners_dir

            archive_uri = repo.resolve_asset_uri(runner_item.archive)
            chunks, total = _build_source(
                archive_uri,
                expected_size=runner_item.archive_size,
                token=repo.token,
                ssl_verify=repo.ssl_verify,
                ca_cert=repo.ca_cert,
            )

            dest = runners_dir() / runner_item.name
            if dest.exists():
                shutil.rmtree(dest)

            with tempfile.TemporaryDirectory(prefix="cellar-runner-import-") as tmp_str:
                tmp = Path(tmp_str)
                extract_dir = tmp / "extracted"
                extract_dir.mkdir()

                _stream_and_extract(
                    chunks, total,
                    is_zst=archive_uri.endswith(".tar.zst"),
                    dest=extract_dir,
                    expected_crc32=runner_item.archive_crc32,
                    cancel_event=None,
                    progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                    stats_cb=None,
                    name_cb=None,
                )

                extracted = [p for p in extract_dir.iterdir() if p.is_dir()]
                if not extracted:
                    raise RuntimeError("Runner archive contained no directory")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(extracted[0], dest)

        def _done(_result) -> None:
            progress.force_close()

        def _error(msg: str) -> None:
            progress.force_close()
            log.error("Runner import failed: %s", msg)
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
        name = item.name
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
                packager.remove_base(repo_root, item.name)
            elif kind == "runner":
                packager.remove_runner_archive(repo_root, item.name)
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

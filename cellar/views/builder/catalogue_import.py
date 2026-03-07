"""Import from Catalogue dialog — download an existing catalogue entry as a project."""

from __future__ import annotations

import html
import logging
import shutil
import threading
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.backend.project import Project, load_projects, save_project
from cellar.utils.async_work import run_in_background
from cellar.views.builder.progress import ProgressDialog

log = logging.getLogger(__name__)


class ImportFromCatalogueDialog(Adw.Dialog):
    """List catalogue entries from all repos and import one as a new project.

    Downloads the archive, extracts it to the project prefix directory, and
    pre-fills all metadata from the catalogue entry.  The resulting project
    has ``origin_app_id`` set so re-publishing updates the existing entry.
    """

    def __init__(self, repos: list, on_imported: Callable) -> None:
        super().__init__(title="Import from Catalogue", content_width=520)
        self._repos = repos
        self._on_imported = on_imported
        self._entries: list[tuple] = []   # (item, repo, kind) — kind = "app" | "base"
        self._selected_idx: int = -1

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        self._import_btn = Gtk.Button(label="Import")
        self._import_btn.add_css_class("suggested-action")
        self._import_btn.set_sensitive(False)
        self._import_btn.connect("clicked", self._on_import_clicked)
        header.pack_end(self._import_btn)

        toolbar.add_top_bar(header)

        self._stack = Gtk.Stack()

        # Loading state
        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        spinner_box.set_valign(Gtk.Align.CENTER)
        spinner_box.set_vexpand(True)
        spinner = Gtk.Spinner(spinning=True)
        spinner.set_size_request(32, 32)
        spinner_box.append(spinner)
        lbl = Gtk.Label(label="Fetching catalogue\u2026")
        lbl.add_css_class("dim-label")
        spinner_box.append(lbl)
        self._stack.add_named(spinner_box, "loading")

        # List state
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_min_content_height(360)
        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_top(12)
        self._list_box.set_margin_bottom(12)
        self._list_box.set_margin_start(12)
        self._list_box.set_margin_end(12)
        self._list_box.connect("row-selected", self._on_row_selected)
        scroll.set_child(self._list_box)
        self._stack.add_named(scroll, "list")

        self._stack.set_visible_child_name("loading")
        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        threading.Thread(target=self._fetch_entries, daemon=True).start()

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
        for item, repo, kind in results:
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
            self._list_box.append(row)

        if not results:
            empty_row = Adw.ActionRow(
                title="No entries found",
                subtitle="Add a repository with published apps.",
            )
            self._list_box.append(empty_row)

        self._stack.set_visible_child_name("list")

    def _on_row_selected(self, _lb, row: Gtk.ListBoxRow | None) -> None:
        if row is None:
            self._selected_idx = -1
            self._import_btn.set_sensitive(False)
            return
        self._selected_idx = row.get_index()
        self._import_btn.set_sensitive(0 <= self._selected_idx < len(self._entries))

    def _on_import_clicked(self, _btn) -> None:
        if not (0 <= self._selected_idx < len(self._entries)):
            return
        item, repo, kind = self._entries[self._selected_idx]

        if kind == "base":
            self._start_import_base(item, repo)
        else:
            self._start_import(item, repo)

    def _start_import_base(self, base_entry, repo) -> None:
        root = self.get_root()
        self.close()

        progress = ProgressDialog(label=f"Downloading {base_entry.runner}\u2026")
        progress.present(root)

        def _work():
            import tempfile
            import time
            from cellar.backend.base_store import install_base
            from cellar.backend.installer import _build_source  # noqa: PLC2701
            from cellar.utils.progress import fmt_stats

            archive_uri = repo.resolve_asset_uri(base_entry.archive)
            chunks, total = _build_source(
                archive_uri,
                expected_size=base_entry.archive_size,
                token=repo.token,
                ssl_verify=repo.ssl_verify,
                ca_cert=repo.ca_cert,
            )

            with tempfile.NamedTemporaryFile(
                prefix="cellar-base-", suffix=".tar.zst", delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                received = 0
                t0 = time.monotonic()
                for chunk in chunks:
                    tmp.write(chunk)
                    received += len(chunk)
                    elapsed = time.monotonic() - t0
                    speed = received / elapsed if elapsed > 0 else 0.0
                    if total:
                        GLib.idle_add(progress.set_fraction, received / total)
                    GLib.idle_add(progress.set_stats, fmt_stats(received, total or 0, speed))

            GLib.idle_add(progress.set_stats, "")
            GLib.idle_add(progress.set_label, "Installing base\u2026")
            GLib.idle_add(progress.set_fraction, 0.0)
            install_base(
                tmp_path,
                base_entry.runner,
                repo_source=repo.uri,
                progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
            )
            tmp_path.unlink(missing_ok=True)

        def _done(_result) -> None:
            progress.force_close()
            self._on_imported(None)

        def _error(msg: str) -> None:
            progress.force_close()
            log.error("Base download failed: %s", msg)
            err = Adw.AlertDialog(heading="Download failed", body=msg)
            err.add_response("ok", "OK")
            err.present(root)

        run_in_background(_work, on_done=_done, on_error=_error)

    def _start_import(self, entry, repo) -> None:
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

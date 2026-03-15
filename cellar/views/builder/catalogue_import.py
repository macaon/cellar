"""Catalogue Entries dialog — browse, import for editing, and delete catalogue content."""

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
from cellar.utils.progress import fmt_stats
from cellar.views.builder.progress import ProgressDialog
from cellar.views.widgets import set_margins

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
        self._entries: list[tuple] = []  # (item, repo, kind) — kind = "app" | "base" | "runner"
        self._row_widgets: list[Adw.ActionRow] = []
        self._del_buttons: list[Gtk.Button] = []

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        toolbar.add_top_bar(header)

        # Loading page
        self._stack = Gtk.Stack()
        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        spinner_box.set_valign(Gtk.Align.CENTER)
        spinner_box.set_vexpand(True)
        spinner = Adw.Spinner()
        spinner_box.append(spinner)
        loading_lbl = Gtk.Label(label="Fetching catalogue\u2026")
        loading_lbl.add_css_class("dim-label")
        spinner_box.append(loading_lbl)
        self._stack.add_named(spinner_box, "loading")

        # List page: three PreferencesGroups inside a scroll
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_min_content_height(500)
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=24)
        set_margins(vbox, 12)
        self._apps_group = Adw.PreferencesGroup(title="Apps")
        self._bases_group = Adw.PreferencesGroup(title="Base Images")
        self._runners_group = Adw.PreferencesGroup(title="Runners")
        vbox.append(self._apps_group)
        vbox.append(self._bases_group)
        vbox.append(self._runners_group)
        scroll.set_child(vbox)
        self._stack.add_named(scroll, "list")
        self._stack.set_visible_child_name("loading")

        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        run_in_background(self._fetch_entries)

    def _fetch_entries(self) -> None:
        apps: list[tuple] = []
        bases: list[tuple] = []
        runners: list[tuple] = []
        seen_bases: set[str] = set()
        seen_runners: set[str] = set()
        for repo in self._repos:
            try:
                for entry in repo.fetch_catalogue():
                    apps.append((entry, repo, "app"))
            except Exception as exc:
                log.warning("Could not fetch catalogue from %s: %s", repo.uri, exc)
            try:
                for name, runner_entry in repo.fetch_runners().items():
                    if name not in seen_runners:
                        seen_runners.add(name)
                        runners.append((runner_entry, repo, "runner"))
                for name, base_entry in repo.fetch_bases().items():
                    if name not in seen_bases:
                        seen_bases.add(name)
                        bases.append((base_entry, repo, "base"))
            except Exception as exc:
                log.warning("Could not fetch bases from %s: %s", repo.uri, exc)
        apps.sort(key=lambda t: t[0].name.lower())
        bases.sort(key=lambda t: t[0].name.lower())
        runners.sort(key=lambda t: t[0].name.lower())
        GLib.idle_add(self._populate, apps + bases + runners)

    def _populate(self, results: list[tuple]) -> None:
        self._entries = results
        self._row_widgets = []

        # Build dependency sets to disable delete on entries that have dependents.
        depended_bases: set[tuple[str, str]] = set()
        for item, repo, kind in results:
            if kind == "app" and item.base_image:
                depended_bases.add((repo.uri, item.base_image))
        depended_runners: set[tuple[str, str]] = set()
        for item, repo, kind in results:
            if kind == "base":
                depended_runners.add((repo.uri, item.runner))

        groups = {
            "app": self._apps_group,
            "base": self._bases_group,
            "runner": self._runners_group,
        }
        counts = {"app": 0, "base": 0, "runner": 0}

        for idx, (item, repo, kind) in enumerate(results):
            size_mb = (item.archive_size or 0) / 1_000_000
            size_str = f"{size_mb:.0f} MB" if size_mb else ""
            repo_name = repo.name or repo.uri
            if kind == "app":
                base_str = f"Base: {item.base_image}" if item.base_image else ""
                subtitle_parts = [item.version or "", base_str, size_str, repo_name]
            elif kind == "base":
                runner_str = f"Runner: {item.runner}" if item.runner != item.name else ""
                subtitle_parts = [runner_str, size_str, repo_name]
            else:  # runner
                subtitle_parts = [size_str, repo_name]
            subtitle = "  \u00b7  ".join(p for p in subtitle_parts if p)
            row = Adw.ActionRow(title=html.escape(item.name), subtitle=html.escape(subtitle))

            dl_btn = Gtk.Button(
                icon_name="folder-download-symbolic",
                valign=Gtk.Align.CENTER,
                has_frame=False,
                tooltip_text=(
                    "Install runner locally" if kind == "runner" else "Import for editing"
                ),
            )
            dl_btn.connect("clicked", self._on_download_clicked, row)
            row.add_suffix(dl_btn)

            if kind == "base":
                is_depended = (repo.uri, item.name) in depended_bases
                can_delete = repo.is_writable and not is_depended
                del_tooltip = (
                    "Apps in this repo depend on this base" if is_depended
                    else "Read-only repo" if not repo.is_writable
                    else "Remove from repo"
                )
            elif kind == "runner":
                is_depended = (repo.uri, item.name) in depended_runners
                can_delete = repo.is_writable and not is_depended
                del_tooltip = (
                    "Base images in this repo depend on this runner" if is_depended
                    else "Read-only repo" if not repo.is_writable
                    else "Remove from repo"
                )
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
            del_btn.connect("clicked", self._on_delete_clicked, row)
            row.add_suffix(del_btn)

            groups[kind].add(row)
            self._row_widgets.append(row)
            self._del_buttons.append(del_btn)
            counts[kind] += 1

        self._update_empty_state()

        self._stack.set_visible_child_name("list")

    def _update_empty_state(self) -> None:
        """Show/hide groups and placeholder based on remaining entries."""
        counts = {"app": 0, "base": 0, "runner": 0}
        for _, _, kind in self._entries:
            counts[kind] += 1
        has_any = bool(self._entries)

        if not has_any:
            if not hasattr(self, "_empty_row"):
                self._empty_row = Adw.ActionRow(
                    title="No entries found",
                    subtitle="Add a repository with published apps.",
                )
            self._apps_group.add(self._empty_row)
            self._apps_group.set_visible(True)
        else:
            if hasattr(self, "_empty_row"):
                try:
                    self._apps_group.remove(self._empty_row)
                except Exception:
                    pass
            self._apps_group.set_visible(counts["app"] > 0)
        self._bases_group.set_visible(counts["base"] > 0)
        self._runners_group.set_visible(counts["runner"] > 0)

    def _refresh_delete_buttons(self) -> None:
        """Recalculate dependency sets and update delete button sensitivity."""
        depended_bases: set[tuple[str, str]] = set()
        for item, repo, kind in self._entries:
            if kind == "app" and item.base_image:
                depended_bases.add((repo.uri, item.base_image))
        depended_runners: set[tuple[str, str]] = set()
        for item, repo, kind in self._entries:
            if kind == "base":
                depended_runners.add((repo.uri, item.runner))

        for i, (item, repo, kind) in enumerate(self._entries):
            del_btn = self._del_buttons[i]
            if not repo.is_writable:
                continue
            if kind == "base":
                is_depended = (repo.uri, item.name) in depended_bases
                can_delete = not is_depended
                tooltip = (
                    "Apps in this repo depend on this base" if is_depended else "Remove from repo"
                )
            elif kind == "runner":
                is_depended = (repo.uri, item.name) in depended_runners
                can_delete = not is_depended
                tooltip = (
                    "Base images in this repo depend on this runner"
                    if is_depended else "Remove from repo"
                )
            else:
                continue  # apps are always deletable if writable
            del_btn.set_sensitive(can_delete)
            del_btn.set_tooltip_text(tooltip)
            if can_delete:
                del_btn.add_css_class("destructive-action")
            else:
                del_btn.remove_css_class("destructive-action")

    # ------------------------------------------------------------------
    # Download / import for editing
    # ------------------------------------------------------------------

    def _on_download_clicked(self, _btn, row: Adw.ActionRow) -> None:
        try:
            idx = self._row_widgets.index(row)
        except ValueError:
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

        cancel = threading.Event()
        progress = ProgressDialog(label=f"Downloading {base_entry.name}\u2026",
                                  cancel_event=cancel)
        progress.present(root)

        def _work():
            import tempfile

            from cellar.backend.installer import (  # noqa: PLC2701
                InstallCancelled,
                _build_source,
                _find_top_dir,
                _install_chunks,
                _stream_and_extract,
            )
            from cellar.backend.packager import slugify

            archive_uri = repo.resolve_asset_uri(base_entry.archive)

            from cellar.backend.config import install_data_dir
            try:
                with tempfile.TemporaryDirectory(prefix="cellar-base-import-",
                                                 dir=install_data_dir()) as tmp_str:
                    tmp = Path(tmp_str)
                    extract_dir = tmp / "extracted"
                    extract_dir.mkdir()

                    if base_entry.archive_chunks:
                        _install_chunks(
                            archive_uri, base_entry.archive_chunks, extract_dir,
                            strip_top_dir=True,
                            cancel_event=cancel,
                            progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                            stats_cb=lambda d, t, s: GLib.idle_add(
                                progress.set_stats, fmt_stats(d, t, s)),
                            token=repo.token,
                            ssl_verify=repo.ssl_verify,
                            ca_cert=repo.ca_cert,
                        )
                    else:
                        chunks, total = _build_source(
                            archive_uri,
                            expected_size=base_entry.archive_size,
                            token=repo.token,
                            ssl_verify=repo.ssl_verify,
                            ca_cert=repo.ca_cert,
                        )
                        _stream_and_extract(
                            chunks, total,
                            is_zst=archive_uri.endswith(".tar.zst"),
                            dest=extract_dir,
                            expected_crc32=base_entry.archive_crc32,
                            cancel_event=cancel,
                            progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                            stats_cb=lambda d, t, s: GLib.idle_add(
                                progress.set_stats, fmt_stats(d, t, s)),
                            name_cb=None,
                        )

                    if base_entry.archive_chunks:
                        content_src = extract_dir
                    else:
                        content_src = _find_top_dir(extract_dir)

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

                    GLib.idle_add(progress.set_label, "Copying content\u2026")
                    GLib.idle_add(progress.set_fraction, 0.0)
                    project.content_path.mkdir(parents=True, exist_ok=True)
                    for src in content_src.rglob("*"):
                        rel = src.relative_to(content_src)
                        dst = project.content_path / rel
                        if src.is_dir():
                            dst.mkdir(parents=True, exist_ok=True)
                        elif src.is_file():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dst)

                    save_project(project)
                    return project
            except InstallCancelled:
                return None

        def _done(project) -> None:
            progress.force_close()
            if project is None:
                return
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

        cancel = threading.Event()
        progress = ProgressDialog(label=f"Downloading {runner_item.name}\u2026",
                                  cancel_event=cancel)
        progress.present(root)

        def _work():
            import tempfile

            from cellar.backend.installer import (  # noqa: PLC2701
                InstallCancelled,
                _build_source,
                _install_chunks,
                _stream_and_extract,
            )
            from cellar.backend.umu import runners_dir

            archive_uri = repo.resolve_asset_uri(runner_item.archive)

            dest = runners_dir() / runner_item.name
            if dest.exists():
                shutil.rmtree(dest)

            from cellar.backend.config import install_data_dir
            try:
                with tempfile.TemporaryDirectory(prefix="cellar-runner-import-",
                                                 dir=install_data_dir()) as tmp_str:
                    tmp = Path(tmp_str)
                    extract_dir = tmp / "extracted"
                    extract_dir.mkdir()

                    if runner_item.archive_chunks:
                        _install_chunks(
                            archive_uri, runner_item.archive_chunks, extract_dir,
                            strip_top_dir=True,
                            cancel_event=cancel,
                            progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                            stats_cb=lambda d, t, s: GLib.idle_add(
                                progress.set_stats, fmt_stats(d, t, s)),
                            token=repo.token,
                            ssl_verify=repo.ssl_verify,
                            ca_cert=repo.ca_cert,
                        )
                    else:
                        chunks, total = _build_source(
                            archive_uri,
                            expected_size=runner_item.archive_size,
                            token=repo.token,
                            ssl_verify=repo.ssl_verify,
                            ca_cert=repo.ca_cert,
                        )
                        _stream_and_extract(
                            chunks, total,
                            is_zst=archive_uri.endswith(".tar.zst"),
                            dest=extract_dir,
                            expected_crc32=runner_item.archive_crc32,
                            cancel_event=cancel,
                            progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                            stats_cb=lambda d, t, s: GLib.idle_add(
                                progress.set_stats, fmt_stats(d, t, s)),
                            name_cb=None,
                        )

                    extracted = [p for p in extract_dir.iterdir() if p.is_dir()]
                    if not extracted:
                        raise RuntimeError("Runner archive contained no directory")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copytree(extracted[0], dest)
            except InstallCancelled:
                return None

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

        cancel = threading.Event()
        progress = ProgressDialog(label="Downloading\u2026", cancel_event=cancel)
        progress.present(root)

        def _work():
            nonlocal entry
            import tempfile

            from cellar.backend.installer import (
                InstallCancelled,  # noqa: PLC2701
                _build_source,  # noqa: PLC2701
                _find_top_dir,  # noqa: PLC2701
                _install_chunks,  # noqa: PLC2701
                _stream_and_extract,  # noqa: PLC2701
            )
            if entry.is_partial:
                GLib.idle_add(progress.set_label, "Fetching metadata\u2026")
                entry = repo.fetch_app_metadata(entry.id)
                GLib.idle_add(progress.set_label, "Downloading\u2026")
            archive_uri = repo.resolve_asset_uri(entry.archive)

            from cellar.backend.config import install_data_dir
            try:
                with tempfile.TemporaryDirectory(prefix="cellar-import-",
                                                 dir=install_data_dir()) as tmp_str:
                    tmp = Path(tmp_str)
                    extract_dir = tmp / "extracted"
                    extract_dir.mkdir()

                    if entry.archive_chunks:
                        _install_chunks(
                            archive_uri, entry.archive_chunks, extract_dir,
                            strip_top_dir=True,
                            cancel_event=cancel,
                            progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                            stats_cb=lambda d, t, s: GLib.idle_add(
                                progress.set_stats, fmt_stats(d, t, s)),
                            token=repo.token,
                            ssl_verify=repo.ssl_verify,
                            ca_cert=repo.ca_cert,
                        )
                    else:
                        chunks, total = _build_source(
                            archive_uri,
                            expected_size=entry.archive_size,
                            token=repo.token,
                            ssl_verify=repo.ssl_verify,
                            ca_cert=repo.ca_cert,
                        )
                        _stream_and_extract(
                            chunks, total,
                            is_zst=archive_uri.endswith(".tar.zst"),
                            dest=extract_dir,
                            expected_crc32=entry.archive_crc32,
                            cancel_event=cancel,
                            progress_cb=lambda f: GLib.idle_add(progress.set_fraction, f),
                            stats_cb=lambda d, t, s: GLib.idle_add(
                                progress.set_stats, fmt_stats(d, t, s)),
                            name_cb=None,
                        )

                    if entry.archive_chunks:
                        content_src = extract_dir
                    else:
                        content_src = _find_top_dir(extract_dir)

                    # Measure delta-only size from the extracted content (which IS
                    # the delta for delta-packaged apps).  This lets us display
                    # accurate install size on CoW filesystems when republishing.
                    from cellar.utils.paths import dir_size_bytes as _dir_size
                    _delta_size = _dir_size(content_src) if entry.base_image else 0

                    from cellar.backend.packager import slugify
                    slug = slugify(entry.id)
                    existing = {p.slug for p in load_projects()}
                    base_slug, i = slug, 2
                    while slug in existing:
                        slug = f"{base_slug}-{i}"
                        i += 1

                    project = Project(
                        name=entry.name,
                        slug=slug,
                        project_type={"windows": "app", "linux": "linux", "dos": "dos"}.get(entry.platform, "app"),
                        runner=entry.base_image,
                        entry_points=[dict(t) for t in entry.launch_targets],
                        steam_appid=entry.steam_appid,
                        initialized=True,
                        origin_app_id=entry.id,
                        # Catalogue metadata
                        version=entry.version or "1.0",
                        category=entry.category or "",
                        developer=entry.developer or "",
                        publisher=entry.publisher or "",
                        release_year=entry.release_year,
                        website=entry.website or "",
                        genres=list(entry.genres) if entry.genres else [],
                        summary=entry.summary or "",
                        description=entry.description or "",
                        hide_title=entry.hide_title,
                        delta_size=_delta_size,
                    )

                    GLib.idle_add(progress.set_label, "Copying content\u2026")
                    project.content_path.mkdir(parents=True, exist_ok=True)
                    for src in content_src.rglob("*"):
                        rel = src.relative_to(content_src)
                        dst = project.content_path / rel
                        if src.is_dir():
                            dst.mkdir(parents=True, exist_ok=True)
                        elif src.is_file():
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(src, dst)

                    # Download image assets from the repo into the project directory
                    GLib.idle_add(progress.set_label, "Downloading images\u2026")
                    project.project_dir.mkdir(parents=True, exist_ok=True)

                    for slot, rel_path in [
                        ("icon", entry.icon), ("cover", entry.cover), ("logo", entry.logo)
                    ]:
                        if not rel_path:
                            continue
                        try:
                            local = repo.resolve_asset_uri(rel_path)
                            if local and Path(local).is_file():
                                ext = Path(local).suffix or Path(rel_path).suffix
                                dest = project.project_dir / f"{slot}{ext}"
                                shutil.copy2(local, dest)
                                setattr(project, f"{slot}_path", str(dest))
                        except Exception:
                            log.warning("Could not download %s for import", slot)

                    # Download screenshots
                    screenshot_paths: list[str] = []
                    for idx, ss_rel in enumerate(entry.screenshots):
                        try:
                            local = repo.resolve_asset_uri(ss_rel)
                            if local and Path(local).is_file():
                                ext = Path(local).suffix or Path(ss_rel).suffix
                                dest = project.project_dir / f"screenshot_{idx}{ext}"
                                shutil.copy2(local, dest)
                                screenshot_paths.append(str(dest))
                        except Exception:
                            log.warning("Could not download screenshot %s", ss_rel)
                    project.screenshot_paths = screenshot_paths
                    # Map new local paths to original Steam source URLs for dedup
                    import_sources: dict[str, str] = {}
                    for idx, ss_rel in enumerate(entry.screenshots):
                        if ss_rel in entry.screenshot_sources and idx < len(screenshot_paths):
                            import_sources[screenshot_paths[idx]] = (
                                entry.screenshot_sources[ss_rel]
                            )
                    project.screenshot_sources = import_sources

                    # Linux imports: pre-fill source_dir with the extracted prefix
                    # so the project is immediately publishable. User can still
                    # pick a different folder via "Choose…" if needed.
                    if project.project_type == "linux":
                        project.source_dir = str(project.content_path)

                    save_project(project)
                    return project
            except InstallCancelled:
                return None

        def _done(project) -> None:
            progress.force_close()
            if project is None:
                return
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

    def _on_delete_clicked(self, _btn, row: Adw.ActionRow) -> None:
        try:
            idx = self._row_widgets.index(row)
        except ValueError:
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
        dialog.connect("response", self._on_delete_confirmed, row)
        dialog.present(self)

    def _on_delete_confirmed(self, _dialog, response: str, row: Adw.ActionRow) -> None:
        if response != "remove":
            return
        try:
            idx = self._row_widgets.index(row)
        except ValueError:
            return
        item, repo, kind = self._entries[idx]

        def _work():
            from cellar.backend import packager
            repo_root = repo.writable_path()
            if kind == "base":
                packager.remove_base(repo_root, item.name)
            elif kind == "runner":
                packager.remove_runner(repo_root, item.name)
            else:
                packager.remove_from_repo(repo_root, item)

        def _done(_result) -> None:
            _item, _repo, kind = self._entries.pop(idx)
            row = self._row_widgets.pop(idx)
            self._del_buttons.pop(idx)
            group = {
                "app": self._apps_group,
                "base": self._bases_group,
                "runner": self._runners_group,
            }[kind]
            group.remove(row)
            self._refresh_delete_buttons()
            self._update_empty_state()
            if self._on_catalogue_changed:
                self._on_catalogue_changed()

        def _error(msg: str) -> None:
            log.error("Delete failed: %s", msg)
            err = Adw.AlertDialog(heading="Delete failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

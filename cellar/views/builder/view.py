"""Package Builder view — create and publish WINEPREFIX-based app packages.

Shown in the main window when at least one writable repo is configured.
Maintainers use this view to:

1. Create a project (App or Base) that owns a WINEPREFIX.
2. Select a GE-Proton runner and initialize the prefix.
3. Install dependencies (winetricks verbs) and run installers.
4. Set an entry point (App projects only).
5. Test-launch the app to verify it works.
6. Publish — stream-compress the prefix directly to the repo and update
   catalogue.json (App and Base alike).  No intermediate local archive.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from dataclasses import replace as _dc_replace
from pathlib import Path
from typing import Callable

import gi

from cellar.utils import natural_sort_key
from cellar.utils.async_work import run_in_background

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from cellar.backend.project import (
    Project,
    create_project,
    delete_project,
    load_projects,
    save_project,
)
from cellar.views.builder.dependencies import DependencyPickerDialog
from cellar.views.builder.pickers import (
    AddLaunchTargetDialog,
    BasePickerDialog,
    RunnerPickerDialog,
    pick_repo,
)
from cellar.views.builder.progress import ProgressDialog
from cellar.views.metadata_editor import MetadataEditorDialog, ProjectContext, RepoContext
from cellar.views.widgets import set_margins

log = logging.getLogger(__name__)

class PackageBuilderView(Adw.Bin):
    """Package builder: project list → detail page via stack navigation."""

    def __init__(
        self,
        *,
        writable_repos: list | None = None,
        all_repos: list | None = None,
        on_catalogue_changed: Callable | None = None,
    ) -> None:
        super().__init__()
        self._writable_repos: list = writable_repos or []
        self._all_repos: list = all_repos or []
        self._on_catalogue_changed = on_catalogue_changed
        self._project: Project | None = None
        self._project_cards: list[_ProjectCard] = []
        self._replacing_detail = False
        self._search_text: str = ""
        self._active_types: set[str] = set()
        self._active_repos: set[str] = set()

        self._setup_actions()
        self._build()
        self._reload_projects()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update_repos(self, writable_repos: list, *, all_repos: list | None = None) -> None:
        self._writable_repos = writable_repos
        if all_repos is not None:
            self._all_repos = all_repos
        self._reload_projects()

    def set_search_text(self, text: str) -> None:
        self._search_text = text
        self._apply_filter()

    def set_active_types(self, types: set[str]) -> None:
        self._active_types = types
        self._apply_filter()

    def set_active_repos(self, repos: set[str]) -> None:
        self._active_repos = repos
        self._apply_filter()

    def _apply_filter(self) -> None:
        """Show/hide cards based on current search text and active filters."""
        child = self._flow_box.get_child_at_index(0)
        i = 0
        while child is not None:
            if isinstance(child, _NewProjectCard):
                child.set_visible(True)
            elif isinstance(child, (_ProjectCard, _CatalogueCard)):
                child.set_visible(
                    child.matches(self._search_text, self._active_types, self._active_repos)
                )
            i += 1
            child = self._flow_box.get_child_at_index(i)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    @staticmethod
    def _find_scrolled_window(widget: Gtk.Widget) -> Gtk.ScrolledWindow | None:
        """Walk the widget tree to find the first GtkScrolledWindow child."""
        if isinstance(widget, Gtk.ScrolledWindow):
            return widget
        child = widget.get_first_child()
        while child is not None:
            result = PackageBuilderView._find_scrolled_window(child)
            if result is not None:
                return result
            child = child.get_next_sibling()
        return None

    def _get_detail_scroll_position(self) -> float:
        """Get current scroll position of the detail page, or 0.0."""
        visible = self._nav_view.get_visible_page()
        if visible is not None and visible.get_tag() == "detail":
            sw = self._find_scrolled_window(visible)
            if sw is not None:
                return sw.get_vadjustment().get_value()
        return 0.0

    def _restore_scroll_position(self, pos: float) -> None:
        """Restore scroll position on the current detail page after it's laid out."""
        if pos <= 0.0:
            return
        visible = self._nav_view.get_visible_page()
        if visible is None:
            return
        sw = self._find_scrolled_window(visible)
        if sw is not None:
            def _apply(*_args):
                sw.get_vadjustment().set_value(pos)
                return False  # run once
            # Defer until after layout so upper bound is correct
            GLib.idle_add(_apply)

    def _setup_actions(self) -> None:
        """Register Gio actions for the builder view."""
        ag = Gio.SimpleActionGroup()

        delete_act = Gio.SimpleAction.new("delete", None)
        delete_act.connect(
            "activate",
            lambda *_: self._on_delete_clicked(self._project) if self._project else None,
        )
        ag.add_action(delete_act)

        self.insert_action_group("builder", ag)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build(self) -> None:
        # ── Navigation view (stack: list → detail) ───────────────────────
        self._nav_view = Adw.NavigationView()
        self._nav_view.connect("popped", self._on_nav_popped)

        # ── List page ────────────────────────────────────────────────────
        # Register CSS for the dashed new-project card
        css = Gtk.CssProvider()
        css.load_from_string(
            ".new-project-card {"
            "  border: 2px dashed alpha(@card_shade_color, 0.5);"
            "  border-radius: 12px;"
            "  background: none;"
            "}"
        )
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # Project card grid — matches browse view FlowBox settings
        self._flow_box = Gtk.FlowBox()
        self._flow_box.set_valign(Gtk.Align.START)
        self._flow_box.set_halign(Gtk.Align.CENTER)
        self._flow_box.set_homogeneous(False)
        self._flow_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._flow_box.set_min_children_per_line(2)
        self._flow_box.set_max_children_per_line(8)
        set_margins(self._flow_box, 18)
        self._flow_box.connect("child-activated", self._on_card_activated)

        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_vexpand(True)
        scroll.set_child(self._flow_box)

        self._list_page = Adw.NavigationPage(title="Package Builder", child=scroll)
        self._nav_view.add(self._list_page)

        self.set_child(self._nav_view)

    # ------------------------------------------------------------------
    # Project list management
    # ------------------------------------------------------------------

    def _reload_projects(self) -> None:
        """Reload project list from disk and refresh the card grid."""
        projects = load_projects()
        # Clear existing cards
        while True:
            child = self._flow_box.get_child_at_index(0)
            if child is None:
                break
            self._flow_box.remove(child)
        self._project_cards: list[_ProjectCard] = []

        # Always-visible "New Project" card at the start
        self._flow_box.append(_NewProjectCard())

        for p in sorted(projects, key=lambda x: natural_sort_key(x.name)):
            card = _ProjectCard(p)
            self._project_cards.append(card)
            self._flow_box.append(card)

        # Add catalogue entry cards from writable repos
        imported_ids = {p.origin_app_id for p in projects if p.origin_app_id}
        catalogue_entries, used_bases = self._fetch_writable_catalogue_entries(imported_ids)
        for entry, repo, kind in catalogue_entries:
            has_dependants = kind == "base" and entry.name in used_bases
            card = _CatalogueCard(
                entry, repo, kind,
                on_download=self._on_catalogue_download,
                on_delete=self._on_catalogue_delete,
                on_edit=self._on_catalogue_edit if kind == "app" else None,
                has_dependants=has_dependants,
                show_repo=len(self._writable_repos) > 1,
            )
            self._flow_box.append(card)

        if not projects:
            self._project = None

    def _fetch_writable_catalogue_entries(
        self, imported_ids: set[str],
    ) -> tuple[list[tuple], set[str]]:
        """Return catalogue entries from writable repos not already imported.

        Also returns the set of base names referenced by at least one app
        (via ``base_image`` in the slim index), so callers can prevent
        deletion of bases that still have dependants.
        """
        results: list[tuple] = []
        used_bases: set[str] = set()
        for repo in self._writable_repos:
            try:
                for entry in repo.fetch_catalogue():
                    if entry.id not in imported_ids:
                        results.append((entry, repo, "app"))
                    if entry.base_image:
                        used_bases.add(entry.base_image)
            except Exception as exc:
                log.warning("Could not fetch catalogue from %s: %s", repo.uri, exc)
            try:
                for name, base_entry in repo.fetch_bases().items():
                    if base_entry.name not in imported_ids:
                        results.append((base_entry, repo, "base"))
            except Exception as exc:
                log.warning("Could not fetch bases from %s: %s", repo.uri, exc)
        results.sort(key=lambda t: natural_sort_key(t[0].name))
        return results, used_bases

    def _on_card_activated(self, _fb, child: Gtk.FlowBoxChild) -> None:
        if isinstance(child, _ProjectCard):
            self._project = child.project
            self._show_project(self._project)
        elif isinstance(child, _NewProjectCard):
            self._on_new_project_clicked(None)

    def _on_nav_popped(self, _nav, _page) -> None:
        """User navigated back to the list page."""
        if not self._replacing_detail:
            self._project = None

    def _on_new_project_clicked(self, _btn) -> None:
        """Open the guided New Project chooser dialog."""
        dialog = _NewProjectDialog(
            on_windows=self._on_new_windows,
            on_linux=lambda: self._on_new_linux_clicked(None),
            on_dos=lambda: self._on_new_dos_clicked(None),
            on_base=lambda: self._on_new_base_clicked(None),
            on_import=self._on_project_created,
            parent_view=self,
        )
        dialog.present(self)

    def _on_new_windows(self) -> None:
        """Create a new Windows project — base image is selected in detail view."""
        dialog = MetadataEditorDialog(
            context=ProjectContext(), on_created=self._on_project_created,
        )
        dialog.present(self)

    def _on_new_linux_clicked(self, _btn) -> None:
        dialog = MetadataEditorDialog(
            context=ProjectContext(project_type="linux"), on_created=self._on_project_created,
        )
        dialog.present(self)

    def _on_new_dos_clicked(self, _btn) -> None:
        dialog = MetadataEditorDialog(
            context=ProjectContext(project_type="dos"), on_created=self._on_project_created,
        )
        dialog.present(self)

    def _on_new_base_clicked(self, _btn) -> None:
        from cellar.backend import runners as _runners
        installed = _runners.installed_runners()
        runner = installed[0] if installed else ""
        project = create_project("", "base", runner=runner)
        self._on_project_created(project)

    def _on_project_imported(self, project: Project) -> None:
        self._reload_projects()
        self._project = project
        self._show_project(project)

    def _on_delete_clicked(self, project: Project) -> None:
        name = project.name
        slug = project.slug

        dialog = Adw.AlertDialog(
            heading=f"Delete '{name}'?",
            body="The project directory (including the prefix) will be permanently deleted.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def _on_response(d, resp):
            if resp == "delete":
                delete_project(slug)
                self._project = None
                self._reload_projects()
                self._nav_view.pop_to_page(self._list_page)

        dialog.connect("response", _on_response)
        dialog.present(self)

    def _on_project_created(self, project: Project) -> None:
        self._reload_projects()
        self._project = project
        self._show_project(project)

    # ------------------------------------------------------------------
    # Catalogue card actions
    # ------------------------------------------------------------------

    def _on_catalogue_download(self, card: "_CatalogueCard") -> None:
        """Confirm and import a published catalogue entry for editing."""
        entry, repo, kind = card.entry, card.repo, card.kind
        dialog = Adw.AlertDialog(
            heading="Download for Editing?",
            body=f"\u201c{entry.name}\u201d will be downloaded from the repository for editing.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("download", "Download")
        dialog.set_response_appearance("download", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_download_confirmed, entry, repo, kind)
        dialog.present(self)

    def _on_download_confirmed(self, _d, response: str, entry, repo, kind: str) -> None:
        if response != "download":
            return
        if kind == "base":
            self._import_base_entry(entry, repo)
        else:
            self._import_app_entry(entry, repo)

    def _import_app_entry(self, entry, repo) -> None:
        """Download and import an app catalogue entry as a builder project."""
        cancel = threading.Event()
        progress = ProgressDialog(label="Downloading\u2026", cancel_event=cancel)
        progress.present(self)

        def _work():
            import tempfile

            from cellar.backend.installer import (
                InstallCancelled,
                _build_source,
                _find_top_dir,
                _install_chunks,
                _stream_and_extract,
            )
            from cellar.backend.packager import slugify
            from cellar.utils.progress import fmt_stats

            nonlocal entry
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
                        content_src = extract_dir  # strip_top_dir already applied
                    else:
                        content_src = _find_top_dir(extract_dir)

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

                    # Download image assets
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

                    if project.project_type in ("linux", "dos"):
                        project.source_dir = str(project.content_path)

                    save_project(project)
                    return project
            except InstallCancelled:
                return None

        def _done(project) -> None:
            progress.force_close()
            if project is None:
                return
            self._on_project_imported(project)

        def _error(msg: str) -> None:
            progress.force_close()
            log.error("Import failed: %s", msg)
            err = Adw.AlertDialog(heading="Import failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

    def _import_base_entry(self, base_entry, repo) -> None:
        """Download and import a base catalogue entry as a builder project."""
        cancel = threading.Event()
        progress = ProgressDialog(label=f"Downloading {base_entry.name}\u2026",
                                  cancel_event=cancel)
        progress.present(self)

        def _work():
            import tempfile

            from cellar.backend.installer import (
                InstallCancelled,
                _build_source,
                _find_top_dir,
                _install_chunks,
                _stream_and_extract,
            )
            from cellar.backend.packager import slugify
            from cellar.utils.progress import fmt_stats

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
            self._on_project_imported(project)

        def _error(msg: str) -> None:
            progress.force_close()
            log.error("Base import failed: %s", msg)
            err = Adw.AlertDialog(heading="Import failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

    def _on_catalogue_delete(self, card: "_CatalogueCard") -> None:
        """Confirm and remove a catalogue entry from its repo."""
        entry, repo, kind = card.entry, card.repo, card.kind
        dialog = Adw.AlertDialog(
            heading="Remove from Repository?",
            body=f"\u201c{entry.name}\u201d will be permanently deleted from the repository.",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("remove", "Remove")
        dialog.set_response_appearance("remove", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", self._on_delete_confirmed, entry, repo, kind)
        dialog.present(self)

    def _on_delete_confirmed(self, _d, response: str, entry, repo, kind: str) -> None:
        if response != "remove":
            return

        def _work():
            from cellar.backend import packager
            repo_root = repo.writable_path()
            if kind == "base":
                packager.remove_base(repo_root, entry.name)
            else:
                packager.remove_from_repo(repo_root, entry)

        def _done(_result) -> None:
            self._reload_projects()
            if self._on_catalogue_changed:
                self._on_catalogue_changed()

        def _error(msg: str) -> None:
            log.error("Delete failed: %s", msg)
            err = Adw.AlertDialog(heading="Delete failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

    def _on_catalogue_edit(self, card: "_CatalogueCard") -> None:
        """Fetch full metadata for the selected card then open MetadataEditorDialog."""
        entry, repo = card.entry, card.repo

        def _fetch():
            return repo.fetch_app_metadata(entry.id)

        def _open(full_entry) -> None:
            def _on_done(_updated_entry) -> None:
                self._reload_projects()
                if self._on_catalogue_changed:
                    self._on_catalogue_changed()

            MetadataEditorDialog(
                context=RepoContext(entry=full_entry, repo=repo),
                on_done=_on_done,
            ).present(self)

        def _error(msg: str) -> None:
            log.error("Failed to load metadata for %s: %s", entry.id, msg)
            err = Adw.AlertDialog(heading="Could not load metadata", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_fetch, on_done=_open, on_error=_error)

    # ------------------------------------------------------------------
    # Detail panel
    # ------------------------------------------------------------------

    def _show_project(self, project: Project, *, expand_sel: bool = False) -> None:
        """Build a detail page for *project* and push it onto the nav stack."""
        _type_labels = {"app": "Windows App", "linux": "Linux App", "dos": "DOS App", "base": "Base Image"}

        # Build toolbar with header
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)

        self._content_title = Adw.WindowTitle(
            title=project.name,
            subtitle=_type_labels.get(project.project_type, ""),
        )
        header.set_title_widget(self._content_title)

        # Gear menu
        self._detail_gear_btn = Gtk.MenuButton(icon_name="view-more-symbolic")
        self._detail_gear_btn.set_tooltip_text("Options")
        self._detail_gear_btn.add_css_class("flat")
        self._refresh_detail_menu(project)
        header.pack_end(self._detail_gear_btn)

        toolbar.add_top_bar(header)

        page = Adw.PreferencesPage()
        toolbar.set_content(page)

        detail_page = Adw.NavigationPage(title=project.name, child=toolbar)
        detail_page.set_tag("detail")

        # ── 1. Metadata section (App / Linux projects — first, to set title/slug) ──
        if project.project_type in ("app", "linux"):
            meta_group = Adw.PreferencesGroup(title="Metadata")

            # Title — always visible
            self._meta_name_row = Adw.EntryRow(title="Title")
            self._meta_name_row.set_text(project.name)
            _steam_btn = Gtk.Button(icon_name="system-search-symbolic")
            _steam_btn.add_css_class("flat")
            _steam_btn.set_valign(Gtk.Align.CENTER)
            _steam_btn.set_tooltip_text("Look up on Steam")
            _steam_btn.connect("clicked", self._on_meta_steam_lookup)
            self._meta_name_row.add_suffix(_steam_btn)

            def _on_name_changed(row):
                if self._project:
                    self._project.name = row.get_text()
                    save_project(self._project)
                    self._content_title.set_title(self._project.name)
                    for r in self._project_cards:
                        if r.project.slug == self._project.slug:
                            r._name_label.set_text(self._project.name)
                            break

            self._meta_name_row.connect("changed", _on_name_changed)
            meta_group.add(self._meta_name_row)

            # App ID — always visible, read-only
            _slug_row = Adw.ActionRow(title="App ID", subtitle=project.slug)
            _slug_row.add_css_class("property")
            meta_group.add(_slug_row)

            # Category — visible inline so users don't miss it
            from cellar.backend.packager import BASE_CATEGORIES
            _cat_strings = Gtk.StringList.new(BASE_CATEGORIES)
            _cat_row = Adw.ComboRow(title="Category", model=_cat_strings)
            try:
                _cat_idx = BASE_CATEGORIES.index(project.category) if project.category else -1
            except ValueError:
                _cat_idx = -1
            if _cat_idx >= 0:
                _cat_row.set_selected(_cat_idx)
            else:
                _cat_row.set_selected(Gtk.INVALID_LIST_POSITION)

            def _on_category_selected(row, _pspec):
                idx = row.get_selected()
                if self._project and idx != Gtk.INVALID_LIST_POSITION:
                    self._project.category = BASE_CATEGORIES[idx]
                    save_project(self._project)

            _cat_row.connect("notify::selected", _on_category_selected)
            meta_group.add(_cat_row)

            # Details summary row — opens MetadataEditorDialog
            _details_row = Adw.ActionRow(title="Details")
            _details_summary = self._make_metadata_summary(project)
            if _details_summary:
                _details_row.set_subtitle(_details_summary)
            _details_btn = Gtk.Button(label="Edit\u2026", valign=Gtk.Align.CENTER)
            _details_btn.connect("clicked", self._on_edit_metadata_clicked)
            _details_row.add_suffix(_details_btn)
            _details_row.set_activatable_widget(_details_btn)
            meta_group.add(_details_row)

            page.add(meta_group)

        # ── 2. Runner / Base Image (Windows packages only) ────────────────
        if project.project_type not in ("linux", "dos"):
            sel_group = Adw.PreferencesGroup()
            if project.project_type == "base":
                sel_group_title = "Runner"
                sel_active_label = project.runner or "No runner selected"
            else:
                sel_group_title = "Base Image"
                sel_active_label = project.runner or "No base image selected"

            self._sel_active_row = Adw.ActionRow(title=sel_group_title)
            self._sel_active_row.set_subtitle(sel_active_label)

            if project.project_type == "base":
                # Flat layout: runner list + Download button, no expander
                self._sel_expander = sel_group
                dl_btn = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
                dl_btn.add_css_class("suggested-action")
                dl_btn.connect("clicked", self._on_download_runner_clicked)
                self._sel_active_row.add_suffix(dl_btn)
                sel_group.add(self._sel_active_row)
            else:
                # Flat layout: base list + Download button, no expander
                self._sel_expander = sel_group
                self._dl_base_btn = Gtk.Button(label="Download", valign=Gtk.Align.CENTER)
                self._dl_base_btn.connect("clicked", self._on_download_base_clicked)
                self._sel_active_row.add_suffix(self._dl_base_btn)
                sel_group.add(self._sel_active_row)

            page.add(sel_group)

            if project.project_type == "base":
                self._populate_runner_expander(project)
            else:
                self._populate_base_expander(project)

        # ── 2b. Base Name (base projects only) ───────────────────────────
        if project.project_type == "base":
            name_group = Adw.PreferencesGroup(title="Base Name")
            self._base_name_row = Adw.EntryRow(title="Name")
            self._base_name_row.set_text(project.name)

            def _on_base_name_changed(row):
                if self._project:
                    self._project.name = row.get_text()
                    save_project(self._project)
                    self._content_title.set_title(self._project.name)
                    for r in self._project_cards:
                        if r.project.slug == self._project.slug:
                            r._name_label.set_text(self._project.name)
                            break

            self._base_name_row.connect("changed", _on_base_name_changed)
            name_group.add(self._base_name_row)
            page.add(name_group)

        # ── 3. Prefix (Windows / Base only) ───────────────────────────────
        if project.project_type not in ("linux", "dos"):
            prefix_group = Adw.PreferencesGroup(title="Prefix")
            prefix_exists = project.content_path.is_dir()
            status_text = "Initialized" if (prefix_exists and project.initialized) else (
                "Directory exists (not initialized)" if prefix_exists else "Not initialized"
            )
            self._prefix_status_row = Adw.ActionRow(title="Status", subtitle=status_text)
            self._init_btn = Gtk.Button(label="Initialize")
            self._init_btn.set_valign(Gtk.Align.CENTER)
            self._init_btn.add_css_class("suggested-action")
            self._init_btn.set_sensitive(bool(project.runner) and not project.initialized)
            self._init_btn.connect("clicked", self._on_init_prefix_clicked)
            self._prefix_status_row.add_suffix(self._init_btn)
            prefix_group.add(self._prefix_status_row)
            page.add(prefix_group)

        # ── 4. Files section (Windows app only) ───────────────────────────
        if project.project_type == "app":
            files_group = Adw.PreferencesGroup(title="Files")

            # Import Data row — shown when a Windows folder was dropped via smart import
            if project.source_dir and not project.installer_path:
                _import_row = Adw.ActionRow(
                    title="Import Folder",
                    subtitle=Path(project.source_dir).name,
                )
                _import_btn = Gtk.Button(label="Copy to Prefix")
                _import_btn.set_valign(Gtk.Align.CENTER)
                _import_btn.add_css_class("suggested-action")
                _import_btn.connect("clicked", self._on_import_folder_to_prefix)
                _import_row.add_suffix(_import_btn)
                _import_row.set_sensitive(project.initialized)
                files_group.add(_import_row)
            else:
                # Run Installer — only shown when no source_dir (not a folder import)
                run_installer_row = Adw.ActionRow(
                    title="Run Installer",
                )
                if project.installer_path:
                    run_installer_row.set_subtitle(Path(project.installer_path).name)
                    run_btn = Gtk.Button(label="Launch")
                    run_btn.set_valign(Gtk.Align.CENTER)
                    run_btn.add_css_class("suggested-action")
                    run_btn.connect("clicked", self._on_launch_prefilled_installer)
                    run_installer_row.add_suffix(run_btn)
                else:
                    run_installer_row.set_subtitle("Run an installer inside the prefix")
                    run_btn = Gtk.Button(label="Choose\u2026")
                    run_btn.set_valign(Gtk.Align.CENTER)
                    run_btn.connect("clicked", self._on_run_installer_clicked)
                    run_installer_row.add_suffix(run_btn)
                run_installer_row.set_sensitive(project.initialized)
                files_group.add(run_installer_row)

            _browse_row = Adw.ActionRow(
                title="Browse Prefix",
                subtitle="Open drive_c in the file manager",
            )
            _browse_row.set_sensitive(project.initialized)
            _browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
            _browse_btn.set_valign(Gtk.Align.CENTER)
            _browse_btn.add_css_class("flat")
            _browse_btn.connect("clicked", self._on_browse_prefix_clicked)
            _browse_row.add_suffix(_browse_btn)
            files_group.add(_browse_row)

            _winecfg_row = Adw.ActionRow(
                title="Wine Configuration",
                subtitle="Open winecfg (DLL overrides, Windows version, …)",
            )
            _winecfg_row.set_sensitive(project.initialized)
            _winecfg_btn = Gtk.Button(label="Open")
            _winecfg_btn.set_valign(Gtk.Align.CENTER)
            _winecfg_btn.connect("clicked", self._on_winecfg_clicked)
            _winecfg_row.add_suffix(_winecfg_btn)
            files_group.add(_winecfg_row)

            page.add(files_group)

            # Launch Targets (Windows app)
            targets_group = Adw.PreferencesGroup(title="Launch Targets")
            for _ep in project.entry_points:
                _ep_subtitle = _ep.get("path", "")
                if _ep.get("args"):
                    _ep_subtitle += "  " + _ep["args"]
                _ep_row = Adw.EntryRow(title=_ep_subtitle)
                _ep_row.set_text(_ep.get("name", ""))
                _ep_row.connect("changed", self._on_entry_point_name_changed, _ep)
                _rm_btn = Gtk.Button(icon_name="user-trash-symbolic")
                _rm_btn.set_valign(Gtk.Align.CENTER)
                _rm_btn.add_css_class("flat")
                _rm_btn.connect("clicked", self._on_remove_entry_point_clicked, _ep)
                _ep_row.add_suffix(_rm_btn)
                targets_group.add(_ep_row)

            _add_ep_row = Adw.ActionRow(title="Add Launch Target\u2026")
            _add_ep_row.set_sensitive(project.initialized)
            _add_ep_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
            _add_ep_btn.add_css_class("suggested-action")
            _add_ep_btn.connect("clicked", self._on_add_entry_point_clicked)
            _add_ep_row.add_suffix(_add_ep_btn)
            _add_ep_row.set_activatable_widget(_add_ep_btn)
            targets_group.add(_add_ep_row)

            page.add(targets_group)

        # ── 5b. Source Folder + Launch Targets (Linux / DOS) ──────────────
        elif project.project_type in ("linux", "dos"):
            _linux_ready = bool(project.source_dir) and Path(project.source_dir).is_dir()
            src_group = Adw.PreferencesGroup(title="Source Folder")

            src_row = Adw.ActionRow(title="Folder")
            src_row.set_subtitle(project.source_dir or "Not set")
            src_row.set_subtitle_selectable(True)

            if _linux_ready:
                _open_btn = Gtk.Button(icon_name="folder-open-symbolic")
                _open_btn.set_valign(Gtk.Align.CENTER)
                _open_btn.add_css_class("flat")
                _open_btn.connect("clicked", self._on_browse_prefix_clicked)
                src_row.add_suffix(_open_btn)

            _choose_btn = Gtk.Button(label="Choose\u2026")
            _choose_btn.set_valign(Gtk.Align.CENTER)
            _choose_btn.connect("clicked", self._on_choose_source_dir_clicked)
            src_row.add_suffix(_choose_btn)

            src_group.add(src_row)
            page.add(src_group)

            # Launch Targets (Linux)
            targets_group = Adw.PreferencesGroup(title="Launch Targets")
            for _ep in project.entry_points:
                _ep_subtitle = _ep.get("path", "")
                if _ep.get("args"):
                    _ep_subtitle += "  " + _ep["args"]
                _ep_row = Adw.EntryRow(title=_ep_subtitle)
                _ep_row.set_text(_ep.get("name", ""))
                _ep_row.connect("changed", self._on_entry_point_name_changed, _ep)
                _rm_btn = Gtk.Button(icon_name="user-trash-symbolic")
                _rm_btn.set_valign(Gtk.Align.CENTER)
                _rm_btn.add_css_class("flat")
                _rm_btn.connect("clicked", self._on_remove_entry_point_clicked, _ep)
                _ep_row.add_suffix(_rm_btn)
                targets_group.add(_ep_row)

            _add_ep_row = Adw.ActionRow(title="Add Launch Target\u2026")
            _add_ep_row.set_sensitive(_linux_ready)
            _add_ep_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
            _add_ep_btn.add_css_class("suggested-action")
            _add_ep_btn.connect("clicked", self._on_add_entry_point_clicked)
            _add_ep_row.add_suffix(_add_ep_btn)
            _add_ep_row.set_activatable_widget(_add_ep_btn)
            targets_group.add(_add_ep_row)

            page.add(targets_group)

        # ── 5c. DOSBox Settings (DOS only) ───────────────────────────────
        if project.project_type == "dos" and project.source_dir:
            _src = Path(project.source_dir)
            dos_group = Adw.PreferencesGroup(title="DOSBox Staging")

            _settings_row = Adw.ActionRow(
                title="DOSBox Settings\u2026",
                subtitle="Display, CPU, sound, MIDI, mixer effects, and config files",
                activatable=True,
            )
            _settings_btn = Gtk.Button(label="Open\u2026", valign=Gtk.Align.CENTER)
            _settings_btn.connect("clicked", self._on_dosbox_settings_clicked)
            _settings_row.add_suffix(_settings_btn)
            _settings_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            _settings_row.set_activatable_widget(_settings_btn)
            dos_group.add(_settings_row)

            page.add(dos_group)

        # ── 6. Dependencies (Windows / Base only) ─────────────────────────
        if project.project_type not in ("linux", "dos"):
            dep_group = Adw.PreferencesGroup(title="Dependencies")
            for verb in project.deps_installed:
                row = Adw.ActionRow(title=verb)
                dep_group.add(row)

            add_dep_row = Adw.ActionRow(title="Add Dependencies\u2026")
            add_dep_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
            add_dep_btn.add_css_class("suggested-action")
            add_dep_btn.connect("clicked", self._on_add_dep_clicked)
            add_dep_row.add_suffix(add_dep_btn)
            add_dep_row.set_activatable_widget(add_dep_btn)
            add_dep_row.set_sensitive(project.initialized)
            dep_group.add(add_dep_row)
            page.add(dep_group)

        # ── 7. Publish section ────────────────────────────────────────────
        # Browse Prefix for base projects (app projects have it in the Files section)
        if project.project_type == "base":
            base_files_group = Adw.PreferencesGroup(title="Files")
            _browse_row = Adw.ActionRow(
                title="Browse Prefix",
                subtitle="Open drive_c in the file manager",
            )
            _browse_row.set_sensitive(project.initialized)
            _browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
            _browse_btn.set_valign(Gtk.Align.CENTER)
            _browse_btn.add_css_class("flat")
            _browse_btn.connect("clicked", self._on_browse_prefix_clicked)
            _browse_row.add_suffix(_browse_btn)
            base_files_group.add(_browse_row)

            _winecfg_row = Adw.ActionRow(
                title="Wine Configuration",
                subtitle="Open winecfg (DLL overrides, Windows version, …)",
            )
            _winecfg_row.set_sensitive(project.initialized)
            _winecfg_btn = Gtk.Button(label="Open")
            _winecfg_btn.set_valign(Gtk.Align.CENTER)
            _winecfg_btn.connect("clicked", self._on_winecfg_clicked)
            _winecfg_row.add_suffix(_winecfg_btn)
            base_files_group.add(_winecfg_row)

            page.add(base_files_group)

        pkg_group = Adw.PreferencesGroup(title="Publish")

        if project.project_type in ("app", "linux", "dos"):
            _ready = (
                bool(project.source_dir) and Path(project.source_dir).is_dir()
                if project.project_type in ("linux", "dos")
                else project.initialized
            )

            # Build a list of missing prerequisites for informative subtitles
            _missing: list[str] = []
            if not _ready:
                _missing.append("initialize prefix" if project.project_type not in ("linux", "dos")
                                else "set source folder")
            if not project.entry_points:
                _missing.append("add a launch target")
            if not project.category:
                _missing.append("set a category")

            # Test launch
            test_row = Adw.ActionRow(
                title="Test Launch",
                subtitle="Launch the app to verify it works",
            )
            test_btn = Gtk.Button(label="Launch")
            test_btn.set_valign(Gtk.Align.CENTER)
            test_btn.connect("clicked", self._on_test_launch_clicked)
            test_row.add_suffix(test_btn)
            pkg_group.add(test_row)

            if project.origin_app_id:
                origin_row = Adw.ActionRow(
                    title="Origin",
                    subtitle=f"Updating catalogue entry: {project.origin_app_id}",
                )
                origin_row.add_css_class("property")
                pkg_group.add(origin_row)

                _pub_subtitle = (
                    "Needs: " + ", ".join(_missing) if _missing
                    else "Re-archive and replace the catalogue entry"
                )
                pub_row = Adw.ActionRow(
                    title="Publish Update",
                    subtitle=_pub_subtitle,
                )
                pub_btn = Gtk.Button(label="Publish\u2026")
                pub_btn.set_valign(Gtk.Align.CENTER)
                pub_btn.add_css_class("suggested-action")
                pub_btn.connect("clicked", self._on_publish_app_clicked)
                pub_row.add_suffix(pub_btn)
                pkg_group.add(pub_row)
            else:
                _pub_subtitle = (
                    "Needs: " + ", ".join(_missing) if _missing
                    else "Archive and upload to repository"
                )
                publish_row = Adw.ActionRow(
                    title="Publish App",
                    subtitle=_pub_subtitle,
                )
                pub_btn = Gtk.Button(label="Publish\u2026")
                pub_btn.set_valign(Gtk.Align.CENTER)
                pub_btn.add_css_class("suggested-action")
                pub_btn.connect("clicked", self._on_publish_app_clicked)
                publish_row.add_suffix(pub_btn)
                pkg_group.add(publish_row)

        else:
            # Base: publish base
            publish_row = Adw.ActionRow(
                title="Publish Base",
                subtitle="Archive prefix and runner, and upload to repository",
            )
            publish_row.set_sensitive(project.initialized)
            pub_btn = Gtk.Button(label="Publish\u2026")
            pub_btn.set_valign(Gtk.Align.CENTER)
            pub_btn.add_css_class("suggested-action")
            pub_btn.connect("clicked", self._on_publish_base_clicked)
            publish_row.add_suffix(pub_btn)
            pkg_group.add(publish_row)

        page.add(pkg_group)

        # Pop any existing detail page, then push the new one.
        # Save scroll position so refreshes don't jump to top.
        saved_scroll = self._get_detail_scroll_position()
        # Guard against _on_nav_popped clearing self._project during the swap.
        self._replacing_detail = True
        self._nav_view.pop_to_page(self._list_page)
        self._replacing_detail = False
        self._nav_view.push(detail_page)
        self._restore_scroll_position(saved_scroll)

    def _refresh_detail_menu(self, project: Project) -> None:
        """Build/update the gear menu for the content header bar."""
        danger_section = Gio.Menu()
        danger_section.append("Delete Project\u2026", "builder.delete")
        menu = Gio.Menu()
        menu.append_section(None, danger_section)
        self._detail_gear_btn.set_menu_model(menu)

    # ------------------------------------------------------------------
    # Signal handlers — runners (base projects)
    # ------------------------------------------------------------------

    def _populate_runner_expander(self, project: Project) -> None:
        """Populate the Runner group with radio rows for installed runners."""
        from cellar.backend import runners as _runners

        # Runners referenced by at least one published base (in the base's
        # source repo) cannot be deleted.  We scope the check per-repo so
        # that publishing the same base to a second repo doesn't lock the
        # runner on the first repo indefinitely.
        from cellar.backend.database import get_all_installed_bases

        repo_by_uri = {repo.uri: repo for repo in self._all_repos}
        runners_in_use: set[str] = set()
        for rec in get_all_installed_bases():
            base_runner = rec["runner"]
            repo_source = rec.get("repo_source") or ""
            target_repo = repo_by_uri.get(repo_source)
            if target_repo is None:
                continue
            base_entry = target_repo._bases.get(base_runner)
            if base_entry and base_entry.runner:
                runners_in_use.add(base_entry.runner)

        first_check: Gtk.CheckButton | None = None
        for rname in _runners.installed_runners():
            row = Adw.ActionRow(title=rname)
            check = Gtk.CheckButton()
            check.set_valign(Gtk.Align.CENTER)
            if first_check is None:
                first_check = check
            else:
                check.set_group(first_check)
            check.set_active(rname == project.runner)
            check.connect("toggled", self._on_runner_radio_toggled, rname)
            row.add_prefix(check)
            row.set_activatable_widget(check)

            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.add_css_class("flat")
            in_use = rname in runners_in_use
            del_btn.set_sensitive(not in_use)
            del_btn.set_tooltip_text(
                "Runner is used by a published base image" if in_use else "Delete runner"
            )
            del_btn.connect("clicked", self._on_delete_runner_clicked, rname)
            row.add_suffix(del_btn)

            self._sel_expander.add(row)

    def _on_runner_radio_toggled(self, check: Gtk.CheckButton, runner_name: str) -> None:
        """Select a runner for the current base project."""
        if not check.get_active() or self._project is None:
            return
        # Pre-fill the base name with the runner name if it's empty or
        # still matches the previous runner (i.e. the user hasn't customised it).
        old_runner = self._project.runner
        self._project.runner = runner_name
        if (
            not self._project.name
            or self._project.name == old_runner
            or self._project.name == "(no runner)"
        ):
            self._project.name = runner_name
            if hasattr(self, "_base_name_row"):
                self._base_name_row.set_text(runner_name)
        for r in self._project_cards:
            if r.project is self._project:
                r.refresh_label()
                break
        save_project(self._project)
        if hasattr(self, "_init_btn"):
            self._init_btn.set_sensitive(
                bool(self._project.runner) and not self._project.initialized
            )
        if hasattr(self, "_sel_active_row"):
            self._sel_active_row.set_subtitle(runner_name)

    def _on_download_runner_clicked(self, _btn) -> None:
        """Open the runner picker to download a new GE-Proton release."""
        project = self._project
        dialog = RunnerPickerDialog(
            on_installed=lambda name: self._on_runner_installed(name, project),
        )
        dialog.present(self)

    def _on_runner_installed(self, runner_name: str, project: Project | None) -> None:
        """Called after a runner finishes installing — refresh the detail panel."""
        if project is not None and self._project is project:
            if not project.runner:
                project.runner = runner_name
                if not project.name or project.name == "(no runner)":
                    project.name = runner_name
                for r in self._project_cards:
                    if r.project is project:
                        r.refresh_label()
                        break
                save_project(project)
            self._show_project(project, expand_sel=True)

    def _on_delete_runner_clicked(self, _btn, runner_name: str) -> None:
        """Confirm and delete an installed runner."""
        projects = load_projects()
        using = [p.name for p in projects if p.runner == runner_name]

        body = f"Delete runner \u201c{runner_name}\u201d?"
        if using:
            names = ", ".join(using)
            body += f"\n\nUsed by: {names}"

        dialog = Adw.AlertDialog(heading="Delete Runner", body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda d, r: self._do_delete_runner(runner_name) if r == "delete" else None,
        )
        dialog.present(self)

    def _do_delete_runner(self, runner_name: str) -> None:
        from cellar.backend.runners import remove_runner
        remove_runner(runner_name)
        if self._project is not None:
            if self._project.runner == runner_name:
                self._project.runner = ""
                self._project.name = "(no runner)"
                for r in self._project_cards:
                    if r.project is self._project:
                        r.refresh_label()
                        break
                save_project(self._project)
            self._show_project(self._project, expand_sel=True)

    # ------------------------------------------------------------------
    # Signal handlers — base images (app projects)
    # ------------------------------------------------------------------

    def _populate_base_expander(self, project: Project) -> None:
        """Populate the Base Image expander with radio rows for installed bases.

        Sources available bases from the repos' catalogues (not the DB) and
        shows only those that are also present on disk.
        """
        from cellar.backend.base_store import is_base_installed
        from cellar.backend.database import get_all_installed_bases

        # Build install-date index so we can sort by newest last
        all_base_recs = get_all_installed_bases()  # ordered by installed_at
        install_order = {rec["runner"]: i for i, rec in enumerate(all_base_recs)}

        seen: set[str] = set()
        base_images: list[str] = []
        for repo in self._all_repos:
            for name in repo._bases:
                if name not in seen and is_base_installed(name):
                    seen.add(name)
                    base_images.append(name)
        # Sort by install date (newest last); fall back to alpha for unknowns
        base_images.sort(key=lambda n: (install_order.get(n, -1), n))

        # Toggle Download button accent: only highlight when no bases installed
        if hasattr(self, "_dl_base_btn"):
            if base_images:
                self._dl_base_btn.remove_css_class("suggested-action")
            else:
                self._dl_base_btn.add_css_class("suggested-action")

        # Base images referenced by at least one published app (in the base's
        # source repo) cannot be deleted.  Scoped per-repo so that mirroring a
        # base to a second repo doesn't prevent cleanup on the first.
        repo_by_uri = {repo.uri: repo for repo in self._all_repos}
        bases_in_use: set[str] = set()
        for rec in all_base_recs:
            base_runner = rec["runner"]
            repo_source = rec.get("repo_source") or ""
            target_repo = repo_by_uri.get(repo_source)
            if target_repo is None:
                continue
            for entry in target_repo.fetch_catalogue():
                if entry.base_image == base_runner:
                    bases_in_use.add(base_runner)
                    break

        # If no runner set yet, default to the newest installed base (last by install date)
        effective_runner = project.runner or (base_images[-1] if base_images else "")
        if effective_runner and not project.runner:
            project.runner = effective_runner
            save_project(project)
            if hasattr(self, "_sel_active_row"):
                self._sel_active_row.set_subtitle(effective_runner)

        first_check: Gtk.CheckButton | None = None
        for runner in base_images:
            row = Adw.ActionRow(title=runner)
            check = Gtk.CheckButton()
            check.set_valign(Gtk.Align.CENTER)
            if first_check is None:
                first_check = check
            else:
                check.set_group(first_check)
            check.set_active(runner == effective_runner)
            check.connect("toggled", self._on_base_radio_toggled, runner)
            row.add_prefix(check)
            row.set_activatable_widget(check)

            del_btn = Gtk.Button(icon_name="user-trash-symbolic")
            del_btn.set_valign(Gtk.Align.CENTER)
            del_btn.add_css_class("flat")
            in_use = runner in bases_in_use
            del_btn.set_sensitive(not in_use)
            del_btn.set_tooltip_text(
                "Base image is used by a published app" if in_use else "Delete base image"
            )
            del_btn.connect("clicked", self._on_delete_base_clicked, runner)
            row.add_suffix(del_btn)

            self._sel_expander.add(row)

    def _on_base_radio_toggled(self, check: Gtk.CheckButton, runner: str) -> None:
        """Select a base image for the current app project."""
        if not check.get_active() or self._project is None:
            return
        self._project.runner = runner
        save_project(self._project)
        if hasattr(self, "_init_btn"):
            self._init_btn.set_sensitive(
                bool(self._project.runner) and not self._project.initialized
            )
        if hasattr(self, "_sel_active_row"):
            self._sel_active_row.set_subtitle(runner)

    def _on_download_base_clicked(self, _btn) -> None:
        """Open the base picker to download a base image from a repo."""
        project = self._project
        dialog = BasePickerDialog(
            repos=self._all_repos,
            on_installed=lambda runner: self._on_base_installed(runner, project),
        )
        dialog.present(self)

    def _on_base_installed(self, runner: str, project: Project | None) -> None:
        """Called after a base finishes installing — refresh the detail panel."""
        if project is not None and self._project is project:
            if not project.runner:
                project.runner = runner
                save_project(project)
            self._show_project(project, expand_sel=True)

    def _on_delete_base_clicked(self, _btn, runner: str) -> None:
        """Confirm and delete an installed base image."""
        dialog = Adw.AlertDialog(
            heading="Delete Base Image",
            body=f"Delete base image \u201c{runner}\u201d from local storage?",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect(
            "response",
            lambda d, r: self._do_delete_base(runner) if r == "delete" else None,
        )
        dialog.present(self)

    def _do_delete_base(self, runner: str) -> None:
        from cellar.backend.base_store import remove_base
        remove_base(runner)
        if self._project is not None:
            if self._project.runner == runner:
                self._project.runner = ""
                save_project(self._project)
            self._show_project(self._project, expand_sel=True)

    def _resolve_runner_name(self, project: "Project") -> str:
        """Return the GE-Proton runner name to pass to umu for *project*.

        For base projects, ``project.runner`` is already the runner name.
        For app projects, ``project.runner`` is a base image name; look up
        the corresponding base entry to get the underlying runner name.
        """
        if project.project_type != "app":
            return project.runner
        base_name = project.runner
        for repo in self._all_repos:
            entry = repo._bases.get(base_name)
            if entry is not None:
                return entry.runner
        # Fallback — hope the base name is also a valid runner directory.
        return base_name

    def _on_init_prefix_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if project.project_type in ("linux", "dos"):
            project.content_path.mkdir(parents=True, exist_ok=True)
            self._on_init_done(project, True)
            return
        if not project.runner:
            return
        project.content_path.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label="Initializing prefix…")
        progress.present(self)

        runner_name = self._resolve_runner_name(project)

        # App projects seed from the installed base image (CoW copy)
        # instead of running init_prefix + setup_prefix — those components
        # are already in the base.  Base projects do the full init + setup.
        from cellar.backend.base_store import base_path, is_base_installed
        base_dir = (
            base_path(project.runner)
            if project.project_type == "app" and is_base_installed(project.runner)
            else None
        )

        # Look up the base builder project for its deps_installed list.
        base_project = None
        if base_dir:
            for p in load_projects():
                if p.project_type == "base" and p.name == project.runner:
                    base_project = p
                    break

        def _work():
            if base_dir and base_dir.is_dir():
                GLib.idle_add(progress.set_label, "Copying base prefix…")
                from cellar.backend.installer import _seed_from_base
                _seed_from_base(base_dir, project.content_path)
                return True

            from cellar.backend.umu import init_prefix, setup_prefix
            result = init_prefix(
                project.content_path,
                runner_name,
                steam_appid=project.steam_appid,
            )
            # umu-run "" initializes the prefix then tries to execute an
            # empty string, which Wine rejects with exit code 1.  Use the
            # presence of drive_c as the real success indicator.
            ok = result.returncode == 0 or (project.content_path / "drive_c").is_dir()
            if not ok:
                return False
            def _step(label, current, total):
                GLib.idle_add(progress.set_label, f"{label} ({current}/{total})")
            setup_prefix(
                project.content_path,
                runner_name,
                steam_appid=project.steam_appid,
                step_cb=_step,
            )
            return True

        def _finish(ok: bool) -> None:
            progress.force_close()
            if ok:
                if base_project:
                    # Carry base deps into the app project so the dependency
                    # picker shows them as already installed.
                    for verb in base_project.deps_installed:
                        if verb not in project.deps_installed:
                            project.deps_installed.append(verb)
                elif base_dir:
                    for verb in ("corefonts", "msls31", "d3dx9"):
                        if verb not in project.deps_installed:
                            project.deps_installed.append(verb)
                else:
                    for verb in ("corefonts", "msls31", "d3dx9"):
                        if verb not in project.deps_installed:
                            project.deps_installed.append(verb)
            self._on_init_done(project, ok)
            if not ok:
                self._show_toast("Prefix initialization failed. Check logs.")

        def _on_err(msg: str) -> None:
            log.error("init_prefix failed: %s", msg)
            _finish(False)

        run_in_background(_work, on_done=_finish, on_error=_on_err)

    def _on_init_done(self, project: Project, ok: bool) -> None:
        if ok:
            project.initialized = True
            save_project(project)
            self._show_project(project)
            # Auto-trigger folder copy if a source_dir is pending import
            if (
                project.project_type == "app"
                and project.source_dir
                and not project.installer_path
                and Path(project.source_dir).is_dir()
            ):
                self._on_import_folder_to_prefix(None)

    # ------------------------------------------------------------------
    # Signal handlers — metadata
    # ------------------------------------------------------------------

    def _on_meta_steam_lookup(self, _btn) -> None:
        if self._project is None:
            return
        from cellar.views.steam_picker import SteamPickerDialog
        query = self._project.name
        if hasattr(self, "_meta_name_row"):
            query = self._meta_name_row.get_text().strip() or query
        picker = SteamPickerDialog(query=query, on_picked=self._apply_steam_to_meta)
        picker.present(self.get_root())

    def _apply_steam_to_meta(self, result: dict) -> None:
        if self._project is None:
            return
        p = self._project
        if result.get("name") and hasattr(self, "_meta_name_row"):
            self._meta_name_row.set_text(result["name"])
        if result.get("developer") and not p.developer:
            p.developer = result["developer"]
        if result.get("publisher") and not p.publisher:
            p.publisher = result["publisher"]
        if result.get("year") and not p.release_year:
            p.release_year = result["year"]
        if result.get("summary") and not p.summary:
            p.summary = result["summary"]
        if result.get("summary") and not p.description:
            p.description = result["summary"]
        if result.get("steam_appid") and p.steam_appid is None:
            p.steam_appid = result["steam_appid"]
        if result.get("website") and not p.website:
            p.website = result["website"]
        if result.get("genres") and not p.genres:
            p.genres = list(result["genres"])
        if result.get("category") and not p.category:
            from cellar.backend.packager import BASE_CATEGORIES as _BASE_CATS
            if result["category"] in _BASE_CATS:
                p.category = result["category"]
        save_project(p)
        self._show_project(p)

    def _on_edit_metadata_clicked(self, _btn) -> None:
        if self._project is None:
            return
        dialog = MetadataEditorDialog(
            context=ProjectContext(project=self._project),
            on_changed=lambda: self._show_project(self._project),
        )
        dialog.present(self)

    def _make_metadata_summary(self, project: Project) -> str:
        """One-line summary of filled optional metadata for the Details row subtitle."""
        parts: list[str] = []
        if project.category:
            parts.append(project.category)
        if project.developer:
            parts.append(project.developer)
        if project.release_year:
            parts.append(str(project.release_year))
        return "  ·  ".join(parts)

    # ------------------------------------------------------------------
    # Signal handlers — dependencies
    # ------------------------------------------------------------------

    def _on_add_dep_clicked(self, _btn) -> None:
        if self._project is None:
            return
        if not self._project.runner:
            what = "a base image" if self._project.project_type == "app" else "a runner"
            self._show_toast(f"Select {what} before adding dependencies.")
            return
        dialog = DependencyPickerDialog(
            project=self._project,
            on_dep_changed=lambda: self._show_project(self._project),
            runner_name=self._resolve_runner_name(self._project),
        )
        dialog.present(self)

    # ------------------------------------------------------------------
    # Signal handlers — files
    # ------------------------------------------------------------------

    def _scan_entry_points_after_install(
        self,
        project: Project,
        pre_install_exes: set[Path] | None = None,
    ) -> None:
        """Scan drive_c for exe files and auto-populate launch targets if empty.

        When *pre_install_exes* is provided (a snapshot taken before the
        installer ran), only newly created executables are used as
        candidates.  Falls back to the full list if the diff is empty.
        """
        if project.entry_points:
            return
        drive_c = project.content_path / "drive_c"
        if not drive_c.is_dir():
            return
        from cellar.backend.detect import scan_prefix_exes
        from cellar.utils.paths import to_win32_path
        all_exes = scan_prefix_exes(project.content_path)
        if not all_exes:
            return
        # Prefer only exes the installer created
        candidates = sorted(all_exes, key=lambda p: p.name.lower())
        if pre_install_exes is not None:
            new_exes = [c for c in candidates if c not in pre_install_exes]
            if new_exes:
                candidates = new_exes
        project.entry_points = [
            {
                "name": c.stem,
                "path": to_win32_path(str(c), str(drive_c)),
            }
            for c in candidates[:5]
        ]
        save_project(project)

    def _on_launch_prefilled_installer(self, _btn) -> None:
        """Launch the pre-filled installer from smart import."""
        if self._project is None or not self._project.installer_path:
            return
        if not self._project.runner:
            self._show_toast("Select a base image before running an installer.")
            return
        project = self._project
        exe_path = project.installer_path

        # Snapshot existing exes so we can detect what the installer adds
        from cellar.backend.detect import scan_prefix_exes
        pre_exes = scan_prefix_exes(project.content_path)

        def _on_installer_done(ok: bool) -> None:
            log.info("Installer exited ok=%s", ok)
            # Revert to normal "Choose…" button so user can run DLC/patches
            project.installer_path = ""

            # Check if the installed game is a DOSBox game — offer conversion
            if ok and self._check_dosbox_after_install(project):
                return  # conversion flow takes over

            self._scan_entry_points_after_install(project, pre_exes)
            save_project(project)
            if self._project is project:
                self._show_project(project)

        self._run_in_prefix_with_progress(
            project,
            exe=exe_path,
            label=f"Running {Path(exe_path).name}\u2026",
            on_done=_on_installer_done,
        )

    def _on_import_folder_to_prefix(self, _btn) -> None:
        """Copy a Windows folder into the prefix's drive_c (smart import)."""
        if self._project is None or not self._project.source_dir:
            return
        project = self._project
        src = Path(project.source_dir)
        if not src.is_dir():
            self._show_toast("Source folder no longer exists.")
            return

        dest = project.content_path / "drive_c" / src.name

        cancel = threading.Event()
        progress = ProgressDialog(
            label=f"Copying {src.name}\u2026", cancel_event=cancel,
        )
        progress.present(self)

        def _work():
            from cellar.utils.progress import fmt_stats
            import time

            dest.parent.mkdir(parents=True, exist_ok=True)

            # Try CoW copy first (near-instant on btrfs/XFS)
            try:
                result = subprocess.run(
                    ["cp", "--reflink=auto", "-a", str(src), str(dest)],
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    GLib.idle_add(progress.set_fraction, 1.0)
                    GLib.idle_add(progress.set_stats, "CoW copy complete")
                    return True
            except FileNotFoundError:
                pass  # cp not available (shouldn't happen on Linux)

            # Fallback: file-by-file copy with progress
            if dest.exists():
                shutil.rmtree(dest)

            total_bytes = 0
            for dirpath, _dirs, files in os.walk(src):
                for f in files:
                    total_bytes += os.path.getsize(os.path.join(dirpath, f))

            copied_bytes = 0
            t0 = time.monotonic()
            last_ui = t0

            for dirpath, dirs, files in os.walk(src):
                if cancel.is_set():
                    raise RuntimeError("Cancelled")
                rel = os.path.relpath(dirpath, src)
                dst_dir = dest / rel if rel != "." else dest
                dst_dir.mkdir(parents=True, exist_ok=True)
                shutil.copystat(dirpath, str(dst_dir))
                for fname in files:
                    if cancel.is_set():
                        raise RuntimeError("Cancelled")
                    s = os.path.join(dirpath, fname)
                    d = dst_dir / fname
                    shutil.copy2(s, str(d))
                    copied_bytes += os.path.getsize(s)
                    now = time.monotonic()
                    if now - last_ui >= 0.1:
                        last_ui = now
                        elapsed = now - t0
                        speed = copied_bytes / elapsed if elapsed > 0 else 0
                        frac = copied_bytes / total_bytes if total_bytes else 1.0
                        stats = fmt_stats(copied_bytes, total_bytes, speed)
                        GLib.idle_add(progress.set_fraction, frac)
                        GLib.idle_add(progress.set_stats, stats)
            return True

        def _done(_ok):
            progress.force_close()

            # Check if the imported folder is a DOSBox game — offer conversion
            if self._check_dosbox_after_install(project):
                return  # conversion flow takes over

            # Detect exe candidates for entry points
            from cellar.backend.detect import scan_prefix_exes
            from cellar.utils.paths import to_win32_path
            drive_c = project.content_path / "drive_c"
            all_exes = scan_prefix_exes(project.content_path)
            if all_exes and not project.entry_points:
                candidates = sorted(all_exes, key=lambda p: p.name.lower())
                project.entry_points = [
                    {
                        "name": c.stem,
                        "path": to_win32_path(str(c), str(drive_c)),
                    }
                    for c in candidates[:5]
                ]
            project.source_dir = ""  # clear — data is now in the prefix
            save_project(project)
            self._show_project(project)
            self._show_toast(f"Copied {src.name} into prefix.")

        def _err(msg):
            progress.force_close()
            if "Cancelled" not in str(msg):
                self._show_toast(f"Copy failed: {msg}")

        run_in_background(_work, on_done=_done, on_error=_err)

    def _on_run_installer_clicked(self, _btn) -> None:
        if self._project is None or not self._project.runner:
            if self._project:
                what = "a base image" if self._project.project_type == "app" else "a runner"
                self._show_toast(f"Select {what} before running an installer.")
            return
        project = self._project
        chooser = Gtk.FileChooserNative(
            title="Select Installer",
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Run",
        )
        win = self.get_root()
        if isinstance(win, Gtk.Window):
            chooser.set_transient_for(win)
        f = Gtk.FileFilter()
        f.set_name("Windows executables")
        for ext in ("exe", "msi", "bat", "cmd", "com", "lnk"):
            f.add_pattern(f"*.{ext}")
            f.add_pattern(f"*.{ext.upper()}")
        chooser.add_filter(f)
        all_filter = Gtk.FileFilter()
        all_filter.set_name("All files")
        all_filter.add_pattern("*")
        chooser.add_filter(all_filter)
        chooser.connect(
            "response",
            lambda c, r: self._on_installer_chosen(c, r, project),
        )
        chooser.show()
        # Keep a reference
        self._installer_chooser = chooser

    def _on_installer_chosen(
        self, chooser: Gtk.FileChooserNative, response: int, project: Project
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        exe_path = chooser.get_file().get_path()

        # Snapshot existing exes so we can detect what the installer adds
        from cellar.backend.detect import scan_prefix_exes
        pre_exes = scan_prefix_exes(project.content_path)

        def _on_manual_installer_done(ok: bool) -> None:
            log.info("Installer exited ok=%s", ok)
            self._scan_entry_points_after_install(project, pre_exes)
            if self._project is project:
                self._show_project(project)

        self._run_in_prefix_with_progress(
            project,
            exe=exe_path,
            label=f"Running {Path(exe_path).name}\u2026",
            on_done=_on_manual_installer_done,
        )

    def _on_choose_source_dir_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        chooser = Gtk.FileChooserNative(
            title="Select Installation Folder",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="Select",
        )
        win = self.get_root()
        if isinstance(win, Gtk.Window):
            chooser.set_transient_for(win)
        if project.source_dir and Path(project.source_dir).parent.is_dir():
            chooser.set_current_folder(
                Gio.File.new_for_path(str(Path(project.source_dir).parent))
            )
        chooser.connect("response", lambda c, r: self._on_source_dir_chosen(c, r, project))
        chooser.show()
        self._source_dir_chooser = chooser

    def _on_source_dir_chosen(
        self, chooser: Gtk.FileChooserNative, response: int, project: Project
    ) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        path = chooser.get_file().get_path()
        project.source_dir = path
        save_project(project)
        self._show_project(project)

    def _on_browse_prefix_clicked(self, _btn) -> None:
        if self._project is None:
            return
        if self._project.project_type in ("linux", "dos"):
            target = Path(self._project.source_dir) if self._project.source_dir else None
        else:
            target = self._project.content_path / "drive_c"
            if not target.is_dir():
                target = self._project.content_path
        if not target or not target.is_dir():
            self._show_toast("Directory not set yet.")
            return
        subprocess.Popen(["xdg-open", str(target)], start_new_session=True)

    def _on_winecfg_clicked(self, _btn) -> None:
        if self._project is None or not self._project.runner:
            if self._project:
                what = "a base image" if self._project.project_type == "app" else "a runner"
                self._show_toast(f"Select {what} first.")
            return
        from cellar.backend.umu import launch_app
        launch_app(
            app_id=f"project-{self._project.slug}",
            entry_point="winecfg",
            runner_name=self._resolve_runner_name(self._project),
            steam_appid=self._project.steam_appid,
            prefix_dir=self._project.content_path,
        )

    def _on_add_entry_point_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if project.project_type in ("linux", "dos"):
            if not project.source_dir:
                self._show_toast("Choose a source folder first.")
                return
            content_path = Path(project.source_dir)
            platform = project.project_type
        else:
            content_path = project.content_path
            platform = "windows"
        dialog = AddLaunchTargetDialog(
            content_path=content_path,
            platform=platform,
            on_added=lambda ep: self._on_entry_point_added(project, ep),
        )
        dialog.present(self)

    def _on_entry_point_added(self, project: Project, ep: dict) -> None:
        project.entry_points.append(ep)
        save_project(project)
        self._show_project(project)

    def _on_entry_point_name_changed(self, row: Adw.EntryRow, ep: dict) -> None:
        if self._project is None:
            return
        ep["name"] = row.get_text().strip()
        save_project(self._project)

    def _on_remove_entry_point_clicked(self, _btn, ep: dict) -> None:
        if self._project is None:
            return
        try:
            self._project.entry_points.remove(ep)
        except ValueError:
            return
        save_project(self._project)
        self._show_project(self._project)

    # ------------------------------------------------------------------
    # Signal handlers — package
    # ------------------------------------------------------------------

    def _on_test_launch_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if project.project_type not in ("linux", "dos") and not project.initialized:
            self._show_toast("Initialize the prefix before test launching.")
            return
        if not project.entry_points:
            self._show_toast("Add a launch target before test launching.")
            return
        if len(project.entry_points) == 1:
            self._do_test_launch(project, project.entry_points[0])
            return
        # Multiple targets — let the user pick
        dialog = Adw.AlertDialog(
            heading="Select Launch Target",
            body="Choose which target to test:",
        )
        dialog.add_response("cancel", "Cancel")
        dialog.set_response_appearance("cancel", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_close_response("cancel")
        for i, ep in enumerate(project.entry_points):
            dialog.add_response(str(i), ep.get("name", ep.get("path", "")))
        dialog.connect("response", self._on_launch_target_chosen, project)
        dialog.present(self)

    def _on_launch_target_chosen(self, _dialog, response: str, project) -> None:
        if response == "cancel":
            return
        try:
            idx = int(response)
        except ValueError:
            return
        if 0 <= idx < len(project.entry_points):
            self._do_test_launch(project, project.entry_points[idx])

    def _do_test_launch(self, project, ep: dict) -> None:
        entry_path = ep.get("path", "")
        entry_args = ep.get("args", "")
        if not entry_path:
            self._show_toast("Launch target has no executable path.")
            return
        if project.project_type in ("linux", "dos"):
            if not project.source_dir:
                self._show_toast("Set a source folder first.")
                return
            exe = Path(project.source_dir) / entry_path
            if not exe.exists():
                self._show_toast(f"Executable not found: {exe}")
                return
            import shlex

            from cellar.backend.umu import is_cellar_sandboxed
            cmd = [str(exe)]
            if entry_args:
                cmd += shlex.split(entry_args)
            if is_cellar_sandboxed():
                cmd = ["flatpak-spawn", "--host"] + cmd
            subprocess.Popen(cmd, cwd=str(exe.parent), start_new_session=True)
            return
        if not project.runner:
            what = "a base image" if project.project_type == "app" else "a runner"
            self._show_toast(f"Select {what} before test launching.")
            return
        from cellar.backend.umu import dll_overrides, launch_app
        from cellar.backend.config import load_audio_driver  # noqa: PLC0415
        extra_env: dict[str, str] = {}
        overrides = dll_overrides(audio_driver=load_audio_driver())
        if overrides:
            extra_env["WINEDLLOVERRIDES"] = overrides
        launch_app(
            app_id=f"project-{project.slug}",
            entry_point=entry_path,
            runner_name=self._resolve_runner_name(project),
            steam_appid=project.steam_appid,
            prefix_dir=project.content_path,
            launch_args=entry_args,
            extra_env=extra_env or None,
        )

    def _on_publish_app_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if project.project_type not in ("linux", "dos") and not project.initialized:
            self._show_toast("Initialize the prefix before publishing.")
            return
        if not project.entry_point:
            self._show_toast("Add a launch target before publishing.")
            return
        if project.project_type in ("linux", "dos") and not project.source_dir:
            self._show_toast("Choose a source folder before publishing.")
            return
        if project.project_type not in ("linux", "dos") and not project.runner:
            what = "a base image" if project.project_type == "app" else "a runner"
            self._show_toast(f"Select {what} before publishing.")
            return
        if not project.category:
            self._show_toast("Set a category in Metadata before publishing.")
            return
        if not self._writable_repos:
            self._show_toast("No writable repository configured.")
            return

        if len(self._writable_repos) > 1:
            pick_repo(
                self._writable_repos,
                self,
                lambda repo: self._do_publish_app(project, repo),
            )
            return
        self._do_publish_app(project, self._writable_repos[0])

    def _do_publish_app(self, project: Project, repo) -> None:
        _src_path = (
            Path(project.source_dir) if project.project_type in ("linux", "dos") else project.content_path
        )

        # Build AppEntry from project metadata.
        from cellar.models.app_entry import AppEntry
        _slug = project.slug
        _raw_icon_ext = Path(project.icon_path).suffix.lower() if project.icon_path else ".png"
        _icon_ext = ".png" if _raw_icon_ext in (".ico", ".bmp") else _raw_icon_ext
        _cover_ext = Path(project.cover_path).suffix if project.cover_path else ".jpg"
        entry = AppEntry(
            id=_slug,
            name=project.name,
            version=project.version or "1.0",
            category=project.category,
            summary=project.summary,
            description=project.description,
            developer=project.developer,
            publisher=project.publisher,
            release_year=project.release_year,
            website=project.website,
            genres=tuple(project.genres),
            steam_appid=project.steam_appid,
            icon=f"apps/{_slug}/icon{_icon_ext}" if project.icon_path else "",
            cover=f"apps/{_slug}/cover{_cover_ext}" if project.cover_path else "",
            logo=f"apps/{_slug}/logo.png" if project.logo_path else "",
            hide_title=project.hide_title,
            screenshots=tuple(
                f"apps/{_slug}/screenshots/{i + 1:02d}{Path(p).suffix}"
                for i, p in enumerate(project.screenshot_paths)
            ),
            archive=f"apps/{_slug}/{_slug}.tar.zst",
            launch_targets=tuple(project.entry_points),
            update_strategy="safe",
            platform={"linux": "linux", "dos": "dos"}.get(project.project_type, "windows"),
        )
        images: dict = {}
        if project.icon_path:
            images["icon"] = project.icon_path
        if project.cover_path:
            images["cover"] = project.cover_path
        if project.logo_path:
            images["logo"] = project.logo_path
        if project.screenshot_paths:
            images["screenshots"] = list(project.screenshot_paths)

        cancel_event = threading.Event()
        progress = ProgressDialog(
            label="Compressing and uploading\u2026", cancel_event=cancel_event,
        )
        progress.present(self)

        import time
        from cellar.utils.progress import fmt_size

        _prev: list[tuple[float, int]] = []  # (time, bytes)

        def _bytes_cb(n: int) -> None:
            now = time.monotonic()
            _prev.append((now, n))
            # sliding 2-second window for smoothed speed
            cutoff = now - 2.0
            while _prev and _prev[0][0] < cutoff:
                _prev.pop(0)
            if len(_prev) >= 2:
                dt = _prev[-1][0] - _prev[0][0]
                db = _prev[-1][1] - _prev[0][1]
                speed = db / dt if dt > 0 else 0
                spd = f" ({fmt_size(int(speed))}/s)" if speed > 0 else ""
            else:
                spd = ""
            GLib.idle_add(progress.set_stats, fmt_size(n) + " written" + spd)

        def _reset_phase(label: str) -> None:
            _prev.clear()
            GLib.idle_add(progress.set_label, label)
            GLib.idle_add(progress.set_stats, "")
            GLib.idle_add(progress.start_pulse)

        all_repos = list(self._all_repos)

        def _work():
            from cellar.backend.packager import (
                CancelledError,
                compress_prefix_delta_zst,
                compress_prefix_zst,
                import_to_repo,
            )

            # Download any Steam screenshots the user selected in metadata
            nonlocal entry, images
            if project.selected_steam_urls:
                GLib.idle_add(progress.set_label, "Downloading screenshots\u2026")
                from cellar.utils.http import make_session as _make_session
                _session = _make_session()
                dl_dir = project.project_dir / "screenshots"
                dl_dir.mkdir(parents=True, exist_ok=True)
                _selected = set(project.selected_steam_urls)
                _downloaded: list[str] = []
                _steam_url_for_path: dict[str, str] = {}
                for i, ss in enumerate(project.steam_screenshots):
                    if ss.get("full") not in _selected:
                        continue
                    try:
                        _resp = _session.get(ss["full"], timeout=30)
                        if _resp.ok:
                            _suffix = ".jpg" if ss["full"].lower().endswith(".jpg") else ".png"
                            _dest = dl_dir / f"steam_{i:02d}{_suffix}"
                            _dest.write_bytes(_resp.content)
                            _downloaded.append(str(_dest))
                            _steam_url_for_path[str(_dest)] = ss["full"]
                    except Exception as _exc:  # noqa: BLE001
                        log.warning("Screenshot download failed: %s", _exc)
                if _downloaded:
                    _n_existing = len(project.screenshot_paths)
                    project.screenshot_paths = list(project.screenshot_paths) + _downloaded
                    _new_rels = tuple(
                        f"apps/{_slug}/screenshots/{j + 1:02d}{Path(p).suffix}"
                        for j, p in enumerate(project.screenshot_paths)
                    )
                    _ss_sources = {
                        _new_rels[_n_existing + k]: _steam_url_for_path[_downloaded[k]]
                        for k in range(len(_downloaded))
                    }
                    entry = _dc_replace(
                        entry, screenshots=_new_rels, screenshot_sources=_ss_sources,
                    )
                    images["screenshots"] = list(project.screenshot_paths)
                project.steam_screenshots = []
                project.selected_steam_urls = []
                from cellar.backend.project import save_project as _save_project
                _save_project(project)

            repo_root = repo.writable_path()
            archive_dest = repo_root / entry.archive
            archive_dest.parent.mkdir(parents=True, exist_ok=True)

            # Remove old archive chunks before writing new ones — they share
            # the same filename pattern, so cleanup after would delete new files.
            from cellar.backend.packager import (
                _cleanup_chunks,
                _cleanup_old_archive,
            )
            _cleanup_old_archive(repo_root, entry)

            # Track what we've written so we can clean up on cancel.
            _written_archive = False
            _runner_upserted: tuple[str, object] | None = None  # (name, dest)
            _base_upserted: tuple[str, object] | None = None    # (name, dest)
            _partial_dest = None  # dest currently being compressed
            _app_imported = False

            try:
                _reset_phase("Compressing and uploading\u2026")
                if project.project_type in ("linux", "dos"):
                    _partial_dest = archive_dest
                    size, crc32, chunks = compress_prefix_zst(
                        _src_path,
                        archive_dest,
                        cancel_event=cancel_event,
                        bytes_cb=_bytes_cb,
                    )
                    base_image = ""
                else:
                    from cellar.backend.base_store import base_path, is_base_installed
                    if not is_base_installed(project.runner):
                        raise RuntimeError(
                            f"Base image \u201c{project.runner}\u201d is not installed locally. "
                            "Install the base image before publishing."
                        )
                    _reset_phase("Scanning files\u2026")
                    _partial_dest = archive_dest
                    size, crc32, chunks = compress_prefix_delta_zst(
                        _src_path,
                        base_path(project.runner),
                        archive_dest,
                        cancel_event=cancel_event,
                        phase_cb=_reset_phase,
                        bytes_cb=_bytes_cb,
                    )
                    base_image = project.runner
                _partial_dest = None
                _written_archive = True

                # ── Auto-publish base image + runner if missing ───────
                if base_image:
                    import json as _json

                    from cellar.backend.base_store import base_path
                    from cellar.backend.packager import (
                        compress_runner_zst,
                        upsert_base,
                        upsert_runner,
                    )
                    from cellar.backend.umu import runners_dir

                    _target_bases: dict[str, str] = {}
                    _target_runners: dict[str, str] = {}
                    _cat_path = repo_root / "catalogue.json"
                    try:
                        if _cat_path.exists():
                            with _cat_path.open("r") as _f:
                                _cat_raw = _json.load(_f)
                            if isinstance(_cat_raw, dict):
                                _target_bases = _cat_raw.get("bases", {})
                                _target_runners = _cat_raw.get("runners", {})
                    except Exception:
                        pass

                    _need_base = base_image not in _target_bases
                    if _need_base:
                        _runner_name = base_image
                        for _r in all_repos:
                            try:
                                _rb = _r.fetch_bases()
                                if base_image in _rb:
                                    _runner_name = _rb[base_image].runner
                                    break
                            except Exception:
                                continue

                        _need_runner = _runner_name not in _target_runners

                        if _need_runner:
                            _runner_src = runners_dir() / _runner_name
                            _runner_rel = f"runners/{_runner_name}.tar.zst"
                            _runner_dest = repo_root / _runner_rel
                            _runner_dest.parent.mkdir(parents=True, exist_ok=True)
                            _reset_phase("Uploading runner\u2026")
                            _partial_dest = _runner_dest
                            _rs, _rc, _rch = compress_runner_zst(
                                _runner_src,
                                _runner_dest,
                                cancel_event=cancel_event,
                                bytes_cb=_bytes_cb,
                            )
                            _partial_dest = None
                            upsert_runner(repo_root, _runner_name, _runner_rel, _rc, _rs, _rch)
                            _runner_upserted = (_runner_name, _runner_dest)

                        _base_rel = f"bases/{base_image}-base.tar.zst"
                        _base_dest = repo_root / _base_rel
                        _base_dest.parent.mkdir(parents=True, exist_ok=True)
                        _reset_phase("Uploading base image\u2026")
                        _partial_dest = _base_dest
                        _bs, _bc, _bch = compress_prefix_zst(
                            base_path(base_image),
                            _base_dest,
                            cancel_event=cancel_event,
                            bytes_cb=_bytes_cb,
                        )
                        _partial_dest = None
                        upsert_base(repo_root, base_image, _runner_name, _base_rel, _bc, _bs, _bch)
                        _base_upserted = (base_image, _base_dest)

                GLib.idle_add(progress.set_label, "Finalizing\u2026")
                GLib.idle_add(progress.set_stats, "")
                GLib.idle_add(progress.start_pulse)
                final_entry = _dc_replace(
                    entry,
                    archive_crc32=crc32,
                    archive_size=size,
                    archive_chunks=chunks,
                    base_image=base_image,
                )
                import_to_repo(
                    repo_root,
                    final_entry,
                    "",
                    images,
                    archive_in_place=True,
                    phase_cb=lambda s: GLib.idle_add(progress.set_label, s),
                )
                _app_imported = True

            except CancelledError:
                from cellar.backend.packager import (
                    _remove_from_catalogue,
                    _rmtree,
                    remove_base,
                    remove_runner,
                )
                # Clean up partial compression in progress.
                if _partial_dest is not None:
                    try:
                        _cleanup_chunks(_partial_dest)
                    except Exception:
                        pass
                # Reverse completed writes in LIFO order.
                if _app_imported:
                    try:
                        _remove_from_catalogue(repo_root, entry.id)
                    except Exception:
                        pass
                    try:
                        _rmtree(repo_root / "apps" / entry.id, ignore_errors=True)
                    except Exception:
                        pass
                if _base_upserted:
                    try:
                        remove_base(repo_root, _base_upserted[0])
                    except Exception:
                        pass
                if _runner_upserted:
                    try:
                        remove_runner(repo_root, _runner_upserted[0])
                    except Exception:
                        pass
                if _written_archive:
                    try:
                        _cleanup_chunks(archive_dest)
                    except Exception:
                        pass
                raise

        def _done(_result) -> None:
            progress.force_close()
            self._show_toast(f"Published '{project.name}' to {repo.name or repo.uri}.")
            if self._on_catalogue_changed:
                self._on_catalogue_changed()
            self._ask_keep_project(project.slug)

        def _error(msg: str) -> None:
            progress.force_close()
            if cancel_event.is_set():
                self._show_toast("Publish cancelled.")
                return
            err = Adw.AlertDialog(heading="Publish failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)


    def _on_publish_base_clicked(self, _btn) -> None:
        if self._project is None:
            return
        project = self._project
        if not project.runner:
            self._show_toast("Select a runner before publishing.")
            return
        if not self._writable_repos:
            self._show_toast("No writable repository configured.")
            return

        if len(self._writable_repos) > 1:
            pick_repo(
                self._writable_repos,
                self,
                lambda repo: self._do_publish_base(project, repo),
            )
            return
        self._do_publish_base(project, self._writable_repos[0])

    def _do_publish_base(self, project: Project, repo) -> None:
        cancel_event = threading.Event()
        progress = ProgressDialog(
            label="Compressing and uploading\u2026", cancel_event=cancel_event,
        )
        progress.present(self)

        import time
        from cellar.utils.progress import fmt_size

        _prevb: list[tuple[float, int]] = []

        def _bytes_cb(n: int) -> None:
            now = time.monotonic()
            _prevb.append((now, n))
            cutoff = now - 2.0
            while _prevb and _prevb[0][0] < cutoff:
                _prevb.pop(0)
            if len(_prevb) >= 2:
                dt = _prevb[-1][0] - _prevb[0][0]
                db = _prevb[-1][1] - _prevb[0][1]
                speed = db / dt if dt > 0 else 0
                spd = f" ({fmt_size(int(speed))}/s)" if speed > 0 else ""
            else:
                spd = ""
            GLib.idle_add(progress.set_stats, fmt_size(n) + " written" + spd)

        base_name = project.name

        def _work():
            from cellar.backend.base_store import install_base_from_dir
            from cellar.backend.packager import (
                CancelledError,
                compress_prefix_zst,
                compress_runner_zst,
                upsert_base,
                upsert_runner,
            )
            from cellar.backend.umu import runners_dir

            runner = project.runner
            repo_root = repo.writable_path()
            _partial_files = []  # track files to clean up on cancel

            try:
                # ── Compress and upload the runner ────────────────────────
                runner_src = runners_dir() / runner
                runner_archive_rel = f"runners/{runner}.tar.zst"
                runner_archive_dest = repo_root / runner_archive_rel
                runner_archive_dest.parent.mkdir(parents=True, exist_ok=True)

                GLib.idle_add(progress.set_label, "Compressing and uploading runner\u2026")
                GLib.idle_add(progress.set_stats, "")
                _partial_files.append(runner_archive_dest)
                runner_size, runner_crc32, runner_chunks = compress_runner_zst(
                    runner_src,
                    runner_archive_dest,
                    cancel_event=cancel_event,
                    bytes_cb=_bytes_cb,
                )

                # ── Compress and upload the base image ────────────────────
                GLib.idle_add(progress.set_label, "Compressing and uploading base image\u2026")
                GLib.idle_add(progress.set_stats, "")
                archive_dest_rel = f"bases/{base_name}-base.tar.zst"
                archive_dest = repo_root / archive_dest_rel
                archive_dest.parent.mkdir(parents=True, exist_ok=True)

                _partial_files.append(archive_dest)
                size, crc32, base_chunks = compress_prefix_zst(
                    project.content_path,
                    archive_dest,
                    cancel_event=cancel_event,
                    bytes_cb=_bytes_cb,
                )
            except CancelledError:
                from cellar.backend.packager import _cleanup_chunks
                for f in _partial_files:
                    try:
                        _cleanup_chunks(f)
                    except Exception:
                        pass
                raise

            GLib.idle_add(progress.set_label, "Finalizing\u2026")
            GLib.idle_add(progress.set_stats, "")
            GLib.idle_add(progress.start_pulse)
            upsert_runner(
                repo_root, runner, runner_archive_rel, runner_crc32, runner_size,
                runner_chunks,
            )
            upsert_base(
                repo_root, base_name, runner, archive_dest_rel, crc32, size,
                base_chunks,
            )

            GLib.idle_add(progress.set_label, "Installing base locally\u2026")
            GLib.idle_add(progress.set_stats, "")
            install_base_from_dir(
                project.content_path,
                base_name,
                repo_source=repo.uri,
            )

        def _done(_result) -> None:
            progress.force_close()
            self._show_toast(f"Base '{base_name}' published.")
            if self._on_catalogue_changed:
                self._on_catalogue_changed()
            self._ask_keep_project(project.slug)

        def _error(msg: str) -> None:
            progress.force_close()
            if cancel_event.is_set():
                self._show_toast("Publish cancelled.")
                return
            err = Adw.AlertDialog(heading="Failed", body=msg)
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(_work, on_done=_done, on_error=_error)

    def _ask_keep_project(self, slug: str) -> None:
        """Ask the user whether to keep or delete the project after publishing."""
        dlg = Adw.AlertDialog(
            heading="Keep project?",
            body="The project was published successfully. Do you want to keep it in the builder for future updates?",
        )
        dlg.add_response("delete", "Delete")
        dlg.add_response("keep", "Keep")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("keep")

        def _on_response(_dlg, response):
            if response == "delete":
                delete_project(slug)
            self._project = None
            self._reload_projects()
            self._nav_view.pop_to_page(self._list_page)

        dlg.connect("response", _on_response)
        dlg.present(self)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _run_in_prefix_with_progress(
        self,
        project: Project,
        exe: str,
        label: str,
        on_done: Callable[[bool], None],
    ) -> None:
        """Run *exe* in *project*'s prefix on a background thread with a progress dialog."""
        if not project.runner:
            what = "a base image" if project.project_type == "app" else "a runner"
            self._show_toast(f"Select {what} first.")
            return

        project.content_path.mkdir(parents=True, exist_ok=True)
        runner_name = self._resolve_runner_name(project)

        progress = ProgressDialog(label=label)
        progress.present(self)

        def _work():
            from cellar.backend.umu import run_in_prefix
            result = run_in_prefix(
                project.content_path,
                runner_name,
                exe,
                timeout=600,
            )
            return result.returncode == 0

        def _finish(ok: bool) -> None:
            progress.force_close()
            on_done(ok)
            if not ok:
                self._show_toast("Command exited with non-zero status. Check logs.")

        def _on_err(msg: str) -> None:
            log.error("run_in_prefix failed: %s", msg)
            _finish(False)

        run_in_background(_work, on_done=_finish, on_error=_on_err)

    def _show_toast(self, message: str) -> None:
        win = self.get_root()
        if hasattr(win, "toast_overlay"):
            win.toast_overlay.add_toast(Adw.Toast(title=message))

    def _on_dosbox_settings_clicked(self, _btn) -> None:
        """Open the DOSBox Settings dialog for the current DOS project."""
        if self._project is None or not self._project.source_dir:
            return
        from cellar.views.dosbox_settings import DosboxSettingsDialog

        src = Path(self._project.source_dir)
        DosboxSettingsDialog(
            config_dir=src / "config",
            assets_dir=src / "assets",
            on_saved=lambda: self._show_project(self._project) if self._project else None,
        ).present(self)

    # ── DOS config helpers ─────────────────────────────────────────

    def _get_dosbox_override(self, project: Project, section: str, key: str) -> str:
        """Read a value from dosbox-overrides.conf, or empty string."""
        if not project.source_dir:
            return ""
        conf = Path(project.source_dir) / "config" / "dosbox-overrides.conf"
        if not conf.is_file():
            return ""
        import configparser
        text = conf.read_text(encoding="utf-8", errors="replace")
        # Strip autoexec for configparser
        from cellar.backend.dosbox import _strip_autoexec
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(_strip_autoexec(text))
        if parser.has_section(section) and parser.has_option(section, key):
            return parser.get(section, key).strip()
        return ""

    def _set_dosbox_override(self, project: Project, section: str, key: str, value: str) -> None:
        """Set a value in dosbox-overrides.conf, creating the section if needed."""
        if not project.source_dir:
            return
        conf = Path(project.source_dir) / "config" / "dosbox-overrides.conf"
        if not conf.is_file():
            return
        text = conf.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        new_lines: list[str] = []
        in_target = False
        key_written = False

        for line in lines:
            stripped = line.strip().lower()
            if stripped.startswith("[") and stripped.endswith("]"):
                # Leaving a section — if we were in target and didn't write, insert now
                if in_target and not key_written:
                    new_lines.append(f"{key} = {value}")
                    key_written = True
                in_target = (stripped == f"[{section}]")
                new_lines.append(line)
                continue
            if in_target and stripped.startswith(f"{key.lower()}"):
                new_lines.append(f"{key} = {value}")
                key_written = True
                continue
            new_lines.append(line)

        if in_target and not key_written:
            new_lines.append(f"{key} = {value}")
            key_written = True

        # Section doesn't exist yet — add it before [autoexec]
        if not key_written:
            autoexec_idx = None
            for i, line in enumerate(new_lines):
                if line.strip().lower() == "[autoexec]":
                    autoexec_idx = i
                    break
            insert = [f"\n[{section}]", f"{key} = {value}"]
            if autoexec_idx is not None:
                for j, il in enumerate(insert):
                    new_lines.insert(autoexec_idx + j, il)
            else:
                new_lines.extend(insert)

        conf.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    def _on_dosbox_fullscreen_toggled(self, row, _pspec) -> None:
        if self._project is None:
            return
        self._set_dosbox_override(
            self._project, "sdl", "fullscreen",
            "true" if row.get_active() else "false",
        )

    # ── DOS audio asset management ──────────────────────────────────

    def _on_add_dos_asset_clicked(self, _btn) -> None:
        """Browse for SoundFont or MT-32 ROM files to add to the DOS project."""
        if self._project is None or self._project.project_type != "dos":
            return
        chooser = Gtk.FileChooserNative(
            title="Select SoundFont or MT-32 ROM Files",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Add",
        )
        chooser.set_select_multiple(True)

        audio_filter = Gtk.FileFilter()
        audio_filter.set_name("SoundFonts & ROMs (*.sf2, *.sf3, *.rom)")
        audio_filter.add_pattern("*.sf2")
        audio_filter.add_pattern("*.SF2")
        audio_filter.add_pattern("*.sf3")
        audio_filter.add_pattern("*.SF3")
        audio_filter.add_pattern("*.rom")
        audio_filter.add_pattern("*.ROM")
        chooser.add_filter(audio_filter)

        all_filter = Gtk.FileFilter()
        all_filter.set_name("All files")
        all_filter.add_pattern("*")
        chooser.add_filter(all_filter)

        chooser.connect("response", self._on_dos_asset_chosen, chooser)
        chooser.show()
        self._asset_chooser = chooser

    def _on_dos_asset_chosen(self, _c, response, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT or self._project is None:
            return
        project = self._project
        src_dir = Path(project.source_dir)
        files = chooser.get_files()

        added_sf = False
        added_rom = False

        for gfile in files:
            path = Path(gfile.get_path())
            suffix = path.suffix.lower()

            if suffix in (".sf2", ".sf3"):
                dest_dir = src_dir / "assets" / "soundfonts"
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest_dir / path.name)
                added_sf = True
            elif suffix == ".rom":
                dest_dir = src_dir / "assets" / "mt32-roms"
                dest_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest_dir / path.name)
                added_rom = True

        # Auto-update DOSBox overrides config
        if added_sf or added_rom:
            self._update_dosbox_audio_config(project, added_sf, added_rom)
            save_project(project)
            self._show_project(project)

        if added_sf and added_rom:
            self._show_toast("Added SoundFont and MT-32 ROMs")
        elif added_sf:
            self._show_toast("Added SoundFont")
        elif added_rom:
            self._show_toast("Added MT-32 ROMs")

    def _on_remove_dos_asset(self, _btn, project: Project, asset_path: Path) -> None:
        """Remove a single audio asset file and update config."""
        if not asset_path.is_file():
            return
        asset_path.unlink()
        # Check if any soundfonts/roms remain
        sf_dir = Path(project.source_dir) / "assets" / "soundfonts"
        rom_dir = Path(project.source_dir) / "assets" / "mt32-roms"
        has_sf = sf_dir.is_dir() and any(sf_dir.iterdir())
        has_rom = rom_dir.is_dir() and any(rom_dir.iterdir())
        self._update_dosbox_audio_config(project, has_sf, has_rom, remove_missing=True)
        save_project(project)
        self._show_project(project)
        self._show_toast(f"Removed {asset_path.name}")

    def _on_remove_dos_asset_dir(self, _btn, project: Project, dir_path: Path) -> None:
        """Remove an entire asset directory (e.g. mt32-roms) and update config."""
        if dir_path.is_dir():
            shutil.rmtree(dir_path)
        sf_dir = Path(project.source_dir) / "assets" / "soundfonts"
        has_sf = sf_dir.is_dir() and any(sf_dir.iterdir())
        self._update_dosbox_audio_config(project, has_sf, False, remove_missing=True)
        save_project(project)
        self._show_project(project)
        self._show_toast("Removed MT-32 ROMs")

    def _update_dosbox_audio_config(
        self,
        project: Project,
        has_soundfont: bool,
        has_mt32: bool,
        *,
        remove_missing: bool = False,
    ) -> None:
        """Update dosbox-overrides.conf with audio asset paths.

        When *remove_missing* is True, removes config entries for asset types
        that are no longer present.
        """
        overrides_path = Path(project.source_dir) / "config" / "dosbox-overrides.conf"
        if not overrides_path.is_file():
            return

        text = overrides_path.read_text(encoding="utf-8")
        lines = text.splitlines()
        new_lines: list[str] = []

        # Track which sections/keys we've seen
        in_section = ""
        midi_written = False
        fluidsynth_written = False
        mt32_written = False

        # Strip existing audio config lines — we'll re-add them
        for line in lines:
            stripped = line.strip().lower()
            if stripped.startswith("[") and stripped.endswith("]"):
                in_section = stripped[1:-1]

            # Remove existing midi/fluidsynth/mt32 lines we manage
            if in_section == "midi" and stripped.startswith("mididevice"):
                continue
            if in_section == "fluidsynth" and stripped.startswith("soundfont"):
                continue
            if in_section == "mt32" and stripped.startswith("romdir"):
                continue
            # Remove empty section headers for sections we manage
            if stripped in ("[fluidsynth]", "[mt32]"):
                # Skip — we'll re-add if needed
                continue
            if stripped == "[midi]":
                continue

            new_lines.append(line)

        # Remove trailing blank lines
        while new_lines and not new_lines[-1].strip():
            new_lines.pop()

        # Add audio config at the end (before [autoexec] if present)
        autoexec_idx = None
        for i, line in enumerate(new_lines):
            if line.strip().lower() == "[autoexec]":
                autoexec_idx = i
                break

        audio_lines: list[str] = []

        if has_soundfont:
            sf_dir = Path(project.source_dir) / "assets" / "soundfonts"
            sfs = sorted(sf_dir.glob("*.sf[23]")) if sf_dir.is_dir() else []
            if sfs:
                audio_lines.append("")
                audio_lines.append("[midi]")
                audio_lines.append("mididevice = fluidsynth")
                audio_lines.append("")
                audio_lines.append("[fluidsynth]")
                audio_lines.append(f"soundfont = assets/soundfonts/{sfs[0].name}")
        elif has_mt32:
            audio_lines.append("")
            audio_lines.append("[midi]")
            audio_lines.append("mididevice = mt32")
            audio_lines.append("")
            audio_lines.append("[mt32]")
            audio_lines.append("romdir = assets/mt32-roms")

        if audio_lines:
            if autoexec_idx is not None:
                for i, al in enumerate(audio_lines):
                    new_lines.insert(autoexec_idx + i, al)
            else:
                new_lines.extend(audio_lines)

        new_lines.append("")  # trailing newline
        overrides_path.write_text("\n".join(new_lines), encoding="utf-8")

    def _open_file_in_editor(self, path: Path | None) -> None:
        """Open a file in the default text editor via xdg-open."""
        if path is None or not path.is_file():
            return
        Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)

    def _open_folder(self, path: Path | None) -> None:
        """Open a folder in the default file manager."""
        if path is None or not path.is_dir():
            return
        Gio.AppInfo.launch_default_for_uri(path.as_uri(), None)

    # ── GOG DOSBox game detection and conversion ─────────────────────

    def _check_dosbox_after_install(self, project: Project) -> bool:
        """Check a WINEPREFIX for a GOG DOSBox game after installer/import.

        If found, prompts the user and converts the project from Windows to
        DOS.  Returns ``True`` if a DOSBox game was detected (conversion may
        be pending user confirmation), ``False`` otherwise.
        """
        from cellar.backend.dosbox import detect_gog_dosbox_in_prefix

        result = detect_gog_dosbox_in_prefix(project.content_path)
        if result is None:
            return False

        game_folder, dosbox_info = result

        dlg = Adw.AlertDialog(
            heading="DOSBox game detected",
            body=(
                f'"{dosbox_info.game_name}" uses DOSBox.\n\n'
                "Convert to a native DOS package with DOSBox Staging? "
                "This avoids running DOSBox through Wine."
            ),
        )
        dlg.add_response("cancel", "Keep as Windows")
        dlg.add_response("convert", "Convert to DOS")
        dlg.set_response_appearance("convert", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("convert")
        dlg.set_close_response("cancel")

        def _on_response(_dlg, response):
            if response == "convert":
                self._convert_prefix_to_dos(project, game_folder, dosbox_info)
            else:
                # User chose to keep as Windows — proceed normally
                self._scan_entry_points_after_install(project)
                save_project(project)
                if self._project is project:
                    self._show_project(project)

        dlg.connect("response", _on_response)
        dlg.present(self)
        return True

    def _convert_prefix_to_dos(
        self, project: Project, game_folder: Path, dosbox_info,
    ) -> None:
        """Convert a Windows WINEPREFIX project to a DOS project.

        Extracts the game files from the WINEPREFIX, strips Wine artifacts,
        and runs the standard DOSBox conversion pipeline.
        """
        import tempfile

        progress = ProgressDialog(label="Converting to DOS package\u2026")
        progress.present(self)

        def _work():
            from cellar.backend.dosbox import convert_gog_dosbox

            with tempfile.TemporaryDirectory() as tmp:
                tmp_dest = Path(tmp) / "converted"
                def _on_progress(downloaded, total):
                    if total > 0:
                        GLib.idle_add(progress.set_fraction, downloaded / total)
                entry_points = convert_gog_dosbox(
                    game_folder, tmp_dest, dosbox_info, progress_cb=_on_progress,
                )

                content = project.content_path
                shutil.rmtree(content, ignore_errors=True)
                shutil.move(str(tmp_dest), str(content))

            return entry_points

        def _done(entry_points):
            progress.force_close()
            project.project_type = "dos"
            project.source_dir = str(project.content_path)
            project.initialized = True
            project.runner = ""
            if entry_points:
                project.entry_points = entry_points
            save_project(project)
            # Reload project list so the card icon/label updates
            self._reload_projects()
            if self._project is project:
                self._show_project(project)
            self._show_toast("Converted to DOS package")

        def _error(msg):
            progress.force_close()
            log.error("DOSBox conversion failed: %s", msg)
            err = Adw.AlertDialog(
                heading="Conversion failed",
                body=f"Could not convert to DOS package:\n{msg}",
            )
            err.add_response("ok", "OK")
            err.present(self)

        run_in_background(work=_work, on_done=_done, on_error=_error)


# ---------------------------------------------------------------------------
# Helper widgets
# ---------------------------------------------------------------------------

_CARD_WIDTH = 300
_CARD_HEIGHT = 96
_ICON_SIZE = 52
_ICON_MARGIN = 22

_TYPE_ICONS = {
    "base": "package-x-generic-symbolic",
    "linux": "penguin-alt-symbolic",
    "dos": "floppy-symbolic",
    "app": "grid-large-symbolic",
}
_TYPE_LABELS = {"app": "Proton App", "linux": "Native App", "dos": "DOS App", "base": "Base Image"}

# Map internal project_type / kind values to filter-pill identifiers.
_FILTER_TYPE_PROTON = "proton"
_FILTER_TYPE_NATIVE = "native"
_FILTER_TYPE_BASE = "base"

def _resolve_filter_type(project_type: str, platform: str = "windows") -> str:
    """Return the filter-pill type id for a project type + platform combo."""
    if project_type == "base":
        return _FILTER_TYPE_BASE
    if project_type in ("linux", "dos") or platform in ("linux", "dos"):
        return _FILTER_TYPE_NATIVE
    return _FILTER_TYPE_PROTON


class _NewProjectDialog(Adw.Dialog):
    """Guided new-project chooser — smart import drop zone + manual platform selection."""

    def __init__(
        self,
        *,
        on_windows: Callable[[], None],
        on_linux: Callable[[], None],
        on_dos: Callable[[], None],
        on_base: Callable[[], None],
        on_import: Callable,
        parent_view,
    ) -> None:
        super().__init__(title="New Project", content_width=420, content_height=520)
        self._on_windows = on_windows
        self._on_linux = on_linux
        self._on_dos = on_dos
        self._on_base = on_base
        self._on_import = on_import
        self._parent_view = parent_view
        self._file_chooser = None  # prevent GC

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)
        toolbar.add_top_bar(header)

        # ── Outer scrollable container ──────────────────────────────────
        scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        outer = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            margin_top=18,
            margin_bottom=18,
            margin_start=18,
            margin_end=18,
        )
        scroll.set_child(outer)

        # ── Drop zone frame ─────────────────────────────────────────────
        self._drop_frame = Gtk.Frame()
        self._drop_frame.add_css_class("drop-zone")

        drop_inner = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER,
            margin_top=18,
            margin_bottom=18,
            margin_start=12,
            margin_end=12,
        )
        icon = Gtk.Image.new_from_icon_name("document-open-symbolic")
        icon.set_pixel_size(36)
        icon.add_css_class("dim-label")
        drop_inner.append(icon)

        heading = Gtk.Label(label="Drop an .exe file or app folder")
        heading.add_css_class("heading")
        drop_inner.append(heading)

        caption = Gtk.Label(label="Auto-detects platform and imports metadata")
        caption.add_css_class("dim-label")
        caption.add_css_class("caption")
        drop_inner.append(caption)

        self._drop_frame.set_child(drop_inner)
        outer.append(self._drop_frame)

        # Drop zone CSS
        _css = b"""
.drop-zone {
    border: 2px dashed alpha(@borders, 0.8);
    border-radius: 12px;
    min-height: 110px;
}
.drop-zone.drag-hover {
    border-color: @accent_color;
    background-color: alpha(@accent_color, 0.08);
}
"""
        _provider = Gtk.CssProvider()
        _provider.load_from_data(_css)
        self._drop_frame.get_style_context().add_provider(
            _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        drop = Gtk.DropTarget.new(Gdk.FileList, Gdk.DragAction.COPY)
        drop.connect("drop", self._on_drop)
        drop.connect("enter", self._on_drag_enter)
        drop.connect("leave", self._on_drag_leave)
        self._drop_frame.add_controller(drop)

        # ── Browse buttons ──────────────────────────────────────────────
        browse_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            homogeneous=True,
        )
        file_btn = Gtk.Button(label="Browse File\u2026")
        file_btn.add_css_class("pill")
        file_btn.connect("clicked", self._on_browse_file)
        folder_btn = Gtk.Button(label="Browse Folder\u2026")
        folder_btn.add_css_class("pill")
        folder_btn.connect("clicked", self._on_browse_folder)
        browse_box.append(file_btn)
        browse_box.append(folder_btn)
        outer.append(browse_box)

        # ── Separator ───────────────────────────────────────────────────
        sep_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=8,
            margin_top=6,
            margin_bottom=6,
        )
        sep_box.append(Gtk.Separator(hexpand=True, valign=Gtk.Align.CENTER))
        or_lbl = Gtk.Label(label="or create manually")
        or_lbl.add_css_class("dim-label")
        or_lbl.add_css_class("caption")
        sep_box.append(or_lbl)
        sep_box.append(Gtk.Separator(hexpand=True, valign=Gtk.Align.CENTER))
        outer.append(sep_box)

        # ── Manual platform group ────────────────────────────────────────
        group = Adw.PreferencesGroup()

        has_bases = any(repo._bases for repo in self._parent_view._all_repos)
        win_row = Adw.ActionRow(
            title="Proton Package",
            subtitle=(
                "App running in Proton/Wine" if has_bases
                else "No base images available — add a repo with base images first"
            ),
            activatable=has_bases,
            sensitive=has_bases,
        )
        win_row.add_prefix(Gtk.Image.new_from_icon_name("grid-large-symbolic"))
        win_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        win_row.connect("activated", self._on_windows_activated)
        group.add(win_row)

        linux_row = Adw.ActionRow(
            title="Native Package",
            subtitle="Native Linux application",
            activatable=True,
        )
        linux_row.add_prefix(Gtk.Image.new_from_icon_name("penguin-alt-symbolic"))
        linux_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        linux_row.connect("activated", self._on_linux_activated)
        group.add(linux_row)

        dos_row = Adw.ActionRow(
            title="DOS Package",
            subtitle="DOS game with DOSBox Staging",
            activatable=True,
        )
        dos_row.add_prefix(Gtk.Image.new_from_icon_name("floppy-symbolic"))
        dos_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        dos_row.connect("activated", self._on_dos_activated)
        group.add(dos_row)

        base_row = Adw.ActionRow(
            title="Base Image",
            subtitle="Reusable Wine runtime for Proton packages",
            activatable=True,
        )
        base_row.add_prefix(Gtk.Image.new_from_icon_name("package-x-generic-symbolic"))
        base_row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        base_row.connect("activated", self._on_base_activated)
        group.add(base_row)

        clamp = Adw.Clamp(maximum_size=400)
        clamp.set_child(group)
        outer.append(clamp)

        toolbar.set_content(scroll)
        self.set_child(toolbar)

    # ── Drop-zone handlers ──────────────────────────────────────────────

    def _on_drag_enter(self, _target, _x, _y) -> Gdk.DragAction:
        self._drop_frame.add_css_class("drag-hover")
        return Gdk.DragAction.COPY

    def _on_drag_leave(self, _target) -> None:
        self._drop_frame.remove_css_class("drag-hover")

    def _on_drop(self, _target, value, _x, _y) -> bool:
        self._drop_frame.remove_css_class("drag-hover")
        files = value.get_files()
        if not files:
            return False
        path = Path(files[0].get_path())
        self.close()
        self._start_import(path)
        return True

    # ── Browse handlers ─────────────────────────────────────────────────

    def _on_browse_file(self, _btn) -> None:
        chooser = Gtk.FileChooserNative(
            title="Select Installer or Executable",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Import",
        )
        f = Gtk.FileFilter()
        f.set_name("Windows Executables")
        for pat in ("*.exe", "*.EXE", "*.msi", "*.MSI",
                    "*.bat", "*.BAT", "*.cmd", "*.CMD",
                    "*.com", "*.COM", "*.lnk", "*.LNK"):
            f.add_pattern(pat)
        all_f = Gtk.FileFilter()
        all_f.set_name("All Files")
        all_f.add_pattern("*")
        chooser.add_filter(f)
        chooser.add_filter(all_f)
        chooser.connect("response", self._on_file_chosen, chooser)
        chooser.show()
        self._file_chooser = chooser

    def _on_browse_folder(self, _btn) -> None:
        chooser = Gtk.FileChooserNative(
            title="Select App Folder",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            accept_label="Import",
        )
        chooser.connect("response", self._on_file_chosen, chooser)
        chooser.show()
        self._file_chooser = chooser

    def _on_file_chosen(self, _chooser, response: int, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        path = Path(chooser.get_file().get_path())
        self.close()
        self._start_import(path)

    # ── Import dispatch ─────────────────────────────────────────────────

    def _start_import(self, path: Path) -> None:
        """Detect platform, parse name, and open MetadataEditorDialog pre-filled."""
        from cellar.backend.detect import (
            detect_platform,
            find_gameinfo,
            parse_app_name,
            parse_version_hint,
            unsupported_reason,
        )

        platform = detect_platform(path)

        if platform == "unsupported":
            msg = unsupported_reason(path)
            err = Adw.AlertDialog(heading="Cannot import", body=msg)
            err.add_response("ok", "OK")
            err.present(self._parent_view)
            return

        if platform == "ambiguous":
            self._show_platform_picker(path)
            return

        app_name = parse_app_name(path)
        version = parse_version_hint(path)

        # Check for GOG DOSBox game — auto-convert to DOS platform
        self._dosbox_info = None
        if platform == "windows" and path.is_dir():
            from cellar.backend.dosbox import detect_gog_dosbox
            dosbox_info = detect_gog_dosbox(path)
            if dosbox_info is not None:
                platform = "dos"
                self._dosbox_info = dosbox_info
                if dosbox_info.game_name:
                    app_name = dosbox_info.game_name

        # Check for GoG gameinfo — inside folders or GOG .sh installers
        if path.is_dir():
            gi = find_gameinfo(path)
        elif path.suffix.lower() == ".sh":
            from cellar.utils.gog import read_gog_gameinfo
            gi = read_gog_gameinfo(path)
        else:
            gi = None
        if gi:
            if gi["name"]:
                app_name = gi["name"]
            if gi["version"]:
                version = gi["version"]

        self._open_metadata_editor(path, platform, app_name, version)

    def _show_platform_picker(self, path: Path) -> None:
        """Show a small dialog to disambiguate platform."""
        from cellar.backend.detect import find_gameinfo, parse_app_name, parse_version_hint

        dlg = Adw.AlertDialog(
            heading="Which platform?",
            body="Could not auto-detect the platform. Please choose:",
        )
        dlg.add_response("windows", "Proton (Windows)")
        dlg.add_response("linux", "Native (Linux)")
        dlg.add_response("cancel", "Cancel")
        dlg.set_default_response("windows")
        dlg.set_close_response("cancel")

        def _on_response(_dlg, response):
            if response in ("windows", "linux"):
                app_name = parse_app_name(path)
                version = parse_version_hint(path)
                if path.is_dir():
                    gi = find_gameinfo(path)
                    if gi:
                        if gi["name"]:
                            app_name = gi["name"]
                        if gi["version"]:
                            version = gi["version"]
                self._open_metadata_editor(path, response, app_name, version)

        dlg.connect("response", _on_response)
        dlg.present(self._parent_view)

    def _open_metadata_editor(
        self, path: Path, platform: str, app_name: str, version: str | None,
    ) -> None:
        """Open the standard MetadataEditorDialog with smart-import pre-fill."""
        from cellar.backend.detect import find_linux_executables

        project_type = {"linux": "linux", "dos": "dos"}.get(platform, "app")
        ctx = ProjectContext(project_type=project_type)

        def _on_created(project):
            # Post-creation: set import-specific fields on the project
            changed = False
            if platform == "dos" and path.is_dir() and hasattr(self, '_dosbox_info') and self._dosbox_info:
                # GOG DOSBox game: convert to native Linux DOSBox in background
                self._convert_dosbox_game(project, path, self._dosbox_info)
                return  # _convert_dosbox_game calls _on_import when done
            elif platform == "windows" and path.is_file():
                # .exe import: store installer path
                project.installer_path = str(path)
                changed = True
            elif platform == "linux" and path.is_file() and path.suffix.lower() == ".sh":
                # GOG Linux installer: extract in background (can be multi-GB)
                self._extract_gog_installer(project, path)
                return  # _extract_gog_installer calls _on_import when done
            elif platform in ("linux", "dos") and path.is_dir():
                # Linux/DOS folder: set source_dir and detect entry points
                project.source_dir = str(path)
                project.initialized = True
                candidates = find_linux_executables(path)
                if candidates:
                    project.entry_points = [
                        {"name": c.name, "path": str(c.relative_to(path))}
                        for c in candidates[:5]
                    ]
                changed = True
            elif platform == "windows" and path.is_dir():
                # Windows folder: store source_dir for later import
                project.source_dir = str(path)
                changed = True

            if version and project.version == "1.0":
                project.version = version
                changed = True

            if changed:
                save_project(project)
            self._on_import(project)

        dialog = MetadataEditorDialog(
            context=ctx,
            on_created=_on_created,
            auto_steam_query=app_name,
            auto_version=version or "",
        )
        dialog.present(self._parent_view)

    # ── GOG Linux installer extraction ────────────────────────────────

    def _extract_gog_installer(self, project, src_path: Path) -> None:
        """Extract a GOG Linux .sh installer into the project in a background thread."""
        from cellar.backend.detect import find_linux_executables
        from cellar.utils.gog import extract_gog_game_data

        content = project.content_path
        content.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label="Extracting GOG game\u2026")
        progress.present(self._parent_view)

        def _work():
            def _on_progress(extracted, total):
                if total > 0:
                    GLib.idle_add(progress.set_fraction, extracted / total)
            extract_gog_game_data(src_path, content, progress_cb=_on_progress)
            return find_linux_executables(content)

        def _done(candidates):
            progress.force_close()
            project.source_dir = str(content)
            project.initialized = True
            if candidates:
                project.entry_points = [
                    {
                        "name": project.name if c.name == "start.sh" else c.name,
                        "path": str(c.relative_to(content)),
                    }
                    for c in candidates[:5]
                ]
            save_project(project)
            self._on_import(project)

        def _error(msg):
            progress.force_close()
            log.error("GOG extraction failed: %s", msg)
            err = Adw.AlertDialog(
                heading="Extraction failed",
                body=f"Could not extract GOG installer:\n{msg}",
            )
            err.add_response("ok", "OK")
            err.present(self._parent_view)

        run_in_background(work=_work, on_done=_done, on_error=_error)

    def _convert_dosbox_game(self, project, src_path: Path, dosbox_info) -> None:
        """Convert a GOG DOSBox Windows game to native Linux DOSBox Staging."""
        from cellar.backend.dosbox import convert_gog_dosbox

        content = project.content_path
        content.mkdir(parents=True, exist_ok=True)

        progress = ProgressDialog(label="Setting up DOS game\u2026")
        progress.present(self._parent_view)

        def _work():
            def _on_progress(downloaded, total):
                if total > 0:
                    GLib.idle_add(progress.set_fraction, downloaded / total)
            return convert_gog_dosbox(
                src_path, content, dosbox_info, progress_cb=_on_progress,
            )

        def _done(entry_points):
            progress.force_close()
            project.source_dir = str(content)
            project.initialized = True
            if entry_points:
                project.entry_points = entry_points
            save_project(project)
            self._on_import(project)

        def _error(msg):
            progress.force_close()
            log.error("DOSBox conversion failed: %s", msg)
            err = Adw.AlertDialog(
                heading="Conversion failed",
                body=f"Could not convert DOSBox game:\n{msg}",
            )
            err.add_response("ok", "OK")
            err.present(self._parent_view)

        run_in_background(work=_work, on_done=_done, on_error=_error)

    # ── Manual platform row handlers ────────────────────────────────────

    def _on_windows_activated(self, _row) -> None:
        self.close()
        self._on_windows()

    def _on_linux_activated(self, _row) -> None:
        self.close()
        self._on_linux()

    def _on_dos_activated(self, _row) -> None:
        self.close()
        self._on_dos()

    def _on_base_activated(self, _row) -> None:
        self.close()
        self._on_base()


class _NewProjectCard(Gtk.FlowBoxChild):
    """Persistent 'New Project' card — always first in the grid."""

    def __init__(self) -> None:
        super().__init__()
        self.add_css_class("app-card-cell")
        set_margins(self, 6)

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        card.add_css_class("new-project-card")
        card.add_css_class("activatable")
        card.set_overflow(Gtk.Overflow.HIDDEN)

        icon = Gtk.Image.new_from_icon_name("list-add-symbolic")
        icon.set_pixel_size(_ICON_SIZE)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        icon.set_margin_start(_ICON_MARGIN)
        icon.add_css_class("dim-label")
        card.append(icon)

        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_hexpand(True)
        text_box.set_margin_start(_ICON_MARGIN)
        text_box.set_margin_end(18)
        card.append(text_box)

        title = Gtk.Label(label="New Project")
        title.add_css_class("heading")
        title.set_halign(Gtk.Align.START)
        text_box.append(title)

        subtitle = Gtk.Label(label="Create a new package")
        subtitle.add_css_class("dim-label")
        subtitle.set_halign(Gtk.Align.START)
        text_box.append(subtitle)

        from cellar.views.browse import _FixedBox
        fixed = _FixedBox(_CARD_WIDTH, _CARD_HEIGHT, clip=False)
        fixed.set_child(card)
        self.set_child(fixed)

    def do_dispose(self) -> None:
        from cellar.views.browse import _dispose_subtree
        child = self.get_first_child()
        if child is not None:
            _dispose_subtree(child)
        self.set_child(None)
        super().do_dispose()


class _ProjectCard(Gtk.FlowBoxChild):
    """A project card matching the browse view's AppCard layout."""

    def __init__(self, project: Project) -> None:
        super().__init__()
        self.project = project
        self.add_css_class("app-card-cell")

        set_margins(self, 6)

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        card.add_css_class("card")
        card.add_css_class("activatable")
        card.add_css_class("app-card")
        card.set_overflow(Gtk.Overflow.HIDDEN)

        # Left: type icon
        icon = Gtk.Image.new_from_icon_name(
            _TYPE_ICONS.get(project.project_type, "grid-large-symbolic")
        )
        icon.set_pixel_size(_ICON_SIZE)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        icon.set_margin_start(_ICON_MARGIN)
        icon.add_css_class("dim-label")
        card.append(icon)

        # Right: name + type subtitle
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_hexpand(True)
        text_box.set_margin_start(_ICON_MARGIN)
        text_box.set_margin_end(18)
        card.append(text_box)

        self._name_label = Gtk.Label(label=project.name)
        self._name_label.add_css_class("heading")
        self._name_label.set_halign(Gtk.Align.START)
        self._name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._name_label.set_tooltip_text(project.name)
        text_box.append(self._name_label)

        type_label = _TYPE_LABELS.get(project.project_type, "")
        if not project.origin_app_id:
            type_label += " \u00b7 Draft"
        subtitle = Gtk.Label(label=type_label)
        subtitle.add_css_class("dim-label")
        subtitle.set_halign(Gtk.Align.START)
        text_box.append(subtitle)

        from cellar.views.browse import _FixedBox
        fixed = _FixedBox(_CARD_WIDTH, _CARD_HEIGHT, clip=False)
        fixed.set_child(card)
        self.set_child(fixed)

    def do_dispose(self) -> None:
        from cellar.views.browse import _dispose_subtree
        child = self.get_first_child()
        if child is not None:
            _dispose_subtree(child)
        self.set_child(None)
        super().do_dispose()

    def matches(self, search: str, active_types: set[str], active_repos: set[str]) -> bool:
        """Return True if this card should be visible given the current filters."""
        if active_types:
            ft = _resolve_filter_type(self.project.project_type)
            if ft not in active_types:
                return False
        if search and search.lower() not in self.project.name.lower():
            return False
        # Projects are local — always pass repo filter.
        return True

    def refresh_label(self) -> None:
        """Update the displayed name."""
        self._name_label.set_label(self.project.name)
        self._name_label.set_tooltip_text(self.project.name)


class _CatalogueCard(Gtk.FlowBoxChild):
    """A dimmed card for a published catalogue entry — edit, download, or delete actions."""

    def __init__(
        self,
        entry,
        repo,
        kind: str,
        *,
        on_download: Callable,
        on_delete: Callable,
        on_edit: Callable | None = None,
        has_dependants: bool = False,
        show_repo: bool = False,
    ) -> None:
        super().__init__()
        self.entry = entry
        self.repo = repo
        self.kind = kind  # "app" or "base"
        self.add_css_class("app-card-cell")

        set_margins(self, 6)

        card = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        card.add_css_class("card")
        card.add_css_class("app-card")
        card.set_overflow(Gtk.Overflow.HIDDEN)

        # Left: type icon
        if kind == "base":
            icon_name = "package-x-generic-symbolic"
            type_label = "Base Image"
        else:
            platform = getattr(entry, "platform", "windows")
            icon_name = _TYPE_ICONS.get({"linux": "linux", "dos": "dos"}.get(platform, "app"), "grid-large-symbolic")
            type_label = {"linux": "Native App", "dos": "DOS App"}.get(platform, "Proton App")

        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(_ICON_SIZE)
        icon.set_halign(Gtk.Align.CENTER)
        icon.set_valign(Gtk.Align.CENTER)
        icon.set_margin_start(_ICON_MARGIN)
        icon.add_css_class("dim-label")
        card.append(icon)

        # Middle: name + type subtitle
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_hexpand(True)
        text_box.set_margin_start(_ICON_MARGIN)
        card.append(text_box)

        name_label = Gtk.Label(label=entry.name)
        name_label.add_css_class("heading")
        name_label.set_halign(Gtk.Align.START)
        name_label.set_ellipsize(Pango.EllipsizeMode.END)
        name_label.set_tooltip_text(entry.name)
        text_box.append(name_label)

        subtitle_text = (repo.name or repo.uri) if show_repo else type_label
        subtitle = Gtk.Label(label=subtitle_text)
        subtitle.add_css_class("dim-label")
        subtitle.set_halign(Gtk.Align.START)
        subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        text_box.append(subtitle)

        # Right: single actions menu button
        action_group = Gio.SimpleActionGroup()

        dl_action = Gio.SimpleAction.new("download", None)
        dl_action.connect("activate", lambda *_: on_download(self))
        action_group.add_action(dl_action)

        del_action = Gio.SimpleAction.new("delete", None)
        del_action.set_enabled(not has_dependants)
        del_action.connect("activate", lambda *_: on_delete(self))
        action_group.add_action(del_action)

        menu = Gio.Menu()
        if on_edit:
            edit_action = Gio.SimpleAction.new("edit", None)
            edit_action.connect("activate", lambda *_: on_edit(self))
            action_group.add_action(edit_action)
            menu.append("Edit metadata", "card.edit")
        menu.append("Download for editing", "card.download")
        del_label = (
            "Delete from catalogue" if not has_dependants else "Delete (base has dependants)"
        )
        menu.append(del_label, "card.delete")

        menu_btn = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu)
        menu_btn.add_css_class("flat")
        menu_btn.set_valign(Gtk.Align.CENTER)
        menu_btn.set_margin_end(8)
        card.append(menu_btn)

        # Dim icon + text but keep action button fully opaque
        icon.set_opacity(0.6)
        text_box.set_opacity(0.6)

        from cellar.views.browse import _FixedBox
        fixed = _FixedBox(_CARD_WIDTH, _CARD_HEIGHT, clip=False)
        fixed.set_child(card)
        self.set_child(fixed)
        self.insert_action_group("card", action_group)

    def matches(self, search: str, active_types: set[str], active_repos: set[str]) -> bool:
        """Return True if this card should be visible given the current filters."""
        if active_repos and self.repo.uri not in active_repos:
            return False
        if active_types:
            platform = getattr(self.entry, "platform", "windows")
            ft = _resolve_filter_type(self.kind, platform)
            if ft not in active_types:
                return False
        if search and search.lower() not in self.entry.name.lower():
            return False
        return True

    def do_dispose(self) -> None:
        from cellar.views.browse import _dispose_subtree
        child = self.get_first_child()
        if child is not None:
            _dispose_subtree(child)
        self.set_child(None)
        super().do_dispose()


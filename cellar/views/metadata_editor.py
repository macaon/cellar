"""Unified metadata editor dialog.

Can be opened from any entry point in the app:

- New project:       ``MetadataEditorDialog(context=ProjectContext(), on_created=cb)``
- Existing project:  ``MetadataEditorDialog(context=ProjectContext(project=p), on_changed=cb)``
- Catalogue entry:   ``MetadataEditorDialog(context=RepoContext(entry=e, repo=r), on_done=cb)``
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.utils.async_work import run_in_background
from cellar.utils.progress import fmt_stats as _fmt_stats
from cellar.views.builder.media_panel import MediaPanel
from cellar.views.widgets import make_progress_page, set_margins

log = logging.getLogger(__name__)

_STRATEGIES = ["safe", "full"]
_STRATEGY_LABELS = ["Safe (preserve user data)", "Full (complete replacement)"]


# ── Save contexts ─────────────────────────────────────────────────────────────

class _SaveContext(ABC):
    """Abstract base that encapsulates where metadata comes from and where it goes."""

    @property
    @abstractmethod
    def app_id(self) -> str:
        """App identifier shown in the read-only App ID row (empty in create mode)."""

    @property
    @abstractmethod
    def is_create(self) -> bool:
        """True when creating a brand-new project (no existing data)."""

    @property
    @abstractmethod
    def title_locked(self) -> bool:
        """True when the title field should be read-only."""

    @property
    @abstractmethod
    def project_type(self) -> str:
        """'app', 'linux', or 'base'."""

    @property
    @abstractmethod
    def show_launch_settings(self) -> bool:
        """True when the Launch Settings group (update strategy + targets) should be shown."""

    @property
    @abstractmethod
    def save_is_async(self) -> bool:
        """True when save() must run in a background thread (shows progress stack)."""

    @abstractmethod
    def get_categories(self) -> list[str]:
        """Return the list of available category strings."""

    @abstractmethod
    def get_fields(self) -> dict:
        """Return a dict of initial field values for pre-filling the form."""

    @abstractmethod
    def populate_media(self, media: "MediaPanel") -> None:
        """Load images and screenshots into the media panel."""

    @abstractmethod
    def save(
        self,
        fields: dict,
        images: dict,
        *,
        progress_cb: Callable | None = None,
        phase_cb: Callable | None = None,
        stats_cb: Callable | None = None,
        cancel_event: threading.Event | None = None,
    ):
        """Perform the save operation.

        For sync contexts (ProjectContext) this is called directly on the UI thread.
        For async contexts (RepoContext) this is called from a background thread.
        Returns the saved object (Project or AppEntry).
        """

    def cancel_revert(self) -> None:
        """Revert any in-memory changes when the user cancels. No-op by default."""


class ProjectContext(_SaveContext):
    """Save context for local project.json files (builder new/edit)."""

    def __init__(
        self,
        *,
        project=None,       # cellar.backend.project.Project | None
        project_type: str = "app",
    ) -> None:
        self._project = project
        self._project_type = project.project_type if project is not None else project_type
        self._snapshot: dict | None = project.to_dict() if project is not None else None

    @property
    def app_id(self) -> str:
        return self._project.slug if self._project else ""

    @property
    def is_create(self) -> bool:
        return self._project is None

    @property
    def title_locked(self) -> bool:
        return bool(self._project and self._project.initialized)

    @property
    def project_type(self) -> str:
        return self._project_type

    @property
    def show_launch_settings(self) -> bool:
        return False

    @property
    def save_is_async(self) -> bool:
        return False

    def get_categories(self) -> list[str]:
        from cellar.backend.packager import BASE_CATEGORIES
        return list(BASE_CATEGORIES)

    def get_fields(self) -> dict:
        p = self._project
        if not p:
            return {
                "name": "",
                "version": "1.0",
                "category": "Other",
                "developer": "",
                "publisher": "",
                "release_year": "",
                "steam_appid": "",
                "website": "",
                "genres": "",
                "summary": "",
                "description": "",
                "update_strategy": "safe",
                "launch_targets": [],
            }
        return {
            "name": p.name,
            "version": p.version or "1.0",
            "category": p.category or "Other",
            "developer": p.developer or "",
            "publisher": p.publisher or "",
            "release_year": str(p.release_year) if p.release_year else "",
            "steam_appid": str(p.steam_appid) if p.steam_appid is not None else "",
            "website": p.website or "",
            "genres": ", ".join(p.genres) if p.genres else "",
            "summary": p.summary or "",
            "description": p.description or "",
            "update_strategy": "safe",
            "launch_targets": [],
        }

    def populate_media(self, media: "MediaPanel") -> None:
        p = self._project
        if not p:
            return
        media.set_images(
            p.icon_path or "", p.cover_path or "", p.logo_path or "",
            bool(p.hide_title),
        )
        source_urls = (
            [p.screenshot_sources.get(sp) for sp in p.screenshot_paths]
            if p.screenshot_sources else None
        )
        media.set_screenshots_local(list(p.screenshot_paths), source_urls)
        if p.steam_screenshots:
            media.add_steam_screenshots(p.steam_screenshots)
            if p.selected_steam_urls:
                media.select_steam_by_urls(set(p.selected_steam_urls))

    def save(self, fields, images, *, progress_cb=None, phase_cb=None,
             stats_cb=None, cancel_event=None):
        from cellar.backend.project import create_project, save_project

        p = self._project
        if p is None:
            p = create_project(fields["name"], self._project_type)
        else:
            if not self.title_locked:
                p.name = fields.get("name", p.name)

        p.version = fields.get("version") or "1.0"
        p.category = fields.get("category", "")
        p.developer = fields.get("developer", "")
        p.publisher = fields.get("publisher", "")
        year_txt = fields.get("release_year", "")
        try:
            p.release_year = int(year_txt) if year_txt else None
        except ValueError:
            p.release_year = None
        steam_txt = fields.get("steam_appid", "")
        p.steam_appid = int(steam_txt) if steam_txt.isdigit() else None
        p.website = fields.get("website", "")
        genres_txt = fields.get("genres", "")
        p.genres = [g.strip() for g in genres_txt.split(",") if g.strip()] if genres_txt else []
        p.summary = fields.get("summary", "")
        p.description = fields.get("description", "") or p.summary
        p.hide_title = bool(images.get("hide_title", False))

        # Single images: None=keep existing, ""=clear, str=new path
        icon = images.get("icon")
        if icon is not None:
            p.icon_path = self._import_image(p, icon, "icon") if icon else ""
        cover = images.get("cover")
        if cover is not None:
            p.cover_path = self._import_image(p, cover, "cover") if cover else ""
        logo = images.get("logo")
        if logo is not None:
            p.logo_path = self._import_image(p, logo, "logo") if logo else ""

        # Screenshots — always collect all current items
        all_items = images.get("all_screenshot_items", [])
        p.screenshot_paths = [i.local_path for i in all_items if i.local_path]
        p.screenshot_sources = {
            i.local_path: i.source_url
            for i in all_items
            if i.local_path and i.source_url
        }
        all_steam = images.get("all_steam_items", [])
        p.steam_screenshots = [
            {"full": i.full_url, "thumbnail": i.thumb_url or ""}
            for i in all_steam if i.full_url
        ]
        p.selected_steam_urls = [
            i.full_url for i in all_items if i.is_steam and i.full_url
        ]

        # Rename project directory if the title changed and it's not locked.
        if self._project is not None and not self.title_locked:
            self._maybe_rename_project(p)

        save_project(p)
        self._project = p
        return p

    @staticmethod
    def _maybe_rename_project(p) -> None:
        """Move the project directory to match a changed title, if possible."""
        import shutil as _shutil

        from cellar.backend.packager import slugify
        from cellar.backend.umu import projects_dir

        new_slug = slugify(p.name)
        if not new_slug or new_slug == p.slug:
            return
        new_dir = projects_dir() / new_slug
        if new_dir.exists():
            return  # collision — keep old slug silently
        try:
            _shutil.move(str(p.project_dir), str(new_dir))
            p.slug = new_slug
        except OSError as exc:
            log.warning("Could not rename project directory: %s", exc)

    @staticmethod
    def _import_image(project, src_path: str, slot: str) -> str:
        import shutil
        src = Path(src_path)
        project.project_dir.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() in (".ico", ".bmp"):
            from cellar.utils.images import Image
            dest = project.project_dir / f"{slot}.png"
            with Image.open(src) as img:
                img.convert("RGBA").save(dest, format="PNG")
            return str(dest)
        dest = project.project_dir / f"{slot}{src.suffix}"
        if src.resolve() != dest.resolve():
            shutil.copy2(src, dest)
        return str(dest)

    def cancel_revert(self) -> None:
        if self._project and self._snapshot:
            from cellar.backend.project import Project
            restored = Project.from_dict(self._snapshot)
            for attr in (
                "name", "version", "category", "developer", "publisher",
                "release_year", "website", "genres", "summary", "description",
                "steam_appid", "icon_path", "cover_path", "logo_path",
                "hide_title", "screenshot_paths", "screenshot_sources",
                "steam_screenshots", "selected_steam_urls",
            ):
                setattr(self._project, attr, getattr(restored, attr))


class RepoContext(_SaveContext):
    """Save context for catalogue entries in a writable Repo."""

    def __init__(self, *, entry, repo) -> None:
        self._entry = entry
        self._repo = repo

    @property
    def app_id(self) -> str:
        return self._entry.id

    @property
    def is_create(self) -> bool:
        return False

    @property
    def title_locked(self) -> bool:
        return False

    @property
    def project_type(self) -> str:
        return getattr(self._entry, "platform", "app") or "app"

    @property
    def show_launch_settings(self) -> bool:
        return True

    @property
    def save_is_async(self) -> bool:
        return True

    def get_categories(self) -> list[str]:
        from cellar.backend.packager import BASE_CATEGORIES
        return list(BASE_CATEGORIES)

    def get_fields(self) -> dict:
        e = self._entry
        return {
            "name": e.name,
            "version": e.version or "",
            "category": e.category or "",
            "developer": e.developer or "",
            "publisher": e.publisher or "",
            "release_year": str(e.release_year) if e.release_year else "",
            "steam_appid": str(e.steam_appid) if e.steam_appid is not None else "",
            "website": e.website or "",
            "genres": ", ".join(e.genres) if e.genres else "",
            "summary": e.summary or "",
            "description": e.description or "",
            "update_strategy": e.update_strategy or "safe",
            "launch_targets": [dict(t) for t in e.launch_targets],
            "dxvk": e.dxvk,
            "vkd3d": e.vkd3d,
            "debug": e.debug,
            "direct_proton": e.direct_proton,
            "audio_driver": e.audio_driver,
        }

    def populate_media(self, media: "MediaPanel") -> None:
        import os as _os
        e = self._entry

        # Set subtitles immediately (no I/O)
        media.set_image_subtitles(e.icon, e.cover, e.logo, bool(e.hide_title))

        peek = self._repo.peek_asset_cache

        def _peek_or_none(rel: str) -> str | None:
            p = peek(rel) if rel else None
            return p if (p and _os.path.isfile(p)) else None

        # Synchronous cache peek for single images
        for key, rel in [("icon", e.icon), ("cover", e.cover), ("logo", e.logo)]:
            if rel:
                cached = _peek_or_none(rel)
                if cached:
                    media.set_thumbnail(key, cached)

        # Screenshots: try cache first, collect misses for background fetch
        _ss_rels = list(e.screenshots)
        _ss_cached: list[str] = []
        _ss_missing: list[tuple[int, str]] = []
        for i, rel in enumerate(_ss_rels):
            cached = _peek_or_none(rel)
            if cached:
                _ss_cached.append(cached)
            else:
                _ss_missing.append((i, rel))

        if _ss_cached:
            _ss_source_urls = [e.screenshot_sources.get(r) for r in _ss_rels if _peek_or_none(r)]
            media.set_screenshots_local(_ss_cached, _ss_source_urls)

        _uncached_single = {
            k: rel for k, rel in [("icon", e.icon), ("cover", e.cover), ("logo", e.logo)]
            if rel and not _peek_or_none(rel)
        }

        if _uncached_single or _ss_missing:
            def _resolve_missing():
                singles = {}
                for key, rel in _uncached_single.items():
                    try:
                        singles[key] = self._repo.resolve_asset_uri(rel)
                    except Exception:
                        pass
                extra_ss: list[tuple[int, str]] = []
                for idx, rel in _ss_missing:
                    try:
                        p = self._repo.resolve_asset_uri(rel)
                        if p:
                            extra_ss.append((idx, p))
                    except Exception:
                        pass
                return singles, extra_ss

            def _on_missing_resolved(res):
                singles, extra_ss = res
                for key, path in singles.items():
                    media.set_thumbnail(key, path)
                if extra_ss:
                    merged = list(_ss_cached)
                    for _idx, path in extra_ss:
                        merged.append(path)
                    merged_sources = [e.screenshot_sources.get(r) for r in _ss_rels]
                    media.set_screenshots_local(merged, merged_sources)

            run_in_background(_resolve_missing, on_done=_on_missing_resolved)

    def save(self, fields, images, *, progress_cb=None, phase_cb=None,
             stats_cb=None, cancel_event=None):
        """Run in a background thread. Returns final AppEntry."""
        import tempfile as _tmp
        from dataclasses import replace as _dc_replace

        from cellar.backend.packager import update_in_repo
        from cellar.utils.http import make_session as _make_session

        e = self._entry
        app_id = e.id

        name = fields.get("name", e.name)
        version = fields.get("version") or e.version
        category = fields.get("category") or e.category
        summary = fields.get("summary", "")
        description = fields.get("description", "")
        developer = fields.get("developer", "")
        publisher = fields.get("publisher", "")
        year_text = fields.get("release_year", "")
        release_year = int(year_text) if year_text.isdigit() else None
        steam_appid_text = fields.get("steam_appid", "")
        steam_appid = int(steam_appid_text) if steam_appid_text.isdigit() else None
        website = fields.get("website", "")
        genres_text = fields.get("genres", "")
        genres = (
            tuple(g.strip() for g in genres_text.split(",") if g.strip()) if genres_text else ()
        )
        strategy = fields.get("update_strategy", "safe")
        launch_targets = tuple(fields.get("launch_targets", []))
        dxvk = bool(fields.get("dxvk", True))
        vkd3d = bool(fields.get("vkd3d", True))
        debug = bool(fields.get("debug", False))
        direct_proton = bool(fields.get("direct_proton", False))
        audio_driver = fields.get("audio_driver", "auto")
        hide_title = bool(images.get("hide_title", e.hide_title))

        icon_path = images.get("icon")    # None=keep, ""=clear, str=new
        cover_path = images.get("cover")
        logo_path = images.get("logo")

        if icon_path is None:
            icon_rel = e.icon
        elif icon_path == "":
            icon_rel = ""
        else:
            sfx = Path(icon_path).suffix.lower()
            ext = ".png" if sfx in (".ico", ".bmp") else Path(icon_path).suffix
            icon_rel = f"apps/{app_id}/icon{ext}"

        if cover_path is None:
            cover_rel = e.cover
        elif cover_path == "":
            cover_rel = ""
        else:
            cover_rel = f"apps/{app_id}/cover{Path(cover_path).suffix}"

        if logo_path is None:
            logo_rel = e.logo
        elif logo_path == "":
            logo_rel = ""
        else:
            logo_rel = f"apps/{app_id}/logo.png"

        screenshot_rels = e.screenshots
        grid_items = images.get("screenshot_items")       # None if not dirty
        excluded_locals = images.get("excluded_locals", [])

        new_entry = _dc_replace(
            e,
            name=name,
            version=version,
            category=category,
            summary=summary,
            description=description,
            developer=developer,
            publisher=publisher,
            release_year=release_year,
            icon=icon_rel,
            cover=cover_rel,
            logo=logo_rel,
            hide_title=hide_title,
            screenshots=screenshot_rels,
            website=website,
            genres=genres,
            update_strategy=strategy,
            launch_targets=launch_targets,
            steam_appid=steam_appid,
            dxvk=dxvk,
            vkd3d=vkd3d,
            debug=debug,
            direct_proton=direct_proton,
            audio_driver=audio_driver,
        )

        repo_images = {
            "icon": icon_path,
            "cover": cover_path,
            "logo": logo_path,
            "screenshots": None,
        }

        _run_entry = new_entry

        if grid_items is not None:
            if phase_cb:
                phase_cb("Downloading screenshots\u2026")
            dl_dir = Path(_tmp.mkdtemp(prefix="cellar_ss_"))
            final_paths: list[str] = []
            final_sources: list[str | None] = []
            _session = _make_session()
            for _item in grid_items:
                if _item.local_path:
                    final_paths.append(_item.local_path)
                    final_sources.append(_item.source_url)
                elif _item.full_url:
                    _fname = _item.full_url.split("/")[-1].split("?")[0] or "screenshot.jpg"
                    _dest = dl_dir / _fname
                    try:
                        _r = _session.get(_item.full_url, timeout=30)
                        _r.raise_for_status()
                        _dest.write_bytes(_r.content)
                        final_paths.append(str(_dest))
                        final_sources.append(_item.full_url)
                    except Exception as exc:
                        log.warning("Screenshot download failed: %s", exc)
            repo_images["screenshots"] = final_paths
            ss_rels = tuple(
                f"apps/{app_id}/screenshots/ss_placeholder_{i:03d}{Path(p).suffix}"
                for i, p in enumerate(final_paths)
            )
            ss_sources = {
                rel: src
                for rel, src in zip(ss_rels, final_sources)
                if src
            }
            _run_entry = _dc_replace(new_entry, screenshots=ss_rels, screenshot_sources=ss_sources)

        repo_root = self._repo.writable_path()
        final_entry = update_in_repo(
            repo_root,
            e,
            _run_entry,
            repo_images,
            progress_cb=progress_cb,
            phase_cb=phase_cb,
            stats_cb=stats_cb,
            cancel_event=cancel_event,
        )
        if final_entry is not None:
            _run_entry = final_entry

        if excluded_locals:
            new_rels_set = set(_run_entry.screenshots)
            for _excl in excluded_locals:
                if not _excl.local_path:
                    continue
                for old_rel in e.screenshots:
                    if str(repo_root / old_rel) == _excl.local_path:
                        if old_rel not in new_rels_set:
                            try:
                                (repo_root / old_rel).unlink(missing_ok=True)
                            except Exception:
                                pass
                        break

        if grid_items is not None:
            for _rel in e.screenshots:
                self._repo.evict_asset_cache(_rel)

        return _run_entry


# ── Dialog ────────────────────────────────────────────────────────────────────

class MetadataEditorDialog(Adw.Dialog):
    """Unified metadata editor dialog for all entry points in the app."""

    def __init__(
        self,
        *,
        context: _SaveContext,
        on_created: Callable | None = None,
        on_changed: Callable | None = None,
        on_done: Callable | None = None,
        auto_steam_query: str = "",
        auto_version: str = "",
    ) -> None:
        _new_titles = {"linux": "New Linux App", "base": "New Base Image"}
        if context.is_create:
            title = _new_titles.get(context.project_type, "New App")
        elif isinstance(context, RepoContext):
            title = "Edit Catalogue Entry"
        else:
            title = "Details"

        super().__init__(title=title, content_width=1100, content_height=680)

        self._context = context
        self._on_created = on_created
        self._on_changed = on_changed
        self._on_done = on_done
        self._cancel_event = threading.Event()
        self._screenshots_dirty = False
        self._locally_installed = self._check_locally_installed()
        self._saved_result = None
        self._auto_steam_query = auto_steam_query
        self._auto_version = auto_version

        # Launch target state (used only when context.show_launch_settings)
        self._launch_targets: list[dict] = []
        self._target_rows: list[Adw.ExpanderRow] = []
        self._targets_group: Adw.PreferencesGroup | None = None
        self._add_target_row_widget: Adw.ActionRow | None = None

        self._build_ui()

        # Pre-fill version from smart import (gameinfo, filename, etc.)
        if auto_version and hasattr(self, "_version_row"):
            self._version_row.set_text(auto_version)

        # Auto-open Steam picker after dialog is presented
        if auto_steam_query:
            GLib.idle_add(self._auto_open_steam_picker)

    def _check_locally_installed(self) -> bool:
        if not isinstance(self._context, RepoContext):
            return False
        e = self._context._entry
        try:
            if e.platform == "linux":
                from cellar.backend.database import get_installed
                rec = get_installed(e.id)
                return bool(rec and rec.get("install_path"))
            else:
                from cellar.backend.umu import prefixes_dir
                return (prefixes_dir() / e.id / "drive_c").is_dir()
        except Exception:
            return False

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        ctx = self._context
        fields = ctx.get_fields()
        categories = ctx.get_categories()

        # Header
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", self._on_cancel_clicked)
        header.pack_start(cancel_btn)

        if ctx.is_create:
            action_label = "Create"
            action_sensitive = False
        elif ctx.save_is_async:
            action_label = "Save Changes"
            action_sensitive = bool(fields.get("name", ""))
        else:
            action_label = "Done"
            action_sensitive = True

        self._action_btn = Gtk.Button(label=action_label)
        self._action_btn.add_css_class("suggested-action")
        self._action_btn.set_sensitive(action_sensitive)
        self._action_btn.connect("clicked", self._on_action_clicked)
        header.pack_end(self._action_btn)

        toolbar.add_top_bar(header)

        # Stack
        self._stack = Gtk.Stack()
        self._stack.add_named(self._build_form(fields, categories), "form")
        self._stack.add_named(self._build_progress(), "progress")
        self._stack.add_named(self._build_spinner(), "spinner")
        self._stack.set_visible_child_name("form")

        toolbar.set_content(self._stack)
        self.set_child(toolbar)

        # Pre-fill launch targets and update strategy (RepoContext only)
        if ctx.show_launch_settings:
            for t in fields.get("launch_targets", []):
                self._add_target_row_ui(dict(t))
            strategy = fields.get("update_strategy", "safe")
            if strategy in _STRATEGIES:
                self._strategy_row.set_selected(_STRATEGIES.index(strategy))
            self._dxvk_row.set_active(bool(fields.get("dxvk", True)))
            self._vkd3d_row.set_active(bool(fields.get("vkd3d", True)))
            self._debug_row.set_active(bool(fields.get("debug", False)))
            self._direct_proton_row.set_active(bool(fields.get("direct_proton", False)))
            audio = fields.get("audio_driver", "auto")
            if audio in self._AUDIO_VALUES:
                self._audio_driver_row.set_selected(self._AUDIO_VALUES.index(audio))

        # Wire steam appid → media panel
        self._steam_row.connect("changed", self._on_steam_appid_changed)
        self._on_steam_appid_changed(self._steam_row)

        # Populate media panel
        ctx.populate_media(self._media)

    def _build_form(self, fields: dict, categories: list[str]) -> Gtk.Widget:
        ctx = self._context

        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        scroll.set_child(hbox)

        # ── Left column: metadata (fixed width) ──────────────────────────
        left_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        left_box.set_size_request(360, -1)
        left_box.set_hexpand(False)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        set_margins(page, 12)
        page.set_vexpand(True)
        page.set_hexpand(False)
        left_box.append(page)
        hbox.append(left_box)

        # Identity
        id_group = Adw.PreferencesGroup()

        if ctx.title_locked:
            self._title_widget = Adw.ActionRow(title="Title")
            self._title_widget.set_subtitle(fields.get("name", ""))
            self._title_widget.add_css_class("property")
            self._locked_name = fields.get("name", "")
        else:
            self._title_widget = Adw.EntryRow(title="Title *" if ctx.save_is_async else "Title")
            self._title_widget.set_text(fields.get("name", ""))
            self._title_widget.connect("changed", self._on_title_changed)
            steam_btn = Gtk.Button(icon_name="system-search-symbolic")
            steam_btn.add_css_class("flat")
            steam_btn.set_valign(Gtk.Align.CENTER)
            steam_btn.set_tooltip_text("Look up on Steam")
            steam_btn.connect("clicked", self._on_steam_lookup)
            self._title_widget.add_suffix(steam_btn)
            self._locked_name = ""
        id_group.add(self._title_widget)

        self._slug_row = Adw.ActionRow(title="App ID", subtitle=ctx.app_id)
        self._slug_row.add_css_class("property")
        if ctx.save_is_async:
            self._slug_row.set_subtitle_selectable(True)
        id_group.add(self._slug_row)

        page.append(id_group)

        # Details
        det_group = Adw.PreferencesGroup(title="Details")

        self._version_row = Adw.EntryRow(title="Version")
        self._version_row.set_text(fields.get("version", "1.0"))
        det_group.add(self._version_row)

        self._cats = categories
        self._cat_row = Adw.ComboRow(title="Category")
        self._cat_row.set_model(Gtk.StringList.new(self._cats))
        cat_val = fields.get("category", "Other")
        if cat_val in self._cats:
            self._cat_row.set_selected(self._cats.index(cat_val))
        det_group.add(self._cat_row)

        self._dev_row = Adw.EntryRow(title="Developer")
        self._dev_row.set_text(fields.get("developer", ""))
        det_group.add(self._dev_row)

        self._pub_row = Adw.EntryRow(title="Publisher")
        self._pub_row.set_text(fields.get("publisher", ""))
        det_group.add(self._pub_row)

        self._year_row = Adw.EntryRow(title="Release Year")
        self._year_row.set_text(fields.get("release_year", ""))
        det_group.add(self._year_row)

        self._steam_row = Adw.EntryRow(title="Steam App ID")
        self._steam_row.set_tooltip_text("Used for protonfixes. Leave empty for GAMEID=0.")
        self._steam_row.set_text(fields.get("steam_appid", ""))
        det_group.add(self._steam_row)

        self._website_row = Adw.EntryRow(title="Website")
        self._website_row.set_text(fields.get("website", ""))
        det_group.add(self._website_row)

        self._genres_row = Adw.EntryRow(title="Genres")
        self._genres_row.set_tooltip_text("Comma-separated, e.g. Action, RPG")
        self._genres_row.set_text(fields.get("genres", ""))
        det_group.add(self._genres_row)

        page.append(det_group)

        # Descriptions
        desc_group = Adw.PreferencesGroup(title="Descriptions")

        desc_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        desc_outer.add_css_class("card")

        self._summary_row = Adw.EntryRow(title="Summary")
        self._summary_row.set_text(fields.get("summary", ""))
        desc_outer.append(self._summary_row)

        desc_outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Description header with HTML formatting toolbar
        desc_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        desc_header.set_margin_top(8)
        desc_header.set_margin_bottom(4)
        desc_header.set_margin_start(12)
        desc_header.set_margin_end(6)
        desc_label = Gtk.Label(label="Description")
        desc_label.set_hexpand(True)
        desc_label.set_xalign(0)
        desc_header.append(desc_label)

        fmt_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        bold_btn = Gtk.Button(label="B")
        bold_btn.add_css_class("flat")
        bold_btn.set_tooltip_text("Bold (<b>text</b>)")
        bold_btn.connect("clicked", lambda _: self._desc_fmt_wrap("b"))
        italic_btn = Gtk.Button(label="I")
        italic_btn.add_css_class("flat")
        italic_btn.set_tooltip_text("Italic (<i>text</i>)")
        italic_btn.connect("clicked", lambda _: self._desc_fmt_wrap("i"))
        h2_btn = Gtk.Button(label="H2")
        h2_btn.add_css_class("flat")
        h2_btn.set_tooltip_text("Heading (<h2>text</h2>)")
        h2_btn.connect("clicked", lambda _: self._desc_fmt_wrap("h2"))
        bullet_btn = Gtk.Button(icon_name="view-list-bullet-symbolic")
        bullet_btn.add_css_class("flat")
        bullet_btn.set_tooltip_text("Bullet list (<li>item</li>)")
        bullet_btn.connect("clicked", lambda _: self._desc_fmt_bullet())
        hr_btn = Gtk.Button(label="\u2014")
        hr_btn.add_css_class("flat")
        hr_btn.set_tooltip_text("Horizontal rule (<hr>)")
        hr_btn.connect("clicked", lambda _: self._desc_fmt_hr())
        fmt_box.append(bold_btn)
        fmt_box.append(italic_btn)
        fmt_box.append(h2_btn)
        fmt_box.append(bullet_btn)
        fmt_box.append(hr_btn)
        desc_header.append(fmt_box)
        desc_outer.append(desc_header)

        desc_outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        self._desc_view = Gtk.TextView()
        self._desc_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._desc_view.set_margin_top(8)
        self._desc_view.set_margin_bottom(8)
        self._desc_view.set_margin_start(12)
        self._desc_view.set_margin_end(12)
        self._desc_view.set_size_request(-1, 100)
        self._desc_view.get_buffer().set_text(fields.get("description", ""))
        desc_outer.append(self._desc_view)

        desc_group.add(desc_outer)
        page.append(desc_group)

        # Launch Settings (RepoContext only)
        if ctx.show_launch_settings:
            launch_group = Adw.PreferencesGroup(title="Launch Settings")

            self._strategy_row = Adw.ComboRow(title="Update Strategy")
            strat_model = Gtk.StringList()
            for lbl in _STRATEGY_LABELS:
                strat_model.append(lbl)
            self._strategy_row.set_model(strat_model)
            launch_group.add(self._strategy_row)

            self._dxvk_row = Adw.SwitchRow(
                title="DXVK",
                subtitle="Translate D3D9/10/11 to Vulkan",
            )
            self._dxvk_row.set_active(True)
            launch_group.add(self._dxvk_row)

            self._vkd3d_row = Adw.SwitchRow(
                title="VKD3D-Proton",
                subtitle="Translate D3D12 to Vulkan",
            )
            self._vkd3d_row.set_active(True)
            launch_group.add(self._vkd3d_row)

            self._debug_row = Adw.SwitchRow(
                title="Proton Debug Logging",
                subtitle="Enable PROTON_LOG=1 when launching",
            )
            launch_group.add(self._debug_row)

            self._direct_proton_row = Adw.SwitchRow(
                title="Direct Proton Launch",
                subtitle="Bypass umu-run and call Proton directly",
            )
            launch_group.add(self._direct_proton_row)

            self._AUDIO_LABELS = ("Auto", "PulseAudio", "ALSA", "OSS")
            self._AUDIO_VALUES = ("auto", "pulseaudio", "alsa", "oss")
            self._audio_driver_row = Adw.ComboRow(
                title="Audio Driver",
                subtitle="Wine audio backend override",
            )
            audio_model = Gtk.StringList()
            for lbl in self._AUDIO_LABELS:
                audio_model.append(lbl)
            self._audio_driver_row.set_model(audio_model)
            launch_group.add(self._audio_driver_row)

            add_target_row = Adw.ActionRow(title="Add Launch Target\u2026")
            add_btn = Gtk.Button(label="Add\u2026", valign=Gtk.Align.CENTER)
            add_btn.add_css_class("flat")
            add_btn.connect("clicked", self._on_add_target_clicked)
            add_target_row.add_suffix(add_btn)
            add_target_row.set_activatable_widget(add_btn)
            self._add_target_row_widget = add_target_row
            self._targets_group = launch_group
            launch_group.add(add_target_row)

            page.append(launch_group)

        # ── Vertical separator ────────────────────────────────────────────
        hbox.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        # ── Right column: media panel ─────────────────────────────────────
        self._media = MediaPanel(on_changed=self._on_screenshots_changed)
        hbox.append(self._media)

        return scroll

    def _build_progress(self) -> Gtk.Widget:
        box, self._progress_label, self._progress_bar, self._cancel_progress_btn = (
            make_progress_page("Saving changes\u2026", self._on_cancel_progress_clicked)
        )
        return box

    def _build_spinner(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_top(48)
        box.set_margin_bottom(48)
        box.set_margin_start(24)
        box.set_margin_end(24)

        spinner = Gtk.Spinner(spinning=True)
        spinner.set_size_request(32, 32)
        spinner.set_halign(Gtk.Align.CENTER)

        self._spinner_label = Gtk.Label(label="Saving\u2026")
        self._spinner_label.add_css_class("dim-label")

        self._cancel_spinner_btn = Gtk.Button(label="Cancel")
        self._cancel_spinner_btn.set_halign(Gtk.Align.CENTER)
        self._cancel_spinner_btn.connect("clicked", self._on_cancel_progress_clicked)

        box.append(spinner)
        box.append(self._spinner_label)
        box.append(self._cancel_spinner_btn)
        return box

    # ── Field collection ──────────────────────────────────────────────────

    def _collect_fields(self) -> dict:
        ctx = self._context
        cat_idx = self._cat_row.get_selected()

        buf = self._desc_view.get_buffer()
        description = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()

        if isinstance(self._title_widget, Adw.EntryRow):
            name = self._title_widget.get_text().strip()
        else:
            name = self._locked_name

        fields = {
            "name": name,
            "version": self._version_row.get_text().strip(),
            "category": self._cats[cat_idx] if 0 <= cat_idx < len(self._cats) else "",
            "developer": self._dev_row.get_text().strip(),
            "publisher": self._pub_row.get_text().strip(),
            "release_year": self._year_row.get_text().strip(),
            "steam_appid": self._steam_row.get_text().strip(),
            "website": self._website_row.get_text().strip(),
            "genres": self._genres_row.get_text().strip(),
            "summary": self._summary_row.get_text().strip(),
            "description": description,
        }

        if ctx.show_launch_settings:
            fields["launch_targets"] = list(self._launch_targets)
            fields["update_strategy"] = _STRATEGIES[self._strategy_row.get_selected()]
            fields["dxvk"] = self._dxvk_row.get_active()
            fields["vkd3d"] = self._vkd3d_row.get_active()
            fields["debug"] = self._debug_row.get_active()
            fields["direct_proton"] = self._direct_proton_row.get_active()
            fields["audio_driver"] = self._AUDIO_VALUES[self._audio_driver_row.get_selected()]

        return fields

    def _collect_images(self) -> dict:
        grid = self._media.screenshot_grid
        icon = self._media.get_icon_path() if self._media.icon_changed else None
        cover = self._media.get_cover_path() if self._media.cover_changed else None
        logo = self._media.get_logo_path() if self._media.logo_changed else None

        all_items = grid.get_items()
        all_steam = grid.get_all_steam_items()

        return {
            "icon": icon,
            "cover": cover,
            "logo": logo,
            "hide_title": self._media.get_hide_title(),
            # Always-available (ProjectContext uses these)
            "all_screenshot_items": all_items,
            "all_steam_items": all_steam,
            # Dirty-gated (RepoContext uses these)
            "screenshot_items": all_items if self._screenshots_dirty else None,
            "excluded_locals": (
                grid.get_excluded_local_items() if self._screenshots_dirty else []
            ),
        }

    # ── Action button ─────────────────────────────────────────────────────

    def _on_action_clicked(self, _btn) -> None:
        ctx = self._context
        fields = self._collect_fields()
        images = self._collect_images()

        if ctx.save_is_async:
            self._do_async_save(fields, images)
        else:
            is_create = ctx.is_create  # capture before save() mutates ctx state
            result = ctx.save(fields, images)
            self._saved_result = result
            self.close()
            if is_create and self._on_created:
                self._on_created(result)
            elif self._on_changed:
                self._on_changed()

    def _do_async_save(self, fields: dict, images: dict) -> None:
        self._cancel_event.clear()
        self._stack.set_visible_child_name("progress")
        self._progress_bar.set_fraction(0.0)
        self._progress_label.set_text("Saving changes\u2026")

        ctx = self._context
        _last_stats_t = [0.0]

        def _phase(label: str) -> None:
            GLib.idle_add(self._progress_label.set_text, label)
            GLib.idle_add(self._progress_bar.set_text, "")

        def _stats(copied: int, total: int, speed: float) -> None:
            now = time.monotonic()
            if now - _last_stats_t[0] >= 0.1:
                _last_stats_t[0] = now
                GLib.idle_add(self._progress_bar.set_text, _fmt_stats(copied, total, speed))

        def _progress(fraction: float) -> None:
            GLib.idle_add(self._progress_bar.set_fraction, fraction)

        def _run():
            from cellar.backend.packager import CancelledError
            try:
                result = ctx.save(
                    fields, images,
                    progress_cb=_progress,
                    phase_cb=_phase,
                    stats_cb=_stats,
                    cancel_event=self._cancel_event,
                )
                self._saved_result = result
                GLib.idle_add(self._on_save_done)
            except CancelledError:
                GLib.idle_add(self._on_save_cancelled)
            except Exception as exc:
                GLib.idle_add(self._on_save_error, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _on_cancel_progress_clicked(self, _btn) -> None:
        self._cancel_event.set()
        self._progress_label.set_text("Cancelling\u2026")
        self._cancel_progress_btn.set_sensitive(False)

    def _on_save_done(self) -> None:
        self.close()
        if self._on_done:
            self._on_done(self._saved_result)

    def _on_save_cancelled(self) -> None:
        self._stack.set_visible_child_name("form")
        self._cancel_progress_btn.set_sensitive(True)

    def _on_save_error(self, message: str) -> None:
        self._stack.set_visible_child_name("form")
        self._cancel_progress_btn.set_sensitive(True)
        alert = Adw.AlertDialog(heading="Save Failed", body=message)
        alert.add_response("ok", "OK")
        alert.present(self)

    # ── Cancel ────────────────────────────────────────────────────────────

    def _on_cancel_clicked(self, _btn) -> None:
        self._context.cancel_revert()
        self.close()

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_title_changed(self, row) -> None:
        ctx = self._context
        title = row.get_text().strip()
        if ctx.is_create:
            from cellar.backend.packager import slugify
            self._slug_row.set_subtitle(slugify(title) if title else "")
            self._action_btn.set_sensitive(bool(title))
        elif ctx.save_is_async:
            self._action_btn.set_sensitive(bool(title))

    def _on_steam_appid_changed(self, _row) -> None:
        steam_txt = self._steam_row.get_text().strip()
        appid = int(steam_txt) if steam_txt.isdigit() else None
        self._media.set_steam_appid(appid)

    def _on_screenshots_changed(self) -> None:
        self._screenshots_dirty = True

    # ── Steam lookup ──────────────────────────────────────────────────────

    def _on_steam_lookup(self, _btn) -> None:
        from cellar.views.steam_picker import SteamPickerDialog
        if isinstance(self._title_widget, Adw.EntryRow):
            query = self._title_widget.get_text().strip()
        else:
            query = self._locked_name
        picker = SteamPickerDialog(query=query, on_picked=self._apply_steam_result)
        picker.present(self.get_root())

    def _auto_open_steam_picker(self) -> None:
        """Open the Steam picker automatically with a pre-filled query (smart import)."""
        from cellar.views.steam_picker import SteamPickerDialog
        picker = SteamPickerDialog(
            query=self._auto_steam_query,
            on_picked=self._apply_steam_result,
        )
        picker.present(self.get_root())
        self._auto_steam_query = ""  # only auto-open once

    def _apply_steam_result(self, result: dict) -> None:
        log.debug("Steam result: year=%r", result.get("year"))
        if result.get("name") and isinstance(self._title_widget, Adw.EntryRow):
            self._title_widget.set_text(result["name"])
        if result.get("developer"):
            self._dev_row.set_text(result["developer"])
        if result.get("publisher"):
            self._pub_row.set_text(result["publisher"])
        if result.get("year"):
            self._year_row.set_text(str(result["year"]))
        if result.get("website"):
            self._website_row.set_text(result["website"])
        if result.get("genres"):
            genres = result["genres"]
            self._genres_row.set_text(
                ", ".join(genres) if isinstance(genres, list) else str(genres)
            )
        if result.get("summary"):
            self._summary_row.set_text(result["summary"])
            self._desc_view.get_buffer().set_text(result["summary"])
        if result.get("steam_appid"):
            self._steam_row.set_text(str(result["steam_appid"]))
        if result.get("category") and result["category"] in self._cats:
            self._cat_row.set_selected(self._cats.index(result["category"]))
        if result.get("screenshots"):
            self._media.replace_steam_screenshots(result["screenshots"])

    # ── Description formatting ────────────────────────────────────────────

    def _desc_fmt_wrap(self, tag: str) -> None:
        buf = self._desc_view.get_buffer()
        buf.begin_user_action()
        if buf.get_has_selection():
            start, end = buf.get_selection_bounds()
            text = buf.get_text(start, end, False)
            buf.delete(start, end)
            buf.insert(buf.get_iter_at_mark(buf.get_insert()), f"<{tag}>{text}</{tag}>")
        else:
            buf.insert_at_cursor(f"<{tag}></{tag}>")
        buf.end_user_action()

    def _desc_fmt_bullet(self) -> None:
        buf = self._desc_view.get_buffer()
        buf.begin_user_action()
        if buf.get_has_selection():
            start, end = buf.get_selection_bounds()
            text = buf.get_text(start, end, False)
            buf.delete(start, end)
            buf.insert(buf.get_iter_at_mark(buf.get_insert()), f"<li>{text}</li>")
        else:
            buf.insert_at_cursor("<li></li>")
        buf.end_user_action()

    def _desc_fmt_hr(self) -> None:
        buf = self._desc_view.get_buffer()
        it = buf.get_iter_at_mark(buf.get_insert())
        it.set_line_offset(0)
        buf.begin_user_action()
        buf.insert(it, "<hr>\n")
        buf.end_user_action()

    # ── Launch target management (RepoContext only) ───────────────────────

    def _add_target_row_ui(self, target: dict) -> None:
        self._launch_targets.append(target)
        idx = len(self._launch_targets) - 1

        name = target.get("name", "Main")
        path = target.get("path", "")
        args = target.get("args", "")
        env = target.get("env", "")

        row = Adw.ExpanderRow(title=GLib.markup_escape_text(name))
        row.set_subtitle(GLib.markup_escape_text(path) if path else "Not set")
        row.set_subtitle_lines(1)

        browse_btn = Gtk.Button(icon_name="folder-open-symbolic")
        browse_btn.add_css_class("flat")
        browse_btn.set_valign(Gtk.Align.CENTER)
        browse_btn.set_sensitive(self._locally_installed)
        browse_btn.set_tooltip_text(
            "Browse for executable\u2026" if self._locally_installed else "Not installed locally"
        )
        browse_btn.connect("clicked", self._on_browse_target, idx)
        row.add_suffix(browse_btn)

        del_btn = Gtk.Button(icon_name="user-trash-symbolic")
        del_btn.add_css_class("flat")
        del_btn.set_valign(Gtk.Align.CENTER)
        del_btn.connect("clicked", self._on_remove_target, idx)
        row.add_suffix(del_btn)

        name_entry = Adw.EntryRow(title="Name")
        name_entry.set_text(name)
        name_entry.connect("changed", self._on_target_name_changed, idx, row)
        row.add_row(name_entry)

        args_entry = Adw.EntryRow(title="Arguments")
        args_entry.set_text(args)
        args_entry.connect("changed", self._on_target_args_changed, idx)
        row.add_row(args_entry)

        env_entry = Adw.EntryRow(title="Environment")
        env_entry.set_text(env)
        env_entry.set_tooltip_text(
            "Environment variables. Paste Steam launch options directly, e.g. "
            "PROTON_USE_WINED3D=1 PROTON_NO_ESYNC=1 %command% — "
            "%command% and unrecognised tokens are ignored automatically."
        )
        env_entry.connect("changed", self._on_target_env_changed, idx)
        row.add_row(env_entry)

        self._target_rows.append(row)
        grp = self._targets_group
        grp.remove(self._add_target_row_widget)
        grp.add(row)
        grp.add(self._add_target_row_widget)

    def _on_add_target_clicked(self, _btn) -> None:
        self._add_target_row_ui({"name": "New Target", "path": ""})

    def _on_remove_target(self, _btn, idx: int) -> None:
        if idx >= len(self._launch_targets):
            return
        self._targets_group.remove(self._target_rows[idx])
        self._launch_targets.pop(idx)
        self._target_rows.pop(idx)
        self._rebind_target_indices()

    def _rebind_target_indices(self) -> None:
        old_targets = list(self._launch_targets)
        for row in self._target_rows:
            self._targets_group.remove(row)
        self._launch_targets.clear()
        self._target_rows.clear()
        for t in old_targets:
            self._add_target_row_ui(t)

    def _on_target_name_changed(self, entry: Adw.EntryRow, idx: int,
                                row: Adw.ExpanderRow) -> None:
        if idx < len(self._launch_targets):
            self._launch_targets[idx]["name"] = entry.get_text().strip()
            row.set_title(GLib.markup_escape_text(entry.get_text().strip()))

    def _on_target_args_changed(self, entry: Adw.EntryRow, idx: int) -> None:
        if idx < len(self._launch_targets):
            text = entry.get_text().strip()
            if text:
                self._launch_targets[idx]["args"] = text
            else:
                self._launch_targets[idx].pop("args", None)

    def _on_target_env_changed(self, entry: Adw.EntryRow, idx: int) -> None:
        if idx < len(self._launch_targets):
            text = entry.get_text().strip()
            if text:
                self._launch_targets[idx]["env"] = text
            else:
                self._launch_targets[idx].pop("env", None)

    # ── Entry point file browser (RepoContext only) ───────────────────────

    def _on_browse_target(self, _btn, idx: int) -> None:
        e = self._context._entry
        if e.platform == "linux":
            from cellar.backend.database import get_installed
            rec = get_installed(e.id)
            has_path = rec and rec.get("install_path")
            install_path = Path(rec["install_path"]) / e.id if has_path else Path.home()
            browse_root = install_path
        else:
            from cellar.backend.umu import prefixes_dir
            prefix = prefixes_dir() / e.id / "drive_c"
            browse_root = prefix if prefix.is_dir() else Path.home()

        chooser = Gtk.FileChooserNative(
            title="Select Executable",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            accept_label="Select",
        )
        from gi.repository import Gio
        chooser.set_current_folder(Gio.File.new_for_path(str(browse_root)))
        if e.platform != "linux":
            exe_filter = Gtk.FileFilter()
            exe_filter.set_name("Windows executables")
            for ext in ("exe", "msi", "bat", "cmd", "com", "lnk"):
                exe_filter.add_pattern(f"*.{ext}")
                exe_filter.add_pattern(f"*.{ext.upper()}")
            chooser.add_filter(exe_filter)
            all_filter = Gtk.FileFilter()
            all_filter.set_name("All files")
            all_filter.add_pattern("*")
            chooser.add_filter(all_filter)
        chooser.connect("response", self._on_target_path_chosen, chooser,
                        browse_root, e.platform, idx)
        chooser.show()
        self._ep_chooser = chooser

    def _on_target_path_chosen(self, _c, response, chooser, browse_root: Path,
                               platform: str, idx: int) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        abs_path = chooser.get_file().get_path()
        if platform == "linux":
            import os
            try:
                formatted = os.path.relpath(abs_path, str(browse_root))
            except ValueError:
                formatted = abs_path
        else:
            from cellar.utils.paths import to_win32_path
            formatted = to_win32_path(abs_path, str(browse_root))
        if idx < len(self._launch_targets):
            self._launch_targets[idx]["path"] = formatted
            self._target_rows[idx].set_subtitle(GLib.markup_escape_text(formatted))

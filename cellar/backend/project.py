"""Package Builder project management.

Projects are stored under ``~/.local/share/cellar/projects/<slug>/``:

::

    <slug>/
        project.json   — metadata (name, runner, entry_point, …)
        prefix/        — the WINEPREFIX being built
        <slug>.tar.zst — generated archive (created on Publish, deleted on next Publish)
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

ProjectType = Literal["app", "base", "linux", "dos"]


def _parse_entry_points(data: dict) -> list[dict]:
    """Read entry_points from project.json, migrating old single entry_point if needed."""
    if "entry_points" in data:
        return list(data["entry_points"])
    old = data.get("entry_point", "")
    if old:
        return [{"name": "Main", "path": old}]
    return []


@dataclass
class Project:
    """Metadata for a Package Builder project."""

    name: str
    slug: str
    project_type: ProjectType = "app"
    runner: str = ""
    entry_points: list[dict] = field(default_factory=list)  # [{"name": str, "path": str}, ...]
    steam_appid: int | None = None
    deps_installed: list[str] = field(default_factory=list)
    notes: str = ""
    # True once prefix has been initialized (Windows) or source_dir is set (Linux)
    initialized: bool = False
    origin_app_id: str = ""    # set when project was imported from a catalogue entry
    source_dir: str = ""       # Linux projects only: path to the pre-installed app directory
    installer_path: str = ""   # Smart import: path to .exe/.msi/.sh/.run to run in prefix
    installer_type: str = ""   # "", "isolated" (bwrap sandbox), "folder" (direct copy)
    disc_images: list[str] = field(default_factory=list)    # relative paths to CDs
    floppy_images: list[str] = field(default_factory=list)  # relative paths to floppies
    engine: str = ""               # "dosbox" or "scummvm"; empty = platform default
    scummvm_id: str = ""           # ScummVM game ID (e.g. "sky")

    # ── Launch options (committed to catalogue metadata on publish) ───────
    dxvk: bool = True
    vkd3d: bool = True
    audio_driver: str = "auto"       # "auto" | "pulseaudio" | "alsa" | "oss"
    debug: bool = False
    direct_proton: bool = False
    no_lsteamclient: bool = False  # disable Proton's lsteamclient.dll shim
    lock_runner: bool = False

    # ── Catalogue metadata (App only) ─────────────────────────────────────
    version: str = "1.0"
    category: str = ""
    developer: str = ""
    publisher: str = ""
    release_year: int | None = None
    website: str = ""
    genres: list[str] = field(default_factory=list)
    summary: str = ""
    description: str = ""
    icon_path: str = ""        # local file path to icon image
    cover_path: str = ""       # local file path to cover image
    logo_path: str = ""        # local file path to logo image (transparent PNG)
    hide_title: bool = False   # True when logo already contains the app name
    screenshot_paths: list[str] = field(default_factory=list)   # local file paths
    screenshot_sources: dict[str, str] = field(default_factory=dict)  # {local_path: steam_url}
    delta_size: int = 0            # uncompressed delta-only size (bytes); 0 = not measured
    steam_screenshots: list[dict] = field(default_factory=list) # [{"full": url, "thumbnail": url}]
    selected_steam_urls: list[str] = field(default_factory=list) # full URLs the user checked

    # ------------------------------------------------------------------
    # Derived paths
    # ------------------------------------------------------------------

    @property
    def entry_point(self) -> str:
        """Primary entry point path (first in list). Read-only; modify entry_points instead."""
        return self.entry_points[0]["path"] if self.entry_points else ""

    @property
    def entry_args(self) -> str:
        """Launch arguments for the primary entry point."""
        return self.entry_points[0].get("args", "") if self.entry_points else ""

    @property
    def project_dir(self) -> Path:
        from cellar.backend.umu import projects_dir
        return projects_dir() / self.slug

    @property
    def content_path(self) -> Path:
        return self.project_dir / "content"

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict) -> "Project":
        return cls(
            name=data.get("name", ""),
            slug=data.get("slug", ""),
            project_type=data.get("project_type", "app"),
            runner=data.get("runner", ""),
            entry_points=_parse_entry_points(data),
            steam_appid=data.get("steam_appid"),
            deps_installed=list(data.get("deps_installed", [])),
            notes=data.get("notes", ""),
            initialized=bool(data.get("initialized", False)),
            origin_app_id=data.get("origin_app_id", ""),
            source_dir=data.get("source_dir", ""),
            installer_path=data.get("installer_path", ""),
            installer_type=data.get("installer_type", ""),
            disc_images=list(data.get("disc_images", [])),
            floppy_images=list(data.get("floppy_images", [])),
            engine=data.get("engine", ""),
            scummvm_id=data.get("scummvm_id", ""),
            dxvk=bool(data.get("dxvk", True)),
            vkd3d=bool(data.get("vkd3d", True)),
            audio_driver=data.get("audio_driver", "auto"),
            debug=bool(data.get("debug", False)),
            direct_proton=bool(data.get("direct_proton", False)),
            no_lsteamclient=bool(data.get("no_lsteamclient", False)),
            lock_runner=bool(data.get("lock_runner", False)),
            version=data.get("version", "1.0"),
            category=data.get("category", ""),
            developer=data.get("developer", ""),
            publisher=data.get("publisher", ""),
            release_year=data.get("release_year"),
            website=data.get("website", ""),
            genres=list(data.get("genres", [])),
            summary=data.get("summary", ""),
            description=data.get("description", ""),
            icon_path=data.get("icon_path", ""),
            cover_path=data.get("cover_path", ""),
            logo_path=data.get("logo_path", ""),
            hide_title=bool(data.get("hide_title", False)),
            screenshot_paths=list(data.get("screenshot_paths", [])),
            screenshot_sources=dict(data.get("screenshot_sources", {})),
            delta_size=int(data.get("delta_size", 0)),
            steam_screenshots=list(data.get("steam_screenshots", [])),
            selected_steam_urls=list(data.get("selected_steam_urls", [])),
        )

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "slug": self.slug,
            "project_type": self.project_type,
        }
        if self.runner:
            d["runner"] = self.runner
        if self.entry_points:
            d["entry_points"] = list(self.entry_points)
        if self.steam_appid is not None:
            d["steam_appid"] = self.steam_appid
        if self.deps_installed:
            d["deps_installed"] = self.deps_installed
        if self.notes:
            d["notes"] = self.notes
        if self.initialized:
            d["initialized"] = True
        if self.origin_app_id:
            d["origin_app_id"] = self.origin_app_id
        if self.source_dir:
            d["source_dir"] = self.source_dir
        if self.installer_path:
            d["installer_path"] = self.installer_path
        if self.installer_type:
            d["installer_type"] = self.installer_type
        if self.disc_images:
            d["disc_images"] = list(self.disc_images)
        if self.floppy_images:
            d["floppy_images"] = list(self.floppy_images)
        if self.engine:
            d["engine"] = self.engine
        if self.scummvm_id:
            d["scummvm_id"] = self.scummvm_id
        if not self.dxvk:
            d["dxvk"] = False
        if not self.vkd3d:
            d["vkd3d"] = False
        if self.audio_driver != "auto":
            d["audio_driver"] = self.audio_driver
        if self.debug:
            d["debug"] = True
        if self.direct_proton:
            d["direct_proton"] = True
        if self.no_lsteamclient:
            d["no_lsteamclient"] = True
        if self.lock_runner:
            d["lock_runner"] = True
        if self.version and self.version != "1.0":
            d["version"] = self.version
        if self.category:
            d["category"] = self.category
        if self.developer:
            d["developer"] = self.developer
        if self.publisher:
            d["publisher"] = self.publisher
        if self.release_year is not None:
            d["release_year"] = self.release_year
        if self.website:
            d["website"] = self.website
        if self.genres:
            d["genres"] = list(self.genres)
        if self.summary:
            d["summary"] = self.summary
        if self.description:
            d["description"] = self.description
        if self.icon_path:
            d["icon_path"] = self.icon_path
        if self.cover_path:
            d["cover_path"] = self.cover_path
        if self.logo_path:
            d["logo_path"] = self.logo_path
        if self.hide_title:
            d["hide_title"] = True
        if self.screenshot_paths:
            d["screenshot_paths"] = list(self.screenshot_paths)
        if self.screenshot_sources:
            d["screenshot_sources"] = dict(self.screenshot_sources)
        if self.delta_size:
            d["delta_size"] = self.delta_size
        if self.steam_screenshots:
            d["steam_screenshots"] = list(self.steam_screenshots)
        if self.selected_steam_urls:
            d["selected_steam_urls"] = list(self.selected_steam_urls)
        return d


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

def load_projects() -> list[Project]:
    """Return all projects from the projects directory, sorted by name."""
    from cellar.backend.umu import projects_dir
    projects: list[Project] = []
    pd = projects_dir()
    for entry in pd.iterdir():
        if not entry.is_dir():
            continue
        json_file = entry / "project.json"
        if not json_file.exists():
            continue
        try:
            data = json.loads(json_file.read_text())
            projects.append(Project.from_dict(data))
        except Exception:
            log.warning("Failed to read project from %s", entry)
    projects.sort(key=lambda p: p.name.lower())
    return projects


def load_project(slug: str) -> Project | None:
    """Load a single project by slug, or return None if not found."""
    from cellar.backend.umu import projects_dir
    json_file = projects_dir() / slug / "project.json"
    if not json_file.exists():
        return None
    try:
        data = json.loads(json_file.read_text(encoding="utf-8"))
        return Project.from_dict(data)
    except Exception:
        log.warning("Failed to load project %s", slug)
        return None


def save_project(project: Project) -> None:
    """Write ``project.json`` for *project*."""
    project.project_dir.mkdir(parents=True, exist_ok=True)
    path = project.project_dir / "project.json"
    new_content = json.dumps(project.to_dict(), indent=2)
    if not path.exists() or path.read_text(encoding="utf-8") != new_content:
        path.write_text(new_content, encoding="utf-8")


def create_project(
    name: str,
    project_type: ProjectType,
    runner: str = "",
) -> Project:
    """Create, persist, and return a new project with a unique slug.

    For base projects the *name* is ignored — it is auto-generated from
    *runner* (e.g. ``"GE-Proton10-32"``).  The slug is similarly derived so
    the directory name is predictable and one base per runner is natural.
    """
    from cellar.backend.packager import slugify

    if project_type == "base":
        name = runner if runner else "(no runner)"
        slug = slugify(runner) if runner else "base"
    else:
        slug = slugify(name)

    existing = {p.slug for p in load_projects()}
    base_slug = slug
    i = 2
    while slug in existing:
        slug = f"{base_slug}-{i}"
        i += 1

    project = Project(name=name, slug=slug, project_type=project_type, runner=runner)
    save_project(project)
    return project


def delete_project(slug: str) -> None:
    """Delete the project directory entirely."""
    import shutil

    from cellar.backend.umu import projects_dir
    p = projects_dir() / slug
    if p.exists():
        shutil.rmtree(p)


# ---------------------------------------------------------------------------
# Packaging
# ---------------------------------------------------------------------------

def package_project(
    project: Project,
    *,
    cancel_event=None,
    progress_cb=None,
) -> tuple[Path, int, str]:
    """Archive the project prefix as a Cellar-native ``.tar.zst``.

    The archive is written to ``<project_dir>/<slug>.tar.zst``.
    Any previous archive at that path is overwritten.

    Returns ``(archive_path, size_bytes, crc32_hex)``.

    Raises ``RuntimeError`` if the prefix directory does not exist.
    """
    from cellar.backend.packager import compress_prefix_zst

    if not project.content_path.is_dir():
        raise RuntimeError(
            f"Content directory not found: {project.content_path}\n"
            "Initialize the project before packaging."
        )

    dest = project.project_dir / f"{project.slug}.tar.zst"
    size, crc32 = compress_prefix_zst(
        project.content_path,
        dest,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
    )
    return dest, size, crc32

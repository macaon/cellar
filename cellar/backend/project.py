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

ProjectType = Literal["app", "base"]


@dataclass
class Project:
    """Metadata for a Package Builder project."""

    name: str
    slug: str
    project_type: ProjectType = "app"
    runner: str = ""
    entry_point: str = ""   # relative to drive_c (App only)
    steam_appid: int | None = None
    deps_installed: list[str] = field(default_factory=list)
    notes: str = ""
    initialized: bool = False  # True once prefix has been initialized

    # ------------------------------------------------------------------
    # Derived paths
    # ------------------------------------------------------------------

    @property
    def project_dir(self) -> Path:
        from cellar.backend.umu import projects_dir
        return projects_dir() / self.slug

    @property
    def prefix_path(self) -> Path:
        return self.project_dir / "prefix"

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
            entry_point=data.get("entry_point", ""),
            steam_appid=data.get("steam_appid"),
            deps_installed=list(data.get("deps_installed", [])),
            notes=data.get("notes", ""),
            initialized=bool(data.get("initialized", False)),
        )

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "slug": self.slug,
            "project_type": self.project_type,
        }
        if self.runner:
            d["runner"] = self.runner
        if self.entry_point:
            d["entry_point"] = self.entry_point
        if self.steam_appid is not None:
            d["steam_appid"] = self.steam_appid
        if self.deps_installed:
            d["deps_installed"] = self.deps_installed
        if self.notes:
            d["notes"] = self.notes
        if self.initialized:
            d["initialized"] = True
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


def save_project(project: Project) -> None:
    """Write ``project.json`` for *project*."""
    project.project_dir.mkdir(parents=True, exist_ok=True)
    (project.project_dir / "project.json").write_text(
        json.dumps(project.to_dict(), indent=2)
    )


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

    if not project.prefix_path.is_dir():
        raise RuntimeError(
            f"Prefix directory not found: {project.prefix_path}\n"
            "Initialize the prefix before packaging."
        )

    dest = project.project_dir / f"{project.slug}.tar.zst"
    size, crc32 = compress_prefix_zst(
        project.prefix_path,
        dest,
        cancel_event=cancel_event,
        progress_cb=progress_cb,
    )
    return dest, size, crc32

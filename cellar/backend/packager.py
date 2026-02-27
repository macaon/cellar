"""Packaging helpers for writing apps into a local Cellar repo.

This module handles:
- Reading ``bottle.yml`` from a Bottles backup archive (no PyYAML required)
- Generating URL-safe app IDs from human names
- Writing a complete archive + images + catalogue entry into a repo
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


# ---------------------------------------------------------------------------
# Category constants
# ---------------------------------------------------------------------------

#: Built-in categories always present in every repo.  Custom user-defined
#: categories are stored in the ``categories`` key of ``catalogue.json`` and
#: merged with this list at read time.
BASE_CATEGORIES: list[str] = ["Games", "Productivity", "Graphics", "Utility"]


# ---------------------------------------------------------------------------
# bottle.yml extraction
# ---------------------------------------------------------------------------

def read_bottle_yml(archive_path: str) -> dict:
    """Extract and return top-level scalar fields from ``bottle.yml``.

    Searches for ``bottle.yml`` at any depth inside the ``.tar.gz``.
    Uses a simple line-by-line parser — no PyYAML dependency.

    Returns an empty dict if the file is not found or cannot be read.
    """
    try:
        with tarfile.open(archive_path, "r:gz") as tf:
            for member in tf.getmembers():
                if member.name == "bottle.yml" or member.name.endswith("/bottle.yml"):
                    f = tf.extractfile(member)
                    if f:
                        return _parse_top_level(f.read())
    except (tarfile.TarError, OSError):
        pass
    return {}


def _parse_top_level(content: bytes) -> dict:
    """Parse only non-indented ``Key: value`` lines.

    This is sufficient for all the scalar fields we care about
    (Name, Runner, DXVK, VKD3D, Windows, Environment).
    """
    result: dict = {}
    for line in content.decode(errors="replace").splitlines():
        m = re.match(r"^([A-Za-z_]\w*): (.+)$", line)
        if m:
            result[m.group(1)] = m.group(2).strip().strip("'\"")
    return result


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def slugify(name: str) -> str:
    """Convert a human name to a URL-safe app ID.

    Examples::

        slugify("Notepad++")  → "notepad-plus-plus"
        slugify("My App")     → "my-app"
        slugify("Half-Life 2") → "half-life-2"
    """
    slug = name.lower()
    # Replace runs of '+' with '-plus' (one '-plus' per '+')
    slug = re.sub(r"\++", lambda m: "-plus" * len(m.group()), slug)
    # Replace any remaining non-alphanumeric runs with a single '-'
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    # Collapse multiple dashes, strip leading/trailing
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "app"


# ---------------------------------------------------------------------------
# Repo write
# ---------------------------------------------------------------------------

def import_to_repo(
    repo_root: Path,
    entry,                      # AppEntry — avoid circular import; checked by caller
    archive_src: str,
    images: dict,
    *,
    progress_cb: Callable[[float], None] | None = None,
    cancel_event=None,          # threading.Event — checked during archive copy
) -> None:
    """Copy archive + images into *repo_root* and update ``catalogue.json``.

    *images* is a dict with optional keys:
      ``"icon"``, ``"cover"``, ``"hero"`` → str path
      ``"screenshots"`` → list[str] paths

    *progress_cb* receives a float in [0, 1] during the archive copy phase.
    *cancel_event* is a ``threading.Event``; when set the copy is aborted and
    the partial destination file is removed.
    """
    app_dir = repo_root / "apps" / entry.id
    app_dir.mkdir(parents=True, exist_ok=True)

    # ── Archive copy (chunked for progress reporting) ─────────────────────
    archive_dest = repo_root / entry.archive
    archive_dest.parent.mkdir(parents=True, exist_ok=True)
    src_size = Path(archive_src).stat().st_size
    chunk = 1 * 1024 * 1024  # 1 MB
    copied = 0
    try:
        with open(archive_src, "rb") as src_f, open(archive_dest, "wb") as dst_f:
            while True:
                if cancel_event and cancel_event.is_set():
                    dst_f.close()
                    archive_dest.unlink(missing_ok=True)
                    raise CancelledError("Import cancelled by user")
                buf = src_f.read(chunk)
                if not buf:
                    break
                dst_f.write(buf)
                copied += len(buf)
                if progress_cb and src_size > 0:
                    progress_cb(min(copied / src_size * 0.9, 0.9))
    except CancelledError:
        raise
    except OSError as exc:
        archive_dest.unlink(missing_ok=True)
        raise RuntimeError(f"Failed to copy archive: {exc}") from exc

    # ── Single images (icon, cover, hero) ─────────────────────────────────
    for key in ("icon", "cover", "hero"):
        src = images.get(key)
        if src and getattr(entry, key):
            dest = repo_root / getattr(entry, key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)

    # ── Screenshots ───────────────────────────────────────────────────────
    ss_dir = app_dir / "screenshots"
    for i, src in enumerate(images.get("screenshots", []), 1):
        ss_dir.mkdir(exist_ok=True)
        shutil.copyfile(src, ss_dir / f"{i:02d}{Path(src).suffix}")

    # ── catalogue.json ────────────────────────────────────────────────────
    _upsert_catalogue(repo_root, entry)

    if progress_cb:
        progress_cb(1.0)


def update_in_repo(
    repo_root: Path,
    old_entry,                          # AppEntry
    new_entry,                          # AppEntry
    images: dict,                       # {"icon": path|None, "cover": path|None, ...}
    new_archive_src: str | None = None,
    *,
    progress_cb: Callable[[float], None] | None = None,
    cancel_event=None,
) -> None:
    """Update an existing entry in *repo_root*.

    - If *new_archive_src* is given, the archive is replaced (chunked copy,
      old file removed if the path changed and the file still exists).
    - Only image keys with a non-empty value in *images* are overwritten.
    - Screenshots are fully replaced when *images["screenshots"]* is non-empty.
    - ``catalogue.json`` is updated in place (matched by ID).
    """
    app_dir = repo_root / "apps" / new_entry.id
    app_dir.mkdir(parents=True, exist_ok=True)

    # ── Archive (optional replacement) ────────────────────────────────────
    if new_archive_src:
        archive_dest = repo_root / new_entry.archive
        archive_dest.parent.mkdir(parents=True, exist_ok=True)
        src_size = Path(new_archive_src).stat().st_size
        chunk = 1 * 1024 * 1024
        copied = 0
        try:
            with open(new_archive_src, "rb") as src_f, open(archive_dest, "wb") as dst_f:
                while True:
                    if cancel_event and cancel_event.is_set():
                        dst_f.close()
                        archive_dest.unlink(missing_ok=True)
                        raise CancelledError("Update cancelled by user")
                    buf = src_f.read(chunk)
                    if not buf:
                        break
                    dst_f.write(buf)
                    copied += len(buf)
                    if progress_cb and src_size > 0:
                        progress_cb(min(copied / src_size * 0.9, 0.9))
        except CancelledError:
            raise
        except OSError as exc:
            archive_dest.unlink(missing_ok=True)
            raise RuntimeError(f"Failed to copy archive: {exc}") from exc

        # Remove the old archive file if its path changed
        old_archive = repo_root / old_entry.archive
        if old_archive != archive_dest and old_archive.exists():
            old_archive.unlink(missing_ok=True)

    # ── Single images (icon, cover, hero) ─────────────────────────────────
    for key in ("icon", "cover", "hero"):
        src = images.get(key)
        if src and getattr(new_entry, key):
            dest = repo_root / getattr(new_entry, key)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)

    # ── Screenshots ───────────────────────────────────────────────────────
    # None = keep existing, [] = clear all, [...] = replace
    new_screenshots = images.get("screenshots")
    if new_screenshots is not None:
        ss_dir = repo_root / "apps" / new_entry.id / "screenshots"
        if ss_dir.exists():
            shutil.rmtree(ss_dir)
        if new_screenshots:
            ss_dir.mkdir(parents=True, exist_ok=True)
            for i, src in enumerate(new_screenshots, 1):
                shutil.copyfile(src, ss_dir / f"{i:02d}{Path(src).suffix}")

    # ── catalogue.json ────────────────────────────────────────────────────
    _upsert_catalogue(repo_root, new_entry)

    if progress_cb:
        progress_cb(1.0)


def remove_from_repo(
    repo_root: Path,
    entry,                          # AppEntry
    *,
    move_archive_to: str | None = None,
    cancel_event=None,
) -> None:
    """Remove *entry* from *repo_root*.

    If *move_archive_to* is set and the archive file exists, it is moved to
    that directory before the rest of the app directory is deleted.
    """
    archive_file = repo_root / entry.archive

    if move_archive_to and archive_file.exists():
        if cancel_event and cancel_event.is_set():
            raise CancelledError("Delete cancelled by user")
        shutil.move(str(archive_file), Path(move_archive_to) / archive_file.name)

    if cancel_event and cancel_event.is_set():
        raise CancelledError("Delete cancelled by user")

    shutil.rmtree(repo_root / "apps" / entry.id, ignore_errors=True)

    if cancel_event and cancel_event.is_set():
        raise CancelledError("Delete cancelled by user")

    _remove_from_catalogue(repo_root, entry.id)


# ---------------------------------------------------------------------------
# catalogue.json helpers (shared by import / update / remove)
# ---------------------------------------------------------------------------

def _upsert_catalogue(repo_root: Path, entry) -> None:
    """Replace or append *entry* in ``catalogue.json``."""
    cat_path = repo_root / "catalogue.json"
    categories: list[str] | None = None
    if cat_path.exists():
        raw = json.loads(cat_path.read_text())
        apps = raw.get("apps", raw) if isinstance(raw, dict) else raw
        if isinstance(raw, dict):
            categories = raw.get("categories")
    else:
        apps = []
    apps = [a for a in apps if a.get("id") != entry.id]
    apps.append(entry.to_dict())
    _write_catalogue(cat_path, apps, categories)


def _remove_from_catalogue(repo_root: Path, app_id: str) -> None:
    """Filter *app_id* out of ``catalogue.json``."""
    cat_path = repo_root / "catalogue.json"
    if not cat_path.exists():
        return
    raw = json.loads(cat_path.read_text())
    apps = raw.get("apps", raw) if isinstance(raw, dict) else raw
    categories = raw.get("categories") if isinstance(raw, dict) else None
    apps = [a for a in apps if a.get("id") != app_id]
    _write_catalogue(cat_path, apps, categories)


def _write_catalogue(
    cat_path: Path,
    apps: list,
    categories: list[str] | None = None,
) -> None:
    data: dict = {
        "cellar_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apps": apps,
    }
    if categories is not None:
        data["categories"] = categories
    cat_path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def add_catalogue_category(repo_root: Path, category: str) -> None:
    """Append *category* to the top-level ``categories`` list in ``catalogue.json``.

    Does nothing if the category is already in :data:`BASE_CATEGORIES` or
    already present in the stored list.  Creates the ``categories`` key if it
    does not exist yet.
    """
    if category in BASE_CATEGORIES:
        return
    cat_path = repo_root / "catalogue.json"
    if cat_path.exists():
        raw = json.loads(cat_path.read_text())
    else:
        raw = {"cellar_version": 1, "apps": []}
    if not isinstance(raw, dict):
        return
    stored: list[str] = raw.get("categories") or []
    if category in stored:
        return
    stored.append(category)
    raw["categories"] = stored
    raw["generated_at"] = datetime.now(timezone.utc).isoformat()
    cat_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False))


class CancelledError(Exception):
    """Raised when an import is cancelled via *cancel_event*."""

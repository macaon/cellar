"""Packaging helpers for writing apps into a local Cellar repo.

This module handles:
- Reading ``bottle.yml`` from a Bottles backup archive
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
# Image optimisation at import time
# ---------------------------------------------------------------------------

#: Maximum dimensions per image role.  Images larger than these are downscaled
#: (preserving aspect ratio) and saved as JPEG at 85% quality.  Images already
#: within limits are copied as-is.
_IMAGE_MAX_SIZE: dict[str, tuple[int, int]] = {
    "icon":       (256, 256),
    "cover":      (300, 400),
    "hero":       (1920, 620),
    "screenshot": (1920, 1080),
}

_JPEG_QUALITY = 85


def _ico_to_png(src: Path, dest: Path) -> bool:
    """Extract the largest image from an ICO file and save it as PNG.

    Modern ICO files embed PNG data for large frames (typically 256 px).
    For BMP-encoded frames we skip conversion and return ``False``.

    Returns ``True`` if a PNG was successfully written to *dest*.
    """
    import struct

    try:
        data = src.read_bytes()
    except OSError:
        return False

    if len(data) < 6:
        return False

    _reserved, _type, count = struct.unpack_from("<HHH", data, 0)
    if _type not in (1, 2) or count == 0:
        return False

    # Find the largest entry.
    best_offset = 0
    best_size = 0
    best_pixels = 0
    for i in range(count):
        off = 6 + i * 16
        if off + 16 > len(data):
            break
        w = data[off] or 256
        h = data[off + 1] or 256
        img_size = struct.unpack_from("<I", data, off + 8)[0]
        img_offset = struct.unpack_from("<I", data, off + 12)[0]
        if w * h > best_pixels:
            best_pixels = w * h
            best_offset = img_offset
            best_size = img_size

    if best_size == 0 or best_offset + best_size > len(data):
        return False

    frame = data[best_offset : best_offset + best_size]

    # Check if the frame is PNG-encoded (starts with PNG magic).
    if frame[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            dest.write_bytes(frame)
            return True
        except OSError:
            return False

    return False


def _optimize_image(src: str | Path, dest: Path, role: str) -> None:
    """Copy *src* to *dest*, downscaling if it exceeds the role's max size.

    Uses GdkPixbuf for the resize (HYPER interpolation) and saves as JPEG
    when the source needs resizing, otherwise copies as-is to preserve the
    original format for small/already-optimised images.

    ICO icons are converted to PNG at import time since GdkPixbuf on most
    systems lacks an ICO loader.
    """
    src = Path(src)
    max_dims = _IMAGE_MAX_SIZE.get(role)

    # Convert ICO → PNG for any role (GdkPixbuf typically can't load ICO).
    if src.suffix.lower() == ".ico":
        png_dest = dest.with_suffix(".png")
        if _ico_to_png(src, png_dest):
            if png_dest != dest:
                png_dest.rename(dest)
            return
        # Fallback: copy as-is and hope for the best.
        shutil.copyfile(src, dest)
        return

    if not max_dims or role == "icon":
        shutil.copyfile(src, dest)
        return

    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):
        shutil.copyfile(src, dest)
        return

    try:
        fmt, orig_w, orig_h = GdkPixbuf.Pixbuf.get_file_info(str(src))
    except Exception:
        shutil.copyfile(src, dest)
        return

    if fmt is None:
        shutil.copyfile(src, dest)
        return

    max_w, max_h = max_dims
    if orig_w <= max_w and orig_h <= max_h:
        shutil.copyfile(src, dest)
        return

    try:
        pixbuf = GdkPixbuf.Pixbuf.new_from_file_at_scale(
            str(src), max_w, max_h, True,
        )
        jpeg_dest = dest.with_suffix(".jpg")
        pixbuf.savev(str(jpeg_dest), "jpeg", ["quality"], [str(_JPEG_QUALITY)])
        # If the caller expected a different extension, rename.
        if jpeg_dest != dest:
            jpeg_dest.rename(dest)
    except Exception:
        shutil.copyfile(src, dest)


# ---------------------------------------------------------------------------
# bottle.yml extraction
# ---------------------------------------------------------------------------

class _ProgressFileObj:
    """Wraps a binary file, calling *cb(fraction)* after each read chunk.

    Used to track how far through the compressed stream tarfile has read,
    so callers can show a progress bar while scanning for ``bottle.yml``.
    """
    __slots__ = ("_f", "_size", "_cb")

    def __init__(self, f, size: int, cb):
        self._f = f
        self._size = size
        self._cb = cb

    def read(self, n=-1):
        data = self._f.read(n)
        if self._cb and self._size > 0:
            self._cb(min(self._f.tell() / self._size, 1.0))
        return data

    def seek(self, *args): return self._f.seek(*args)
    def tell(self): return self._f.tell()
    def readable(self): return True
    def writable(self): return False
    def seekable(self): return True

    @property
    def name(self): return getattr(self._f, "name", "")


def read_bottle_yml(archive_path: str, *, progress_cb=None) -> dict:
    """Extract and parse ``bottle.yml`` from a Bottles ``.tar.gz`` backup.

    Searches for ``bottle.yml`` at any depth inside the archive.
    Iterates members one at a time and stops as soon as the file is found.

    *progress_cb*, if given, is called with a float in ``[0, 1]``
    representing how far through the compressed stream has been read.

    Returns an empty dict if the file is not found or cannot be parsed.
    """
    import yaml

    try:
        with open(archive_path, "rb") as raw:
            if progress_cb:
                file_size = Path(archive_path).stat().st_size
                fileobj = _ProgressFileObj(raw, file_size, progress_cb)
            else:
                fileobj = raw
            with tarfile.open(fileobj=fileobj, mode="r:gz") as tf:
                for member in tf:
                    if member.name == "bottle.yml" or member.name.endswith("/bottle.yml"):
                        f = tf.extractfile(member)
                        if f:
                            if progress_cb:
                                progress_cb(1.0)
                            data = yaml.safe_load(f.read())
                            return data if isinstance(data, dict) else {}
    except (tarfile.TarError, OSError, yaml.YAMLError):
        pass
    return {}


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
            _optimize_image(src, dest, key)

    # ── Screenshots ───────────────────────────────────────────────────────
    ss_dir = app_dir / "screenshots"
    for i, src in enumerate(images.get("screenshots", []), 1):
        ss_dir.mkdir(exist_ok=True)
        _optimize_image(src, ss_dir / f"{i:02d}{Path(src).suffix}", "screenshot")

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
            _optimize_image(src, dest, key)

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
                _optimize_image(src, ss_dir / f"{i:02d}{Path(src).suffix}", "screenshot")

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

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
            shutil.copy2(src, dest)

    # ── Screenshots ───────────────────────────────────────────────────────
    ss_dir = app_dir / "screenshots"
    for i, src in enumerate(images.get("screenshots", []), 1):
        ss_dir.mkdir(exist_ok=True)
        shutil.copy2(src, ss_dir / f"{i:02d}{Path(src).suffix}")

    # ── catalogue.json ────────────────────────────────────────────────────
    cat_path = repo_root / "catalogue.json"
    if cat_path.exists():
        raw = json.loads(cat_path.read_text())
        apps = raw.get("apps", raw) if isinstance(raw, dict) else raw
    else:
        apps = []
    # Replace existing entry with same ID, or append
    apps = [a for a in apps if a.get("id") != entry.id]
    apps.append(entry.to_dict())
    cat_path.write_text(
        json.dumps(
            {
                "cellar_version": 1,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "apps": apps,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    if progress_cb:
        progress_cb(1.0)


class CancelledError(Exception):
    """Raised when an import is cancelled via *cancel_event*."""

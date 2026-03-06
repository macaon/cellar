"""Local base-image store for delta-package installs.

Base images live at ``~/.local/share/cellar/bases/<runner>/`` as extracted
bottle directories (not archives).  They are managed entirely by Cellar and
are never visible to Bottles.

When a delta app is installed the installer calls ``base_path(runner)`` to
get the ``--link-dest`` reference directory, which seeds the new bottle with
hardlinks before the delta archive is overlaid on top.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable

from cellar.backend import database

_BASES_DIR = Path.home() / ".local" / "share" / "cellar" / "bases"


class BaseStoreError(Exception):
    """Raised when a base store operation fails."""


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def base_path(runner: str) -> Path:
    """Return the local directory path for the extracted *runner* base."""
    return _BASES_DIR / runner


def is_base_installed(runner: str) -> bool:
    """Return ``True`` if the extracted base for *runner* is present on disk."""
    return base_path(runner).is_dir()


# ---------------------------------------------------------------------------
# Install / remove
# ---------------------------------------------------------------------------

def install_base(
    archive_path: Path | str,
    runner: str,
    *,
    progress_cb: Callable[[float], None] | None = None,
    repo_source: str = "",
    cancel_event=None,  # threading.Event | None
) -> None:
    """Extract *archive_path* and store it as the base for *runner*.

    The archive must be a standard Bottles backup (``.tar.gz``) whose
    top-level directory is the bottle root.  Any previously installed base
    for *runner* is atomically replaced.

    *progress_cb* receives a 0 → 1 fraction during extraction.
    *repo_source* is the URI or path of the repo the archive came from,
    stored in the database for informational purposes.

    Raises :exc:`BaseStoreError` on failure.
    Raises :exc:`~cellar.backend.installer.InstallCancelled` if *cancel_event*
    is set during extraction or the file-copy phase.
    """
    # Import lazily to avoid circular-import issues with installer.py.
    from cellar.backend.installer import (  # noqa: PLC0415
        InstallCancelled, InstallError, _extract_archive, _find_bottle_dir,
    )

    archive_path = Path(archive_path)
    dest = base_path(runner)

    with tempfile.TemporaryDirectory(prefix="cellar-base-") as tmp_str:
        tmp = Path(tmp_str)
        extract_dir = tmp / "extracted"
        extract_dir.mkdir()

        try:
            _extract_archive(archive_path, extract_dir, cancel_event,
                             progress_cb=progress_cb)
        except InstallCancelled:
            raise
        except InstallError as exc:
            raise BaseStoreError(f"Failed to extract base archive: {exc}") from exc

        try:
            bottle_src = _find_bottle_dir(extract_dir)
        except InstallError as exc:
            raise BaseStoreError(str(exc)) from exc

        # Atomic replace: remove old base, copy new one in.
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            for src in bottle_src.rglob("*"):
                if cancel_event and cancel_event.is_set():
                    shutil.rmtree(dest, ignore_errors=True)
                    raise InstallCancelled("Base installation cancelled")
                rel = src.relative_to(bottle_src)
                dst = dest / rel
                if src.is_dir():
                    dst.mkdir(parents=True, exist_ok=True)
                elif src.is_file():
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
        except InstallCancelled:
            raise
        except Exception as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise BaseStoreError(f"Failed to store base: {exc}") from exc

    database.mark_base_installed(runner, repo_source)


def install_base_from_dir(
    prefix_path: Path,
    runner: str,
    *,
    progress_cb: Callable[[float], None] | None = None,
    repo_source: str = "",
    cancel_event=None,  # threading.Event | None
) -> None:
    """Copy an already-extracted prefix directory into the base store.

    Equivalent to :func:`install_base` but skips the archive
    extraction step — useful when the prefix is already available
    locally (e.g. immediately after publishing from the Package Builder).

    Raises :exc:`BaseStoreError` on failure.
    """
    from cellar.backend.installer import InstallCancelled  # noqa: PLC0415

    dest = base_path(runner)
    if dest.exists():
        return
    dest.parent.mkdir(parents=True, exist_ok=True)

    all_files = [p for p in prefix_path.rglob("*") if not p.is_dir()]
    total = len(all_files)
    try:
        for i, src in enumerate(all_files):
            if cancel_event and cancel_event.is_set():
                shutil.rmtree(dest, ignore_errors=True)
                raise InstallCancelled("Base installation cancelled")
            rel = src.relative_to(prefix_path)
            dst = dest / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            if progress_cb and total:
                progress_cb((i + 1) / total)
    except InstallCancelled:
        raise
    except Exception as exc:
        shutil.rmtree(dest, ignore_errors=True)
        raise BaseStoreError(f"Failed to store base: {exc}") from exc

    database.mark_base_installed(runner, repo_source)


def remove_base(runner: str) -> None:
    """Remove the installed base for *runner*.  No-op if not present."""
    dest = base_path(runner)
    if dest.is_dir():
        shutil.rmtree(dest)
    database.remove_base_record(runner)

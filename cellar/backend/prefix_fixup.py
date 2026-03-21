"""Fix absolute paths inside a WINEPREFIX after relocation.

When a prefix directory is moved (per-app or bulk), internal absolute
references become stale:

* **Symlinks** — Proton creates ``pfx`` and various ``drive_c/users/``
  symlinks as absolute targets.  After a move they dangle.
* **Wine registry files** — ``system.reg``, ``user.reg``, and
  ``userdef.reg`` may embed the old WINEPREFIX path.
* **Proton tracked_files** — lists absolute paths of files Proton
  installed into the prefix.

:func:`fixup_prefix` rewrites all of the above in a single pass.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

_REG_FILES = ("system.reg", "user.reg", "userdef.reg")


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def fixup_prefix(prefix: Path, old_path: str) -> None:
    """Rewrite stale absolute references inside *prefix*.

    *old_path* is the **previous** absolute path of the prefix directory
    (before the move).  Every occurrence inside symlink targets, registry
    files, and ``tracked_files`` is replaced with ``str(prefix)``.

    Safe to call even when nothing needs fixing — no-ops silently.
    """
    new_path = str(prefix)
    if old_path == new_path:
        return

    _fix_symlinks(prefix, old_path, new_path)
    _fix_registry_files(prefix, old_path, new_path)
    _fix_tracked_files(prefix, old_path, new_path)

    log.info(
        "Prefix fixup complete: %s → %s",
        old_path, new_path,
    )


# ------------------------------------------------------------------
# Symlinks
# ------------------------------------------------------------------

def _fix_symlinks(prefix: Path, old_path: str, new_path: str) -> None:
    """Repoint absolute symlinks whose target contains *old_path*."""
    for dirpath, dirnames, filenames in os.walk(prefix, followlinks=False):
        for name in (*dirnames, *filenames):
            full = os.path.join(dirpath, name)
            if not os.path.islink(full):
                continue
            target = os.readlink(full)
            if old_path not in target:
                continue
            new_target = target.replace(old_path, new_path, 1)
            os.remove(full)
            os.symlink(new_target, full)
            log.debug("Relinked %s → %s", full, new_target)


# ------------------------------------------------------------------
# Wine registry (.reg) files
# ------------------------------------------------------------------

def _fix_registry_files(prefix: Path, old_path: str, new_path: str) -> None:
    """Replace *old_path* references inside Wine .reg files.

    Wine stores Linux paths in the registry with forward slashes and
    with backslash-escaped forward slashes (``\\/``).  We handle both
    representations.
    """
    escaped_old = old_path.replace("/", "\\/")
    escaped_new = new_path.replace("/", "\\/")

    for name in _REG_FILES:
        reg = prefix / name
        if not reg.is_file():
            continue
        try:
            text = reg.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError:
            log.warning("Could not read %s", reg)
            continue

        updated = text.replace(old_path, new_path)
        updated = updated.replace(escaped_old, escaped_new)
        if updated is text:  # identity check — nothing changed
            continue

        try:
            reg.write_text(updated, encoding="utf-8", errors="surrogateescape")
            log.debug("Patched registry file %s", reg)
        except OSError:
            log.warning("Could not write %s", reg)

    # Also check pfx/ subdirectory (Proton compat-data layout).
    pfx = prefix / "pfx"
    if pfx.is_dir() and not pfx.is_symlink():
        for name in _REG_FILES:
            reg = pfx / name
            if not reg.is_file():
                continue
            try:
                text = reg.read_text(encoding="utf-8", errors="surrogateescape")
            except OSError:
                continue
            updated = text.replace(old_path, new_path)
            updated = updated.replace(escaped_old, escaped_new)
            if updated is text:
                continue
            try:
                reg.write_text(updated, encoding="utf-8", errors="surrogateescape")
                log.debug("Patched registry file %s", reg)
            except OSError:
                log.warning("Could not write %s", reg)


# ------------------------------------------------------------------
# Proton tracked_files
# ------------------------------------------------------------------

def _fix_tracked_files(prefix: Path, old_path: str, new_path: str) -> None:
    """Rewrite absolute paths in Proton's ``tracked_files`` manifest."""
    for candidate in (prefix / "tracked_files", prefix / "pfx" / "tracked_files"):
        if not candidate.is_file():
            continue
        try:
            text = candidate.read_text(encoding="utf-8", errors="surrogateescape")
        except OSError:
            continue
        updated = text.replace(old_path, new_path)
        if updated is text:
            continue
        try:
            candidate.write_text(updated, encoding="utf-8", errors="surrogateescape")
            log.debug("Patched tracked_files %s", candidate)
        except OSError:
            log.warning("Could not write %s", candidate)

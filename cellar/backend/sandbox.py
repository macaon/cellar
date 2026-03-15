"""Bubblewrap-based sandbox for running Linux installers in isolation.

Runs arbitrary Linux installer scripts (.sh, .run, ELF binaries) inside a
bwrap sandbox where the host filesystem is mounted read-only and all writes
are captured to a controlled output directory.  This allows the Package
Builder to import Linux apps that ship as installer scripts rather than
plain tarballs.

Requires ``bwrap`` (bubblewrap) on the host.  On Flatpak, uses
``flatpak-spawn --host bwrap …`` since bwrap lives on the host.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Literal

log = logging.getLogger(__name__)

InstallerType = Literal["makeself", "shell", "elf", "unknown"]

# Markers found in the first few KB of Makeself archives.
_MAKESELF_MARKERS = (b"makeself", b"Makeself", b"MAKESELF")


# ---------------------------------------------------------------------------
# Detection helpers
# ---------------------------------------------------------------------------


def is_bwrap_available() -> bool:
    """Return True if bubblewrap (bwrap) is usable.

    Checks the local PATH first.  If Cellar is running inside a Flatpak
    sandbox, checks the host via ``flatpak-spawn --host``.
    """
    from cellar.backend.umu import is_cellar_sandboxed

    if shutil.which("bwrap"):
        return True

    if is_cellar_sandboxed():
        try:
            result = subprocess.run(
                ["flatpak-spawn", "--host", "which", "bwrap"],
                capture_output=True, timeout=5,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

    return False


def detect_installer_type(path: Path) -> InstallerType:
    """Classify an installer file as makeself, shell, ELF, or unknown."""
    try:
        with path.open("rb") as f:
            header = f.read(4096)
    except OSError:
        return "unknown"

    if header[:4] == b"\x7fELF":
        return "elf"

    if any(marker in header for marker in _MAKESELF_MARKERS):
        return "makeself"

    if header[:2] in (b"#!", b"\n#"):
        return "shell"

    return "unknown"


def is_makeself(path: Path) -> bool:
    """Return True if *path* looks like a Makeself self-extracting archive."""
    return detect_installer_type(path) == "makeself"


# ---------------------------------------------------------------------------
# Sandbox execution
# ---------------------------------------------------------------------------


def _build_bwrap_cmd(
    installer_path: Path,
    capture_dir: Path,
    *,
    install_prefix: str = "/opt/app",
    installer_args: list[str] | None = None,
    block_network: bool = False,
) -> list[str]:
    """Build the bwrap command line for an isolated installer run.

    Mount layout:
    - ``/`` read-only (full host view — libraries, binaries, locales)
    - ``/dev``, ``/proc`` device/process nodes
    - ``/tmp`` writable tmpfs
    - ``<capture_dir>/root`` bound to ``<install_prefix>`` (writable)
    - ``<capture_dir>/home`` bound to ``/home/<user>`` (writable)

    Display passthrough (X11/Wayland) is enabled so interactive installers
    (MojoSetup, etc.) can show their GUI.
    """
    root_capture = capture_dir / "root"
    home_capture = capture_dir / "home"
    root_capture.mkdir(parents=True, exist_ok=True)
    home_capture.mkdir(parents=True, exist_ok=True)

    user = os.environ.get("USER", "user")

    cmd: list[str] = [
        "bwrap",
        "--ro-bind", "/", "/",
        "--dev", "/dev",
        "--proc", "/proc",
        "--tmpfs", "/tmp",
        "--bind", str(root_capture), install_prefix,
        "--bind", str(home_capture), f"/home/{user}",
        "--die-with-parent",
    ]

    # Display passthrough — interactive installers need GUI access.
    # X11: bind the X socket; Wayland: bind XDG_RUNTIME_DIR for the socket.
    x11_display = os.environ.get("DISPLAY")
    if x11_display:
        x11_socket = f"/tmp/.X11-unix/X{x11_display.rsplit(':', 1)[-1].split('.')[0]}"
        if Path(x11_socket).exists():
            cmd.extend(["--bind", x11_socket, x11_socket])

    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime and Path(xdg_runtime).is_dir():
        cmd.extend(["--bind", xdg_runtime, xdg_runtime])

    if block_network:
        cmd.append("--unshare-net")

    # The installer itself — make it accessible inside the sandbox
    cmd.extend(["--ro-bind", str(installer_path), str(installer_path)])

    # Run via bash for shell scripts; direct exec for ELF
    itype = detect_installer_type(installer_path)
    if itype == "elf":
        cmd.append(str(installer_path))
    else:
        cmd.extend(["bash", str(installer_path)])

    if installer_args:
        cmd.extend(installer_args)

    return cmd


def run_isolated_installer(
    installer_path: Path,
    capture_dir: Path,
    *,
    install_prefix: str = "/opt/app",
    installer_args: list[str] | None = None,
    block_network: bool = False,
    timeout: int = 3600,
) -> int:
    """Run an installer inside a bwrap sandbox, capturing writes to *capture_dir*.

    The installer runs interactively — stdout/stderr are inherited so GUI
    installers (MojoSetup, etc.) can display and accept user input.

    Returns the installer's exit code.

    *capture_dir* will contain two subdirectories after completion:
    - ``root/`` — files written to *install_prefix*
    - ``home/`` — files written to the user's home directory

    If Cellar is running inside a Flatpak, the bwrap command is executed on
    the host via ``flatpak-spawn --host``.
    """
    from cellar.backend.umu import is_cellar_sandboxed

    cmd = _build_bwrap_cmd(
        installer_path,
        capture_dir,
        install_prefix=install_prefix,
        installer_args=installer_args,
        block_network=block_network,
    )

    if is_cellar_sandboxed():
        cmd = ["flatpak-spawn", "--host"] + cmd

    log.info("Running isolated installer: %s", " ".join(cmd[:6]) + " …")

    # Run interactively — inherit stdio so the installer can show its GUI
    # and the user can interact with it (same as run_in_prefix for Windows).
    result = subprocess.run(cmd, timeout=timeout, capture_output=False)

    log.info("Isolated installer exited with code %d", result.returncode)
    return result.returncode


def try_makeself_noexec(
    installer_path: Path,
    capture_dir: Path,
    *,
    line_cb: Callable[[str], None] | None = None,
) -> bool:
    """Try fast Makeself extraction via --noexec --target=<dir>.

    Returns True if extraction produced files, False otherwise.
    This is faster and safer than full bwrap isolation since it just
    unpacks the archive without running the embedded install script.
    """
    if not is_makeself(installer_path):
        return False

    target = capture_dir / "root"
    target.mkdir(parents=True, exist_ok=True)

    cmd = ["bash", str(installer_path), "--noexec", f"--target={target}"]
    log.info("Trying Makeself --noexec extraction: %s", installer_path.name)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
        )
    except subprocess.TimeoutExpired:
        log.warning("Makeself --noexec timed out after 300s")
        return False

    if result.returncode != 0:
        log.info("Makeself --noexec failed (exit %d), will try bwrap", result.returncode)
        return False

    # Check if anything was actually extracted
    extracted = list(target.iterdir())
    if not extracted:
        log.info("Makeself --noexec produced empty output, will try bwrap")
        return False

    log.info("Makeself --noexec extracted %d top-level entries", len(extracted))
    return True


# ---------------------------------------------------------------------------
# Post-install cleanup
# ---------------------------------------------------------------------------


def cleanup_captured_install(capture_dir: Path) -> Path:
    """Consolidate captured installer output into a clean app directory.

    Scans ``capture_dir/root/`` and ``capture_dir/home/`` for the actual
    app files.  If the root capture has a single subdirectory, promotes it
    to be the content root.  Removes empty directories, temp files, and
    installer logs.

    Returns the effective content root path (may be *capture_dir* itself
    if files are at the top level already, or a subdirectory).
    """
    root = capture_dir / "root"
    home = capture_dir / "home"

    # Merge home captures into root (if any meaningful files exist)
    if home.is_dir():
        home_files = list(home.rglob("*"))
        if home_files:
            log.info("Merging %d home-captured entries into root", len(home_files))
            _merge_tree(home, root)
        shutil.rmtree(home, ignore_errors=True)

    if not root.is_dir():
        return capture_dir

    # Strip single-child directory chains (e.g. root/opt/app/MyGame/ → root/MyGame/)
    effective = _unwrap_single_child(root)

    # Remove common installer junk
    _remove_junk(effective)

    # If the effective root differs from root, move contents up
    if effective != root:
        _promote_contents(effective, root)

    return root


def _merge_tree(src: Path, dst: Path) -> None:
    """Recursively copy src into dst, overwriting on conflict."""
    for item in src.rglob("*"):
        if not item.is_file():
            continue
        rel = item.relative_to(src)
        target = dst / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target)


def _unwrap_single_child(root: Path) -> Path:
    """Walk down single-child directory chains to find the real app root."""
    current = root
    for _ in range(10):  # safety limit
        children = [c for c in current.iterdir() if not c.name.startswith(".")]
        if len(children) == 1 and children[0].is_dir():
            current = children[0]
        else:
            break
    return current


def _promote_contents(src: Path, dst: Path) -> None:
    """Move all contents of *src* directly into *dst*."""
    for item in list(src.iterdir()):
        target = dst / item.name
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        shutil.move(str(item), str(target))

    # Remove the now-empty chain between dst and src
    current = src
    while current != dst:
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


_JUNK_NAMES = {
    ".install_log", "install.log", "installer.log",
    ".installed", ".install", "__pycache__",
}

_JUNK_SUFFIXES = {".tmp", ".bak", ".log"}


def _remove_junk(root: Path) -> None:
    """Remove common installer temp files and empty directories."""
    # Remove junk files
    for p in list(root.rglob("*")):
        if not p.is_file():
            continue
        if p.name.lower() in _JUNK_NAMES or p.suffix.lower() in _JUNK_SUFFIXES:
            p.unlink(missing_ok=True)

    # Remove empty directories (bottom-up)
    for p in sorted(root.rglob("*"), reverse=True):
        if p.is_dir():
            try:
                p.rmdir()
            except OSError:
                pass

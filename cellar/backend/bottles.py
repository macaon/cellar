"""Bottles installation detection and bottles-cli wrapper.

Detection order for the Bottles data directory
-----------------------------------------------
1. ``bottles_data_path`` override from ``config.json``  (variant = ``"custom"``)
2. ``~/.var/app/com.usebottles.bottles/data/bottles/bottles/``  (variant = ``"flatpak"``)
3. ``~/.local/share/bottles/bottles/``                 (variant = ``"native"``)

If none of the above directories exist, ``detect_bottles()`` returns ``None``.

bottles-cli invocation
----------------------
Two independent factors determine the final command:

* **Cellar sandbox** — if Cellar itself runs inside a Flatpak sandbox
  (``/.flatpak-info`` exists), every bottles-cli call must be prefixed
  with ``flatpak-spawn --host`` to escape the sandbox.

* **Bottles variant** — the Flatpak edition of Bottles does not install
  ``bottles-cli`` on ``$PATH``; it must be called as
  ``flatpak run --command=bottles-cli com.usebottles.bottles``.
  Native installs expose it directly as ``bottles-cli``.

``BottlesInstall.cli_cmd`` contains the fully-resolved base command list.
Callers extend it with subcommand and flags::

    subprocess.run(install.cli_cmd + ["list", "bottles"], capture_output=True)
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


_FLATPAK_DATA = Path.home() / ".var/app/com.usebottles.bottles/data/bottles/bottles"
_NATIVE_DATA = Path.home() / ".local/share/bottles/bottles"
_FLATPAK_INFO = Path("/.flatpak-info")

_BOTTLES_FLATPAK_ID = "com.usebottles.bottles"


class BottlesError(Exception):
    """Raised when a bottles-cli call fails or the executable is not found."""


@dataclass
class BottlesInstall:
    """Result of a successful Bottles detection."""

    data_path: Path      # path to the bottles/ directory
    variant: str         # "flatpak" | "native" | "custom"
    cli_cmd: list[str]   # base command list for bottles-cli invocation


def is_cellar_sandboxed() -> bool:
    """Return True if Cellar is running inside a Flatpak sandbox."""
    return _FLATPAK_INFO.exists()


def detect_all_bottles(
    override_path: Path | str | None = None,
) -> list[BottlesInstall]:
    """Return all detected Bottles installations, in preference order.

    Parameters
    ----------
    override_path:
        Explicit path to the Bottles *bottles/* directory, e.g. read from
        ``config.json``.  When supplied, only that path is checked (returns
        a single-item list or an empty list).

    Returns
    -------
    A list of ``BottlesInstall`` objects.  Order is: custom override →
    Flatpak installation → native installation.  An empty list means no
    Bottles installation was found.
    """
    sandboxed = is_cellar_sandboxed()

    if override_path is not None:
        p = Path(override_path).expanduser()
        if p.is_dir():
            return [BottlesInstall(
                data_path=p,
                variant="custom",
                cli_cmd=_build_cli_cmd(is_flatpak_bottles=False, sandboxed=sandboxed),
            )]
        return []

    results: list[BottlesInstall] = []

    if _FLATPAK_DATA.is_dir():
        results.append(BottlesInstall(
            data_path=_FLATPAK_DATA,
            variant="flatpak",
            cli_cmd=_build_cli_cmd(is_flatpak_bottles=True, sandboxed=sandboxed),
        ))

    if _NATIVE_DATA.is_dir():
        results.append(BottlesInstall(
            data_path=_NATIVE_DATA,
            variant="native",
            cli_cmd=_build_cli_cmd(is_flatpak_bottles=False, sandboxed=sandboxed),
        ))

    return results


def detect_bottles(override_path: Path | str | None = None) -> BottlesInstall | None:
    """Detect the first available Bottles installation.

    Convenience wrapper around :func:`detect_all_bottles` that returns the
    highest-priority installation, or ``None`` if none is found.
    """
    results = detect_all_bottles(override_path)
    return results[0] if results else None


def _build_cli_cmd(*, is_flatpak_bottles: bool, sandboxed: bool) -> list[str]:
    """Return the bottles-cli base command for this environment.

    The four combinations::

        native Bottles,  unsandboxed Cellar → ["bottles-cli"]
        native Bottles,  sandboxed   Cellar → ["flatpak-spawn", "--host", "bottles-cli"]
        Flatpak Bottles, unsandboxed Cellar → ["flatpak", "run", "--command=bottles-cli",
                                                "com.usebottles.bottles"]
        Flatpak Bottles, sandboxed   Cellar → ["flatpak-spawn", "--host",
                                                "flatpak", "run", "--command=bottles-cli",
                                                "com.usebottles.bottles"]
    """
    if is_flatpak_bottles:
        inner = ["flatpak", "run", f"--command=bottles-cli", _BOTTLES_FLATPAK_ID]
    else:
        inner = ["bottles-cli"]

    if sandboxed:
        return ["flatpak-spawn", "--host"] + inner
    return inner


# ---------------------------------------------------------------------------
# bottles-cli wrapper
# ---------------------------------------------------------------------------

def list_bottles(install: BottlesInstall) -> list[str]:
    """Return the names of all bottles in this Bottles installation.

    Calls ``bottles-cli list bottles`` and parses the output.
    Returns an empty list when no bottles are found.
    Raises ``BottlesError`` on any subprocess failure.
    """
    result = _run(install, ["list", "bottles"])
    return _parse_bottle_list(result.stdout)


def edit_bottle(
    install: BottlesInstall,
    bottle_name: str,
    key: str,
    value: str,
) -> None:
    """Update a Wine component key in *bottle_name*.

    Calls ``bottles-cli edit -b <name> -k <key> -v <value>``.

    Common keys and example values::

        "Runner"  → "proton-ge-9-1"
        "DXVK"    → "2.3"
        "VKD3D"   → "2.11"

    Raises ``BottlesError`` on any subprocess failure.
    """
    _run(install, ["edit", "-b", bottle_name, "-k", key, "-v", value])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _run(
    install: BottlesInstall,
    args: list[str],
    *,
    timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Run ``install.cli_cmd + args`` and return the completed process.

    Raises ``BottlesError`` if the executable is not found, the process
    times out, or it exits with a non-zero return code.
    """
    cmd = install.cli_cmd + args
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        raise BottlesError(f"bottles-cli not found ({cmd[0]!r} is not on PATH)")
    except subprocess.TimeoutExpired:
        raise BottlesError(f"bottles-cli timed out after {timeout}s")
    if result.returncode != 0:
        msg = result.stderr.strip() or f"bottles-cli exited with code {result.returncode}"
        raise BottlesError(msg)
    return result


def _parse_bottle_list(output: str) -> list[str]:
    """Extract bottle names from ``bottles-cli list bottles`` text output.

    The command prints::

        Found 3 bottles:
        - MyBottle
        - AnotherBottle

    or nothing at all when no bottles are installed.
    Lines not starting with ``"- "`` (after stripping) are ignored.
    """
    names = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            name = stripped[2:].strip()
            if name:
                names.append(name)
    return names

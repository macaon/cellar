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

from dataclasses import dataclass
from pathlib import Path


_FLATPAK_DATA = Path.home() / ".var/app/com.usebottles.bottles/data/bottles/bottles"
_NATIVE_DATA = Path.home() / ".local/share/bottles/bottles"
_FLATPAK_INFO = Path("/.flatpak-info")

_BOTTLES_FLATPAK_ID = "com.usebottles.bottles"


@dataclass
class BottlesInstall:
    """Result of a successful Bottles detection."""

    data_path: Path      # path to the bottles/ directory
    variant: str         # "flatpak" | "native" | "custom"
    cli_cmd: list[str]   # base command list for bottles-cli invocation


def is_cellar_sandboxed() -> bool:
    """Return True if Cellar is running inside a Flatpak sandbox."""
    return _FLATPAK_INFO.exists()


def detect_bottles(override_path: Path | str | None = None) -> BottlesInstall | None:
    """Detect the active Bottles installation.

    Parameters
    ----------
    override_path:
        Explicit path to the Bottles *bottles/* directory, e.g. read from
        ``config.json``.  Skips auto-detection when supplied, but still
        returns ``None`` if the path does not exist.

    Returns
    -------
    ``BottlesInstall`` if a Bottles installation is found, ``None`` otherwise.
    """
    sandboxed = is_cellar_sandboxed()

    if override_path is not None:
        p = Path(override_path).expanduser()
        if p.is_dir():
            return BottlesInstall(
                data_path=p,
                variant="custom",
                cli_cmd=_build_cli_cmd(is_flatpak_bottles=False, sandboxed=sandboxed),
            )
        return None

    if _FLATPAK_DATA.is_dir():
        return BottlesInstall(
            data_path=_FLATPAK_DATA,
            variant="flatpak",
            cli_cmd=_build_cli_cmd(is_flatpak_bottles=True, sandboxed=sandboxed),
        )

    if _NATIVE_DATA.is_dir():
        return BottlesInstall(
            data_path=_NATIVE_DATA,
            variant="native",
            cli_cmd=_build_cli_cmd(is_flatpak_bottles=False, sandboxed=sandboxed),
        )

    return None


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

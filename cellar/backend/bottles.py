"""Bottles installation detection and bottles-cli wrapper.

Detection order
---------------
1. ``bottles_data_path`` override from ``config.json``  (variant = ``"custom"``)
2. Flatpak Bottles — data dir ``~/.var/app/com.usebottles.bottles/data/bottles/bottles/``
   must exist.  (variant = ``"flatpak"``)
3. Native Bottles — ``bottles-cli`` must be on ``$PATH`` **and** the data dir
   ``~/.local/share/bottles/bottles/`` must exist.  (variant = ``"native"``)

The Flatpak variant only checks the data directory (Flatpak always creates it on
first launch and it cannot be confused with another application).  The native
variant additionally requires the binary because ``~/.local/share/bottles/bottles/``
can be left behind by a previous install or created by other tooling.

If none of the conditions match, ``detect_all_bottles()`` returns an empty list.

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

import shutil
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

    if _NATIVE_DATA.is_dir() and shutil.which("bottles-cli"):
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
# bottle.yml program reader
# ---------------------------------------------------------------------------

def read_bottle_programs(bottle_path: Path) -> list[dict]:
    """Return non-removed External_Programs entries from *bottle_path*/bottle.yml.

    Each returned dict contains at least ``name``, ``executable``, ``path``,
    and optionally ``arguments``.  Programs with ``removed: true`` are excluded.
    """
    import yaml

    yml_path = bottle_path / "bottle.yml"
    if not yml_path.exists():
        return []
    try:
        data = yaml.safe_load(yml_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml.YAMLError):
        return []

    if not isinstance(data, dict):
        return []

    external = data.get("External_Programs") or {}
    programs = [p for p in external.values() if isinstance(p, dict)]
    return [p for p in programs if p.get("removed") is not True]


def list_bottle_programs(install: BottlesInstall, bottle_dir: str) -> list[dict]:
    """Return all programs available in a bottle, including auto-discovered ones.

    Calls ``bottles-cli --json programs -b <name>`` which returns a JSON array
    of program dicts — including ``.lnk`` shortcuts auto-discovered by Bottles
    from ``drive_c`` Desktop and Start Menu directories.  Each dict contains
    ``name``, ``executable``, ``path``, and ``auto_discovered`` (bool) fields.

    All returned programs (registered and auto-discovered) can be launched via
    ``bottles-cli run -b <name> -p <program_name>``.

    Falls back to ``External_Programs`` from ``bottle.yml`` only when
    ``bottles-cli`` is unavailable or fails.
    """
    # Try the CLI first — it gives us the full merged list.
    try:
        display_name = _bottle_display_name(install, bottle_dir)
        result = _run(install, ["--json", "programs", "-b", display_name])
        programs = _parse_programs_json(result.stdout)
        if programs is not None:
            return programs
    except (BottlesError, Exception):
        pass

    # Fallback: read External_Programs from bottle.yml directly.
    return read_bottle_programs(install.data_path / bottle_dir)


def _parse_programs_json(output: str) -> list[dict] | None:
    """Parse JSON output from ``bottles-cli --json programs -b``.

    The command outputs logging lines on stderr/stdout followed by a JSON
    array on the last non-empty line.  Returns ``None`` if parsing fails.
    """
    import json

    # The JSON array is on the last non-empty line.
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line:
            continue
        if line.startswith("["):
            try:
                data = json.loads(line)
                if isinstance(data, list):
                    return [p for p in data if isinstance(p, dict)]
            except (json.JSONDecodeError, ValueError):
                pass
            break
    return None


# ---------------------------------------------------------------------------
# Launch helpers
# ---------------------------------------------------------------------------

def _bottle_display_name(install: BottlesInstall, bottle_dir: str) -> str:
    """Return the bottle's display name from bottle.yml.

    ``bottles-cli run -b`` expects the ``Name`` field from ``bottle.yml``,
    not the directory name.  Falls back to *bottle_dir* if the file is missing
    or unparseable.
    """
    import yaml

    yml_path = install.data_path / bottle_dir / "bottle.yml"
    try:
        data = yaml.safe_load(yml_path.read_text(encoding="utf-8", errors="replace"))
        if isinstance(data, dict) and data.get("Name"):
            return str(data["Name"])
    except (OSError, yaml.YAMLError):
        pass
    return bottle_dir


def launch_bottle(
    install: BottlesInstall,
    bottle_name: str,
    entry_point: str | None = None,
    program: dict | None = None,
) -> None:
    """Launch a program inside a bottle, or open the Bottles GUI as a fallback.

    Fire-and-forget: spawned with ``start_new_session=True``.

    ``bottles-cli run`` flags used:

    * ``-b <display_name>`` — the ``Name`` field from ``bottle.yml``; resolved
      via :func:`_bottle_display_name`.
    * ``-p <program_name>`` — for registered ``External_Programs`` entries;
      bottles-cli looks up the entry by ``name``, resolves its path, and
      applies its stored arguments automatically.
    * ``-e <exe_path>`` — for a raw catalogue ``entry_point``; tells
      bottles-cli to run that executable directly via wine.

    Priority:
    1. *program* dict (from ``read_bottle_programs``) — run by program name
       via ``-p``; bottles-cli handles path and arguments from its own config.
    2. *entry_point* string (from the catalogue) — run executable via ``-e``.
    3. Neither — opens the Bottles GUI for the user to start the app manually.
    """
    import subprocess

    if program or entry_point:
        display_name = _bottle_display_name(install, bottle_name)
        if program:
            prog_name = program.get("name") or ""
            cmd = install.cli_cmd + ["run", "-b", display_name, "-p", prog_name]
        else:
            cmd = install.cli_cmd + ["run", "-b", display_name, "-e", entry_point]
    else:
        if install.variant == "flatpak":
            inner: list[str] = ["flatpak", "run", _BOTTLES_FLATPAK_ID]
        else:
            inner = ["bottles"]
        cmd = (["flatpak-spawn", "--host"] + inner) if is_cellar_sandboxed() else inner

    subprocess.Popen(cmd, start_new_session=True)


# ---------------------------------------------------------------------------
# Runner helpers
# ---------------------------------------------------------------------------


def runners_dir(install: BottlesInstall) -> Path:
    """Return the path to the runners/ directory for this Bottles installation.

    Flatpak: ``~/.var/app/com.usebottles.bottles/data/bottles/runners/``
    Native:  ``~/.local/share/bottles/runners/``
    """
    return install.data_path.parent / "runners"


def list_runners(install: BottlesInstall) -> list[str]:
    """Return the names of all runners available to this Bottles installation.

    Three sources are combined:

    1. **Directory runners** — subdirectory names inside ``runners/`` next to
       the Bottles data root (e.g. ``ge-proton10-32``, ``soda-9.0-1``).
    2. **System Wine** — detected by running the appropriate Wine binary and
       mapping the output to the ``sys-wine-<version>`` naming Bottles uses
       (e.g. ``wine-11.0`` → ``sys-wine-11.0``).  For Flatpak Bottles this
       is the Wine binary bundled inside the Flatpak; for native Bottles it
       is ``wine`` on ``$PATH``.
    3. **bottle.yml scan** — any ``sys-wine-*`` runner already referenced by
       an existing bottle; catches edge cases where the version string Bottles
       recorded differs from the current ``wine --version`` output.

    Returns a sorted, de-duplicated list.
    Raises ``BottlesError`` if the runners directory exists but cannot be read.
    """
    found: set[str] = set()

    # 1. Directory-based runners
    rdir = runners_dir(install)
    if rdir.is_dir():
        try:
            found.update(d.name for d in rdir.iterdir() if d.is_dir())
        except OSError as exc:
            raise BottlesError(f"Cannot list runners at {rdir}: {exc}") from exc

    # 2. System Wine via the appropriate wine binary
    found.update(_detect_system_wine(install))

    # 3. Scan bottle.yml files for referenced sys-wine runners
    try:
        import yaml
        for bottle_dir in install.data_path.iterdir():
            yml = bottle_dir / "bottle.yml"
            if not yml.is_file():
                continue
            try:
                data = yaml.safe_load(yml.read_text(encoding="utf-8", errors="replace"))
                runner = (data or {}).get("Runner", "") or ""
                if runner.startswith("sys-wine-"):
                    found.add(runner)
            except Exception:  # noqa: BLE001
                pass
    except OSError:
        pass

    return sorted(found)


# Candidate paths for Wine bundled inside the Bottles Flatpak.
# Bottles is installed either system-wide (/var/lib/flatpak) or per-user
# (~/.local/share/flatpak); we probe both.
_FLATPAK_WINE_PATHS: list[Path] = [
    Path("/var/lib/flatpak/app/com.usebottles.bottles/current/active/files/bin/wine"),
    Path.home() / ".local/share/flatpak/app/com.usebottles.bottles/current/active/files/bin/wine",
]


def _detect_system_wine(install: BottlesInstall) -> list[str]:
    """Return ``['sys-wine-X.Y']`` if Wine is detectable for *install*, else ``[]``.

    **Flatpak Bottles** — Wine is bundled inside the Flatpak, not on
    ``$PATH``.  We probe the two standard Flatpak installation prefixes
    (system and per-user) for the Wine binary and run whichever one exists.
    When Cellar itself is sandboxed the command is prefixed with
    ``flatpak-spawn --host`` so we can reach the host filesystem.

    **Native / custom Bottles** — we run ``wine --version`` from ``$PATH``
    (again with the ``flatpak-spawn --host`` prefix when Cellar is sandboxed).
    """
    for cmd in _wine_version_cmds(install):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                ver = result.stdout.strip()         # e.g. "wine-11.0"
                if ver.startswith("wine-"):
                    return [f"sys-wine-{ver[5:]}"]  # "sys-wine-11.0"
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
    return []


def _wine_version_cmds(install: BottlesInstall) -> list[list[str]]:
    """Return an ordered list of ``[cmd, ...]`` candidates to try for ``wine --version``.

    Each entry is a complete argv list.  Callers try them in order and stop
    at the first that succeeds.
    """
    spawn = ["flatpak-spawn", "--host"] if is_cellar_sandboxed() else []

    if install.variant == "flatpak":
        # The Wine binary lives inside the Flatpak bundle at a fixed path.
        # When Cellar is sandboxed we can't stat host paths, so we emit both
        # candidates and let the subprocess failure serve as the probe.
        return [
            spawn + [str(p), "--version"]
            for p in _FLATPAK_WINE_PATHS
        ]

    # Native or custom: Wine should be on $PATH.
    return [spawn + ["wine", "--version"]]


def get_runners_in_use(install: BottlesInstall) -> set[str]:
    """Return the set of runner names referenced by any bottle in *install*.

    Scans every ``bottle.yml`` under ``install.data_path`` and collects the
    ``Runner`` field value.  Returns an empty set when no bottles are found or
    the data path cannot be read.
    """
    import yaml

    in_use: set[str] = set()
    try:
        for bottle_dir in install.data_path.iterdir():
            yml = bottle_dir / "bottle.yml"
            if not yml.is_file():
                continue
            try:
                data = yaml.safe_load(yml.read_text(encoding="utf-8", errors="replace"))
                runner = (data or {}).get("Runner", "") or ""
                if runner:
                    in_use.add(runner)
            except Exception:  # noqa: BLE001
                pass
    except OSError:
        pass
    return in_use


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

        "DXVK"   → "2.3"
        "VKD3D"  → "2.11"

    .. note::
        bottles-cli does **not** support ``-k Runner``.  Use
        :func:`set_bottle_runner` to change the runner field.

    Raises ``BottlesError`` on any subprocess failure.
    """
    _run(install, ["edit", "-b", bottle_name, "-k", key, "-v", value])


def set_bottle_runner(
    install: BottlesInstall,
    bottle_name: str,
    runner_name: str,
) -> None:
    """Set the ``Runner`` field in *bottle_name*/bottle.yml directly.

    ``bottles-cli edit -k Runner`` is not supported by recent versions of
    bottles-cli.  This function reads the YAML, updates the ``Runner`` key,
    and writes it back — more reliable and no subprocess required.

    Raises ``BottlesError`` if the file cannot be read or written.
    """
    import yaml

    yml_path = install.data_path / bottle_name / "bottle.yml"
    if not yml_path.exists():
        raise BottlesError(f"bottle.yml not found in bottle {bottle_name!r}")
    try:
        data = yaml.safe_load(yml_path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, yaml.YAMLError) as exc:
        raise BottlesError(
            f"Failed to read bottle.yml for {bottle_name!r}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise BottlesError(f"Unexpected bottle.yml format in {bottle_name!r}")
    data["Runner"] = runner_name
    try:
        yml_path.write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False),
            encoding="utf-8",
        )
    except OSError as exc:
        raise BottlesError(
            f"Failed to write bottle.yml for {bottle_name!r}: {exc}"
        ) from exc


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

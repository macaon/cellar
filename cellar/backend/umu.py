"""umu-launcher integration — path helpers, detection, and launch.

umu-launcher (https://github.com/Open-Wine-Components/umu-launcher) replaces
Bottles as the Windows compatibility layer for Cellar.  It runs arbitrary
Windows executables inside a GE-Proton environment without requiring Steam.

Storage layout
--------------
~/.local/share/cellar/
  runners/
    ge-proton10-32/      ← GE-Proton; managed by backend/runners.py
  prefixes/
    <app-id>/            ← one WINEPREFIX per installed app
  projects/
    <slug>/              ← Package Builder working area (Phase 3)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

_FLATPAK_INFO = Path("/.flatpak-info")


def is_cellar_sandboxed() -> bool:
    """Return True if Cellar is running inside a Flatpak sandbox."""
    return _FLATPAK_INFO.exists()


def _probe_umu(cmd: list[str]) -> bool:
    """Return True if *cmd* + ['--help'] exits with code 0."""
    try:
        r = subprocess.run(cmd + ["--help"], capture_output=True, timeout=5)
        return r.returncode == 0
    except Exception:
        return False


def detect_umu(override: str | None = None) -> str | None:
    """Return an invocation string for umu-launcher, or None if not found.

    Search order:
    1. *override* (from ``config.json`` ``umu_path`` key)
    2. ``sys.executable -m umu`` — the Python running Cellar; most likely to
       have umu installed when the user installed it via pip/pipx/uv.
    3. ``umu-run`` on ``$PATH`` — only if the script actually works (its
       shebang may point to a different Python that lacks the umu package).
    4. ``/app/bin/umu-run`` (Flatpak bundle location)
    """
    import sys
    if override:
        return override
    # Prefer the interpreter that's running Cellar right now.
    if _probe_umu([sys.executable, "-m", "umu"]):
        return f"{sys.executable} -m umu"
    # Fall back to the umu-run wrapper script — but only if it actually works.
    found = shutil.which("umu-run")
    if found and _probe_umu([found]):
        return found
    bundled = Path("/app/bin/umu-run")
    if bundled.is_file() and _probe_umu([str(bundled)]):
        return str(bundled)
    return None


def runners_dir() -> Path:
    """Return (and create if needed) the Cellar runners directory."""
    from cellar.backend.config import data_dir
    d = data_dir() / "runners"
    d.mkdir(parents=True, exist_ok=True)
    return d


def prefixes_dir() -> Path:
    """Return (and create if needed) the Cellar prefixes directory."""
    from cellar.backend.config import install_data_dir
    d = install_data_dir() / "prefixes"
    d.mkdir(parents=True, exist_ok=True)
    return d


def projects_dir() -> Path:
    """Return (and create if needed) the Cellar projects directory."""
    from cellar.backend.config import data_dir
    d = data_dir() / "projects"
    d.mkdir(parents=True, exist_ok=True)
    return d


def native_dir() -> Path:
    """Return (and create if needed) the Cellar Linux native apps directory."""
    from cellar.backend.config import install_data_dir
    d = install_data_dir() / "native"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_runner_path(runner_name: str) -> Path | None:
    """Return the path for *runner_name* if installed, else None."""
    p = runners_dir() / runner_name
    return p if p.is_dir() else None


def build_env(
    app_id: str,
    runner_name: str,
    steam_appid: int | None,
    *,
    prefix_dir: Path | None = None,
) -> dict[str, str]:
    """Return the environment variables dict for a umu invocation."""
    gameid = f"umu-{steam_appid}" if steam_appid else "0"
    wineprefix = prefix_dir if prefix_dir is not None else prefixes_dir() / app_id
    return {
        "WINEPREFIX": str(wineprefix),
        "PROTONPATH": str(runners_dir() / runner_name),
        "GAMEID": gameid,
    }


def _umu_cmd() -> list[str]:
    """Return the base umu-run command, prefixed with flatpak-spawn if sandboxed."""
    from cellar.backend.config import load_umu_path
    umu = detect_umu(load_umu_path())
    if umu is None:
        log.warning("umu-launcher not found — launch will likely fail")
        umu = "umu-run"
    # detect_umu may return a multi-word invocation like "/usr/bin/python3 -m umu"
    parts = umu.split()
    if is_cellar_sandboxed():
        return ["flatpak-spawn", "--host"] + parts
    return parts


def launch_app(
    app_id: str,
    entry_point: str,
    runner_name: str,
    steam_appid: int | None,
    *,
    prefix_dir: Path | None = None,
) -> None:
    """Launch *entry_point* inside the *app_id* prefix.  Fire-and-forget.

    *entry_point* should be a Windows-style path (e.g. ``C:\\Program Files\\App\\App.exe``);
    umu-launcher resolves it against WINEPREFIX automatically.  Absolute Linux
    paths are also accepted and passed through unchanged.

    *prefix_dir* overrides the WINEPREFIX; useful when launching from a
    non-standard location such as a Package Builder project prefix.
    """
    import os
    umu_env = build_env(app_id, runner_name, steam_appid, prefix_dir=prefix_dir)
    env = {**os.environ, **umu_env}
    # Pass exe as positional arg — the primary documented umu-run form.
    cmd = _umu_cmd() + [entry_point]
    log.info(
        "Launching app %s: %s\n  WINEPREFIX=%s\n  PROTONPATH=%s\n  GAMEID=%s\n  EXE=%s",
        app_id, " ".join(cmd[:-1]),
        umu_env["WINEPREFIX"], umu_env["PROTONPATH"], umu_env["GAMEID"], entry_point,
    )
    subprocess.Popen(cmd, env=env, start_new_session=True)


def init_prefix(
    prefix_path: Path,
    runner_name: str,
    *,
    steam_appid: int | None = None,
    timeout: int = 120,
) -> subprocess.CompletedProcess:
    """Initialise a fresh WINEPREFIX via ``umu-run ""``.

    Passing an empty string as the executable is the documented umu-launcher
    way to create/initialise a prefix without running anything.  This lets
    umu handle Steam Runtime setup correctly.
    """
    import os
    gameid = f"umu-{steam_appid}" if steam_appid else "0"
    base_env: dict[str, str] = {
        "WINEPREFIX": str(prefix_path),
        "PROTONPATH": str(runners_dir() / runner_name),
        "GAMEID": gameid,
    }
    prefix_path.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **base_env}
    # Empty-string positional arg → umu initialises the prefix, runs nothing.
    cmd = _umu_cmd() + [""]
    log.info(
        "init_prefix: %s\n  WINEPREFIX=%s\n  PROTONPATH=%s\n  GAMEID=%s",
        " ".join(cmd), base_env["WINEPREFIX"], base_env["PROTONPATH"], gameid,
    )
    result = subprocess.run(cmd, env=env, timeout=timeout, capture_output=False)
    # umu-run "" exits with code 1 because Wine rejects an empty executable path,
    # but the WINEPREFIX is fully initialized at that point.  Callers should check
    # for drive_c existence rather than relying solely on returncode.
    log.info(
        "init_prefix exited with code %d (drive_c exists: %s)",
        result.returncode,
        (prefix_path / "drive_c").is_dir(),
    )
    return result


def run_winetricks(
    prefix_path: Path,
    runner_name: str,
    verbs: list[str],
    *,
    gameid: int = 0,
    timeout: int = 600,
    line_cb: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess:
    """Run winetricks verbs inside *prefix_path* via umu-run.

    Winetricks is a positional argument to umu, not an ``EXE`` env var:
    ``umu-run winetricks <verb1> <verb2> …``

    If *line_cb* is provided, stdout and stderr are merged and each output
    line is passed to *line_cb* as it arrives; otherwise output is inherited
    from the parent process (printed to the terminal).
    """
    import os
    base_env: dict[str, str] = {
        "WINEPREFIX": str(prefix_path),
        "PROTONPATH": str(runners_dir() / runner_name),
        "GAMEID": str(gameid) if gameid else "0",
    }
    env = {**os.environ, **base_env}
    cmd = _umu_cmd() + ["winetricks"] + verbs
    log.info(
        "run_winetricks: %s\n  WINEPREFIX=%s\n  PROTONPATH=%s\n  GAMEID=%s\n  verbs=%s",
        " ".join(cmd[:len(_umu_cmd())]),
        base_env["WINEPREFIX"], base_env["PROTONPATH"], base_env["GAMEID"],
        " ".join(verbs),
    )

    if line_cb is None:
        result = subprocess.run(cmd, env=env, timeout=timeout, capture_output=False)
    else:
        with subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        ) as proc:
            assert proc.stdout is not None
            for raw in proc.stdout:
                # curl uses \r for in-place updates; treat each \r-segment as
                # a separate line so callers see clean final-state lines.
                for part in raw.split("\r"):
                    line = part.rstrip("\n")
                    if line:
                        line_cb(line)
            proc.wait(timeout=timeout)
        result = subprocess.CompletedProcess(cmd, proc.returncode)

    log.info("run_winetricks exited with code %d", result.returncode)
    return result


def run_in_prefix(
    prefix_path: Path,
    runner_name: str,
    exe_path: str,
    *,
    gameid: int = 0,
    extra_env: dict[str, str] | None = None,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """Run a Windows executable inside *prefix_path* using *runner_name*.  Blocking.

    Used by the Package Builder to run ``.exe`` installers.  The executable is
    passed as a positional argument to ``umu-run`` (the primary documented form).

    Parameters
    ----------
    prefix_path:
        Full path to the WINEPREFIX directory.
    runner_name:
        Name of a runner inside ``runners_dir()`` (e.g. ``"GE-Proton10-32"``).
    exe_path:
        Absolute path to the Windows executable to run.
    gameid:
        umu GAMEID integer.  0 means no protonfixes.
    extra_env:
        Additional environment variables merged on top of the umu env.
    timeout:
        Subprocess timeout in seconds.
    """
    import os
    base_env: dict[str, str] = {
        "WINEPREFIX": str(prefix_path),
        "PROTONPATH": str(runners_dir() / runner_name),
        "GAMEID": str(gameid) if gameid else "0",
    }
    if extra_env:
        base_env.update(extra_env)
    env = {**os.environ, **base_env}
    # Pass exe as positional arg — the primary documented umu-run form.
    cmd = _umu_cmd() + [exe_path]
    log.info(
        "run_in_prefix: %s\n  WINEPREFIX=%s\n  PROTONPATH=%s\n  GAMEID=%s\n  EXE=%s",
        " ".join(cmd[:-1]),
        base_env["WINEPREFIX"], base_env["PROTONPATH"], base_env["GAMEID"], exe_path,
    )
    result = subprocess.run(cmd, env=env, timeout=timeout, capture_output=False)
    log.info("run_in_prefix exited with code %d", result.returncode)
    return result

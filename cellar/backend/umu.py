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
import subprocess
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

_FLATPAK_INFO = Path("/.flatpak-info")


def is_cellar_sandboxed() -> bool:
    """Return True if Cellar is running inside a Flatpak sandbox."""
    return _FLATPAK_INFO.exists()


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


def _umu_data_env() -> dict[str, str]:
    """Return env vars to redirect umu's data storage into Cellar's data dir.

    Sets ``UMU_FOLDERS_PATH`` so that umu stores steamrt3 and other runtime
    files alongside Cellar's own data (e.g. inside the Flatpak data dir)
    instead of the real ``~/.local/share/``.
    """
    from cellar.backend.config import data_dir  # noqa: PLC0415
    return {"UMU_FOLDERS_PATH": str(data_dir().parent)}


def is_runtime_ready() -> bool:
    """Return True if umu's steamrt3 runtime is already downloaded."""
    from cellar.backend.config import data_dir  # noqa: PLC0415
    return (data_dir().parent / "umu" / "steamrt3").is_dir()


def build_env(
    app_id: str,
    runner_name: str,
    steam_appid: int | None,
    *,
    prefix_dir: Path | None = None,
) -> dict[str, str]:
    """Return the environment variables dict for a umu / proton invocation."""
    gameid = f"umu-{steam_appid}" if steam_appid else "0"
    wineprefix = prefix_dir if prefix_dir is not None else prefixes_dir() / app_id
    env = {
        **_umu_data_env(),
        "WINEPREFIX": str(wineprefix),
        "PROTONPATH": str(runners_dir() / runner_name),
        "GAMEID": gameid,
    }
    return env


def _umu_cmd() -> list[str]:
    """Return the base umu-run command.

    umu-run is bundled inside the Flatpak at ``/app/bin/umu-run`` and is
    always available on ``$PATH`` within the sandbox.
    """
    return ["umu-run"]


def launch_app(
    app_id: str,
    entry_point: str,
    runner_name: str,
    steam_appid: int | None,
    *,
    prefix_dir: Path | None = None,
    launch_args: str = "",
    extra_env: dict[str, str] | None = None,
) -> None:
    """Launch *entry_point* inside the *app_id* prefix.  Fire-and-forget.

    *entry_point* should be a Windows-style path (e.g. ``C:\\Program Files\\App\\App.exe``);
    umu-launcher resolves it against WINEPREFIX automatically.  Absolute Linux
    paths are also accepted and passed through unchanged.

    *prefix_dir* overrides the WINEPREFIX; useful when launching from a
    non-standard location such as a Package Builder project prefix.

    *extra_env* is merged on top of the umu environment, useful for per-app
    Proton tweaks such as ``PROTON_USE_WINED3D=1``.
    """
    import os
    import shlex
    umu_env = build_env(app_id, runner_name, steam_appid, prefix_dir=prefix_dir)
    env = {**os.environ, **umu_env, **(extra_env or {})}
    cmd = _umu_cmd() + [entry_point]
    if launch_args:
        cmd += shlex.split(launch_args)
    log.info(
        "Launching app %s via umu-run\n  WINEPREFIX=%s\n  PROTONPATH=%s\n  GAMEID=%s\n  EXE=%s%s",
        app_id,
        umu_env["WINEPREFIX"], umu_env["PROTONPATH"], umu_env["GAMEID"], entry_point,
        ("\n  EXTRA_ENV=" + " ".join(f"{k}={v}" for k, v in extra_env.items())) if extra_env else "",
    )
    subprocess.Popen(cmd, env=env, start_new_session=True)


def launch_app_monitored(
    app_id: str,
    entry_point: str,
    runner_name: str,
    steam_appid: int | None,
    *,
    prefix_dir: Path | None = None,
    launch_args: str = "",
    extra_env: dict[str, str] | None = None,
    line_cb: Callable[[str], None] | None = None,
) -> None:
    """Launch *entry_point* like :func:`launch_app`, but capture stderr.

    Reads umu-run's stderr line-by-line, calling *line_cb* for each line.
    Returns once the game appears to have started (two-tier marker detection:
    first Proton/container setup lines, then ``fsync:``/``esync:`` indicating
    Wine is ready), after a 30 s timeout, or when stderr closes.  Wine keeps
    stderr open for the lifetime of the process, so we must not wait for EOF.

    *extra_env* is merged on top of the umu environment, useful for per-app
    Proton tweaks such as ``PROTON_USE_WINED3D=1``.
    """
    import os
    import shlex
    import time
    umu_env = build_env(app_id, runner_name, steam_appid, prefix_dir=prefix_dir)
    env = {**os.environ, **umu_env, **(extra_env or {})}
    cmd = _umu_cmd() + [entry_point]
    if launch_args:
        cmd += shlex.split(launch_args)
    log.info(
        "Launching app (monitored) %s via umu-run"
        "\n  WINEPREFIX=%s\n  PROTONPATH=%s\n  GAMEID=%s\n  EXE=%s%s",
        app_id,
        umu_env["WINEPREFIX"], umu_env["PROTONPATH"], umu_env["GAMEID"], entry_point,
        ("\n  EXTRA_ENV=" + " ".join(f"{k}={v}" for k, v in extra_env.items())) if extra_env else "",
    )
    # Tier 1: Proton/container is setting up — keep monitoring.
    _SETUP = ("pressure-vessel", "Proton:", "wine: configuration")
    # Tier 2: Wine sync is up — game is about to start.
    _STARTED = ("fsync:", "esync:")
    _MONITOR_TIMEOUT = 30  # seconds — never hold the dialog forever
    proc = subprocess.Popen(
        cmd, env=env, start_new_session=True,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    assert proc.stderr is not None
    deadline = time.monotonic() + _MONITOR_TIMEOUT
    wine_seen = False
    for raw in proc.stderr:
        line = raw.rstrip("\n")
        if line:
            log.debug("umu-run: %s", line)
            if line_cb:
                line_cb(line)
        if not wine_seen and any(m in line for m in _SETUP):
            wine_seen = True
        if wine_seen and any(m in line for m in _STARTED):
            break
        if time.monotonic() > deadline:
            log.debug("umu-run: monitor timeout reached, detaching")
            break
    # Detach — don't wait for process exit or read remaining stderr.
    proc.stderr.close()


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
    base_env = build_env("", runner_name, steam_appid, prefix_dir=prefix_path)
    gameid = base_env["GAMEID"]
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
    base_env = build_env("", runner_name, gameid, prefix_dir=prefix_path)
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
    base_env = build_env("", runner_name, gameid, prefix_dir=prefix_path)
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

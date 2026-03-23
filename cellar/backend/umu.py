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
import sys
from pathlib import Path
from typing import Callable

_REDACT_KEYWORDS = ("PASSWORD", "TOKEN", "KEY", "SECRET", "PASS", "CREDENTIAL")


def _fmt_env(env: dict[str, str]) -> str:
    """Format env vars for logging, redacting values whose keys look sensitive."""
    parts = []
    for k, v in env.items():
        upper = k.upper()
        if any(s in upper for s in _REDACT_KEYWORDS):
            parts.append(f"{k}=***")
        else:
            parts.append(f"{k}={v}")
    return " ".join(parts)

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


def dos_dir() -> Path:
    """Return (and create if needed) the Cellar DOS games directory."""
    from cellar.backend.config import install_data_dir
    d = install_data_dir() / "dos"
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


_AUDIO_OVERRIDES: dict[str, tuple[str, ...]] = {
    "pulseaudio": ("winealsa.drv", "wineoss.drv"),
    "alsa": ("winepulse.drv", "wineoss.drv"),
    "oss": ("winepulse.drv", "winealsa.drv"),
}


def dll_overrides(
    *, dxvk: bool = True, vkd3d: bool = True, audio_driver: str = "auto",
    no_lsteamclient: bool = False,
) -> str:
    """Build a ``WINEDLLOVERRIDES`` value for DXVK, VKD3D, Mono, audio, and Steam.

    GE-Proton ships DXVK, VKD3D, and Wine Mono inside every prefix, but
    Wine needs explicit overrides to prefer native DLLs over its built-in
    implementations.  ``mscoree`` (the .NET/Mono CLR host) is always
    included when any override is active.

    When *audio_driver* is not ``"auto"``, the unwanted audio driver DLLs
    are disabled (set to empty = disabled) so Wine uses only the chosen
    backend.

    When *no_lsteamclient* is True, Proton's ``lsteamclient.dll`` shim is
    disabled.  The shim can intercept Steam API calls and cause
    access-violation crashes in some apps.

    Returns an empty string when no overrides are needed.
    """
    parts: list[str] = []
    if dxvk:
        parts.extend(f"{d}=n,b" for d in (
            "d3d8", "d3d9", "d3d10core", "d3d11", "dxgi",
        ))
    if vkd3d:
        parts.extend(f"{d}=n,b" for d in ("d3d12", "d3d12core"))
    if audio_driver in _AUDIO_OVERRIDES:
        parts.extend(f"{d}=" for d in _AUDIO_OVERRIDES[audio_driver])
    if no_lsteamclient:
        parts.append("lsteamclient=d")
    if parts:
        parts.append("mscoree=n,b")
    return ";".join(parts)


def proton_compat_env(*, dxvk: bool = True, vkd3d: bool = True) -> dict[str, str]:
    """Return Proton environment variables to disable DXVK or VKD3D.

    GE-Proton enables DXVK and VKD3D by default.  ``WINEDLLOVERRIDES``
    alone cannot disable them — Proton's own setup scripts re-enable them.
    These Proton-specific env vars are the correct mechanism:

    - ``PROTON_USE_WINED3D=1`` — fall back to WineD3D (OpenGL) instead of
      DXVK (Vulkan) for D3D9–D3D11.
    - ``PROTON_NO_D3D12=1`` — disable D3D12 (VKD3D-Proton) entirely.
    """
    env: dict[str, str] = {}
    if not dxvk:
        env["PROTON_USE_WINED3D"] = "1"
    if not vkd3d:
        env["PROTON_NO_D3D12"] = "1"
    return env


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


def _win_to_linux_path(entry_point: str, wineprefix: str) -> str:
    """Convert a Windows-style entry point to a Linux path.

    umu-run handles Linux paths via Proton's ``/unix`` option but silently
    fails with Windows paths like ``C:\\Program Files\\App\\app.exe``.
    """
    if not entry_point or "/" in entry_point:
        return entry_point  # already a Linux path
    # Strip drive letter (e.g. "C:\") and convert backslashes.
    if len(entry_point) >= 3 and entry_point[1] == ":" and entry_point[2] == "\\":
        rel = entry_point[3:].replace("\\", "/")
        return str(Path(wineprefix) / "drive_c" / rel)
    return entry_point


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
    direct_proton: bool = False,
) -> None:
    """Launch *entry_point* inside the *app_id* prefix.  Fire-and-forget.

    *entry_point* should be a Windows-style path (e.g. ``C:\\Program Files\\App\\App.exe``);
    umu-launcher resolves it against WINEPREFIX automatically.  Absolute Linux
    paths are also accepted and passed through unchanged.

    *prefix_dir* overrides the WINEPREFIX; useful when launching from a
    non-standard location such as a Package Builder project prefix.

    *extra_env* is merged on top of the umu environment, useful for per-app
    Proton tweaks such as ``PROTON_USE_WINED3D=1``.

    If *direct_proton* is True, bypass umu-run and call Proton directly.
    """
    import os
    import shlex
    umu_env = build_env(app_id, runner_name, steam_appid, prefix_dir=prefix_dir)
    env = {**os.environ, **umu_env, **(extra_env or {})}
    exe = _win_to_linux_path(entry_point, umu_env["WINEPREFIX"])
    if direct_proton:
        proton_dir = runners_dir() / runner_name
        proton_script = proton_dir / "proton"
        wineprefix = prefix_dir if prefix_dir is not None else prefixes_dir() / app_id
        env["STEAM_COMPAT_DATA_PATH"] = str(wineprefix)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(proton_dir)
        appid_str = str(steam_appid) if steam_appid else "0"
        env["SteamGameId"] = appid_str
        env["SteamAppId"] = appid_str
        cmd = [sys.executable, str(proton_script), "run", exe]
    else:
        cmd = _umu_cmd() + [exe]
    if launch_args:
        cmd += shlex.split(launch_args)
    # Set cwd to the executable's directory — some games (and Steam
    # emulators) resolve config files relative to the working directory.
    exe_dir = str(Path(exe).parent) if "/" in exe else None
    log.info(
        "Launching app %s via %s\n  WINEPREFIX=%s\n  PROTONPATH=%s"
        "\n  GAMEID=%s\n  EXE=%s\n  CWD=%s%s",
        app_id, "proton direct" if direct_proton else "umu-run",
        umu_env["WINEPREFIX"], umu_env["PROTONPATH"], umu_env["GAMEID"], exe,
        exe_dir or "(default)",
        ("\n  EXTRA_ENV=" + _fmt_env(extra_env)) if extra_env else "",
    )
    subprocess.Popen(cmd, env=env, cwd=exe_dir, start_new_session=True)


def _exe_basename(entry_point: str) -> str:
    """Extract the executable basename from a Windows or Linux path.

    ``C:\\Program Files\\Game\\game.exe`` → ``game.exe``
    ``/path/to/game.exe`` → ``game.exe``
    """
    import ntpath

    return ntpath.basename(entry_point)


def monitor_process_tree(
    entry_point: str,
    launch_event,  # threading.Event
    line_cb: Callable[[str], None] | None,
    *,
    timeout: float = 30.0,
    proc: subprocess.Popen | None = None,
) -> None:
    """Poll the host process list for the launch target executable.

    Sets *launch_event* when a process whose ``comm`` matches the
    *entry_point* basename is found.  Polls until found, until
    *launch_event* is set externally, or until *timeout* seconds elapse
    after the launcher process (*proc*) has exited.

    When *proc* is provided the timeout is deferred as long as the
    launcher is still running — this prevents premature timeout when
    umu-run is downloading the Steam Runtime or performing first-launch
    setup.

    Linux truncates ``/proc/<pid>/comm`` to 15 bytes, so comparisons
    use the first 15 characters of the target name.

    Inside a Flatpak sandbox, wine/game processes live in a different PID
    namespace (spawned by pressure-vessel) and are invisible from the
    sandbox's ``/proc``.  In that case we use ``flatpak-spawn --host`` to
    run the scan on the host where the processes are actually visible.
    """
    target = _exe_basename(entry_point).lower()
    if not target:
        return

    if is_cellar_sandboxed():
        _monitor_via_host(target, launch_event, line_cb, timeout=timeout, proc=proc)
    else:
        _monitor_via_proc(target, launch_event, line_cb, timeout=timeout, proc=proc)


_COMM_MAX = 15  # Linux truncates /proc/<pid>/comm to 15 bytes


def _monitor_via_proc(
    target: str,
    launch_event,
    line_cb: Callable[[str], None] | None,
    *,
    timeout: float = 30.0,
    proc: subprocess.Popen | None = None,
) -> None:
    """Scan ``/proc/*/comm`` directly (non-Flatpak)."""
    import os
    import time

    deadline = time.monotonic() + timeout
    log.debug("Launch monitor: scanning /proc for %r (timeout=%.0fs)", target, timeout)
    while not launch_event.is_set():
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/comm", encoding="ascii", errors="replace") as f:
                    comm = f.read().strip()
            except (FileNotFoundError, PermissionError):
                continue
            if comm.lower() == target[:_COMM_MAX]:
                log.info("Launch monitor: %s detected (pid %s)", comm, entry)
                if line_cb:
                    line_cb(f"[pid] {comm}")
                launch_event.set()
                return
        # Defer timeout while the launcher process is still running.
        if proc is not None and proc.poll() is None:
            deadline = time.monotonic() + timeout
        elif time.monotonic() >= deadline:
            log.debug("Launch monitor: timeout reached (%.0fs), detaching", timeout)
            return
        launch_event.wait(timeout=1.0)


# Shell script executed on the host via flatpak-spawn.  Polls /proc/*/comm
# every second looking for a process whose name matches $1 (case-insensitive).
# Prints "pid=<N> <comm>" and exits 0 on match; runs until killed otherwise.
_HOST_MONITOR_SCRIPT = r"""
target=$(echo "$1" | tr '[:upper:]' '[:lower:]' | cut -c1-15)
while true; do
  for f in /proc/*/comm; do
    read -r c < "$f" 2>/dev/null || continue
    lc=$(echo "$c" | tr '[:upper:]' '[:lower:]')
    if [ "$lc" = "$target" ]; then
      pid="${f#/proc/}"
      pid="${pid%%/*}"
      echo "pid=$pid $c"
      exit 0
    fi
  done
  sleep 1
done
"""


def _monitor_via_host(
    target: str,
    launch_event,
    line_cb: Callable[[str], None] | None,
    *,
    timeout: float = 30.0,
    proc: subprocess.Popen | None = None,
) -> None:
    """Scan host ``/proc`` via ``flatpak-spawn --host`` (Flatpak)."""
    import select as _sel
    import time

    log.debug("Launch monitor: scanning host /proc for %r via flatpak-spawn"
              " (timeout=%.0fs)", target, timeout)
    try:
        scan_proc = subprocess.Popen(
            ["flatpak-spawn", "--host", "bash", "-c",
             _HOST_MONITOR_SCRIPT, "bash", target],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except FileNotFoundError:
        log.warning("flatpak-spawn not found — launch monitor disabled")
        return

    if scan_proc.stdout is None:
        raise RuntimeError("Expected stdout pipe but got None")
    deadline = time.monotonic() + timeout
    try:
        fd = scan_proc.stdout.fileno()
        while not launch_event.is_set():
            # Defer timeout while the launcher process is still running.
            if proc is not None and proc.poll() is None:
                deadline = time.monotonic() + timeout
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                log.debug("Launch monitor: timeout reached (%.0fs), detaching", timeout)
                return
            ready, _, _ = _sel.select([fd], [], [], min(remaining, 1.0))
            if ready:
                line = scan_proc.stdout.readline().strip()
                if line:
                    log.info("Launch monitor: %s", line)
                    if line_cb:
                        line_cb(f"[pid] {line}")
                    launch_event.set()
                    return
                else:
                    log.debug("Launch monitor: host script exited")
                    return
    finally:
        scan_proc.kill()
        scan_proc.wait()


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
    direct_proton: bool = False,
) -> None:
    """Launch *entry_point* and wait until the target process appears.

    Starts umu-run (or Proton directly), then uses :func:`_monitor_process_tree`
    to poll the host process list for the launch target executable.  Returns
    once the target process is detected.

    umu-run's stderr is read in a background thread and forwarded to
    *line_cb* for progress display (e.g. runtime download status) but is
    **not** used for launch detection.

    *extra_env* is merged on top of the umu environment, useful for per-app
    Proton tweaks such as ``PROTON_USE_WINED3D=1``.

    If *direct_proton* is True, bypass umu-run and call the Proton ``proton``
    script directly via ``python3 proton run <exe>``.
    """
    import os
    import shlex
    import threading

    umu_env = build_env(app_id, runner_name, steam_appid, prefix_dir=prefix_dir)
    env = {**os.environ, **umu_env, **(extra_env or {})}
    exe = _win_to_linux_path(entry_point, umu_env["WINEPREFIX"])
    if direct_proton:
        proton_dir = runners_dir() / runner_name
        proton_script = proton_dir / "proton"
        wineprefix = prefix_dir if prefix_dir is not None else prefixes_dir() / app_id
        env["STEAM_COMPAT_DATA_PATH"] = str(wineprefix)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(proton_dir)
        appid_str = str(steam_appid) if steam_appid else "0"
        env["SteamGameId"] = appid_str
        env["SteamAppId"] = appid_str
        cmd = [sys.executable, str(proton_script), "run", exe]
    else:
        cmd = _umu_cmd() + [exe]
    if launch_args:
        cmd += shlex.split(launch_args)
    exe_dir = str(Path(exe).parent) if "/" in exe else None
    log.info(
        "Launching app (monitored) %s via %s"
        "\n  WINEPREFIX=%s\n  PROTONPATH=%s\n  GAMEID=%s\n  EXE=%s\n  CWD=%s%s",
        app_id, "proton direct" if direct_proton else "umu-run",
        umu_env["WINEPREFIX"], umu_env["PROTONPATH"], umu_env["GAMEID"], exe,
        exe_dir or "(default)",
        ("\n  EXTRA_ENV=" + _fmt_env(extra_env)) if extra_env else "",
    )
    proc = subprocess.Popen(
        cmd, env=env, cwd=exe_dir, start_new_session=True,
        stderr=subprocess.PIPE, text=True, bufsize=1,
    )
    if proc.stderr is None:
        raise RuntimeError("Expected stderr pipe but got None")

    # Read stderr in a background thread for logging and progress display
    # (e.g. umu runtime downloads).  Not used for launch detection.
    launch_event = threading.Event()

    def _read_stderr() -> None:
        if proc.stderr is None:
            raise RuntimeError("Expected stderr pipe but got None")
        for raw in proc.stderr:
            if launch_event.is_set():
                break
            line = raw.rstrip("\n")
            if not line:
                continue
            _lvl = logging.INFO if "Downloading" in line or "SHA256" in line else logging.DEBUG
            log.log(_lvl, "umu-run: %s", line)
            if line_cb:
                line_cb(line)
        proc.stderr.close()

    stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
    stderr_thread.start()

    # PID-based launch detection — polls host /proc for the target exe.
    # Pass proc so the monitor defers its timeout while umu-run is alive
    # (e.g. downloading the Steam Runtime on first launch).
    monitor_process_tree(entry_point, launch_event, line_cb, proc=proc)

    # Signal stderr reader to stop and clean up.
    launch_event.set()
    stderr_thread.join(timeout=2)


def merge_launch_params(entry, overrides: dict | None, *, installed_runner: str = "") -> dict:
    """Merge catalogue defaults from *entry* with user *overrides*.

    Returns a dict with keys: ``entry_point``, ``launch_args``,
    ``launch_targets``, ``steam_appid``, ``runner``, ``dxvk``, ``vkd3d``,
    ``audio_driver``, ``debug``, ``direct_proton``, ``no_lsteamclient``.

    *overrides* is the result of ``database.get_launch_overrides()``; ``None``
    or empty dict means use catalogue defaults for everything.

    *installed_runner* is the runner stored in the installed DB record — used
    as fallback when no override runner is set.
    """
    if not overrides:
        overrides = {}

    targets_override = overrides.get("launch_targets")
    if targets_override:
        first = targets_override[0]
        entry_point = first.get("path") or entry.entry_point
        entry_args = first.get("args", "") or ""
        launch_targets = targets_override
    else:
        entry_point = entry.entry_point
        entry_args = entry.launch_args
        launch_targets = list(entry.launch_targets)

    return {
        "entry_point": entry_point,
        "launch_args": entry_args,
        "launch_targets": launch_targets,
        "steam_appid": (
            overrides["steam_appid"] if "steam_appid" in overrides else entry.steam_appid
        ),
        "runner": overrides.get("runner") or installed_runner,
        "dxvk": overrides["dxvk"] if "dxvk" in overrides else entry.dxvk,
        "vkd3d": overrides["vkd3d"] if "vkd3d" in overrides else entry.vkd3d,
        "audio_driver": overrides.get("audio_driver") or entry.audio_driver,
        "debug": overrides["debug"] if "debug" in overrides else entry.debug,
        "direct_proton": (
            overrides["direct_proton"] if "direct_proton" in overrides
            else entry.direct_proton
        ),
        "no_lsteamclient": (
            overrides["no_lsteamclient"] if "no_lsteamclient" in overrides
            else entry.no_lsteamclient
        ),
    }


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
    # Install Wine Mono if bundled with the runner.  GE-Proton ships Mono at
    # share/wine/mono/<version>/support/winemono-support.msi but umu-run ""
    # doesn't trigger the automatic install.
    if (prefix_path / "drive_c").is_dir():
        _install_mono(prefix_path, runner_name, env, timeout)
    return result


def _install_mono(
    prefix_path: Path,
    runner_name: str,
    env: dict[str, str],
    timeout: int,
) -> None:
    """Install Wine Mono into the prefix from the runner's bundled MSI."""
    mono_dir = runners_dir() / runner_name / "files" / "share" / "wine" / "mono"
    if not mono_dir.is_dir():
        return
    # Find the support MSI inside the versioned mono directory.
    for child in mono_dir.iterdir():
        msi = child / "support" / "winemono-support.msi"
        if msi.is_file():
            wine_path = "Z:" + str(msi).replace("/", "\\")
            cmd = _umu_cmd() + ["msiexec", "/i", wine_path, "/qn"]
            log.info("Installing Wine Mono from %s", msi)
            subprocess.run(cmd, env=env, timeout=timeout, capture_output=False)
            return


def _install_gecko(
    prefix_path: Path,
    runner_name: str,
) -> None:
    """Install Wine Gecko into the prefix from the runner's bundled files.

    GE-Proton ships Gecko as pre-extracted directories at
    ``files/share/wine/gecko/wine-gecko-<ver>-<arch>/``.  Wine only creates
    a stub (``gecko/plugin/npmshtml.dll``) during prefix init, so we copy
    the full engine into the prefix ourselves:

    - ``wine-gecko-*-x86_64`` → ``drive_c/windows/system32/gecko/``
    - ``wine-gecko-*-x86``    → ``drive_c/windows/syswow64/gecko/``
    """
    import shutil

    gecko_dir = runners_dir() / runner_name / "files" / "share" / "wine" / "gecko"
    if not gecko_dir.is_dir():
        log.debug("No bundled Gecko found at %s — skipping", gecko_dir)
        return

    # Map arch suffix → prefix destination directory.
    arch_map = {
        "x86_64": prefix_path / "drive_c" / "windows" / "system32" / "gecko",
        "x86": prefix_path / "drive_c" / "windows" / "syswow64" / "gecko",
    }

    for src in sorted(gecko_dir.iterdir()):
        if not src.is_dir() or not src.name.startswith("wine-gecko-"):
            continue
        # Determine target from directory name suffix (e.g. "wine-gecko-2.47.4-x86_64").
        for arch, dest in arch_map.items():
            if src.name.endswith(f"-{arch}"):
                log.info("Installing Wine Gecko (%s) from %s → %s", arch, src, dest)
                dest.mkdir(parents=True, exist_ok=True)
                shutil.copytree(src, dest, dirs_exist_ok=True)
                break


def _read_reg(prefix_path: Path, name: str) -> tuple[str, bool] | None:
    """Read a Wine registry file, returning ``(text, is_utf16)`` or None."""
    reg_file = prefix_path / name
    if not reg_file.is_file():
        log.warning("%s not found at %s — skipping", name, reg_file)
        return None
    raw = reg_file.read_bytes()
    is_utf16 = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
    text = raw.decode("utf-16-le" if is_utf16 else "utf-8", errors="replace")
    return text, is_utf16


def _write_reg(prefix_path: Path, name: str, text: str, is_utf16: bool) -> None:
    """Write a Wine registry file, preserving original encoding."""
    encoded = text.encode("utf-16-le" if is_utf16 else "utf-8")
    if is_utf16 and encoded[:2] not in (b"\xff\xfe", b"\xfe\xff"):
        encoded = b"\xff\xfe" + encoded
    (prefix_path / name).write_bytes(encoded)


def _set_reg_values(
    text: str,
    section: str,
    values: dict[str, str],
) -> str:
    """Set registry values within a ``user.reg`` section.

    *section* is the section header without brackets, e.g.
    ``Control Panel\\\\Desktop``.  Wine's ``user.reg`` uses the short form
    (implied ``HKEY_CURRENT_USER``), *not* the full hive path.

    *values* maps quoted key names to full ``"Key"=value`` lines.
    """
    import re

    # Match the section header — may have a trailing timestamp after the ].
    section_re = re.compile(
        rf"(\[{re.escape(section)}\].*?\n)",
        re.IGNORECASE,
    )
    match = section_re.search(text)
    if match:
        rest = text[match.end():]
        next_section = re.search(r"^\[", rest, re.MULTILINE)
        section_end = match.end() + next_section.start() if next_section else len(text)
        section_body = text[match.end():section_end]

        new_body = section_body
        to_insert: list[str] = []
        for key_name, key_line in values.items():
            pattern = re.compile(rf"^{re.escape(key_name)}=.*$", re.MULTILINE)
            if pattern.search(new_body):
                new_body = pattern.sub(key_line, new_body)
            else:
                to_insert.append(key_line)

        if to_insert:
            new_body = new_body.rstrip("\n") + "\n" + "\n".join(to_insert) + "\n"

        return text[:match.end()] + new_body + text[section_end:]

    # Section doesn't exist — append it.
    return text + f"\n[{section}]\n" + "\n".join(values.values()) + "\n"


def _apply_font_smoothing(prefix_path: Path) -> None:
    """Enable ClearType font smoothing in the prefix's ``user.reg``."""
    result = _read_reg(prefix_path, "user.reg")
    if result is None:
        return
    text, is_utf16 = result

    text = _set_reg_values(text, "Control Panel\\\\Desktop", {
        '"FontSmoothing"': '"FontSmoothing"="2"',
        '"FontSmoothingType"': '"FontSmoothingType"=dword:00000002',
        '"FontSmoothingGamma"': '"FontSmoothingGamma"=dword:00000578',
        '"FontSmoothingOrientation"': '"FontSmoothingOrientation"=dword:00000001',
    })

    _write_reg(prefix_path, "user.reg", text, is_utf16)
    log.info("Applied font smoothing to %s", prefix_path / "user.reg")


def apply_run_as_admin(prefix_path: Path, exe_basename: str, *, enable: bool = True) -> None:
    """Set or clear the Wine ``runasadmin`` flag for *exe_basename*.

    Writes to ``user.reg`` under ``Software\\Wine\\AppDefaults\\<exe>\\``
    so that Wine runs the executable with administrator privileges.

    *exe_basename* should be just the filename (e.g. ``Heroes3.exe``),
    not a full path.
    """
    result = _read_reg(prefix_path, "user.reg")
    if result is None:
        return
    text, is_utf16 = result

    section = f"Software\\\\Wine\\\\AppDefaults\\\\{exe_basename}"
    if enable:
        text = _set_reg_values(text, section, {
            '"runasadmin"': '"runasadmin"="admin"',
        })
    else:
        # Remove the runasadmin value by replacing it with an empty line.
        import re
        pattern = re.compile(
            rf'^\[{re.escape(section)}\].*?\n(.*?)(?=^\[|\Z)',
            re.MULTILINE | re.DOTALL,
        )
        match = pattern.search(text)
        if match:
            body = match.group(1)
            new_body = re.sub(r'^"runasadmin"=.*\n?', '', body, flags=re.MULTILINE)
            text = text[:match.start(1)] + new_body + text[match.end(1):]

    _write_reg(prefix_path, "user.reg", text, is_utf16)
    log.info("%s runasadmin for %s in %s",
             "Enabled" if enable else "Disabled", exe_basename, prefix_path)


def _apply_wine_tweaks(prefix_path: Path) -> None:
    """Disable winemenubuilder and file associations in the prefix.

    Prevents Wine from creating unwanted ``.desktop`` files and file type
    associations on the host system.
    """
    result = _read_reg(prefix_path, "user.reg")
    if result is None:
        return
    text, is_utf16 = result

    # Disable winemenubuilder.exe via DLL override.
    text = _set_reg_values(text, "Software\\\\Wine\\\\DllOverrides", {
        '"winemenubuilder.exe"': '"winemenubuilder.exe"=""',
    })

    # Disable file open associations.
    text = _set_reg_values(text, "Software\\\\Wine\\\\FileOpenAssociations", {
        '"Enable"': '"Enable"="N"',
    })

    _write_reg(prefix_path, "user.reg", text, is_utf16)
    log.info("Applied Wine tweaks to %s", prefix_path / "user.reg")


# -- Standard prefix setup ---------------------------------------------------

_SETUP_STEPS: list[tuple[str, str]] = [
    ("Installing Wine Gecko…", "__gecko__"),
    ("Applying font smoothing…", "__fontsmoothing__"),
    ("Applying Wine tweaks…", "__winetweaks__"),
    ("Installing core fonts…", "corefonts"),
    ("Installing msls31…", "msls31"),
    ("Installing DirectX 9…", "d3dx9"),
]


def setup_prefix(
    prefix_path: Path,
    runner_name: str,
    *,
    steam_appid: int | None = None,
    timeout: int = 600,
    step_cb: Callable[[str, int, int], None] | None = None,
) -> bool:
    """Install standard components into an initialised prefix.

    Called after :func:`init_prefix` has successfully created the prefix
    (``drive_c`` must exist).  Installs Wine Gecko, core fonts, msls31,
    d3dx9, and applies ClearType font smoothing.

    *step_cb(label, current_step, total_steps)* is called before each step
    so the UI can show progress.

    Returns ``True`` if every step succeeded, ``False`` if any step failed
    (failures are logged but do not abort remaining steps).
    """
    total = len(_SETUP_STEPS)
    all_ok = True

    for idx, (label, verb) in enumerate(_SETUP_STEPS, 1):
        if step_cb:
            step_cb(label, idx, total)
        try:
            if verb == "__gecko__":
                _install_gecko(prefix_path, runner_name)
            elif verb == "__fontsmoothing__":
                _apply_font_smoothing(prefix_path)
            elif verb == "__winetweaks__":
                _apply_wine_tweaks(prefix_path)
            else:
                run_winetricks(
                    prefix_path, runner_name, [verb], timeout=timeout,
                )
        except Exception:
            log.exception("setup_prefix step %d/%d failed: %s", idx, total, label)
            all_ok = False

    return all_ok


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
            if proc.stdout is None:
                raise RuntimeError("Expected stdout pipe but got None")
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
    env = {**os.environ, **base_env, **(extra_env or {})}
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

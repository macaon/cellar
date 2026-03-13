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


def dll_overrides(*, dxvk: bool = True, vkd3d: bool = True) -> str:
    """Build a ``WINEDLLOVERRIDES`` value for DXVK, VKD3D, and Mono.

    GE-Proton ships DXVK, VKD3D, and Wine Mono inside every prefix, but
    Wine needs explicit overrides to prefer native DLLs over its built-in
    implementations.  ``mscoree`` (the .NET/Mono CLR host) is always
    included when any override is active.  Returns an empty string if
    both DXVK and VKD3D are disabled.
    """
    parts: list[str] = []
    if dxvk:
        parts.extend(f"{d}=n,b" for d in (
            "d3d8", "d3d9", "d3d10core", "d3d11", "dxgi",
        ))
    if vkd3d:
        parts.extend(f"{d}=n,b" for d in ("d3d12", "d3d12core"))
    if parts:
        parts.append("mscoree=n,b")
    return ";".join(parts)


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
    exe = _win_to_linux_path(entry_point, umu_env["WINEPREFIX"])
    cmd = _umu_cmd() + [exe]
    if launch_args:
        cmd += shlex.split(launch_args)
    log.info(
        "Launching app %s via umu-run\n  WINEPREFIX=%s\n  PROTONPATH=%s\n  GAMEID=%s\n  EXE=%s%s",
        app_id,
        umu_env["WINEPREFIX"], umu_env["PROTONPATH"], umu_env["GAMEID"], exe,
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
    direct_proton: bool = False,
) -> None:
    """Launch *entry_point* like :func:`launch_app`, but capture stderr.

    Reads umu-run's stderr line-by-line, calling *line_cb* for each line.
    Returns once the game appears to have started (two-tier marker detection:
    first Proton/container setup lines, then ``fsync:``/``esync:`` indicating
    Wine is ready), after a 30 s timeout, or when stderr closes.  Wine keeps
    stderr open for the lifetime of the process, so we must not wait for EOF.

    *extra_env* is merged on top of the umu environment, useful for per-app
    Proton tweaks such as ``PROTON_USE_WINED3D=1``.

    If *direct_proton* is True, bypass umu-run and call the Proton ``proton``
    script directly via ``python3 proton run <exe>``.  This sets
    ``STEAM_COMPAT_DATA_PATH`` (pointing to the prefix) and
    ``STEAM_COMPAT_CLIENT_INSTALL_PATH`` as Proton expects.  Useful for
    debugging launch issues that might be caused by umu-launcher.
    """
    import os
    import shlex
    import time
    umu_env = build_env(app_id, runner_name, steam_appid, prefix_dir=prefix_dir)
    env = {**os.environ, **umu_env, **(extra_env or {})}
    exe = _win_to_linux_path(entry_point, umu_env["WINEPREFIX"])
    if direct_proton:
        proton_dir = runners_dir() / runner_name
        proton_script = proton_dir / "proton"
        wineprefix = prefix_dir if prefix_dir is not None else prefixes_dir() / app_id
        env["STEAM_COMPAT_DATA_PATH"] = str(wineprefix)
        env["STEAM_COMPAT_CLIENT_INSTALL_PATH"] = str(proton_dir)
        # Proton script reads SteamGameId / SteamAppId, not GAMEID.
        appid_str = str(steam_appid) if steam_appid else "0"
        env["SteamGameId"] = appid_str
        env["SteamAppId"] = appid_str
        cmd = [sys.executable, str(proton_script), "run", exe]
    else:
        cmd = _umu_cmd() + [exe]
    if launch_args:
        cmd += shlex.split(launch_args)
    log.info(
        "Launching app (monitored) %s via %s"
        "\n  WINEPREFIX=%s\n  PROTONPATH=%s\n  GAMEID=%s\n  EXE=%s%s",
        app_id, "proton direct" if direct_proton else "umu-run",
        umu_env["WINEPREFIX"], umu_env["PROTONPATH"], umu_env["GAMEID"], exe,
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


def _apply_font_smoothing(prefix_path: Path) -> None:
    """Enable ClearType font smoothing in the prefix's ``user.reg``.

    Sets the four standard Windows font smoothing registry values under
    ``[HKEY_CURRENT_USER\\Control Panel\\Desktop]``.
    """
    import re

    reg_file = prefix_path / "user.reg"
    if not reg_file.is_file():
        log.warning("user.reg not found at %s — skipping font smoothing", reg_file)
        return

    raw = reg_file.read_bytes()
    is_utf16 = raw[:2] in (b"\xff\xfe", b"\xfe\xff")
    text = raw.decode("utf-16-le" if is_utf16 else "utf-8", errors="replace")

    keys = {
        '"FontSmoothing"': '"FontSmoothing"="2"',
        '"FontSmoothingType"': '"FontSmoothingType"=dword:00000002',
        '"FontSmoothingGamma"': '"FontSmoothingGamma"=dword:00000578',
        '"FontSmoothingOrientation"': '"FontSmoothingOrientation"=dword:00000001',
    }

    section_re = re.compile(
        r"(\[HKEY_CURRENT_USER\\\\Control Panel\\\\Desktop\].*?\n)",
        re.IGNORECASE,
    )
    match = section_re.search(text)
    if match:
        # Find the end of this section (next section header or EOF).
        rest = text[match.end():]
        next_section = re.search(r"^\[", rest, re.MULTILINE)
        section_end = match.end() + next_section.start() if next_section else len(text)
        section_body = text[match.end():section_end]

        # Replace existing keys or collect ones to insert.
        new_body = section_body
        to_insert: list[str] = []
        for key_name, key_line in keys.items():
            pattern = re.compile(rf"^{re.escape(key_name)}=.*$", re.MULTILINE)
            if pattern.search(new_body):
                new_body = pattern.sub(key_line, new_body)
            else:
                to_insert.append(key_line)

        if to_insert:
            new_body = new_body.rstrip("\n") + "\n" + "\n".join(to_insert) + "\n"

        text = text[:match.end()] + new_body + text[section_end:]
    else:
        # Section doesn't exist — append it.
        block = "\n[HKEY_CURRENT_USER\\\\Control Panel\\\\Desktop]\n"
        block += "\n".join(keys.values()) + "\n"
        text += block

    encoded = text.encode("utf-16-le" if is_utf16 else "utf-8")
    if is_utf16 and not encoded[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encoded = b"\xff\xfe" + encoded
    reg_file.write_bytes(encoded)
    log.info("Applied font smoothing to %s", reg_file)


# -- Standard prefix setup ---------------------------------------------------

_SETUP_STEPS: list[tuple[str, str]] = [
    ("Installing Wine Gecko…", "__gecko__"),
    ("Applying font smoothing…", "__fontsmoothing__"),
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
    import os

    total = len(_SETUP_STEPS)
    all_ok = True
    base_env = build_env("", runner_name, steam_appid, prefix_dir=prefix_path)
    env = {**os.environ, **base_env}

    for idx, (label, verb) in enumerate(_SETUP_STEPS, 1):
        if step_cb:
            step_cb(label, idx, total)
        try:
            if verb == "__gecko__":
                _install_gecko(prefix_path, runner_name)
            elif verb == "__fontsmoothing__":
                _apply_font_smoothing(prefix_path)
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

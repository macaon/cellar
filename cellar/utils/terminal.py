"""Terminal emulator detection and launch helpers."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Ordered preference list of known terminal emulators.
_CANDIDATES = [
    "xdg-terminal-exec",  # freedesktop.org standard; routes to DE default
    "kgx",                # GNOME Console
    "gnome-terminal",
    "konsole",
    "xfce4-terminal",
    "mate-terminal",
    "lxterminal",
    "alacritty",
    "kitty",
    "foot",
    "wezterm",
    "xterm",
]

# Map emulator name → flag used to pass the command.
# None means the command is passed directly with no separator flag.
# Most use "--", some use "-e".
_EXEC_FLAG: dict[str, str | None] = {
    "xdg-terminal-exec": None,
    "gnome-terminal": "--",
    "kgx": "--",
    "xterm": "-e",
    "alacritty": "-e",
    "kitty": "--",  # kitty uses @ or -- for subcommand
    "foot": "--",
    "wezterm": "--",
}


def _query_desktop_terminal() -> str | None:
    """Ask the desktop environment which terminal it prefers."""
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    if "GNOME" in desktop or "UNITY" in desktop:
        try:
            out = subprocess.check_output(
                ["gsettings", "get",
                 "org.gnome.desktop.default-applications.terminal", "exec"],
                stderr=subprocess.DEVNULL,
                text=True,
            ).strip().strip("'\"")
            if out and shutil.which(out):
                return out
        except (FileNotFoundError, subprocess.CalledProcessError):
            pass
    if "KDE" in desktop:
        for tool in ("kreadconfig6", "kreadconfig5"):
            if not shutil.which(tool):
                continue
            try:
                out = subprocess.check_output(
                    [tool, "--file", "kdeglobals",
                     "--group", "General", "--key", "TerminalApplication"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                ).strip()
                if out and shutil.which(out):
                    return out
            except (FileNotFoundError, subprocess.CalledProcessError):
                pass
    return None


def find_terminal() -> str | None:
    """Return the path to an available terminal emulator, or ``None``."""
    # Honour user preference first.
    for env_var in ("TERMINAL", "TERM_PROGRAM"):
        val = os.environ.get(env_var, "")
        if val and shutil.which(val):
            return val

    # Ask the desktop environment.
    de_terminal = _query_desktop_terminal()
    if de_terminal:
        return de_terminal

    for name in _CANDIDATES:
        if shutil.which(name):
            return name

    return None


def launch_in_terminal(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> bool:
    """Run *cmd* in a new terminal window, keeping it open after exit.

    *extra_env* is merged on top of the current environment before the terminal
    process is started — use this for things like umu-run's WINEPREFIX/PROTONPATH
    variables so they don't clutter the visible command line.

    Returns ``True`` if the terminal was launched successfully, ``False`` if no
    terminal emulator could be found.
    """
    terminal = find_terminal()
    if not terminal:
        log.warning("No terminal emulator found; cannot launch in terminal")
        return False

    exec_flag = _EXEC_FLAG.get(terminal, "--")

    # Wrap with bash so we can append a "press enter" prompt that keeps the
    # window open after the process exits — mirroring Bottles behaviour.
    inner = " ".join(_shell_quote(c) for c in cmd)
    bash_cmd = f"{inner}; echo; read -p 'Press Enter to close…'"

    if exec_flag is None:
        full_cmd = [terminal, "bash", "-c", bash_cmd]
    else:
        full_cmd = [terminal, exec_flag, "bash", "-c", bash_cmd]

    env = {**os.environ, **(extra_env or {})}
    log.info("Launching in terminal: %s", " ".join(full_cmd))
    subprocess.Popen(full_cmd, cwd=cwd, env=env, start_new_session=True)
    return True


def _shell_quote(s: str) -> str:
    """Minimal single-quote escaping for embedding in a bash -c string."""
    return "'" + s.replace("'", "'\\''") + "'"

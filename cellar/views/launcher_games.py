"""Dialog for managing desktop shortcuts for games detected inside a launcher prefix.

Scans a Wine prefix for games installed by a third-party launcher (Epic Games
Store, Ubisoft Connect, etc.) and lets the user toggle .desktop entries for
each detected game.  Icon extraction from the game executable happens on
demand when a shortcut is enabled.
"""

from __future__ import annotations

import logging
import struct
import tempfile
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.backend.scanners import detect_launchers, scan_prefix
from cellar.backend.scanners.base import DetectedGame
from cellar.utils.async_work import run_in_background
from cellar.views.widgets import make_dialog_header, set_margins

log = logging.getLogger(__name__)

# .desktop IDs for detected games use a "det-" prefix so they don't collide
# with catalogue app IDs and are easy to identify for cleanup.
_DESKTOP_PREFIX = "det-"


def _desktop_id(game: DetectedGame, launcher: str) -> str:
    """Return a stable .desktop-safe ID for a detected game."""
    # Sanitise manifest_id: keep only alphanumeric, hyphens, dots, underscores.
    safe = "".join(
        c if c.isalnum() or c in "-_." else "-"
        for c in game.manifest_id
    ).strip("-")
    return f"{_DESKTOP_PREFIX}{launcher}-{safe}"


def _extract_icon(exe_path: str) -> str:
    """Extract the first icon group from a Windows .exe to a temp .ico file.

    Uses ``pefile`` directly to read RT_GROUP_ICON / RT_ICON resources and
    reassemble them into a standard .ico file.  Returns the path to the
    temp .ico, or "" on failure.
    """
    try:
        import pefile
    except ImportError:
        log.warning("pefile not available; skipping exe icon extraction")
        return ""

    if not exe_path or not Path(exe_path).is_file():
        return ""

    try:
        pe = pefile.PE(name=exe_path, fast_load=True)
        pe.parse_data_directories(
            pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"],
        )
        if not hasattr(pe, "DIRECTORY_ENTRY_RESOURCE"):
            return ""

        resources = {
            rsrc.id: rsrc
            for rsrc in reversed(pe.DIRECTORY_ENTRY_RESOURCE.entries)
        }
        grp_icon_res = resources.get(pefile.RESOURCE_TYPE["RT_GROUP_ICON"])
        rt_icon_res = resources.get(pefile.RESOURCE_TYPE["RT_ICON"])
        if not grp_icon_res or not rt_icon_res:
            return ""

        # Map individual icon IDs to their first language entry.
        icons = {
            entry.id: entry.directory.entries[0]
            for entry in rt_icon_res.directory.entries
        }

        # Pick the first group icon.
        group_entry = grp_icon_res.directory.entries[0]
        if group_entry.struct.DataIsDirectory:
            group_entry = group_entry.directory.entries[0]

        rva = group_entry.data.struct.OffsetToData
        grp_data = pe.get_data(rva, group_entry.data.struct.Size)

        # Parse GRPICONDIR header: reserved(H), type(H), count(H).
        _reserved, _type, count = struct.unpack_from("<HHH", grp_data, 0)

        # Parse each GRPICONDIRENTRY (14 bytes each) starting at offset 6.
        entries: list[tuple[bytes, bytes]] = []
        for i in range(count):
            off = 6 + i * 14
            entry_data = grp_data[off : off + 14]
            # Last 2 bytes of GRPICONDIRENTRY are the icon ID (H).
            icon_id = struct.unpack_from("<H", entry_data, 12)[0]
            icon_entry = icons.get(icon_id)
            if not icon_entry:
                continue
            icon_bytes = pe.get_data(
                icon_entry.data.struct.OffsetToData,
                icon_entry.data.struct.Size,
            )
            # ICONDIRENTRY: first 12 bytes same, then 4-byte file offset.
            entries.append((entry_data[:12], icon_bytes))

        if not entries:
            return ""

        # Assemble .ico file.
        tmp = tempfile.NamedTemporaryFile(suffix=".ico", delete=False)
        data_offset = 6 + len(entries) * 16
        # ICONDIR header.
        tmp.write(struct.pack("<HHH", 0, 1, len(entries)))
        # ICONDIRENTRY for each image.
        for header, icon_bytes in entries:
            tmp.write(header)
            tmp.write(struct.pack("<I", data_offset))
            data_offset += len(icon_bytes)
        # Icon image data.
        for _header, icon_bytes in entries:
            tmp.write(icon_bytes)
        tmp.close()
        return tmp.name

    except Exception as exc:
        log.debug("Icon extraction failed for %s: %s", exe_path, exc)
        return ""


def _create_game_shortcut(
    game: DetectedGame,
    launcher: str,
    prefix_path: str,
    runner_name: str,
) -> bool:
    """Create a .desktop entry for a detected game.  Returns True on success."""
    from cellar.backend.umu import _umu_data_env, runners_dir
    from cellar.utils.desktop import (
        _APPS_DIR,
        _desktop_quote,
        _install_icon,
        _refresh_desktop_db,
        _sanitize,
    )

    app_id = _desktop_id(game, launcher)

    # Icon — try local .ico first, then extract from exe.
    icon_ref = "application-x-executable"
    icon_source = game.icon_path or _extract_icon(game.exe_path)
    if icon_source:
        installed = _install_icon(app_id, icon_source)
        if installed:
            icon_ref = installed
        # Clean up temp file from extraction.
        if icon_source != game.icon_path:
            Path(icon_source).unlink(missing_ok=True)

    # Build umu-run Exec line using the parent launcher's prefix and runner.
    # Launch via Epic's protocol URI so the launcher handles auth/DRM.
    prefix = prefix_path
    proton = str(runners_dir() / runner_name) if runner_name else ""
    umu_data = _umu_data_env()

    env_parts = (
        f"--env=WINEPREFIX={_desktop_quote(prefix)}"
        + (f" --env=PROTONPATH={_desktop_quote(proton)}" if proton else "")
        + " --env=GAMEID=0"
        + f" --env=UMU_FOLDERS_PATH={_desktop_quote(umu_data['UMU_FOLDERS_PATH'])}"
    )

    if game.launch_uri and game.launch_uri.startswith("battlenet://"):
        # Battle.net: invoke the launcher exe directly with --uri arg.
        # Going through cmd /c start loses the URI in Wine's process chain.
        # Use -- separator so umu-run doesn't consume the --uri flag.
        bnet_exe = "C:/Program Files (x86)/Battle.net/Battle.net.exe"
        exec_line = (
            f"flatpak run --command=umu-run {env_parts} io.github.cellar"
            f" -- {_desktop_quote(bnet_exe)}"
            f" {_desktop_quote('--uri=' + game.launch_uri)}"
        )
    elif game.launch_uri:
        # Other launchers (Epic): use protocol URI via cmd /c start.
        exec_line = (
            f"flatpak run --command=umu-run {env_parts} io.github.cellar"
            f" cmd /c start /b {_desktop_quote(game.launch_uri)}"
        )
    else:
        # Fallback: direct exe launch with full Windows path.
        win_path = game.install_location.rstrip("/") + "/" + game.launch_exe
        exec_line = (
            f"flatpak run --command=umu-run {env_parts} io.github.cellar"
            f" {_desktop_quote(win_path)}"
        )

    # Working directory — resolve game's install location to Linux path.
    work_dir = ""
    if game.install_location:
        loc = game.install_location.replace("\\", "/")
        if len(loc) >= 2 and loc[1] == ":":
            loc = loc[2:]
        loc = loc.lstrip("/")
        work_dir = str(Path(prefix_path) / "drive_c" / loc)

    _APPS_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        f"Name={_sanitize(game.display_name)}",
        f"Comment={_sanitize(f'Launch {game.display_name} via umu-launcher.')}",
        f"Icon={_sanitize(icon_ref)}",
        f"Exec={_sanitize(exec_line)}",
        "Terminal=false",
        "Categories=Game;",
        f"StartupWMClass={_sanitize(game.display_name)}",
        f"X-Cellar-Detected={launcher}:{game.manifest_id}",
    ]
    if work_dir:
        lines.append(f"Path={_sanitize(work_dir)}")

    path = _APPS_DIR / f"cellar-{app_id}.desktop"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    _refresh_desktop_db()
    log.info("Created shortcut for detected game: %s → %s", game.display_name, path)
    return True


def _remove_game_shortcut(game: DetectedGame, launcher: str) -> None:
    """Remove the .desktop entry and icon for a detected game."""
    from cellar.utils.desktop import _APPS_DIR, _refresh_desktop_db, _remove_icons

    app_id = _desktop_id(game, launcher)
    path = _APPS_DIR / f"cellar-{app_id}.desktop"
    if path.exists():
        path.unlink(missing_ok=True)
        log.info("Removed shortcut: %s", path)
    _remove_icons(app_id)
    _refresh_desktop_db()


def _has_game_shortcut(game: DetectedGame, launcher: str) -> bool:
    """Check if a .desktop entry exists for this detected game."""
    from cellar.utils.desktop import _APPS_DIR

    app_id = _desktop_id(game, launcher)
    return (_APPS_DIR / f"cellar-{app_id}.desktop").exists()


def _fmt_size(nbytes: int) -> str:
    """Human-readable file size."""
    if nbytes <= 0:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


class LauncherGamesDialog(Adw.Dialog):
    """Dialog showing games detected inside a launcher's Wine prefix."""

    def __init__(
        self,
        *,
        prefix_path: str,
        runner_name: str,
        parent_name: str,
        **kwargs,
    ) -> None:
        super().__init__(
            title=f"Games in {parent_name}",
            content_width=460,
            content_height=480,
            **kwargs,
        )
        self._prefix_path = prefix_path
        self._runner_name = runner_name
        self._games: list[DetectedGame] = []
        self._launchers: list[str] = []
        self._switches: dict[str, Gtk.Switch] = {}  # manifest_id → switch

        self._build_ui()
        self._start_scan()

    def _build_ui(self) -> None:
        toolbar_view, _hdr, _ = make_dialog_header(
            self, cancel_label="Close",
        )

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_vexpand(True)

        # Spinner (scanning)
        spinner_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        spinner_box.set_valign(Gtk.Align.CENTER)
        spinner = Adw.Spinner()
        spinner.set_halign(Gtk.Align.CENTER)
        set_margins(spinner, 48)
        lbl = Gtk.Label(label="Scanning for games\u2026")
        lbl.add_css_class("dim-label")
        lbl.set_halign(Gtk.Align.CENTER)
        lbl.set_margin_bottom(48)
        spinner_box.append(spinner)
        spinner_box.append(lbl)
        self._stack.add_named(spinner_box, "spinner")

        # Results list
        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        self._listbox = Gtk.ListBox()
        self._listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._listbox.add_css_class("boxed-list")
        self._listbox.set_margin_top(12)
        self._listbox.set_margin_bottom(12)
        self._listbox.set_margin_start(12)
        self._listbox.set_margin_end(12)
        scroll.set_child(self._listbox)
        self._stack.add_named(scroll, "results")

        # Empty state
        empty_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        empty_box.set_valign(Gtk.Align.CENTER)
        empty_label = Gtk.Label(label="No installed games found")
        empty_label.add_css_class("dim-label")
        empty_label.set_halign(Gtk.Align.CENTER)
        set_margins(empty_label, 48)
        empty_box.append(empty_label)
        self._stack.add_named(empty_box, "empty")

        self._stack.set_visible_child_name("spinner")
        toolbar_view.set_content(self._stack)
        self.set_child(toolbar_view)

    def _start_scan(self) -> None:
        prefix = Path(self._prefix_path)

        def _work():
            launchers = detect_launchers(prefix)
            games: list[DetectedGame] = []
            for lid in launchers:
                games.extend(scan_prefix(prefix, lid))
            return launchers, games

        run_in_background(
            work=_work,
            on_done=lambda result: self._on_scan_done(*result),
            on_error=lambda msg: self._show_empty(),
        )

    def _show_empty(self) -> None:
        self._stack.set_visible_child_name("empty")

    def _on_scan_done(
        self, launchers: list[str], games: list[DetectedGame],
    ) -> None:
        self._launchers = launchers
        self._games = games

        if not games:
            self._show_empty()
            return

        launcher = launchers[0] if launchers else "unknown"

        for game in games:
            row = Adw.ActionRow(title=GLib.markup_escape_text(game.display_name))
            size_str = _fmt_size(game.install_size)
            if size_str:
                row.set_subtitle(size_str)

            switch = Gtk.Switch(valign=Gtk.Align.CENTER)
            switch.set_active(_has_game_shortcut(game, launcher))
            switch.connect(
                "notify::active",
                self._on_switch_toggled,
                game,
                launcher,
            )
            row.add_suffix(switch)
            self._switches[game.manifest_id] = switch
            self._listbox.append(row)

        self._stack.set_visible_child_name("results")

    def _on_switch_toggled(
        self,
        switch: Gtk.Switch,
        _pspec,
        game: DetectedGame,
        launcher: str,
    ) -> None:
        if switch.get_active():
            # Create shortcut (icon extraction may be slow — run in background).
            switch.set_sensitive(False)

            def _work():
                return _create_game_shortcut(
                    game, launcher, self._prefix_path, self._runner_name,
                )

            def _done(success: bool) -> None:
                switch.set_sensitive(True)
                if not success:
                    switch.set_active(False)

            def _error(msg: str) -> None:
                log.warning("Failed to create shortcut: %s", msg)
                switch.set_sensitive(True)
                switch.set_active(False)

            run_in_background(work=_work, on_done=_done, on_error=_error)
        else:
            _remove_game_shortcut(game, launcher)

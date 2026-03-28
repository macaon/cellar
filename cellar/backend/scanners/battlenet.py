"""Battle.net scanner.

Detects games installed by the Battle.net launcher within a Wine prefix
by parsing the protobuf ``product.db`` file that the Blizzard Agent writes
to ``drive_c/ProgramData/Battle.net/Agent/product.db``.
"""

from __future__ import annotations

import logging
import struct
from pathlib import Path

from cellar.backend.scanners.base import DetectedGame

log = logging.getLogger(__name__)

PRODUCT_DB_REL = "drive_c/ProgramData/Battle.net/Agent/product.db"

# Battle.net product UIDs that are not games.
_SKIP_UIDS = frozenset({"agent", "battle.net", "battle_net", "bna"})

# Known Battle.net game metadata.
# uid → (display_name, protocol_code, main_exe)
_KNOWN_GAMES: dict[str, tuple[str, str, str]] = {
    "diablo3":           ("Diablo III",              "D3",   "Diablo III.exe"),
    "osi":               ("Diablo II: Resurrected",  "OSI",  "D2R.exe"),
    "fen":               ("Diablo IV",               "Fen",  "Diablo IV.exe"),
    "wow":               ("World of Warcraft",       "WoW",  "Wow.exe"),
    "wow_classic":       ("WoW Classic",             "WoW",  "WowClassic.exe"),
    "wow_classic_era":   ("WoW Classic Era",         "WoW",  "WowClassic.exe"),
    "s2":                ("StarCraft II",            "S2",   "SC2_x64.exe"),
    "s1":                ("StarCraft Remastered",    "S1",   "StarCraft.exe"),
    "hero":              ("Heroes of the Storm",     "Hero", "HeroesOfTheStorm_x64.exe"),
    "pro":               ("Overwatch 2",             "Pro",  "Overwatch.exe"),
    "w3":                ("Warcraft III: Reforged",  "W3",   "Warcraft III.exe"),
    "wtcg":              ("Hearthstone",             "WTCG", "Hearthstone.exe"),
    "hs_beta":           ("Hearthstone",             "WTCG", "Hearthstone.exe"),
    "viper":             ("Call of Duty: MW II",     "VIPR", "cod.exe"),
    "fore":              ("Call of Duty: MW III",    "FORE", "cod.exe"),
    "auks":              ("Call of Duty: BO6",       "AUKS", "cod.exe"),
    "anbs":              ("Diablo Immortal",         "ANBS", "DiabloImmortal.exe"),
    "rtro":              ("Blizzard Arcade Collection", "RTRO", "Blizzard Arcade Collection.exe"),
    "lazr":              ("Crash Bandicoot 4",       "LAZR", "CrashBandicoot4.exe"),
    "zeus":              ("Call of Duty: MWII (2022)", "ZEUS", "cod.exe"),
    "odin":              ("Call of Duty: Warzone",   "ODIN", "cod.exe"),
}


class BattlenetScanner:
    """Scan a Wine prefix for Battle.net game installations."""

    launcher_id: str = "battlenet"

    def detect(self, prefix_path: Path) -> bool:
        """Return True if the Battle.net product database exists."""
        return (prefix_path / PRODUCT_DB_REL).is_file()

    def scan(self, prefix_path: Path) -> list[DetectedGame]:
        """Parse product.db and return detected games."""
        db_path = prefix_path / PRODUCT_DB_REL
        if not db_path.is_file():
            return []

        try:
            products = _parse_product_db(db_path.read_bytes())
        except Exception as exc:
            log.warning("Failed to parse Battle.net product.db: %s", exc)
            return []

        games: list[DetectedGame] = []
        for uid, install_path in products:
            if uid.lower() in _SKIP_UIDS:
                continue

            known = _KNOWN_GAMES.get(uid.lower())
            if known:
                display_name, proto_code, main_exe = known
            else:
                # Unknown game — use the UID as name, no exe guess.
                display_name = uid.replace("_", " ").title()
                proto_code = ""
                main_exe = ""

            # Find the launch exe — prefer the .lnk shortcut target
            # (the game's own Launcher exe), then fall back to known exes.
            game_dir = _win_to_linux_path(prefix_path, install_path)
            lnk_target = _find_lnk_target(prefix_path, display_name)
            exe_path = ""
            launch_exe = ""
            if lnk_target:
                # lnk_target is a full Windows path; extract just the filename.
                launch_exe = lnk_target.replace("\\", "/").rsplit("/", 1)[-1]
                lnk_linux = _win_to_linux_path(prefix_path, lnk_target)
                if lnk_linux.is_file():
                    exe_path = str(lnk_linux)
            if not launch_exe and main_exe and (game_dir / main_exe).is_file():
                launch_exe = main_exe
                exe_path = str(game_dir / main_exe)
            if not launch_exe and game_dir.is_dir():
                exes = sorted(game_dir.glob("*.exe"))
                if exes:
                    launch_exe = exes[0].name
                    exe_path = str(exes[0])

            # Estimate install size.
            install_size = 0
            if game_dir.is_dir():
                try:
                    install_size = sum(
                        f.stat().st_size for f in game_dir.rglob("*") if f.is_file()
                    )
                except OSError:
                    pass

            # Icon: check for .ico in game dir first.
            icon_path = _find_icon(game_dir)

            # No protocol URI — Battle.net games are launched via their
            # own Launcher exe (found from .lnk shortcuts above).
            launch_uri = ""

            games.append(DetectedGame(
                manifest_id=uid,
                display_name=display_name,
                install_location=install_path,
                launch_exe=launch_exe,
                install_size=install_size,
                icon_url="",
                icon_path=icon_path,
                exe_path=exe_path,
                catalog_id=uid,
                launch_uri=launch_uri,
            ))

        return games


# -- .lnk shortcut parsing -----------------------------------------------------

_LNK_DIRS = (
    "drive_c/users/Public/Desktop",
    "drive_c/ProgramData/Microsoft/Windows/Start Menu/Programs",
)


def _parse_lnk_target(path: Path) -> str:
    """Extract the LocalBasePath from a Windows .lnk shortcut file.

    Returns the Windows-style path string, or "" on failure.
    """
    try:
        data = path.read_bytes()
        if len(data) < 78 or data[:4] != b"\x4c\x00\x00\x00":
            return ""

        flags = struct.unpack_from("<I", data, 20)[0]
        has_link_target = flags & 0x01
        has_link_info = flags & 0x02

        pos = 76
        # Skip LinkTargetIDList if present.
        if has_link_target:
            idlist_size = struct.unpack_from("<H", data, pos)[0]
            pos += 2 + idlist_size

        if not has_link_info:
            return ""

        info_size = struct.unpack_from("<I", data, pos)[0]
        info_data = data[pos : pos + info_size]
        local_base_path_offset = struct.unpack_from("<I", info_data, 16)[0]
        if not local_base_path_offset or local_base_path_offset >= info_size:
            return ""

        end = info_data.index(0, local_base_path_offset)
        return info_data[local_base_path_offset:end].decode("ascii", errors="replace")
    except Exception:
        return ""


def _find_lnk_target(prefix_path: Path, display_name: str) -> str:
    """Search common shortcut locations for a .lnk matching *display_name*.

    Returns the Windows-style exe path from the shortcut, or "".
    """
    for lnk_dir_rel in _LNK_DIRS:
        lnk_dir = prefix_path / lnk_dir_rel
        if not lnk_dir.is_dir():
            continue
        # Search recursively (Start Menu has subfolders per game).
        for lnk_file in lnk_dir.rglob("*.lnk"):
            target = _parse_lnk_target(lnk_file)
            if not target:
                continue
            # Match by display name in either the .lnk filename or the target path.
            lnk_stem = lnk_file.stem.lower()
            if display_name.lower() in lnk_stem or lnk_stem in display_name.lower():
                return target
    return ""


# -- Protobuf parsing (no external dependency) --------------------------------

def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    """Read a protobuf varint, return (value, new_pos)."""
    result = 0
    shift = 0
    while pos < len(buf):
        b = buf[pos]
        result |= (b & 0x7F) << shift
        pos += 1
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _read_field(buf: bytes, pos: int) -> tuple[int, int, bytes | int | None, int]:
    """Read one protobuf field, return (field_num, wire_type, value, new_pos)."""
    if pos >= len(buf):
        return 0, 0, None, pos
    tag, pos = _read_varint(buf, pos)
    field_num = tag >> 3
    wire_type = tag & 0x7
    if wire_type == 0:  # varint
        val, pos = _read_varint(buf, pos)
        return field_num, wire_type, val, pos
    if wire_type == 2:  # length-delimited
        length, pos = _read_varint(buf, pos)
        return field_num, wire_type, buf[pos : pos + length], pos + length
    if wire_type == 1:  # 64-bit fixed
        return field_num, wire_type, buf[pos : pos + 8], pos + 8
    if wire_type == 5:  # 32-bit fixed
        return field_num, wire_type, buf[pos : pos + 4], pos + 4
    return field_num, wire_type, None, pos + 1


def _parse_product_db(data: bytes) -> list[tuple[str, str]]:
    """Parse Battle.net product.db, return list of (uid, install_path)."""
    products: list[tuple[str, str]] = []
    pos = 0
    while pos < len(data):
        field_num, wire_type, val, pos = _read_field(data, pos)
        if val is None:
            break
        if field_num != 1 or wire_type != 2:
            continue
        # Parse product message.
        uid = ""
        install_path = ""
        ppos = 0
        assert isinstance(val, bytes)
        while ppos < len(val):
            pf, pw, pv, ppos = _read_field(val, ppos)
            if pv is None:
                break
            if pf == 1 and pw == 2 and isinstance(pv, bytes):
                uid = pv.decode("utf-8", errors="replace")
            elif pf == 3 and pw == 2 and isinstance(pv, bytes):
                # Install info sub-message — field 1 is the path.
                ipos = 0
                while ipos < len(pv):
                    ifield, iw, iv, ipos = _read_field(pv, ipos)
                    if iv is None:
                        break
                    if ifield == 1 and iw == 2 and isinstance(iv, bytes):
                        install_path = iv.decode("utf-8", errors="replace")
                        break
        if uid and install_path:
            products.append((uid, install_path))
    return products


# -- Helpers -------------------------------------------------------------------

def _win_to_linux_path(prefix_path: Path, win_path: str) -> Path:
    """Convert a Windows path to a Linux path within the prefix."""
    cleaned = win_path.replace("\\", "/")
    if len(cleaned) >= 2 and cleaned[1] == ":":
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    return prefix_path / "drive_c" / cleaned


def _find_icon(game_dir: Path) -> str:
    """Search *game_dir* for the best .ico or .png file."""
    if not game_dir.is_dir():
        return ""
    candidates: list[Path] = []
    for ext in ("*.ico", "*.png"):
        candidates.extend(game_dir.glob(ext))
    if not candidates:
        return ""
    def sort_key(p: Path) -> tuple[int, int]:
        is_ico = 1 if p.suffix.lower() == ".ico" else 0
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        return (is_ico, size)
    return str(max(candidates, key=sort_key))


# -- CLI runner for testing ----------------------------------------------------

def _fmt_size(nbytes: int) -> str:
    if nbytes <= 0:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"


def main() -> None:
    import sys

    if len(sys.argv) < 2:
        print(f"Usage: python -m cellar.backend.scanners.battlenet <prefix_path>")
        sys.exit(1)

    prefix = Path(sys.argv[1])
    if not prefix.is_dir():
        print(f"Error: {prefix} is not a directory")
        sys.exit(1)

    scanner = BattlenetScanner()
    if not scanner.detect(prefix):
        print(f"No Battle.net detected in {prefix}")
        sys.exit(0)

    games = scanner.scan(prefix)
    if not games:
        print("Battle.net detected, but no installed games found.")
        sys.exit(0)

    print(f"\nFound {len(games)} game(s) in {prefix}\n")
    print(f"{'Name':<35} {'Exe':<30} {'Size':>10}  Icon")
    print("─" * 100)
    for g in games:
        exe_display = g.launch_exe or "(none)"
        if g.icon_path:
            icon_display = f"LOCAL: {Path(g.icon_path).name}"
        elif g.exe_path:
            icon_display = f"EXE: {Path(g.exe_path).name}"
        else:
            icon_display = "—"
        print(
            f"{g.display_name:<35} {exe_display:<30} "
            f"{_fmt_size(g.install_size):>10}  {icon_display}"
        )
    print()

    for g in games:
        print(f"── {g.display_name} ──")
        print(f"  manifest_id:      {g.manifest_id}")
        print(f"  install_location: {g.install_location}")
        print(f"  launch_exe:       {g.launch_exe}")
        print(f"  exe_path:         {g.exe_path or '—'}")
        print(f"  install_size:     {_fmt_size(g.install_size)}")
        print(f"  launch_uri:       {g.launch_uri or '—'}")
        print(f"  icon_path:        {g.icon_path or '—'}")
        print()


if __name__ == "__main__":
    main()

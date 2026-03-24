#!/usr/bin/env python3
"""Generate ``data/scummvm-profiles.json`` from ScummVM's detection data.

Workflow:
1. Parse ``scummvm --dump-all-detection-entries`` output (file fingerprints).
2. Load ``compatibility.yaml`` and ``games.yaml`` from the scummvm-web repo.
3. Filter to "Excellent" compatibility games only.
4. Cross-reference GOG IDs from games.yaml.
5. Output the profile JSON.

Usage:
    # Dump detection entries first:
    scummvm --dump-all-detection-entries > /tmp/scummvm-detection.dat

    # Then run this script:
    python tools/gen_scummvm_profiles.py \\
        --detection /tmp/scummvm-detection.dat \\
        --compatibility data/compatibility.yaml \\
        --games data/games.yaml \\
        --output data/scummvm-profiles.json

You can download the YAML files from:
    https://raw.githubusercontent.com/scummvm/scummvm-web/master/data/en/compatibility.yaml
    https://raw.githubusercontent.com/scummvm/scummvm-web/master/data/en/games.yaml
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("PyYAML required: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Parse ScummVM DAT (CLRMAMEPro format)
# ---------------------------------------------------------------------------

def parse_detection_dat(path: Path) -> dict[str, list[dict]]:
    """Parse the ScummVM detection entries DAT file.

    Returns a dict mapping ``engine:gameid`` to a list of detection entries.
    Each entry has: name, title, extra, language, platform, engine, roms.
    Each rom has: name, size, md5.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    entries: dict[str, list[dict]] = {}

    # Split into game(...) blocks
    for block in re.finditer(
        r"game\s*\(\s*(.*?)\n\s*\)", text, re.DOTALL
    ):
        content = block.group(1)
        entry: dict = {"roms": []}

        for key in ("name", "title", "extra", "language", "platform",
                     "sourcefile", "engine"):
            m = re.search(rf'{key}\s+"([^"]*)"', content)
            if m:
                entry[key] = m.group(1)

        for rom_match in re.finditer(
            r'rom\s*\(\s*name\s+"([^"]+)"\s+size\s+(\d+)\s+md5[^\s]*\s+([a-f0-9]+)',
            content,
        ):
            entry["roms"].append({
                "name": rom_match.group(1),
                "size": int(rom_match.group(2)),
                "md5": rom_match.group(3),
            })

        game_id = entry.get("name", "")
        engine = entry.get("engine", entry.get("sourcefile", ""))
        full_id = f"{engine}:{game_id}" if engine else game_id

        if full_id not in entries:
            entries[full_id] = []
        entries[full_id].append(entry)

    return entries


# ---------------------------------------------------------------------------
# Load YAML data
# ---------------------------------------------------------------------------

def load_compatibility(path: Path) -> dict[str, str]:
    """Load compatibility.yaml; return {game_id: support_level}.

    When the same game_id appears multiple times (different ScummVM versions),
    keep the most recent (last) entry.
    """
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    compat: dict[str, str] = {}
    for entry in data:
        gid = entry.get("id", "")
        support = entry.get("support", "")
        compat[gid] = support  # last entry wins (most recent version)
    return compat


def load_games(path: Path) -> dict[str, dict]:
    """Load games.yaml; return {game_id: {name, gog_id, steam_id, ...}}."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    games: dict[str, dict] = {}
    for entry in data:
        gid = entry.get("id", "")
        games[gid] = entry
    return games


# ---------------------------------------------------------------------------
# Build profiles
# ---------------------------------------------------------------------------

def build_profiles(
    detection: dict[str, list[dict]],
    compat: dict[str, str],
    games: dict[str, dict],
) -> dict:
    """Build the scummvm-profiles.json structure."""
    profiles: dict[str, dict] = {}

    for full_id, support in compat.items():
        if support.lower() != "excellent":
            continue

        game_info = games.get(full_id, {})
        name = game_info.get("name", full_id)
        gog_id = game_info.get("gog_id", "")

        # Extract the short game ID (after the colon)
        parts = full_id.split(":", 1)
        scummvm_id = parts[1] if len(parts) == 2 else parts[0]

        # Get detection file fingerprints
        det_entries = detection.get(full_id, [])
        if not det_entries:
            continue

        # Collect unique required filenames from the first (most common) entry
        # that has DOS platform, or the first entry if no DOS-specific one.
        best_entry = det_entries[0]
        for de in det_entries:
            if de.get("platform", "").lower() == "dos":
                best_entry = de
                break

        rom_files = [r["name"] for r in best_entry.get("roms", [])]
        if not rom_files:
            continue

        # Build the slug from the game name
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

        gog_ids = [gog_id] if gog_id else []

        profiles[slug] = {
            "name": name,
            "scummvm_id": scummvm_id,
            "compatibility": "Excellent",
            "match": {
                "gog_ids": gog_ids,
                "files": rom_files,
            },
            "required_files": rom_files,
        }

    return {"schema_version": 1, "profiles": profiles}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate scummvm-profiles.json from ScummVM data sources.",
    )
    parser.add_argument(
        "--detection", required=True, type=Path,
        help="Path to scummvm --dump-all-detection-entries output",
    )
    parser.add_argument(
        "--compatibility", required=True, type=Path,
        help="Path to scummvm-web compatibility.yaml",
    )
    parser.add_argument(
        "--games", required=True, type=Path,
        help="Path to scummvm-web games.yaml",
    )
    parser.add_argument(
        "--output", default=Path("data/scummvm-profiles.json"), type=Path,
        help="Output path (default: data/scummvm-profiles.json)",
    )
    args = parser.parse_args()

    print(f"Parsing detection entries from {args.detection}...")
    detection = parse_detection_dat(args.detection)
    print(f"  Found {len(detection)} unique game IDs")

    print(f"Loading compatibility from {args.compatibility}...")
    compat = load_compatibility(args.compatibility)
    excellent = sum(1 for v in compat.values() if v.lower() == "excellent")
    print(f"  Found {len(compat)} entries ({excellent} Excellent)")

    print(f"Loading games from {args.games}...")
    games = load_games(args.games)
    print(f"  Found {len(games)} games")

    print("Building profiles...")
    result = build_profiles(detection, compat, games)
    count = len(result["profiles"])
    print(f"  Generated {count} profiles")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()

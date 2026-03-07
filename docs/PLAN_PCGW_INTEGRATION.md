# Plan: PCGamingWiki save/config path integration

## Background

The safe update feature uses `rsync --delete` to remove stale files after an
update while preserving user data. Currently it protects user data with blunt
exclusions covering all of `AppData/Roaming`, `AppData/Local`, and `Documents`.

This is a double-edged sword:
- It misses games that store saves in the **game root** directory (e.g. Slay
  the Spire stores saves in `<game dir>/saves/`), which `--delete` would wipe.
- It over-protects AppData, preventing updates from touching game-managed files
  in those folders even when safe to do so.

[PCGamingWiki](https://www.pcgamingwiki.com) maintains structured, per-game
records of save and config file locations. Its wikitext API is publicly readable
without authentication and provides accurate path information via `{{Game
data/saves|Windows|...}}` and `{{Game data/config|Windows|...}}` templates.

Because `steam_appid` is reliably populated in every `AppEntry` (sourced from
IGDB at package-creation time), we can perform a targeted lookup without fuzzy
title matching.

---

## New catalogue field: `game_root`

The `{{p|game}}` macro in PCGamingWiki paths refers to the game's installation
directory inside the prefix (e.g. `C:\Games\SlayTheSpire`). This cannot be
inferred from `entry_point` alone — for games like The Sims 4 the executable
lives deep in a subdirectory (`Game\Bin\TS4_x64.exe`) and stripping the
filename gives the wrong directory.

The solution is an optional `game_root` field set by the package maintainer in
the Package Builder.

---

## Phase 1 — `game_root` field

### `cellar/models/app_entry.py`
- Add `game_root: str = ""` after `entry_point`.
- Update `from_dict` and `to_dict` (omitted from JSON when empty).

### `cellar/backend/project.py`
- Add `game_root: str = ""` to `Project`.
- Update `from_dict` and `to_dict`.

### `cellar/views/package_builder.py`
- Add a **Game Root** `Adw.ActionRow` in the Files section, directly below the
  Entry Point row.
- Button opens `Gtk.FileChooserNative` with `SELECT_FOLDER`, starting in
  `drive_c`.
- On selection, validate:
  - `game_root` must be a proper ancestor of `entry_point` (i.e. entry_point
    starts with `game_root + "\"`).
  - `game_root` must not be a known system directory: `C:\`, `C:\Program
    Files`, `C:\Program Files (x86)`, `C:\Windows`, `C:\Users`,
    `C:\ProgramData`.
  - Show an `Adw.AlertDialog` with a specific message if either check fails.
- Save to `project.game_root` and persist via `save_project()`.
- Pass `game_root=project.game_root` when building `AppEntry` in
  `_on_publish_app_clicked`.

### `docs/CATALOGUE_FORMAT.md`
- Document the `game_root` field.

---

## Phase 2 — PCGamingWiki backend

### New file: `cellar/backend/pcgw.py`

Public function:

```python
def fetch_save_excludes(
    name: str,
    steam_appid: int,
    game_root: str,
) -> list[str]:
    ...
```

Returns a list of rsync-compatible exclude patterns, or `[]` on any
network/parse failure (caller falls back to blunt rules).

#### Internals

1. **Page lookup** — convert `name` to a PCGW title (`"Slay the Spire"` →
   `"Slay_the_Spire"`) and fetch wikitext:
   ```
   /w/api.php?action=query&prop=revisions&titles=<title>
              &rvprop=content&format=json&rvslots=main
   ```

2. **Fallback search** — if the direct fetch yields a missing or disambiguation
   page, fall back to:
   ```
   /w/api.php?action=query&list=search&srsearch=<name>&srlimit=5
   ```
   Then fetch the first result whose wikitext contains the correct
   `steam_appid` in its infobox.

3. **Parse** — regex-extract all `{{Game data/saves|Windows|...}}` and
   `{{Game data/config|Windows|...}}` template calls. Templates can carry
   multiple path arguments.

4. **Resolve `{{p|...}}` macros** to Wine prefix paths:

   | Macro | Wine prefix path |
   |---|---|
   | `game` | derived from `game_root` (`C:\Games\Title` → `drive_c/Games/Title`) |
   | `localappdata` | `drive_c/users/*/AppData/Local` |
   | `appdata` / `roaming` | `drive_c/users/*/AppData/Roaming` |
   | `userprofile` | `drive_c/users/*` |
   | `documents` | `drive_c/users/*/Documents` |
   | anything else | skip (log a warning) |

   Paths using `{{p|game}}` are skipped if `game_root` is not set.

5. **Return** a deduplicated list of rsync exclude patterns, e.g.:
   ```python
   [
       "drive_c/users/*/AppData/Local/WW3/Saved/",
       "drive_c/Games/SlayTheSpire/saves/",
       "drive_c/Games/SlayTheSpire/runs/",
   ]
   ```

---

## Phase 3 — DB cache

### `cellar/backend/database.py`

New table added as a versioned schema migration:

```sql
CREATE TABLE pcgw_cache (
    steam_appid INTEGER PRIMARY KEY,
    excludes    TEXT NOT NULL,   -- JSON array of rsync patterns
    fetched_at  TIMESTAMP NOT NULL
);
```

New functions:
- `get_pcgw_excludes(steam_appid: int) -> list[str] | None` — returns `None`
  if missing or older than 30 days.
- `set_pcgw_excludes(steam_appid: int, excludes: list[str])` — upsert.

---

## Phase 4 — Installer hook

### `cellar/backend/installer.py`

After a successful install, if `entry.steam_appid` is set and the cache is
absent or stale, spawn a background thread to call `fetch_save_excludes()` and
store the result via `set_pcgw_excludes()`. No-op if `steam_appid` is `None`.

---

## Phase 5 — Updater integration

### `cellar/backend/updater.py`

In the safe update path, after building the baseline `_RSYNC_EXCLUDES` list:

1. Look up `get_pcgw_excludes(entry.steam_appid)` from the DB.
2. Merge with the baseline (union, deduplicated).
3. Log whether PCGW-derived or blunt-only excludes are in use.

The baseline blunt rules are **always** included regardless of PCGW data, as a
safety net for apps with no PCGW coverage.

---

## Future (out of scope for now)

- **Save/config backup** — use the same PCGW path data to copy saves out of
  the prefix before an update and restore them after, giving end users a safety
  net independent of the update strategy.
- **Manual override** — per-app UI to add or remove exclude paths in Settings.

---

## Files touched

| File | Change |
|---|---|
| `cellar/models/app_entry.py` | Add `game_root` field |
| `cellar/backend/project.py` | Add `game_root` field |
| `cellar/views/package_builder.py` | Game Root row + validation |
| `cellar/backend/pcgw.py` | **New** — PCGW fetch + parse |
| `cellar/backend/database.py` | `pcgw_cache` table + accessors |
| `cellar/backend/installer.py` | Post-install cache population |
| `cellar/backend/updater.py` | Merge PCGW excludes into rsync |
| `docs/CATALOGUE_FORMAT.md` | Document `game_root` field |

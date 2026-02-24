# Bottles Repository App — Project Brief

## What this project is

A GNOME desktop application that acts as a software storefront for Wine/Bottles-managed Windows applications. Think GNOME Software, but the "packages" are Bottles full backups stored on a network share (SMB, NFS, or HTTP). The user browses a catalogue, clicks Install, and the app handles downloading the backup, importing it into Bottles, and updating Wine components as needed.

The project is tentatively called **Cellar** (working title — rename freely).

---

## Tech stack

- **Language:** Python 3.11+
- **UI toolkit:** GTK4 + libadwaita (target GNOME 46+)
- **Packaging:** Flatpak (target `io.github.cellar` or similar)
- **Local data:** SQLite via `sqlite3` stdlib for installed app tracking
- **Network I/O:** GIO (`gi.repository.Gio`) for share access; `urllib` or `httpx` for HTTP repos
- **Archive handling:** Python `tarfile` stdlib
- **File sync:** `rsync` subprocess call for smart updates
- **Bottles integration:** `bottles-cli` subprocess calls + direct YAML manipulation

Avoid pulling in heavy dependencies where the stdlib or GLib/GIO covers the need.

---

## Repository / network share format

The app reads from a structured repository. The repo root contains a master index:

```
/repo/
  catalogue.json
  apps/
    appname/
      manifest.json
      icon.png
      screenshots/
        01.png
        02.png
      appname-1.0.tar.gz
```

### `catalogue.json`

Top-level index fetched on launch/refresh. Contains an array of lightweight entries:

```json
[
  {
    "id": "appname",
    "name": "App Name",
    "category": "Productivity",
    "summary": "One-line description",
    "icon": "apps/appname/icon.png",
    "version": "1.0",
    "manifest": "apps/appname/manifest.json"
  }
]
```

### `manifest.json`

Full metadata for a single app. Fetched when opening the detail view or installing:

```json
{
  "id": "appname",
  "name": "App Name",
  "version": "1.0",
  "category": "Productivity",
  "description": "Full description...",
  "icon": "apps/appname/icon.png",
  "screenshots": ["apps/appname/screenshots/01.png"],
  "archive": "apps/appname/appname-1.0.tar.gz",
  "archive_size": 524288000,
  "archive_sha256": "abc123...",
  "built_with": {
    "runner": "proton-ge-9-1",
    "dxvk": "2.3",
    "vkd3d": "2.11"
  },
  "update_strategy": "safe",
  "changelog": "Updated to version X, fixed Y"
}
```

`update_strategy` is either `"safe"` (rsync, preserve user data) or `"full"` (complete replacement, warn user).

---

## Bottles integration

### Data directories

Detect which variant of Bottles is installed and use the appropriate path:

| Variant | Bottles data path |
|---|---|
| Flatpak | `~/.var/app/com.usebottles.bottles/data/bottles/bottles/` |
| Native | `~/.local/share/bottles/bottles/` |

Check both at startup and let the user override in settings if needed.

### Import (install) flow

1. Download archive to a temp directory, verify SHA256
2. Extract archive to a temp location
3. Copy/move extracted bottle directory to the Bottles data path with a sanitised name derived from the app ID
4. Read `bottle.yml` from the extracted archive to get the component versions it was built with
5. Compare against manifest's `built_with` — if newer runners/DXVK are available in the user's Bottles install, offer to upgrade
6. Call `bottles-cli edit -b <BottleName> -k Runner -v <runner>` (and equivalents for DXVK/VKD3D) if the user accepts
7. Record the installation in the local SQLite database

### Update flow — safe strategy (default)

Used when `manifest.json` sets `update_strategy: "safe"`. Preserves user data inside the bottle.

1. Download and verify new archive
2. Extract to temp directory
3. Run rsync to overlay new files onto existing bottle, excluding user data:
   ```
   rsync -av \
     --exclude='drive_c/users/' \
     --exclude='user.reg' \
     --exclude='userdef.reg' \
     /tmp/new-backup/ \
     <bottles-data-path>/<BottleName>/
   ```
4. Merge component versions from new `bottle.yml` into existing one (update runner/dxvk/vkd3d keys only)
5. Run component update via `bottles-cli edit` if needed
6. Update local DB record with new version

### Update flow — full strategy

Used when `manifest.json` sets `update_strategy: "full"`, or user opts in manually.

1. Warn user that in-bottle changes will be lost
2. Remove existing bottle directory
3. Follow normal install flow

### bottles-cli reference commands

```bash
# List installed bottles
bottles-cli list bottles

# Edit a bottle's runner
bottles-cli edit -b "BottleName" -k Runner -v "proton-ge-9-1"

# Edit DXVK version
bottles-cli edit -b "BottleName" -k DXVK -v "2.3"

# Run a program inside a bottle (for post-install smoke test if desired)
bottles-cli run -b "BottleName" -e "explorer.exe"
```

`bottles-cli` is a subprocess call. Handle its absence gracefully (show a setup warning if Bottles isn't detected).

---

## Local database schema

Stored at `~/.local/share/cellar/cellar.db` (or Flatpak equivalent).

```sql
CREATE TABLE installed (
    id TEXT PRIMARY KEY,          -- matches catalogue app id
    bottle_name TEXT NOT NULL,    -- actual directory name in Bottles
    installed_version TEXT,
    installed_at TIMESTAMP,
    last_updated TIMESTAMP,
    repo_source TEXT              -- URL or path of the repo it came from
);

CREATE TABLE repos (
    id INTEGER PRIMARY KEY,
    name TEXT,
    uri TEXT NOT NULL,            -- smb://, nfs://, https://, or local path
    last_refreshed TIMESTAMP,
    enabled INTEGER DEFAULT 1
);
```

Multiple repos should be supported from the start, even if the UI only exposes one initially.

---

## UI structure

Model the layout on GNOME Software. Use libadwaita components throughout.

### Main window

- **Category filter:** horizontal strip of linked `GtkToggleButton` pills (radio behaviour via `set_group`), built dynamically from the catalogue — one button per category plus "All". Scrolls horizontally if categories overflow.
- **Main area:** `GtkFlowBox` of app cards — icon, name, short description. Cards use the `.card` Adwaita style class.
- **Header bar:** Search toggle (reveals `GtkSearchBar`), Refresh button, Menu button. Typing anywhere in the window opens the search bar automatically via `set_key_capture_widget`.
- Empty/error states use `AdwStatusPage`.

### App detail view

- Large icon + name + category badge
- Description
- Screenshots carousel (`AdwCarousel`)
- "Install" / "Update" / "Remove" button (context-sensitive)
- Component info section: runner, DXVK, VKD3D versions
- Changelog (for updates)

### Install/update progress

Use `AdwToastOverlay` for non-blocking progress notifications for small operations. For downloads, show an inline progress bar in the detail view or a separate `AdwDialog` with cancel support.

### Settings

- Repo management (add/remove/enable sources)
- Bottles data directory override
- Default update strategy preference

---

## Project structure

```
cellar/
  cellar/
    __init__.py
    main.py                  # GApplication entry point
    window.py                # Main AdwApplicationWindow
    views/
      browse.py              # Grid browse view
      detail.py              # App detail page
      installed.py           # Installed apps view
      updates.py             # Available updates view
      settings.py            # Settings dialog
    backend/
      repo.py                # Catalogue fetching, manifest parsing
      installer.py           # Download, extract, import to Bottles
      updater.py             # rsync-based update logic
      bottles.py             # bottles-cli wrapper, path detection
      database.py            # SQLite installed/repo tracking
    models/
      app_entry.py           # Dataclass for catalogue entries
      manifest.py            # Dataclass for full app manifest
    utils/
      gio_io.py              # GIO-based file/network helpers
      checksum.py            # SHA256 verification
  data/
    io.github.cellar.gschema.xml
    io.github.cellar.desktop
    io.github.cellar.metainfo.xml
    ui/
      window.ui
      app_card.ui
      detail_view.ui
  po/                        # i18n (set up but don't need to fill out)
  flatpak/
    io.github.cellar.json    # Flatpak manifest
  pyproject.toml
  meson.build
  CLAUDE.md                  # this file
```

---

## Development priorities

Build in this order:

1. ~~**Repo backend** — parse a local `catalogue.json` and `manifest.json`, no network yet~~ ✅
2. ~~**Browse UI** — grid of app cards from parsed catalogue, category filter, search~~ ✅
3. **Detail view** — app page driven by manifest data
4. **Bottles backend** — path detection, `bottles-cli` wrapper, basic install (extract + copy)
5. **Local DB** — track installed apps, wire up Install button state
6. **Update logic** — safe rsync strategy first, full replacement second
7. **Network repo support** — GIO for SMB/NFS, HTTP fallback
8. **Component update UI** — post-install prompt to upgrade runner/DXVK
9. **Multi-repo support** — settings page, repo management
10. **Flatpak packaging**

---

## Running in development

```bash
PYTHONPATH=. CELLAR_REPO=tests/fixtures python3 -m cellar.main
```

`CELLAR_REPO` points to any directory containing a `catalogue.json`. The test fixtures under `tests/fixtures/` work out of the box. Tests: `python3 -m pytest tests/ -v`.

UI files are resolved by `cellar/utils/paths.py` — it checks the source tree (`data/ui/`) first, then the installed location (`/app/share/cellar/ui/`), so no build step is needed during development.

---

## Key constraints and gotchas

- **Flatpak sandbox:** If shipped as Flatpak, `bottles-cli` may need to be called via `flatpak-spawn --host` if Bottles itself is a Flatpak. Handle both cases.
- **rsync availability:** rsync is not always present. Check at runtime and fall back to a Python-based directory merge if missing (slower but functional).
- **Bottle naming collisions:** When importing, check if a bottle with the target name already exists. Append a suffix rather than silently overwriting.
- **Archive size:** These archives can be multi-gigabyte. All download and extract operations must be async (use `GLib.Thread` or `asyncio` with GLib main loop integration). Never block the UI thread.
- **bottles-cli not found:** Show a clear setup prompt rather than crashing. Detect both Flatpak and native installs.
- **Repo unreachable:** Gracefully show cached catalogue if the repo is offline. Don't prevent app launch.

---

## Out of scope (for now)

- Publishing/uploading to a repo from within the app (read-only client for now)
- Per-app sandboxing beyond what Bottles provides
- Windows app auto-detection or installer execution
- Cloud sync of user data inside bottles

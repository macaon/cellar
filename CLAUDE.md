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
- **Network I/O:** `urllib` for HTTP/HTTPS; system `ssh` subprocess for SSH repos; GIO (`gi.repository.Gio`) for SMB/NFS via GVFS
- **Archive handling:** Python `tarfile` stdlib
- **File sync:** `rsync` subprocess call for smart updates
- **Bottles integration:** `bottles-cli` subprocess calls + direct YAML manipulation

Avoid pulling in heavy dependencies where the stdlib or GLib/GIO covers the need.

---

## Repository / network share format

A Cellar repo is a directory (local or remote) containing a single master index and per-app asset directories:

```
repo/
  catalogue.json          ← single source of truth; fetched on launch/refresh
  apps/
    appname/
      icon.png            ← square icon (browse grid)
      cover.png           ← portrait cover (detail view)
      hero.png            ← wide banner (detail view header)
      screenshots/
        01.png
        02.png
      appname-1.0.tar.gz
```

There are no per-app `manifest.json` files. All metadata lives in `catalogue.json`.

### `catalogue.json`

A fat JSON file containing every app's full metadata. All asset paths are relative to the repo root.

```json
{
  "cellar_version": 1,
  "generated_at": "2026-02-25T12:00:00Z",
  "apps": [
    {
      "id": "appname",
      "name": "App Name",
      "version": "1.0",
      "category": "Productivity",
      "tags": ["Productivity", "Office"],
      "summary": "One-line description",
      "description": "Full description...",
      "developer": "Some Studio",
      "publisher": "Some Publisher",
      "release_year": 2020,
      "content_rating": "PEGI 3",
      "languages": ["English", "German"],
      "website": "https://example.com",
      "store_links": { "steam": "https://store.steampowered.com/app/12345" },
      "icon": "apps/appname/icon.png",
      "cover": "apps/appname/cover.png",
      "hero": "apps/appname/hero.png",
      "screenshots": ["apps/appname/screenshots/01.png"],
      "archive": "apps/appname/appname-1.0.tar.gz",
      "archive_size": 524288000,
      "archive_sha256": "abc123...",
      "install_size_estimate": 2147483648,
      "built_with": {
        "runner": "proton-ge-9-1",
        "dxvk": "2.3",
        "vkd3d": "2.11"
      },
      "update_strategy": "safe",
      "entry_point": "Program Files/AppName/app.exe",
      "compatibility_notes": "",
      "changelog": "Updated to version X, fixed Y"
    }
  ]
}
```

`update_strategy` is either `"safe"` (rsync overlay, preserves user data) or `"full"` (complete replacement, warn user).

All fields except `id`, `name`, `version`, and `category` are optional; unset fields default to empty strings / empty collections / `None`.

### Supported URI schemes

| Scheme | Writable | Notes |
|---|---|---|
| Local path / `file://` | Yes | |
| `http://` / `https://` | **No** | Read-only; intended for family/shared access |
| `ssh://[user@]host[:port]/path` | Yes | Uses system `ssh` client; key auth via agent or `ssh_identity=` |
| `smb://` | Yes | Via GIO/GVFS |
| `nfs://` | Yes | Via GIO/GVFS |

If the client reaches a location with no `catalogue.json`, it offers to initialise a new repo (writable transports only). HTTP repos show an error instead.

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
      browse.py              # Grid browse view ✅
      detail.py              # App detail page (Install/Update/Remove) ✅
      add_app.py             # Add-app-to-catalogue dialog ✅
      edit_app.py            # Edit/delete catalogue entry dialog ✅
      update_app.py          # Safe update dialog (backup + rsync overlay) ✅
      settings.py            # Settings / repo management dialog ✅
      installed.py           # Installed apps view (stub)
      updates.py             # Available updates view (stub)
    backend/
      repo.py                # Catalogue fetching, all transport backends ✅
      packager.py            # import_to_repo / update_in_repo / remove_from_repo ✅
      installer.py           # Download, verify, extract, import to Bottles ✅
      updater.py             # Safe rsync overlay update + backup_bottle ✅
      bottles.py             # bottles-cli wrapper, path detection ✅
      database.py            # SQLite installed/repo tracking ✅
      config.py              # JSON config persistence (repos, capsule size) ✅
    models/
      app_entry.py           # Unified AppEntry + BuiltWith dataclasses ✅
    utils/
      gio_io.py              # GIO-based file/network helpers ✅
      paths.py               # UI file path resolution ✅
      checksum.py            # SHA-256 utility ✅
  data/
    io.github.cellar.gschema.xml
    io.github.cellar.desktop
    io.github.cellar.metainfo.xml
    ui/
      window.ui
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

1. ~~**Repo backend** — local catalogue parsing~~ ✅
2. ~~**Browse UI** — grid of app cards, category filter, search~~ ✅
3. ~~**Network repo support** — HTTP/HTTPS, SSH, SMB, NFS transports; unified `AppEntry` model; fat `catalogue.json` format~~ ✅
4. ~~**Detail view** — full app page from catalogue data; `AdwNavigationView` navigation~~ ✅
5. ~~**Bottles backend** — path detection, `bottles-cli` wrapper, install + remove~~ ✅
6. ~~**Local DB** — track installed apps, wire up Install/Remove button state~~ ✅
7. ~~**Update logic** — safe rsync overlay (no --delete; AppData/Documents excluded)~~ ✅
8. **Component update UI** — post-install prompt to upgrade runner/DXVK
9. **Repo management UI** — add/remove/initialise repos from settings; optional IGDB/Steam metadata fetch on add
10. **Flatpak packaging**

---

## Running in development

```bash
PYTHONPATH=. CELLAR_REPO=tests/fixtures python3 -m cellar.main
```

`CELLAR_REPO` accepts a local path or any supported URI (`https://`, `ssh://`, `smb://`, `nfs://`). The test fixtures under `tests/fixtures/` work out of the box. Tests: `PYTHONPATH=. python3 -m pytest tests/ -v`.

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

- Per-app sandboxing beyond what Bottles provides
- Windows app auto-detection or installer execution
- Cloud sync of user data inside bottles

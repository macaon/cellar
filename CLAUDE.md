# Cellar — Project Brief

## What this project is

A GNOME desktop application that acts as a software storefront for Wine/Bottles-managed Windows applications. Think GNOME Software, but the "packages" are Bottles full backups stored on a network share (SMB, NFS, or HTTP). The user browses a catalogue, clicks Install, and the app handles downloading the backup and importing it into Bottles. Component version management (runner, DXVK, VKD3D) is left to Bottles itself.

The project is called **Cellar**.

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
      icon.png            ← square icon (browse grid); PNG, JPG, ICO, or SVG
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
  "categories": ["Games", "Productivity", "Graphics", "Utility", "My Custom Category"],
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

The top-level `categories` array is optional. It defines custom categories beyond the built-in ones (`BASE_CATEGORIES` in `packager.py`: Games, Productivity, Graphics, Utility). Custom categories are merged with the base list and appear in the category filter strip and the Add/Edit app dialogs. The `"Custom…"` sentinel in the combo reveals an `AdwEntryRow` for typing a new category.

All fields except `id`, `name`, `version`, and `category` are optional; unset fields default to empty strings / empty collections / `None`.

### Supported URI schemes

| Scheme | Writable | Notes |
|---|---|---|
| Local path / `file://` | Yes | |
| `http://` / `https://` | **No** | Read-only; optional bearer token auth |
| `ssh://[user@]host[:port]/path` | Yes | Uses system `ssh` client; key auth via agent or `ssh_identity=` |
| `smb://` | Yes | Via GIO/GVFS |
| `nfs://` | Yes | Via GIO/GVFS |

If the client reaches a location with no `catalogue.json`, it offers to initialise a new repo (writable transports only). HTTP repos show an error instead.

### HTTP(S) bearer token authentication

HTTP(S) repos support an optional bearer token for access control. The token is:
- Generated via Settings → Access Control → Generate (`secrets.token_hex(32)`, 64 hex chars)
- Stored per-repo in `~/.local/share/cellar/config.json`
- Sent as `Authorization: Bearer <token>` on every HTTP request (catalogue fetch, image download, archive download)
- Configurable at add-time via the "Access token (optional)" `Adw.EntryRow` in Settings

Image assets on HTTP(S) repos are downloaded to a per-session `tempfile.TemporaryDirectory` cache in `Repo._fetch_to_cache` and returned as local paths. This is necessary because `GdkPixbuf` cannot pass auth headers when given an `http://` URL, and `os.path.isfile()` returns `False` for URLs. Archives are still returned as URLs since the installer's own download code handles auth.

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
4. Record the installation in the local SQLite database

### Update flow — safe strategy (default)

Used when `update_strategy: "safe"`. Preserves user data inside the bottle.

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
4. Update local DB record with new version

### Update flow — full strategy

Used when `update_strategy: "full"`, or user opts in manually.

1. Warn user that in-bottle changes will be lost
2. Remove existing bottle directory
3. Follow normal install flow

`bottles-cli` is used only for detecting installed Bottles instances (`bottles-cli list bottles`). Component version management is left to Bottles. Handle its absence gracefully (show a setup warning if Bottles isn't detected).

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

Multiple repos are supported; the UI merges them (last-repo-wins on ID collision).

---

## UI structure

Model the layout on GNOME Software. Use libadwaita components throughout.

### Main window

- **View switcher:** `AdwViewSwitcher` replaces the window title with three tabs — **Explore** (all apps), **Installed** (installed only), **Updates** (updates available, with badge count). Backed by `AdwViewStack`.
- **Category filter:** horizontal strip of linked `GtkToggleButton` pills (radio behaviour via `set_group`), built dynamically from the catalogue — one button per category plus "All". Scrolls horizontally if categories overflow.
- **Main area:** `GtkFlowBox` (`homogeneous=False`, `halign=CENTER`) of GNOME Software-style horizontal app cards — fixed 300 × 96 px, cover thumbnail (64 × 96, 2:3 ratio) or 48 px icon on the left, name + up-to-two-line summary on the right. Cards use the `.card` Adwaita style class.
- **Header bar:** Search toggle at far left (reveals `GtkSearchBar`), Refresh button, Menu button. Typing anywhere opens search automatically via `set_key_capture_widget`.
- Empty/error states use `AdwStatusPage`.
- Tab icons (`software-explore-symbolic`, `software-installed-symbolic`, `software-updates-symbolic`) are bundled under `data/icons/hicolor/symbolic/apps/` (CC0-1.0) and registered at startup via `Gtk.IconTheme.add_search_path()`.

### App detail view

- Hero banner (full width) + large icon + name + category badge
- Description, component info (runner, DXVK, VKD3D), changelog
- Screenshots carousel (`AdwCarousel`) with navigation arrows and fullscreen viewer
- Context-sensitive action button: **Install** / **Update** / **Remove**
- Edit button (pencil icon) for writable repos

### Install/update progress

`AdwDialog` with a cancel button, inline progress bar (download phase + install phase), and status label. On completion, `AdwToastOverlay` shows a non-blocking confirmation toast.

### Settings

`Adw.PreferencesDialog` with one page:

- **Repositories** group: `Adw.EntryRow` for URI + `Adw.EntryRow` for optional bearer token; add button triggers validation and saves. Existing repos shown as `Adw.ExpanderRow` rows with remove button; HTTP repos with a token show a masked indicator and a **Change…** button.
- **Access Control** group: **Generate** button creates a 64-character random token, shows it in a dialog, and copies it to the clipboard. Intended for configuring a web server; the generated token is not automatically associated with any repo.

---

## Project structure

```
cellar/
  cellar/
    main.py                  # GApplication entry point
    window.py                # Main AdwApplicationWindow
    views/
      browse.py              # BrowseView — used for all three tabs (Explore/Installed/Updates)
      detail.py              # App detail page (Install/Update/Remove)
      add_app.py             # Add-app-to-catalogue dialog
      edit_app.py            # Edit/delete catalogue entry dialog
      update_app.py          # Safe update dialog (backup + rsync overlay)
      settings.py            # Settings / repo management dialog
    backend/
      repo.py                # Catalogue fetching, all transport backends
      packager.py            # import_to_repo / update_in_repo / remove_from_repo
      installer.py           # Download, verify, extract, import to Bottles
      updater.py             # Safe rsync overlay update + backup_bottle
      bottles.py             # Bottles path detection
      database.py            # SQLite installed/repo tracking
      config.py              # JSON config persistence (repos)
    models/
      app_entry.py           # AppEntry + BuiltWith dataclasses
    utils/
      gio_io.py              # GIO-based file/network helpers
      paths.py               # UI + icons path resolution (ui_file, icons_dir)
      checksum.py            # SHA-256 utility
  data/
    icons/
      hicolor/symbolic/apps/ # Bundled tab icons (CC0-1.0, fill=currentColor)
    ui/
      window.ui
  po/                        # i18n (skeleton only)
  tests/
    fixtures/                # Sample catalogue.json for local testing
    test_repo.py
    test_bottles.py
    test_database.py
    test_installer.py
  pyproject.toml
  meson.build
  CLAUDE.md                  # this file
```

---

## Development priorities

1. ~~**Repo backend** — local catalogue parsing~~ ✅
2. ~~**Browse UI** — grid of app cards, category filter, search~~ ✅
3. ~~**Network repo support** — HTTP/HTTPS, SSH, SMB, NFS transports; unified `AppEntry` model; fat `catalogue.json` format~~ ✅
4. ~~**Detail view** — full app page from catalogue data; `AdwNavigationView` navigation~~ ✅
5. ~~**Bottles backend** — path detection, install + remove~~ ✅
6. ~~**Local DB** — track installed apps, wire up Install/Remove button state~~ ✅
7. ~~**Update logic** — safe rsync overlay (no --delete; AppData/Documents excluded)~~ ✅
8. ~~**HTTP(S) auth** — bearer token generation, per-request injection, image asset caching~~ ✅
9. **Flatpak packaging**
10. **KDE support** — GVFS fallback for SMB/NFS (`smbclient` or `gio mount` subprocess), KWallet credential storage, adaptive styling via `XDG_CURRENT_DESKTOP`

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
- **HTTP User-Agent:** Python's default `User-Agent: Python-urllib/3.x` is blocked by Cloudflare and other CDN/WAF bot-protection rules. All outbound HTTP requests use `User-Agent: Mozilla/5.0 (compatible; Cellar/1.0)` (the `_USER_AGENT` constant in `repo.py`).
- **nginx `^~` and image assets:** A plain `location /cellar/ { root /; }` block will lose to any `location ~* \.(jpg|png|...)$` regex block in the same server config (regex locations have higher priority than prefix locations in nginx). Use `location ^~ /cellar/` so the prefix match wins and images are served correctly.
- **GdkPixbuf and HTTP:** `GdkPixbuf.new_from_file` cannot pass auth headers, and `os.path.isfile()` returns `False` for HTTP URLs. For HTTP(S) repos, `Repo.resolve_asset_uri` downloads image assets to a per-session temp cache and returns local paths. Archives still return URLs (handled by the installer).

---

## Out of scope (for now)

- Per-app sandboxing beyond what Bottles provides
- Windows app auto-detection or installer execution
- Cloud sync of user data inside bottles

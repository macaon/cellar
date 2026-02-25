# Cellar

A GNOME desktop application that acts as a software storefront for
[Bottles](https://usebottles.com/)-managed Windows applications. Think GNOME
Software, but the "packages" are Bottles full backups stored on a network share
or web server. Browse a catalogue, click Install, and Cellar handles downloading
the backup, importing it into Bottles, and updating Wine components as needed.

The primary use case is a private family or home-lab server: the person who
manages the repo adds and packages apps; everyone else browses and installs via
a read-only HTTP URL (or directly over SMB/NFS/SSH if they have access).

> **Status: early development** — the browse UI and repo backend are working;
> install/update flows are not yet implemented.

---

## Tech stack

- **Language:** Python 3.11+
- **UI toolkit:** GTK4 + libadwaita (GNOME 46+)
- **Packaging:** Flatpak (`io.github.cellar`)
- **Local data:** SQLite via `sqlite3` stdlib
- **Network I/O:** `urllib` for HTTP/HTTPS; system `ssh` client for SSH; GIO/GVFS for SMB and NFS
- **Bottles integration:** `bottles-cli` subprocess + YAML manipulation

---

## Running in development

### Requirements

- Python 3.11+
- GTK 4 and libadwaita 1.x (`python3-gobject` / `pygobject`)
- `pytest` for running tests

### Quick start

```bash
git clone <repo-url> cellar
cd cellar

# Run against the bundled test fixtures
PYTHONPATH=. CELLAR_REPO=tests/fixtures python3 -m cellar.main
```

`CELLAR_REPO` accepts a local path, a `file://` URI, or any supported remote
URI (`https://`, `ssh://`, `smb://`, `nfs://`). The test fixtures under
`tests/fixtures/` work out of the box for local development.

### Running tests

```bash
PYTHONPATH=. python3 -m pytest tests/ -v
```

---

## Repository format

A Cellar repo is a directory (local or remote) with this structure:

```
repo/
  catalogue.json          ← master index, fetched on launch/refresh
  apps/
    appname/
      icon.png            ← square icon (browse grid)
      cover.png           ← portrait cover (detail view)
      hero.png            ← wide banner (detail view header)
      screenshots/
        01.png
      appname-1.0.tar.gz  ← Bottles full backup archive
```

### `catalogue.json`

A single JSON file containing every app's full metadata — no separate
per-app manifest files. All paths inside are relative to the repo root.

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
      "languages": ["English"],
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
      "changelog": "Updated to 1.0."
    }
  ]
}
```

`update_strategy` is `"safe"` (rsync overlay, preserves user data in the
bottle) or `"full"` (complete replacement, warns the user first).

### Supported repo URI schemes

| Scheme | Example | Writable |
|---|---|---|
| Local path | `/mnt/nas/cellar` | Yes |
| `file://` | `file:///mnt/nas/cellar` | Yes |
| `http://` / `https://` | `https://cellar.home.arpa/repo` | No |
| `ssh://` | `ssh://alice@nas.home.arpa/srv/cellar` | Yes |
| `smb://` | `smb://nas.home.arpa/cellar` | Yes |
| `nfs://` | `nfs://nas.home.arpa/export/cellar` | Yes |

HTTP(S) repos are always read-only. If you point Cellar at a location with no
`catalogue.json`, it will offer to initialise a new repository (writable
transports only).

---

## Project structure

```
cellar/
  cellar/
    main.py          GApplication entry point
    window.py        Main AdwApplicationWindow
    views/
      browse.py      Grid browse view (app cards, category filter, search) ✅
      detail.py      App detail page ✅
      installed.py   Installed apps view (phase 5)
      updates.py     Available updates view (phase 6)
      settings.py    Settings / repo management (phase 9)
    backend/
      repo.py        Catalogue fetching, all transport backends ✅
      installer.py   Download, extract, import to Bottles (phase 4)
      updater.py     rsync-based update logic (phase 6)
      bottles.py     bottles-cli wrapper, path detection (phase 4)
      database.py    SQLite installed/repo tracking (phase 5)
    models/
      app_entry.py   Unified app/game dataclass (AppEntry + BuiltWith) ✅
    utils/
      paths.py       UI file path resolution (source tree + installed) ✅
      gio_io.py      GIO file helpers ✅
      checksum.py    SHA-256 verification (phase 4)
  data/
    ui/
      window.ui      Main window template
      app_card.ui    App card template
      detail_view.ui Detail view template
  tests/
    fixtures/        Sample catalogue.json for local testing
    test_repo.py     Backend unit tests (42 tests)
```

---

## Development roadmap

1. **Repo backend** — catalogue parsing, all transport backends ✅
2. **Browse UI** — app card grid, category filter, search ✅
3. **Detail view** — full app page from catalogue data ✅
4. **Bottles backend** — path detection, `bottles-cli` wrapper, basic install
5. **Local DB** — track installed apps, wire up Install button state
6. **Update logic** — safe rsync strategy, full replacement
7. **Component update UI** — post-install prompt to upgrade runner/DXVK
8. **Repo management UI** — add/remove sources, initialise new repos, pull metadata from IGDB/Steam
9. **Multi-repo support** — settings page
10. **Flatpak packaging**

---

## License

GPL-3.0-or-later

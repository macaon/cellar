# Cellar

A GNOME desktop application that acts as a private software storefront for
Windows and Linux applications. Think GNOME Software, but the "packages" are
pre-configured app archives stored on a network share or web server.

The primary use case is a home-lab or family server: a maintainer packages and
publishes apps from their machine using the built-in Package Builder; everyone
else browses the catalogue and installs with one click.

Windows apps run via [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher)
with GE-Proton. Linux native apps are extracted and launched directly.

---

## Features

**For users**
- GNOME Software-style browse grid — Explore, Installed, and Updates tabs
- Category filter (funnel icon popover), full-text search
- App detail view — icon/logo, screenshots carousel, description, and metadata info cards
- One-click install, update (safe rsync overlay or full replacement), and remove
- Delta package support — shared base images dramatically reduce download size
- Runner management — GE-Proton versions listed from GitHub Releases; prompts to download if missing
- Linux native app support alongside Windows apps
- Desktop shortcut creation for installed apps
- Launch apps directly from Cellar (standard or in a terminal window)

**For maintainers** (requires a writable repo)
- Package Builder — two-panel view for creating and publishing packages:
  - **Windows package** — initialise a WINEPREFIX with a chosen GE-Proton runner, install winetricks dependencies, run `.exe` installers, configure entry points, test-launch, then publish
  - **Linux package** — provide an archive, set an entry point, publish
  - **Base package** — build a shared WINEPREFIX used as the delta base for multiple app packages
  - **Import from catalogue** — pull an existing catalogue entry into a local project for re-packaging
- Steam Store metadata lookup — search by name to auto-fill title, description, developer, genres, cover art, and screenshots
- Edit and delete existing catalogue entries
- Delta archive creation — diff against a base image using BLAKE2b content hashing

---

## Tech stack

- **Language:** Python 3.11+
- **UI toolkit:** GTK4 + libadwaita (GNOME 46+)
- **Windows compatibility:** [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher) + GE-Proton
- **Runner index:** GitHub Releases API (GloriousEggroll/proton-ge-custom), cached in memory
- **Local data:** SQLite via `sqlite3` stdlib
- **Network I/O:** `requests` for HTTP/HTTPS; system `ssh` for SSH; `smbprotocol` for SMB
- **SMB credentials:** `keyring` (system keyring) with `config.json` fallback
- **Image handling:** Pillow (load, resize, crop, ICO→PNG, optimise)
- **Archive handling:** `tarfile` stdlib; `zstandard` for `.tar.zst` archives
- **File sync:** `rsync` subprocess; Python fallback if rsync is absent
- **Metadata:** Steam Store API (no authentication required)

---

## Running in development

### Requirements

- Python 3.11+
- GTK 4 and libadwaita 1.x (`python3-gobject` / `pygobject`)
- `pip install requests Pillow zstandard smbprotocol keyring pytest`
- [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher) for launching/building Windows apps

### Quick start

```bash
git clone https://github.com/macaon/cellar
cd cellar

# Run against the bundled test fixtures
PYTHONPATH=. CELLAR_REPO=tests/fixtures python3 -m cellar.main
```

`CELLAR_REPO` accepts a local path, a `file://` URI, or any supported remote
URI (`https://`, `ssh://`, `smb://`). The test fixtures under `tests/fixtures/`
work out of the box for local development.

### Running tests

```bash
PYTHONPATH=. python3 -m pytest tests/ -v
```

---

## Repository format

A Cellar repo is a directory (local or remote) containing a `catalogue.json`
master index and app assets. If you point Cellar at a writable location with
no `catalogue.json` it will offer to initialise a new repository.

```
repo/
  catalogue.json
  apps/
    <id>/
      icon.png            square icon — PNG, JPG, ICO, or SVG
      cover.png           portrait cover (2:3) — shown in browse cards
      hero.png            wide banner — stored but reserved for future use
      logo.png            transparent logo — replaces icon in detail view (use with hide_title)
      screenshots/
        01.png
      <id>-1.0.tar.zst    full archive, OR delta archive (requires a base image)
  bases/
    <runner>-base.tar.zst shared base image for delta packages
```

Archives contain a single top-level `prefix/` directory holding the WINEPREFIX.

### `catalogue.json`

A single JSON file containing all app metadata. All asset paths are relative
to the repo root.

```json
{
  "cellar_version": 1,
  "generated_at": "2026-02-25T12:00:00Z",
  "category_icons": {
    "Games": "applications-games-symbolic",
    "Productivity": "applications-office-symbolic"
  },
  "categories": ["My Custom Category"],
  "bases": {
    "GE-Proton10-32": {
      "archive": "bases/GE-Proton10-32-base.tar.zst",
      "archive_size": 2684354560,
      "archive_crc32": "aabbccdd"
    }
  },
  "apps": [
    {
      "id": "my-app",
      "name": "My App",
      "version": "1.2.3",
      "category": "Games",

      "summary": "One-line description shown in the browse grid",
      "description": "Full description shown in the detail view.",
      "developer": "Some Studio",
      "publisher": "Some Publisher",
      "release_year": 2022,
      "content_rating": "PEGI 16",
      "languages": ["English", "German"],
      "genres": ["Action", "Adventure"],
      "website": "https://example.com",
      "store_links": { "steam": "https://store.steampowered.com/app/12345" },

      "icon": "apps/my-app/icon.png",
      "cover": "apps/my-app/cover.png",
      "logo": "apps/my-app/logo.png",
      "hide_title": true,
      "screenshots": ["apps/my-app/screenshots/01.png"],

      "archive": "apps/my-app/my-app-1.2.3.tar.zst",
      "archive_size": 104857600,
      "archive_crc32": "11223344",
      "install_size_estimate": 524288000,
      "built_with": {
        "runner": "GE-Proton10-32",
        "dxvk": "2.3",
        "vkd3d": "2.11"
      },
      "base_runner": "GE-Proton10-32",
      "steam_appid": 12345,
      "update_strategy": "safe",
      "entry_point": "C:\\Program Files\\MyApp\\myapp.exe",
      "compatibility_notes": "Runs well at high settings.",
      "changelog": "Updated to 1.2.3.",
      "lock_runner": false
    }
  ]
}
```

**Key fields:**

- `base_runner` — when set, the archive is a delta against the named base
  image. The installer seeds the prefix from the base before applying the
  delta. Omit for a full archive.
- `steam_appid` — sets `GAMEID=umu-<id>` for umu-launcher, enabling
  [protonfixes](https://github.com/Open-Wine-Components/umu-protonfixes) for
  that title. Omit or set to `null` for `GAMEID=0` (no protonfixes).
- `update_strategy` — `"safe"` (rsync overlay, preserves user data) or
  `"full"` (complete replacement with a warning).
- `hide_title` — suppress the app name label in the detail view when a
  transparent `logo` image is provided (Steam-style).
- `lock_runner` — prevent the user from changing the runner for this app.
- `entry_point` — Windows path to the main executable (e.g.
  `C:\Program Files\App\App.exe`) for Windows apps, or a path relative to the
  install directory for Linux native apps.
- `platform` — `"windows"` (default, omitted from JSON) or `"linux"` for
  native Linux apps.
- `category_icons` — optional top-level map of category name → symbolic icon
  name. Uses standard Adwaita icon names.
- `categories` — optional list of custom categories beyond the built-in ones
  (Games, Productivity, Graphics, Utility).

All fields except `id`, `name`, `version`, and `category` are optional.

### Delta packages

Delta archives contain only files that differ from a shared base image,
dramatically reducing download size. A base is a clean, fully-initialised
WINEPREFIX (runner + common dependencies like fonts and runtime libraries)
without any specific app installed.

**Workflow:**
1. Maintainer creates a Base project in the Package Builder, installs shared
   dependencies, and publishes it to the repo.
2. When building an app whose runner matches an installed base, the Package
   Builder computes the delta (BLAKE2b-128 content hashing) and produces a
   `.tar.zst` archive containing only changed/added files plus a
   `.cellar_delete` manifest for removed files.
3. On install, Cellar seeds the new prefix from the base (using hardlinks or
   copy-on-write where available), then overlays the delta. The result is
   byte-for-byte identical to a full archive install.

### Supported repo URI schemes

| Scheme | Example | Writable |
|---|---|---|
| Local path | `/mnt/nas/cellar` | Yes |
| `file://` | `file:///mnt/nas/cellar` | Yes |
| `http://` / `https://` | `https://cellar.home.arpa/repo` | No |
| `ssh://` | `ssh://alice@nas.home.arpa/srv/cellar` | Yes |
| `smb://` | `smb://nas.home.arpa/cellar` | Yes |

SMB uses pure-Python `smbprotocol` (SMBv2/v3) — no GVFS mount required.
Credentials are stored per-repo in the system keyring with a `config.json`
fallback.

### Bearer token authentication for HTTP(S) repos

Cellar supports per-repo bearer token auth for HTTP(S). This lets you share a
private repo URL without making it publicly accessible.

1. Open Preferences → **Access Control** → **Generate**. Copy the token.
2. Paste it into your web server config (see examples below).
3. When adding the repo, enter the URL and paste the token into the
   **Access token** field. Share both with any users who need access.

**nginx example**

```nginx
map $http_authorization $cellar_auth_ok {
    "Bearer YOUR_TOKEN_HERE"  1;
    default                   0;
}

server {
    listen 443 ssl;
    server_name cellar.example.com;

    ssl_certificate     /etc/ssl/certs/cellar.crt;
    ssl_certificate_key /etc/ssl/private/cellar.key;

    # ^~ is required: stops regex location blocks (e.g. image-caching rules)
    # from intercepting asset requests before this block runs.
    location ^~ /cellar/ {
        if ($cellar_auth_ok = 0) { return 401 "Unauthorized\n"; }
        root /;
        autoindex off;
        add_header Accept-Ranges bytes;
        expires 5d;
    }
}
```

**Caddy example**

```caddy
cellar.example.com {
    handle /cellar/* {
        @unauth not header Authorization "Bearer YOUR_TOKEN_HERE"
        respond @unauth "Unauthorized" 401
        file_server { root / }
    }
}
```

---

## Local data layout

```
~/.local/share/cellar/
  cellar.db             SQLite database (installed apps, base images)
  config.json           Repos, bearer tokens, umu path override
  runners/
    GE-Proton10-32/     GE-Proton runner (managed by Cellar)
  prefixes/
    <app-id>/           WINEPREFIX for each installed Windows app
  projects/
    <slug>/             Package Builder working area
      project.json
      prefix/           WINEPREFIX being built
      <slug>.tar.zst    Generated archive (created on Publish)
  bases/
    <runner>/           Installed base images (for delta seeding)

~/.cache/cellar/
  assets/               Persistent image asset cache (HTTP(S) repos)
```

---

## Project structure

```
cellar/
  cellar/
    main.py               GApplication entry point; CSS provider; about dialog
    window.py             Main AdwApplicationWindow; catalogue load/refresh; filter popover
    views/
      browse.py           Explore / Installed / Updates grid; search; category filter popover
      detail.py           App detail page — icon/logo, screenshots, description, info cards, install/update/remove
      package_builder.py  Package Builder — create, build, and publish app/base projects
      edit_app.py         Edit / delete catalogue entries
      update_app.py       Safe update dialog — rsync overlay
      install_runner.py   GE-Proton download + extract dialog
      steam_picker.py     Steam Store game search and metadata picker
      steam_screenshot_picker.py  Steam screenshot browser and importer
      settings.py         Repos, access control, umu path, runners
    backend/
      repo.py             Catalogue fetch; transport backends (local, HTTP, SSH, SMB)
      packager.py         import_to_repo, update_in_repo, remove_from_repo, create_delta_archive
      installer.py        Download → verify → extract → install pipeline; delta install
      updater.py          Safe rsync overlay update; prefix backup
      base_store.py       Delta base image store — install, remove, path helpers
      umu.py              umu-launcher detection, launch, prefix init, winetricks
      runners.py          GE-Proton release listing (GitHub API) and install management
      project.py          Package Builder project CRUD and packaging
      steam.py            Steam Store API client; metadata normalisation
      database.py         SQLite tracking — installed apps, base images
      config.py           JSON config persistence (~/.local/share/cellar/config.json)
    models/
      app_entry.py        AppEntry, BuiltWith, BaseEntry dataclasses
    utils/
      smb.py              SmbPath — pathlib.Path-compatible SMB file access via smbprotocol
      http.py             requests.Session factory (User-Agent, bearer auth, SSL)
      images.py           Pillow helpers — load, crop, fit, ICO→PNG, optimise
      paths.py            UI file and icon dir resolution (source tree vs installed)
      desktop.py          .desktop shortcut creation and removal
      progress.py         Progress formatting helpers (size, speed, filename truncation)
      terminal.py         Terminal emulator detection and launch helpers
  data/
    icons/
      hicolor/symbolic/apps/   Bundled tab and category icons (CC0-1.0)
    ui/
      window.ui                Main window GtkTemplate
  tests/
    fixtures/             Sample catalogue.json and assets for local development
    test_*.py
```

---

## Desktop environment compatibility

| Feature | GNOME | KDE |
|---|---|---|
| Browse, install, update (HTTP(S) / SSH repo) | Yes | Yes |
| Browse, install, update (local path) | Yes | Yes |
| SMB repos | Yes | Yes |
| Visual integration | Native Adwaita | Renders with Adwaita styling |

---

## License

GPL-3.0-or-later

---

## AI assistance

This application was developed with the help of [Claude](https://claude.ai) by Anthropic. Claude assisted with architecture decisions, implementation, and debugging throughout the project.

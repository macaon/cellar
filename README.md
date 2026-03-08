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
- Desktop shortcut creation for installed apps (per launch target)
- Multi-target launch support — apps can define multiple launch targets (e.g. main game, editor, launcher)
- Launch apps directly from Cellar (standard or in a terminal window)
- Offline mode — cached catalogue allows browsing and launching when the repo is unreachable

**For maintainers** (requires a writable repo)
- Package Builder — two-panel project view for creating and publishing packages:
  - **Windows app project** — initialise a WINEPREFIX with a chosen GE-Proton runner, install winetricks dependencies, run `.exe` installers, configure entry points, test-launch, then publish
  - **Linux app project** — provide an archive, set an entry point, publish
  - **Base image project** — build a shared WINEPREFIX used as the delta base for multiple app packages
  - **Import from catalogue** — pull an existing catalogue entry into a local project for re-packaging
- Steam Store metadata lookup — search by name to auto-fill title, description, developer, genres, cover art, and screenshots
- Edit and delete existing catalogue entries
- Delta archive creation — diff against a base image using BLAKE2b content hashing

---

## Tech stack

- **Language:** Python 3.10+
- **UI toolkit:** GTK4 + libadwaita 1.4+ (GNOME 45+)
- **Windows compatibility:** [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher) + GE-Proton
- **Runner index:** GitHub Releases API (GloriousEggroll/proton-ge-custom), cached in memory
- **Local data:** SQLite via `sqlite3` stdlib
- **Network I/O:** `requests` for HTTP/HTTPS; `paramiko` (pure Python) for SFTP/SSH; `smbprotocol` for SMB
- **Credentials:** `gi.repository.Secret` (libsecret / GNOME Keyring / KWallet portal); falls back to `config.json` (chmod 0600)
- **Image handling:** Pillow (load, resize, crop, ICO/BMP→PNG, optimise)
- **Archive handling:** `tarfile` stdlib; `zstandard` for `.tar.zst` archives
- **File sync:** `rsync` subprocess; Python fallback if rsync is absent
- **Metadata:** Steam Store API (no authentication required)

---

## Installation

### System requirements

- Python 3.10+
- GTK 4.0+ and **libadwaita 1.4+** (ships with GNOME 45 / Ubuntu 24.04 / Fedora 39+)

> **Note for Pop_OS / Ubuntu 22.04 users:** the default libadwaita on these
> distros is 1.2, which is too old.  The app uses `AdwNavigationView`
> (libadwaita 1.4) as its navigation backbone — replacing it with the
> deprecated `AdwLeaflet` would be significant work.  The recommended path
> for older distros is to wait for the Flatpak (which will bundle its own
> libadwaita), or upgrade to Ubuntu 24.04 / Pop_OS 24.04.

**Fedora / RHEL 9+:**
```bash
sudo dnf install python3-gobject libadwaita
```

**Ubuntu 24.04+ / Debian bookworm+:**
```bash
sudo apt install python3-gi gir1.2-adw-1
```

### Install

```bash
git clone https://github.com/macaon/cellar
cd cellar
pip install --user .
```

This installs the `cellar` command to `~/.local/bin/` and registers the app
in the GNOME application launcher. Make sure `~/.local/bin` is on your `PATH`
(add `export PATH="$HOME/.local/bin:$PATH"` to `~/.bashrc` if not).

### Launch

- **Terminal:** `cellar`
- **App drawer:** search for *Cellar* in GNOME Activities

---

## Running in development

### Requirements

- Python 3.10+
- GTK 4 and libadwaita 1.x (`python3-gobject` / `pygobject`)
- `pip install requests Pillow zstandard smbprotocol paramiko keyring pytest`
- [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher) for launching/building Windows apps

### Quick start

```bash
git clone https://github.com/macaon/cellar
cd cellar

# Run against the bundled test fixtures
PYTHONPATH=. CELLAR_REPO=tests/fixtures python3 -m cellar.main
```

`CELLAR_REPO` accepts a local path, a `file://` URI, or any supported remote
URI (`https://`, `sftp://`, `smb://`). The test fixtures under `tests/fixtures/`
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
      logo.png            transparent logo — replaces icon in detail view (use with hide_title)
      screenshots/
        01.png
      <id>-1.0.tar.zst    full archive, OR delta archive (requires a base image)
  bases/
    <base-name>-base.tar.zst   shared base image for delta packages
  runners/
    <runner>.tar.zst           GE-Proton runner archive
```

Archives contain a single top-level `prefix/` directory holding the WINEPREFIX.

### `catalogue.json`

A single JSON file with three top-level sections: `runners`, `bases`, and `apps`.
Runners are referenced by bases, and bases are referenced by apps — a clean
dependency chain.

```json
{
  "cellar_version": 1,
  "generated_at": "2026-03-09T12:00:00Z",
  "category_icons": {
    "Games": "applications-games-symbolic",
    "Productivity": "applications-office-symbolic"
  },
  "categories": ["My Custom Category"],
  "runners": {
    "GE-Proton10-32": {
      "archive": "runners/GE-Proton10-32.tar.zst",
      "archive_size": 524288000,
      "archive_crc32": "aabbccdd"
    }
  },
  "bases": {
    "GE-Proton10-32-allfonts": {
      "runner": "GE-Proton10-32",
      "archive": "bases/GE-Proton10-32-allfonts-base.tar.zst",
      "archive_size": 2684354560,
      "archive_crc32": "eeff0011"
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
      "base_image": "GE-Proton10-32-allfonts",
      "steam_appid": 12345,
      "update_strategy": "safe",
      "launch_targets": [
        {"name": "Main", "path": "C:\\Program Files\\MyApp\\myapp.exe", "args": ""},
        {"name": "Editor", "path": "C:\\Program Files\\MyApp\\editor.exe", "args": ""}
      ],
      "compatibility_notes": "Runs well at high settings.",
      "changelog": "Updated to 1.2.3.",
      "lock_runner": false
    }
  ]
}
```

**Key fields:**

- `base_image` — when set, the archive is a delta against the named base
  image. The installer seeds the prefix from the base before applying the
  delta. Omit for a full archive. The runner is derived via
  `bases[base_image].runner`.
- `steam_appid` — sets `GAMEID=umu-<id>` for umu-launcher, enabling
  [protonfixes](https://github.com/Open-Wine-Components/umu-protonfixes) for
  that title. Omit or set to `null` for `GAMEID=0` (no protonfixes).
- `update_strategy` — `"safe"` (rsync overlay, preserves user data) or
  `"full"` (complete replacement with a warning).
- `launch_targets` — array of `{"name", "path", "args"}` dicts. The first
  target is the primary one used for desktop shortcuts and single-click launch.
  When multiple targets exist, Cellar shows a picker dialog.
- `hide_title` — suppress the app name label in the detail view when a
  transparent `logo` image is provided (Steam-style).
- `lock_runner` — prevent the user from changing the runner for this app.
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
2. When building an app whose base image matches an installed base, the Package
   Builder computes the delta (BLAKE2b-128 content hashing) and produces a
   `.tar.zst` archive containing only changed/added files plus a
   `.cellar_delete` manifest for removed files.
3. On install, Cellar seeds the new prefix from the base using copy-on-write
   (`cp --reflink=auto` on btrfs/XFS) or regular copy, then overlays the delta.
   The result is byte-for-byte identical to a full archive install.

### Supported repo URI schemes

| Scheme | Example | Writable |
|---|---|---|
| Local path | `/mnt/nas/cellar` | Yes |
| `file://` | `file:///mnt/nas/cellar` | Yes |
| `http://` / `https://` | `https://cellar.home.arpa/repo` | No |
| `sftp://` | `sftp://alice@nas.home.arpa/srv/cellar` | Yes |
| `smb://` | `smb://nas.home.arpa/cellar` | Yes |

SSH/SFTP uses pure-Python `paramiko` — no system `ssh` binary required.
SMB uses pure-Python `smbprotocol` (SMBv2/v3) — no GVFS mount required.
Credentials are stored per-repo via libsecret (system keyring) with a
`config.json` fallback.

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
  native/
    <app-id>/           Installed Linux native apps
  projects/
    <slug>/             Package Builder working area
      project.json
      prefix/           WINEPREFIX being built
      <slug>.tar.zst    Generated archive (created on Publish)
  bases/
    <runner>/           Installed base images (for delta seeding)

~/.cache/cellar/
  assets/               Persistent image asset cache (HTTP(S)/SSH/SMB repos)
  catalogues/           Cached catalogue.json per repo (offline fallback)
```

---

## Project structure

```
cellar/
  cellar/
    main.py               GApplication entry point; CSS provider; about dialog
    window.py             Main AdwApplicationWindow; catalogue load/refresh
    views/
      browse.py           Explore / Installed / Updates grid; search; category filter
      detail.py           App detail — icon/logo, screenshots, info cards, install/update/remove
      edit_app.py         Edit / delete catalogue entries
      update_app.py       Safe update dialog — rsync overlay
      install_runner.py   GE-Proton download + extract dialog
      settings.py         Repos, access control, umu path, runners
      steam_picker.py     Steam Store game search and metadata picker
      screenshot_grid.py  Screenshot browser, grid display, and importer
      widgets.py          Shared UI widget helpers (progress page, etc.)
      builder/            Package Builder subpackage
        view.py           Main builder view — project list, detail panel, publish
        metadata.py       Metadata editing — name, category, description, images
        dependencies.py   Winetricks dependency picker dialog
        pickers.py        Runner, base image, and entry point pickers
        catalogue_import.py  Import existing catalogue entries into projects
        progress.py       Build/install progress tracking
    backend/
      repo.py             Catalogue fetch; transport backends (local, HTTP, SSH, SMB)
      packager.py         import_to_repo, update_in_repo, remove_from_repo, delta compression
      installer.py        Download → verify → extract → install pipeline; delta install
      updater.py          Safe rsync overlay update; prefix backup
      base_store.py       Delta base image store — install, remove, path helpers
      umu.py              umu-launcher detection, launch, prefix init, winetricks
      runners.py          GE-Proton release listing (GitHub API) and install management
      project.py          Package Builder project CRUD and packaging
      steam.py            Steam Store API client; metadata normalisation
      database.py         SQLite tracking — installed apps, base images
      manifest.py         File manifest (mtime + size) for safe-update diffing
      config.py           JSON config persistence; libsecret credential storage
    models/
      app_entry.py        AppEntry, RunnerEntry, BaseEntry dataclasses
    utils/
      http.py             requests.Session factory (User-Agent, bearer auth, SSL)
      images.py           Pillow helpers — load, crop, fit, ICO→PNG, optimise
      paths.py            UI file and icon dir resolution (source tree vs installed)
      async_work.py       Threading helpers (run_in_background)
      desktop.py          .desktop shortcut creation and removal
      progress.py         Progress formatting helpers (size, speed, filename truncation)
      smb.py              SmbPath — pathlib.Path-compatible SMB file access via smbprotocol
      ssh.py              SshPath — pathlib.Path-compatible SSH/SFTP access via paramiko
      _remote_path.py     RemotePathMixin — shared base for SmbPath and SshPath
  data/
    icons/
      hicolor/symbolic/apps/   Bundled tab and category icons (CC0-1.0)
    ui/
      window.ui                Main window GtkTemplate
  tests/
    fixtures/             Sample catalogue.json and assets for local development
    test_*.py
  pyproject.toml
  meson.build
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

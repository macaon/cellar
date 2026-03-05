# Cellar

A GNOME desktop application that acts as a private software storefront for
[Bottles](https://usebottles.com/)-managed Windows applications. Think GNOME
Software, but the "packages" are pre-configured Bottles backups stored on a
network share or web server.

The primary use case is a home-lab or family server: a maintainer packages and
publishes apps from their machine; everyone else browses the catalogue and
installs with one click. Cellar handles downloading, verifying, and importing
the backup into Bottles — including auto-downloading any missing Wine runner
the app requires.

---

## Features

**For users**
- GNOME Software-style browse grid — Explore, Installed, and Updates tabs
- Category filter strip, full-text search
- App detail view — hero banner, cover art, screenshots, metadata, changelog
- One-click install, update (safe rsync overlay or full replacement), and remove
- Delta package support — shared base images dramatically reduce download size
- Runner compatibility check — prompts to download the required Wine runner if absent
- Linux native app support alongside Windows/Wine apps
- Desktop shortcut creation for installed apps
- Launch apps directly from Cellar or via generated `.desktop` files

**For maintainers** (requires a writable repo)
- Add and edit catalogue entries from within Cellar
- IGDB metadata lookup — auto-fills title, description, developer, cover art
- Delta archive creation — diff against a base image using BLAKE2b content hashing
- Base image management — publish and download shared base images per runner
- Multiple simultaneous repos — local, SSH, SMB, NFS, or HTTP(S)

---

## Tech stack

- **Language:** Python 3.11+
- **UI toolkit:** GTK4 + libadwaita (GNOME 46+)
- **Local data:** SQLite via `sqlite3` stdlib
- **Network I/O:** `requests` for HTTP/HTTPS; system `ssh` for SSH; GIO/GVFS for SMB and NFS
- **Image handling:** Pillow (load, resize, crop, ICO→PNG, optimise)
- **Archive handling:** `tarfile` stdlib; `zstandard` for `.tar.zst` delta archives
- **File sync:** `rsync` subprocess; Python fallback if rsync is absent
- **Runner index:** `dulwich` (pure-Python git) syncing `bottlesdevs/components`
- **Bottles integration:** `bottles-cli` subprocess + `PyYAML` for `bottle.yml`
- **IGDB:** Twitch/IGDB API via `requests` with cached bearer token

---

## Running in development

### Requirements

- Python 3.11+
- GTK 4 and libadwaita 1.x (`python3-gobject` / `pygobject`)
- `pip install requests Pillow PyYAML dulwich zstandard pytest`
- Bottles installed (Flatpak or native)

### Quick start

```bash
git clone https://github.com/macaon/cellar
cd cellar

# Run against the bundled test fixtures (no Bottles required for browsing)
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

A Cellar repo is a directory (local or remote) containing a `catalogue.json`
master index and app assets. If you point Cellar at a writable location with
no `catalogue.json` it will offer to initialise a new repository.

```
repo/
  catalogue.json
  apps/
    <id>/
      icon.png            square icon (browse grid) — PNG, JPG, ICO, or SVG
      cover.png           portrait cover (2:3) — browse grid + detail view
      hero.png            wide banner — detail view header
      logo.png            transparent logo — overlays name in detail view
      screenshots/
        01.png
      <id>-1.0.tar.gz     full Bottles backup archive, OR
      <id>-1.0.tar.zst    delta archive (requires a base image, see below)
  bases/
    <runner>-base.tar.gz  shared base image for delta packages
```

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
    "ge-proton10-32": {
      "archive": "bases/ge-proton10-32-base.tar.gz",
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
      "website": "https://example.com",
      "store_links": { "steam": "https://store.steampowered.com/app/12345" },

      "icon": "apps/my-app/icon.png",
      "cover": "apps/my-app/cover.png",
      "hero": "apps/my-app/hero.png",
      "logo": "apps/my-app/logo.png",
      "hide_title": true,
      "screenshots": ["apps/my-app/screenshots/01.png"],

      "archive": "apps/my-app/my-app-1.2.3.tar.zst",
      "archive_size": 104857600,
      "archive_crc32": "11223344",
      "install_size_estimate": 524288000,
      "built_with": {
        "runner": "ge-proton10-32",
        "dxvk": "2.3",
        "vkd3d": "2.11"
      },
      "base_runner": "ge-proton10-32",
      "update_strategy": "safe",
      "entry_point": "Program Files/MyApp/myapp.exe",
      "compatibility_notes": "Runs well at high settings.",
      "changelog": "Updated to 1.2.3.",
      "lock_runner": false
    }
  ]
}
```

**Key fields:**

- `base_runner` — when set, the archive is a delta against the named base
  image. The installer seeds the bottle from the base before applying the
  delta. Omit for a full archive.
- `update_strategy` — `"safe"` (rsync overlay, preserves user data) or
  `"full"` (complete replacement with a warning).
- `hide_title` — suppress the app name label in the detail view when a
  transparent `logo` image is provided (Steam-style).
- `lock_runner` — prevent the user from changing the runner for this app.
- `entry_point` — path to the main executable relative to `drive_c/` for
  Windows apps, or relative to the install directory for Linux native apps.
- `category_icons` — optional top-level map of category name → symbolic icon
  name. Uses standard Adwaita icon names.
- `categories` — optional list of custom categories beyond the built-in ones
  (Games, Productivity, Graphics, Utility).

All fields except `id`, `name`, `version`, and `category` are optional.

### Delta packages

Delta archives contain only files that differ from a shared base image,
dramatically reducing download size. A base is a clean, fully-configured
bottle (runner + common dependencies like fonts and runtime libraries) without
any specific app installed.

**Workflow:**
1. Maintainer creates a base bottle, uploads it via Settings → Delta Base
   Images → Add.
2. When adding an app whose runner matches an installed base, Cellar
   automatically computes the delta (BLAKE2b-128 content hashing) and
   produces a `.tar.zst` archive containing only changed files plus a
   `.cellar_delete` manifest for removed files.
3. On install, Cellar seeds the new bottle from the base (using copy-on-write
   or hardlinks where available), then overlays the delta. The result is
   byte-for-byte identical to a full archive install.

### Supported repo URI schemes

| Scheme | Example | Writable |
|---|---|---|
| Local path | `/mnt/nas/cellar` | Yes |
| `file://` | `file:///mnt/nas/cellar` | Yes |
| `http://` / `https://` | `https://cellar.home.arpa/repo` | No |
| `ssh://` | `ssh://alice@nas.home.arpa/srv/cellar` | Yes |
| `smb://` | `smb://nas.home.arpa/cellar` | Yes |
| `nfs://` | `nfs://nas.home.arpa/export/cellar` | Yes |

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

## Project structure

```
cellar/
  cellar/
    main.py               GApplication entry point; CSS provider; about dialog
    window.py             Main AdwApplicationWindow; catalogue load/refresh
    views/
      browse.py           Explore / Installed / Updates grid; search; category filter
      detail.py           App detail page — hero, screenshots, install/update/remove
      add_app.py          Add app to catalogue; IGDB lookup; delta creation
      edit_app.py         Edit / delete catalogue entries
      update_app.py       Safe update dialog — backup + rsync overlay
      install_runner.py   Runner download + extract dialog
      igdb_picker.py      IGDB game search and metadata picker dialog
      settings.py         Repos, access control, delta base images, IGDB credentials
    backend/
      repo.py             Catalogue fetch; all transport backends (local, HTTP, SSH, GIO)
      packager.py         import_to_repo, update_in_repo, remove_from_repo, create_delta_archive
      installer.py        Download → verify → extract → import pipeline; delta install
      updater.py          Safe rsync overlay update; bottle backup
      base_store.py       Delta base image store — install, remove, path helpers
      bottles.py          Bottles detection; bottles-cli wrapper; launch helpers
      components.py       bottlesdevs/components runner index sync via dulwich
      igdb.py             IGDB API client; bearer token management; metadata normalisation
      database.py         SQLite tracking — installed apps, base images
      config.py           JSON config persistence (~/.local/share/cellar/config.json)
    models/
      app_entry.py        AppEntry, BuiltWith, BaseEntry dataclasses
    utils/
      gio_io.py           GIO file and network helpers
      http.py             requests.Session factory (User-Agent, bearer auth, SSL)
      images.py           Pillow helpers — load, crop, fit, ICO→PNG, optimise
      paths.py            UI file and icon dir resolution (source tree vs installed)
      desktop.py          .desktop shortcut creation and removal
      progress.py         Progress formatting helpers (size, speed, filename truncation)
  data/
    icons/
      hicolor/symbolic/apps/   Bundled tab and category icons (CC0-1.0)
    ui/
      window.ui                Main window GtkTemplate
  tests/
    fixtures/             Sample catalogue.json and assets for local development
    test_repo.py
    test_bottles.py
    test_components.py
    test_database.py
    test_images.py
    test_installer.py
    test_packager.py
    test_updater.py
    test_igdb.py
    test_desktop.py
```

---

## Desktop environment compatibility

| Feature | GNOME | KDE |
|---|---|---|
| Browse, install, update (HTTP(S) / SSH repo) | ✅ | ✅ |
| Browse, install, update (local path) | ✅ | ✅ |
| SMB / NFS repos | ✅ | ❌ Requires GVFS |
| SMB / NFS credential dialogs | ✅ | ❌ Uses GNOME Keyring; KWallet not supported |
| Visual integration | ✅ Native Adwaita | ⚠️ Renders with Adwaita styling |

KDE support (GVFS/smbclient fallback, KWallet integration) is planned for a future release.

---

## License

GPL-3.0-or-later

---

## AI assistance

This application was developed with the help of [Claude](https://claude.ai) by Anthropic. Claude assisted with architecture decisions, implementation, and debugging throughout the project.

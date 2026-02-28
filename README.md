# Cellar

A GNOME desktop application that acts as a software storefront for
[Bottles](https://usebottles.com/)-managed Windows applications. Think GNOME
Software, but the "packages" are Bottles full backups stored on a network share
or web server. Browse a catalogue, click Install, and Cellar handles downloading
the backup and importing it into Bottles.

The primary use case is a private family or home-lab server: the person who
manages the repo adds and packages apps; everyone else browses and installs via
a read-only HTTP URL (or directly over SMB/NFS/SSH if they have access).

> **Status: early development** — browse (GNOME Software-style horizontal cards
> with Explore/Installed/Updates view switcher), detail view, install, remove,
> and safe update are working; component-upgrade prompts and Flatpak packaging
> are not yet implemented.

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
git clone https://github.com/macaon/cellar
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
      icon.png            ← square icon (browse grid); PNG, JPG, ICO, or SVG
      cover.png           ← portrait cover (2:3 ratio, browse grid + detail)
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
  "categories": ["Games", "Productivity", "Graphics", "Utility", "My Category"],
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

The top-level `categories` array is optional. It defines custom categories
beyond the built-in ones (Games, Productivity, Graphics, Utility). Any
categories listed here appear in the category filter strip and in the Add/Edit
app dialogs.

All app fields except `id`, `name`, `version`, and `category` are optional.

### Supported repo URI schemes

| Scheme | Example | Writable |
|---|---|---|
| Local path | `/mnt/nas/cellar` | Yes |
| `file://` | `file:///mnt/nas/cellar` | Yes |
| `http://` / `https://` | `https://cellar.home.arpa/repo` | No |
| `ssh://` | `ssh://alice@nas.home.arpa/srv/cellar` | Yes |
| `smb://` | `smb://nas.home.arpa/cellar` | Yes |
| `nfs://` | `nfs://nas.home.arpa/export/cellar` | Yes |

HTTP(S) repos are always read-only. If you point Cellar at a writable location
with no `catalogue.json`, it will offer to initialise a new repository.

### Restricting HTTP(S) access with a bearer token

Cellar supports per-repo bearer token authentication for HTTP(S) repos. This
lets you share a repo URL with specific people without making it publicly
accessible.

**Setting up the token**

1. Open Cellar → Preferences → **Access Control** → **Generate**. A
   64-character random token is shown and copied to your clipboard. Paste it
   into your web server config (see nginx/Caddy examples below).
2. When adding the repo in **Repositories**, enter the URL in the URI field and
   paste the token into the **Access token (optional)** field directly below it.
3. Share the URL and token with friends — they add the repo the same way.

To update an existing repo's token: open Preferences, expand the repo row,
click **Change…**.

Tokens are stored in `~/.local/share/cellar/config.json`.

**nginx example**

```nginx
# In your http {} block (outside server {}):
map $http_authorization $cellar_auth_ok {
    "Bearer YOUR_TOKEN_HERE"  1;
    default                   0;
}

server {
    listen 443 ssl;
    server_name cellar.example.com;

    ssl_certificate     /etc/ssl/certs/cellar.crt;
    ssl_certificate_key /etc/ssl/private/cellar.key;

    # The ^~ modifier is critical: it stops nginx from checking regex location
    # blocks when this prefix matches. Without it, a catch-all image-caching
    # block (e.g. "location ~* \.(jpg|png|...)$", common in PHP/WordPress
    # setups) intercepts image asset requests before this block runs, serving
    # them from the wrong root and causing 404 errors for all cover art and icons.
    #
    # 'root' is prepended to the full request URI:
    #   root / + /cellar/catalogue.json  →  /cellar/catalogue.json on disk
    # Adjust if your repo lives elsewhere:
    #   repo at /srv/data/cellar/ with location /cellar/  →  root /srv/data
    location ^~ /cellar/ {
        if ($cellar_auth_ok = 0) {
            return 401 "Unauthorized\n";
        }

        root /;
        autoindex off;
        add_header Accept-Ranges bytes;
        expires 5d;
    }
}
```

Replace `YOUR_TOKEN_HERE` with your generated token. Reload nginx after editing:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

**Caddy example**

```caddy
cellar.example.com {
    handle /cellar/* {
        @unauth not header Authorization "Bearer YOUR_TOKEN_HERE"
        respond @unauth "Unauthorized" 401

        file_server {
            root /
        }
    }
}
```

---

## Project structure

```
cellar/
  cellar/
    main.py              GApplication entry point
    window.py            Main AdwApplicationWindow
    views/
      browse.py          Grid browse view — used for all three tabs (Explore/Installed/Updates)
      detail.py          App detail page (Install / Update / Remove)
      add_app.py         Add-app-to-catalogue dialog
      edit_app.py        Edit / delete catalogue entry dialog
      update_app.py      Safe update dialog (backup + rsync overlay)
      settings.py        Settings / repo management dialog
    backend/
      repo.py            Catalogue fetching, all transport backends
      packager.py        import_to_repo / update_in_repo / remove_from_repo
      installer.py       Download, verify, extract, import to Bottles
      updater.py         Safe rsync overlay update + backup
      bottles.py         Bottles path detection
      database.py        SQLite installed/repo tracking
      config.py          JSON config persistence (repos)
    models/
      app_entry.py       AppEntry + BuiltWith dataclasses
    utils/
      paths.py           UI + icons path resolution (source tree + installed)
      gio_io.py          GIO file helpers
      checksum.py        SHA-256 utility
  data/
    icons/
      hicolor/symbolic/apps/   Bundled tab icons (CC0-1.0)
    ui/
      window.ui          Main window template
  tests/
    fixtures/            Sample catalogue.json for local testing
    test_repo.py
    test_bottles.py
    test_database.py
    test_installer.py
```

---

## Development roadmap

1. **Repo backend** — catalogue parsing, all transport backends ✅
2. **Browse UI** — GNOME Software-style horizontal cards, category filter, search, Explore/Installed/Updates view switcher ✅
3. **Detail view** — full app page from catalogue data ✅
4. **Bottles backend** — path detection, install + remove ✅
5. **Local DB** — track installed apps, wire up Install/Remove button state ✅
6. **Update logic** — safe rsync overlay (no --delete; AppData/Documents excluded) ✅
7. **HTTP(S) auth** — bearer token generation, storage, and per-request injection; image asset caching ✅
8. **Flatpak packaging**
9. **KDE support**

---

## Desktop environment compatibility

| Feature | GNOME | KDE |
|---|---|---|
| Browse, install, update (HTTP(S) repo) | ✅ | ✅ |
| Browse, install, update (local / SSH repo) | ✅ | ✅ |
| SMB / NFS repos | ✅ | ❌ Requires GVFS, which is not present on KDE |
| SMB / NFS credential dialogs | ✅ | ❌ Uses GNOME Keyring; KWallet not supported |
| Visual integration | ✅ Native | ⚠️ Renders with GNOME/Adwaita styling |

KDE support (GVFS fallback, KWallet integration, and adaptive styling) is planned for a future release.

---

## License

GPL-3.0-or-later

---

## AI assistance

This application was developed with the help of [Claude](https://claude.ai) by Anthropic. Claude assisted with architecture decisions, implementation, and debugging throughout the project.


# Cellar

A GNOME desktop application that acts as a software storefront for
[Bottles](https://usebottles.com/)-managed Windows applications. Think GNOME
Software, but the "packages" are Bottles full backups stored on a network share
or web server. Browse a catalogue, click Install, and Cellar handles downloading
the backup, importing it into Bottles, and updating Wine components as needed.

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

### Restricting HTTPS access with a bearer token

Cellar supports per-repo bearer token authentication for HTTPS repos. This lets
you share a repo URL with specific people without making it publicly accessible.

**Setting up the token**

1. In Cellar → Preferences, add the repo URL. If the server returns 401 you
   will be prompted for a token automatically.
2. Click **Generate** to create a 64-character random token, which is copied to
   your clipboard. Paste it into your web server config (see below), then click
   **Save**.
3. Share the URL and token with friends. They paste the token into the same
   prompt when adding the repo.

The token is stored in `~/.local/share/cellar/config.json`. You can change it
at any time via Settings → expand the repo row → **Change…**.

**nginx example**

```nginx
# /etc/nginx/sites-available/cellar
map $http_authorization $cellar_auth_ok {
    "Bearer YOUR_TOKEN_HERE"  1;
    default                   0;
}

server {
    listen 443 ssl;
    server_name cellar.example.com;

    ssl_certificate     /etc/ssl/certs/cellar.crt;
    ssl_certificate_key /etc/ssl/private/cellar.key;

    location /repo/ {
        if ($cellar_auth_ok = 0) {
            return 401 "Unauthorized\n";
        }

        alias /srv/cellar/repo/;
        autoindex off;

        # Optional: allow the client to know the content length for
        # progress bars during archive downloads.
        add_header Accept-Ranges bytes;
    }
}
```

Replace `YOUR_TOKEN_HERE` with your generated token and adjust the `alias`
path to point at your repo directory. Reload nginx after editing:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

**Caddy example**

```caddy
cellar.example.com {
    handle /repo/* {
        @unauth not header Authorization "Bearer YOUR_TOKEN_HERE"
        respond @unauth "Unauthorized" 401

        file_server {
            root /srv/cellar
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
      browse.py          Grid browse view (app cards, category filter, search) ✅
      detail.py          App detail page (Install / Update / Remove) ✅
      add_app.py         Add-app-to-catalogue dialog ✅
      edit_app.py        Edit / delete catalogue entry dialog ✅
      update_app.py      Safe update dialog (backup + rsync overlay) ✅
      settings.py        Settings / repo management dialog ✅
      installed.py       Installed apps view (stub)
      updates.py         Available updates view (stub)
    backend/
      repo.py            Catalogue fetching, all transport backends ✅
      packager.py        import_to_repo / update_in_repo / remove_from_repo ✅
      installer.py       Download, verify, extract, import to Bottles ✅
      updater.py         Safe rsync overlay update + backup ✅
      bottles.py         bottles-cli wrapper, path detection ✅
      database.py        SQLite installed/repo tracking ✅
      config.py          JSON config persistence (repos) ✅
    models/
      app_entry.py       Unified app/game dataclass (AppEntry + BuiltWith) ✅
    utils/
      paths.py           UI + icons path resolution (source tree + installed) ✅
      gio_io.py          GIO file helpers ✅
      checksum.py        SHA-256 utility ✅
  data/
    icons/
      hicolor/symbolic/apps/   Bundled tab icons (CC0-1.0)
    ui/
      window.ui          Main window template
  tests/
    fixtures/            Sample catalogue.json for local testing
    test_repo.py         Backend unit tests
```

---

## Development roadmap

1. **Repo backend** — catalogue parsing, all transport backends ✅
2. **Browse UI** — GNOME Software-style horizontal cards, category filter, search, Explore/Installed/Updates view switcher ✅
3. **Detail view** — full app page from catalogue data ✅
4. **Bottles backend** — path detection, `bottles-cli` wrapper, install + remove ✅
5. **Local DB** — track installed apps, wire up Install/Remove button state ✅
6. **Update logic** — safe rsync overlay (no --delete; AppData/Documents excluded) ✅
7. **Component update UI** — post-install prompt to upgrade runner/DXVK
8. **Repo management UI** — add/remove sources, initialise new repos
9. **Flatpak packaging**

---

## License

GPL-3.0-or-later

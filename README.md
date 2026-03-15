# Cellar

A GNOME desktop application that acts as a private software storefront for
Windows and Linux applications. Think GNOME Software, but the "packages" are
pre-configured app archives stored on a network share or web server.

The primary use case is a home-lab or family server: a maintainer packages and
publishes apps from their machine using the built-in Package Builder; everyone
else browses the catalogue and installs with one click.

Windows apps run via [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher)
with [GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom).
Linux native apps are extracted and launched directly.
DOS games run via [DOSBox Staging](https://dosbox-staging.github.io/)
(auto-downloaded on first use).

---

## Features

**For users**
- GNOME Software-style browse grid — Explore, Installed, and Updates tabs
- Category filter and full-text search
- App detail view — icon/logo, screenshot carousel, description, info cards
- One-click install, update (safe overlay or full replacement), and remove
- Delta packages — shared base images keep download sizes small
- Chunked downloads — archives split into ~1 GB pieces with per-chunk CRC32 verification
- Copy-on-write disk optimisation on btrfs/XFS (reflinks for base image seeding)
- Runner management — GE-Proton versions listed from GitHub Releases
- Linux native app support alongside Windows apps
- **DOS game support (early testing)** — GOG DOSBox games auto-detected and converted to native DOSBox Staging packages; DOSBox Settings dialog for CPU speed, video, sound, MIDI (FluidSynth/MT-32), and mixer effects
- Multi-target launch — apps can define multiple entry points (e.g. main game, editor, launcher)
- Desktop shortcut creation for installed apps
- Offline mode — cached catalogue allows browsing and launching when the repo is unreachable

**For maintainers** (requires a writable repo)
- Package Builder — create and publish packages for Windows apps, Linux apps, and base images
  - Initialise a WINEPREFIX, install winetricks dependencies, run `.exe` installers, configure launch targets, test-launch, and publish
  - Existing catalogue entries from writable repos can be downloaded for re-packaging or deleted
- Steam Store metadata lookup — auto-fill title, description, developer, genres, cover art, and screenshots
- Edit and delete existing catalogue entries directly from the detail view
- Delta archive creation — automatic diff against a base image using BLAKE2b content hashing

---

## Key concepts: Runner → Base → App

Cellar organises Windows app packages into a three-tier dependency chain.
Each tier references exactly one thing directly above it:

```
Runner  (GE-Proton10-32)                     ← GE-Proton binary
  ↑ .runner
Base    (GE-Proton10-32-allfonts)            ← clean WINEPREFIX + shared deps
  ↑ .base_image
App     (my-game)                            ← delta archive (changes only)
```

**Runner** — a [GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom)
build. Cellar lists available versions from the GitHub Releases API (cached
one hour in memory). Runners are stored locally at `runners/<name>/` and
required at runtime as umu-launcher's `PROTONPATH`.

**Base image** — a clean, fully-initialised WINEPREFIX created with a specific
runner plus shared dependencies (fonts, runtime libraries installed via
winetricks). The runner is baked into the base at creation time: `init_prefix`
sets `PROTONPATH=runners/<runner>` and umu writes runner artifacts into the
prefix. That prefix is then archived and published. A base references exactly
one runner via the `runner` field.

**App** — the actual application package. Every Windows app references a base
image via `base_image`. The archive is a *delta* — it contains only files that
differ from that base (see [Delta packages](#delta-packages) below). The
runner for the app is always derived by looking up `bases[base_image].runner`
— no redundant runner reference on the app entry.

Linux native apps skip this hierarchy entirely — they have no runner, no base,
and no WINEPREFIX. The archive is extracted directly and the entry point is
launched as a regular process.

DOS games (early testing) also skip the Runner → Base chain. They bundle a
[DOSBox Staging](https://dosbox-staging.github.io/) binary and configuration
alongside the game files. GOG Windows games that use DOSBox are auto-detected
during import and converted to native DOS packages. DOSBox Staging is
downloaded once from GitHub Releases and managed as a transparent runtime.

---

## How it works

### Chunked archives

All archives — app packages, runner binaries, and base images — are split into
independent ~1 GB `.tar.zst` chunks during compression. Each chunk is a
self-contained tar.zst file that can be downloaded, extracted, and deleted
independently.

**Compression** — the `_ChunkWriter` in `packager.py` monitors output size.
When a chunk reaches the 1 GiB threshold it closes the current zstd
compressor and tarfile, rotates to a new output file, and reopens both.
Each chunk gets its own CRC32 checksum; a cumulative CRC32 is tracked across
all chunks.

**Chunk naming** — files follow the pattern `archive.tar.zst.001`, `.002`,
etc. (1-based). The `archive_chunks` field on each entry stores per-chunk
metadata: `[{"size": 1073741824, "crc32": "aabb0011"}, ...]`.

**Download** — the installer iterates through chunks sequentially. For each:
download to a temp file → verify CRC32 → extract in a single streaming pass →
delete the temp file → proceed to next chunk. Peak temporary disk usage is one
chunk (~1 GB) rather than the full compressed archive size.

### Delta packages

Delta archives contain only files that differ from a shared base image,
dramatically reducing download size. A typical base image is 2–3 GB; app
deltas are 50–500 MB each.

**At publish time** (`compress_prefix_delta_zst`):
1. Walk every file in the app prefix and compute a BLAKE2b-128 hash.
2. For each file that also exists in the base directory, hash the base copy.
   If hashes match, exclude the file from the delta.
3. Files present in the base but absent from the app prefix are recorded in a
   `.cellar_delete` manifest (a text file listing relative paths).
4. The remaining delta files and the delete manifest are packed into chunked
   `.tar.zst` archives.
5. Symlinks under `drive_c/users/` are stripped — umu recreates them on first
   launch.

**At install time** (`installer.py`):
1. Ensure the runner is installed (download if not).
2. Ensure the base image is installed (download + extract if not).
3. Download and extract the app delta to a temporary directory (on the same
   filesystem as `prefixes/` for efficient moves).
4. Seed the destination prefix from the base via copy-on-write:
   `cp -a --reflink=auto <base>/. <dest>/`. On btrfs and XFS this creates
   reflinks — file blocks are shared with the base until modified, using zero
   extra disk. On other filesystems `--reflink=auto` silently falls back to a
   regular copy. A pure-Python fallback handles environments without `cp`.
5. Overlay the delta onto the seeded prefix using rsync (Python fallback if
   unavailable). Each destination file is unlinked before copying to avoid
   modifying shared base inodes.
6. Apply the `.cellar_delete` manifest — remove each listed path.
7. Write a file manifest (`.cellar-manifest.json`) for future safe updates.

### Safe updates

When an app uses `update_strategy: "safe"`, Cellar updates the prefix
in-place while preserving user data (saves, configs, settings).

**Manifest tracking** — after every fresh install or update, Cellar writes
`.cellar-manifest.json` at the prefix root. This records the mtime and file
size of every file:

```json
{"version": 1, "files": {"drive_c/game/data.pak": [1048576, 1709251200], ...}}
```

**Change detection** (`scan_user_files`) — before applying an update, Cellar
re-stats every file:
- Files whose size or mtime changed since the manifest was written are flagged
  as *modified package files* (the user or the app changed them at runtime).
- Files on disk that are not in the manifest and not under Wine system paths
  are flagged as *user-created files* (saves, screenshots, custom configs).

**Update flow:**
1. Optionally back up the existing prefix to a `.tar.gz` archive.
2. Download and verify the new app archive (chunked, CRC32-checked).
3. Extract to a temporary directory.
4. Overlay via rsync (or Python fallback), skipping protected paths.
5. For delta updates: apply the `.cellar_delete` manifest.
6. Restore stashed user-modified and user-created files on top.
7. Rewrite the manifest to establish a new baseline.

**Protected paths** — the overlay never touches:
- `drive_c/users/*/AppData/Roaming/`, `Local/`, `LocalLow/`
- `drive_c/users/*/Documents/`
- `user.reg`, `userdef.reg`
- Wine system directories (`drive_c/windows/`, `drive_c/Program Files*/`, etc.)

A full update (`update_strategy: "full"`) warns the user, removes the entire
prefix, and reinstalls from scratch.

### Install pipeline summary

**Windows app:**
ensure runner → ensure base → download app delta (chunked, streaming CRC32) →
extract to temp → seed prefix from base (CoW) → overlay delta → apply delete
manifest → write file manifest → record in SQLite database

**Linux app:**
download archive (chunked) → extract to `native/<id>/` → `chmod +x` entry
point → write manifest → record in database

**DOS game:**
download archive (chunked) → extract to `dos/<id>/` (includes bundled DOSBox
Staging binary, configs, and game files) → record in database

---

## Guides

### Setting up a repository

A Cellar repository is just a directory — local or remote — that holds a
`catalogue.json` index and app assets.

1. **Create the directory.** Any writable location works: a local path, an
   SFTP server, or an SMB share.
2. **Add it in Cellar.** Open Preferences → Repositories → Add. Enter the
   URI (e.g. `/mnt/nas/cellar`, `sftp://alice@nas/srv/cellar`,
   `smb://nas/cellar`).
3. **Cellar initialises it automatically.** If no `catalogue.json` exists,
   Cellar offers to create one with an empty skeleton.

For an HTTP-served repo, set up a web server (nginx, Caddy, etc.) pointing at
the directory. HTTP repos are read-only — you publish to the underlying
filesystem via a writable transport and serve it over HTTP for users.

### Building and publishing a base image

A base image is a shared WINEPREFIX that all Windows app packages build on
top of. Creating one is the first step before publishing any app packages.

1. Open the **Package Builder** tab → click **New Project** → select **Base**.
2. Enter a name (e.g. "GE-Proton10-32-allfonts"). Pick a GE-Proton runner
   from the dropdown — Cellar will download it from GitHub if not already
   installed.
3. Click **Initialise** to create an empty WINEPREFIX with that runner.
4. Use the **Dependencies** section to install shared libraries via
   winetricks (fonts, Visual C++ runtimes, .NET, DirectX, etc.). The picker
   groups verbs by category with descriptions.
5. When ready, click **Publish** and choose the target repo. Cellar compresses
   the prefix into chunked `.tar.zst` archives and writes the base entry to
   the catalogue.

### Building and publishing an app

1. Open the **Package Builder** → **New Project** → select **App** (Windows
   or Linux).
2. **Select your base image** from the dropdown. The prefix is seeded from
   that base.
3. **Install the application.** Click "Install Software" to run a `.exe`
   installer inside the prefix, or use "Browse Prefix" to manually place
   files (useful for games with no installer).
4. **Configure launch targets.** Add one or more entry points — the exe path
   within the prefix, a display name, and optional arguments. The first
   target becomes the primary one used for desktop shortcuts.
5. **Test-launch** from the builder to verify everything works.
6. **Fill in metadata.** The metadata dialog lets you set the title, category,
   description, and media (icon, cover, screenshots). Use the Steam lookup
   button to auto-fill from the Steam Store.
7. Click **Publish** and choose the target repo. Cellar automatically computes
   the delta against the base image (BLAKE2b diff) — only changed files are
   included in the archive.

### Bearer token authentication for HTTP(S) repos

Cellar supports per-repo bearer token auth for HTTP(S) repos. This lets you
share a private repo URL without making it publicly accessible.

1. Open Preferences → **Access Control** → **Generate**. Copy the token.
2. Paste it into your web server config (see examples below).
3. When adding the repo in Cellar, enter the URL and paste the token into the
   **Access token** field.

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

## Repository format

A Cellar repo is a directory (local or remote) containing a `catalogue.json`
master index and per-app assets:

```
repo/
  catalogue.json
  apps/
    <id>/
      metadata.json                full app metadata (fetched on demand)
      icon.png                     square icon — PNG, JPG, ICO, or SVG
      cover.png                    portrait cover (2:3) — shown in browse cards
      logo.png                     transparent logo — replaces icon in detail view
      screenshots/
        01.png
      <id>-1.0.tar.zst.001         chunked archive (1 GB per chunk)
      <id>-1.0.tar.zst.002
  bases/
    <base-name>-base.tar.zst.001   shared base image (chunked)
    <base-name>-base.tar.zst.002
  runners/
    <runner>.tar.zst.001           GE-Proton runner archive (chunked)
```

Archives contain a single top-level `prefix/` directory holding the WINEPREFIX
(or app contents for Linux apps).

### Two-tier catalogue

The catalogue is split into a **slim index** and **per-app metadata**:

- `catalogue.json` — contains only the fields needed for the browse grid and
  update detection (id, name, category, summary, icon, cover, platform,
  archive_crc32, base_image). Also holds runners, bases, categories, and
  category_icons.
- `apps/<id>/metadata.json` — the full `AppEntry` with all fields. Fetched on
  demand when the user opens the detail view. Cached locally for offline use.

This keeps the initial catalogue fetch small even for repos with many apps.

### `catalogue.json`

```json
{
  "cellar_version": 2,
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
      "archive_crc32": "aabbccdd",
      "archive_chunks": [
        {"size": 524288000, "crc32": "aabbccdd"}
      ]
    }
  },
  "bases": {
    "GE-Proton10-32-allfonts": {
      "runner": "GE-Proton10-32",
      "archive": "bases/GE-Proton10-32-allfonts-base.tar.zst",
      "archive_size": 2684354560,
      "archive_crc32": "eeff0011",
      "archive_chunks": [
        {"size": 1073741824, "crc32": "eeff0011"},
        {"size": 1073741824, "crc32": "22334455"},
        {"size": 536870912,  "crc32": "66778899"}
      ]
    }
  },
  "apps": [
    {
      "id": "my-app",
      "name": "My App",
      "category": "Games",
      "summary": "One-line description shown in the browse grid",
      "icon": "apps/my-app/icon.png",
      "cover": "apps/my-app/cover.png",
      "platform": "windows",
      "archive_crc32": "11223344",
      "base_image": "GE-Proton10-32-allfonts"
    }
  ]
}
```

**Key fields (full metadata in `apps/<id>/metadata.json`):**

| Field | Description |
|---|---|
| `base_image` | The base image this delta is built against. The installer seeds the prefix from this base before overlaying. Runner derived via `bases[base_image].runner`. Required for all Windows apps. |
| `archive_chunks` | Per-chunk size and CRC32. Empty or absent for legacy single-file archives. |
| `steam_appid` | Sets `GAMEID=umu-<id>` for umu-launcher, enabling [protonfixes](https://github.com/Open-Wine-Components/umu-protonfixes). Omit or `null` for `GAMEID=0`. |
| `update_strategy` | `"safe"` (rsync overlay, preserves user data) or `"full"` (complete replacement with warning). |
| `launch_targets` | Array of `{"name", "path", "args"}`. First target is primary (desktop shortcut, single-click launch). Multiple targets show a picker dialog. |
| `platform` | `"windows"` (default, omitted from JSON), `"linux"` for native apps, or `"dos"` for DOS games (early testing). |
| `hide_title` | Suppress app name in detail view when a transparent `logo` image is provided. |
| `lock_runner` | Prevent the user from changing the runner for this app. |
| `category_icons` | Top-level map of category name → Adwaita symbolic icon name. |
| `categories` | Custom categories beyond the built-in set (Games, Productivity, Graphics, Utility). |

All fields except `id`, `name`, `version`, and `category` are optional.

### Supported repo transports

| Scheme | Example | Writable |
|---|---|---|
| Local path | `/mnt/nas/cellar` | Yes |
| `file://` | `file:///mnt/nas/cellar` | Yes |
| `http://` / `https://` | `https://cellar.home.arpa/repo` | No |
| `sftp://` | `sftp://alice@nas.home.arpa/srv/cellar` | Yes |
| `smb://` | `smb://nas.home.arpa/cellar` | Yes |

- **HTTP(S)** — read-only. Optional bearer token auth sent as
  `Authorization: Bearer <token>`. Uses
  `User-Agent: Mozilla/5.0 (compatible; Cellar/1.0)` to avoid Cloudflare
  blocks on the default Python UA.
- **SFTP** — pure-Python via `paramiko`. Key auth via ssh-agent,
  `~/.ssh/config`, or an explicit `ssh_identity` path. No system `ssh`
  binary required.
- **SMB** — pure-Python via `smbprotocol` (SMBv2/v3). No GVFS mount, no file
  manager sidebar entry.
- **Asset caching** — image assets from remote repos are cached persistently
  to `~/.cache/cellar/assets/<sha256-prefix>/`.
- **Catalogue caching** — on each successful fetch the raw JSON is saved to
  `~/.cache/cellar/catalogues/<sha256-prefix>/catalogue.json`. On network
  failure, the cached copy is loaded as a fallback so the app stays usable
  offline.

Credentials are stored per-repo via libsecret (system keyring) with a
`config.json` plaintext fallback when no secret daemon is available.

---

## Tech stack

- **Language:** Python 3.10+
- **UI toolkit:** GTK4 + libadwaita 1.4+ (GNOME 45+)
- **Windows compatibility:** [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher) + GE-Proton
- **DOS compatibility:** [DOSBox Staging](https://dosbox-staging.github.io/) (auto-downloaded runtime)
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
> for older distros is to wait for the Flatpak (which bundles its own
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

### Flatpak

A Flatpak manifest is available at `flatpak/io.github.cellar.json`.

```bash
flatpak-builder --user --install --force-clean --disable-cache \
    cellar-build flatpak/io.github.cellar.json
flatpak run io.github.cellar
```

### Launch

- **Terminal:** `cellar`
- **App drawer:** search for *Cellar* in GNOME Activities

---

## Running in development

### Requirements

- Python 3.10+
- GTK 4 and libadwaita 1.x (`python3-gobject` / `pygobject`)
- `pip install requests Pillow zstandard smbprotocol paramiko pytest`
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

## Local data layout

```
~/.local/share/cellar/
  cellar.db             SQLite database (installed apps, base images)
  config.json           Repos, bearer tokens, preferences
  runners/
    GE-Proton10-32/     GE-Proton runner (managed by Cellar)
  prefixes/
    <app-id>/           WINEPREFIX for each installed Windows app
  native/
    <app-id>/           Installed Linux native apps
  dos/
    <app-id>/           Installed DOS games (bundled DOSBox Staging + configs)
  dosbox-staging/       Shared DOSBox Staging runtime (auto-downloaded)
  projects/
    <slug>/             Package Builder working area
      project.json
      prefix/           WINEPREFIX being built
  bases/
    <runner>/           Installed base images (for delta seeding)

~/.cache/cellar/
  assets/               Persistent image asset cache (HTTP(S)/SSH/SMB repos)
  catalogues/           Cached catalogue.json per repo (offline fallback)
  metadata/             Cached per-app metadata.json
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
      dosbox_settings.py  Reusable DOSBox Staging settings dialog (display, CPU, sound, MIDI, mixer)
      edit_app.py         Edit / delete catalogue entries
      update_app.py       Safe update dialog — rsync overlay
      install_runner.py   GE-Proton download + extract dialog
      settings.py         Repos, access control, base images
      steam_picker.py     Steam Store game search and metadata picker
      screenshot_grid.py  Screenshot browser, grid display, and importer
      widgets.py          Shared UI widget helpers (progress page, etc.)
      builder/            Package Builder subpackage
        view.py           Main builder view — project list, detail panel, publish
        metadata.py       Metadata editing — name, category, description, images
        media_panel.py    Shared image and screenshot picker panel
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
      dosbox.py           DOSBox Staging runtime, GOG detection, config parsing, conversion
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

## Acknowledgements

Cellar is built on the work of many open-source projects:

- **[GTK](https://gtk.org/)** and **[libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/)** — UI toolkit and GNOME platform library
- **[umu-launcher](https://github.com/Open-Wine-Components/umu-launcher)** — unified Wine/Proton launcher
- **[GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom)** — custom Proton builds by GloriousEggroll
- **[DOSBox Staging](https://dosbox-staging.github.io/)** — modern DOS emulator (transparent runtime for DOS games)
- **[NoUniVBE](https://github.com/LowLevelMahn/NoUniVBE)** — UniVBE bypass for GOG DOS games
- **[Pillow](https://python-pillow.org/)** — image processing
- **[paramiko](https://www.paramiko.org/)** — pure-Python SSHv2
- **[smbprotocol](https://github.com/jborean93/smbprotocol)** — pure-Python SMBv2/v3
- **[zstandard](https://github.com/indygreg/python-zstandard)** — Zstandard compression bindings
- **[RapidFuzz](https://github.com/rapidfuzz/RapidFuzz)** — fuzzy string matching (MIT)
- **[Requests](https://docs.python-requests.org/)** — HTTP client
- **[PyGObject](https://pygobject.gnome.org/)** — Python bindings for GLib/GTK/GStreamer

Tab and category icons are [CC0-1.0](https://creativecommons.org/publicdomain/zero/1.0/) sourced from the GNOME icon set.

---

## AI assistance

This application was developed with the help of [Claude](https://claude.ai) by Anthropic. Claude assisted with architecture decisions, implementation, and debugging throughout the project.

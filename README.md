# Cellar

A GNOME desktop application that acts as a private software storefront for
Windows, Linux and DOS games and applications. Think GNOME Software, but the "packages" are
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

## Screenshots

| Browse catalogue | App detail |
|---|---|
| [![Explore view](docs/screenshots/explore.png)](https://macaon.github.io/cellar/screenshots/explore.png) | [![Detail view](docs/screenshots/detail.png)](https://macaon.github.io/cellar/screenshots/detail.png) |

| Package builder | Metadata editor |
|---|---|
| [![Package builder](docs/screenshots/builder.png)](https://macaon.github.io/cellar/screenshots/builder.png) | [![Metadata editor](docs/screenshots/metadata.png)](https://macaon.github.io/cellar/screenshots/metadata.png) |

---

## Features

**For users**
- GNOME Software-style browse grid — Explore, Installed, and Updates tabs
- Category filter and full-text search
- App detail view — icon/logo, screenshot carousel, description, info cards
- One-click install, update (safe overlay or full replacement), and remove
- Delta packages — shared base images keep download sizes small
- Chunked downloads with per-chunk CRC32 verification
- Runner management — GE-Proton versions listed from GitHub Releases
- Multi-target launch — apps can define multiple entry points (e.g. main game, editor, launcher)
- Desktop shortcut creation for installed apps
- Offline mode — cached catalogue allows browsing and launching when the repo is unreachable
- **DOS game support (early testing)** — GOG DOSBox games auto-detected and converted; DOSBox Settings dialog for CPU, video, sound, and MIDI

**For maintainers** (requires a writable repo)
- Package Builder — create and publish packages for Windows apps, Linux apps, and base images
- Steam Store metadata lookup — auto-fill title, description, cover art, and screenshots
- Edit and delete existing catalogue entries from the detail view
- Delta archive creation — automatic diff against a base image

---

## How it works

Cellar organises Windows app packages into a three-tier dependency chain:

```
Runner  (GE-Proton10-32)                     ← GE-Proton binary
  ↑
Base    (GE-Proton10-32-allfonts)            ← clean WINEPREFIX + shared deps
  ↑
App     (my-game)                            ← delta archive (changes only)
```

A **runner** is a GE-Proton build. A **base image** is a clean WINEPREFIX
created with that runner plus shared dependencies (fonts, runtimes via
winetricks). An **app** is a delta archive containing only the files that
differ from its base — typically 50–500 MB instead of the full 2–3 GB prefix.

At install time, Cellar seeds a new prefix from the base using copy-on-write
(reflinks on btrfs/XFS, regular copy elsewhere), then overlays the delta.
Archives are split into ~1 GB chunks so only one chunk needs to be on disk at
a time.

Linux native apps and DOS games skip this hierarchy — they're just extracted
and launched directly.

For the full technical details, see [docs/INTERNALS.md](docs/INTERNALS.md).

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

For HTTP-served repos, point a web server at the directory. HTTP repos are
read-only — publish via a writable transport and serve over HTTP for users.
See [docs/AUTH.md](docs/AUTH.md) for bearer token setup with nginx or Caddy.

### Building and publishing a base image

A base image is a shared WINEPREFIX that Windows app packages build on top of.
This is the first step before publishing any app packages.

1. Open the **Package Builder** tab → **New Project** → select **Base**.
2. Pick a name and a GE-Proton runner from the dropdown.
3. Click **Initialise** to create an empty WINEPREFIX with that runner.
4. Use the **Dependencies** section to install shared libraries via winetricks
   (fonts, Visual C++ runtimes, .NET, DirectX, etc.).
5. Click **Publish** and choose the target repo.

### Building and publishing an app

1. Open the **Package Builder** → **New Project** → select **App**.
2. **Select your base image** from the dropdown.
3. **Install the application** — run a `.exe` installer inside the prefix, or
   manually place files using "Browse Prefix".
4. **Configure launch targets** — add one or more entry points (exe path,
   display name, optional arguments).
5. **Test-launch** from the builder to verify everything works.
6. **Fill in metadata** — title, category, description, icon, cover, and
   screenshots. Use the Steam lookup button to auto-fill from the Steam Store.
7. Click **Publish**. Cellar automatically computes the delta against the base
   image — only changed files are included.

---

## Supported repo transports

| Scheme | Example | Writable |
|---|---|---|
| Local path | `/mnt/nas/cellar` | Yes |
| `file://` | `file:///mnt/nas/cellar` | Yes |
| `http://` / `https://` | `https://cellar.home.arpa/repo` | No |
| `sftp://` | `sftp://alice@nas.home.arpa/srv/cellar` | Yes |
| `smb://` | `smb://nas.home.arpa/cellar` | Yes |

- **HTTP(S)** — read-only with optional [bearer token auth](docs/AUTH.md).
- **SFTP** — pure-Python via paramiko. Key auth via ssh-agent or `~/.ssh/config`.
- **SMB** — pure-Python via smbprotocol (SMBv2/v3). No GVFS mount required.

Credentials are stored per-repo via libsecret (system keyring) with a
plaintext fallback when no secret daemon is available.

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

```bash
git clone https://github.com/macaon/cellar
cd cellar
pip install requests Pillow zstandard smbprotocol paramiko pytest
PYTHONPATH=. CELLAR_REPO=tests/fixtures python3 -m cellar.main
```

`CELLAR_REPO` accepts a local path or any supported URI. The test fixtures
under `tests/fixtures/` work out of the box for local development.

```bash
PYTHONPATH=. python3 -m pytest tests/ -v
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

## Further reading

- [docs/REPO_FORMAT.md](docs/REPO_FORMAT.md) — catalogue schema and field reference
- [docs/INTERNALS.md](docs/INTERNALS.md) — chunked archives, delta packages, safe updates, local data layout
- [docs/AUTH.md](docs/AUTH.md) — bearer token authentication with nginx and Caddy

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

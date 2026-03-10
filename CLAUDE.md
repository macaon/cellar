# Cellar — Project Brief

## What this project is

A GNOME desktop application that acts as a software storefront for Windows and Linux applications. Think GNOME Software, but the "packages" are WINEPREFIX archives (or Linux app tarballs) stored on a network share (SMB or HTTP). The user browses a catalogue, clicks Install, and the app handles downloading, extracting, and setting up the prefix via umu-launcher (for Windows apps) or copying to a local directory (for Linux apps).

The project is called **Cellar**.

---

## Tech stack

- **Language:** Python 3.10+
- **UI toolkit:** GTK4 + libadwaita (target GNOME 46+)
- **Packaging:** Flatpak (target `io.github.cellar` or similar)
- **Local data:** SQLite via `sqlite3` stdlib
- **Network I/O:** `requests` for HTTP/HTTPS; `paramiko` (pure Python) for SFTP/SSH; `smbprotocol` for SMB
- **Image handling:** Pillow (load, resize, crop, ICO→PNG)
- **Archive handling:** `tarfile` stdlib + `zstandard` for `.tar.zst`
- **File sync:** `rsync` subprocess; Python fallback if rsync absent
- **Wine layer:** `umu-launcher` (replaces Bottles); GE-Proton runners via GitHub Releases API
- **Credentials:** `gi.repository.Secret` (libsecret) for all passwords; `config.json` fallback

---

## Repo / catalogue format (v2)

Catalogue v2 splits data into a **slim index** (`catalogue.json`) and **per-app metadata** (`apps/<id>/metadata.json`).

- `catalogue.json` — contains only the fields needed for the browse grid and update detection (id, name, category, summary, icon, cover, platform, archive_crc32, base_image). Runners, bases, categories, and category_icons also live here.
- `apps/<id>/metadata.json` — contains the full `AppEntry` (all fields, self-contained). Fetched on demand when the detail view opens.
- `INDEX_FIELDS` in `app_entry.py` defines which fields go in the index.
- `AppEntry.is_partial` returns `True` for index-only entries (no `archive` field).
- `Repo.fetch_app_metadata(app_id)` fetches and caches per-app metadata.
- The packager writes both files via `_upsert_catalogue()`.
- `regenerate_catalogue(repo_root)` rebuilds the index from all metadata files.

App assets live under `repo/apps/<id>/` (icon, cover, screenshots, archive, metadata.json). Delta base archives under `repo/bases/`.

### Supported URI schemes

| Scheme | Writable | Notes |
|---|---|---|
| Local path / `file://` | Yes | |
| `http://` / `https://` | **No** | Read-only; optional bearer token auth |
| `sftp://[user@]host[:port]/path` | Yes | Pure-Python via `paramiko`; key auth via agent, `~/.ssh/config`, or `ssh_identity=` |
| `smb://` | Yes | Via `smbprotocol` (pure Python, no GVFS) |

HTTP(S) image assets are downloaded to a persistent cache (`Repo._fetch_to_cache`) — GdkPixbuf can't pass auth headers. Per-app metadata is cached to `~/.cache/cellar/metadata/<hash>/`. Archives return URLs (installer handles auth). Bearer token stored per-repo in `config.json`, sent as `Authorization: Bearer <token>`.

---

## umu-launcher integration

umu-launcher replaces Bottles for all Windows app management. GE-Proton is the only supported runner.

| Directory | Path |
|---|---|
| Prefixes | `~/.local/share/cellar/prefixes/<id>/` |
| Runners  | `~/.local/share/cellar/runners/` |
| Projects | `~/.local/share/cellar/projects/` |

**Install:** download + verify CRC32 → extract to temp → copy to prefixes dir → write DB record.

**Safe update** (`update_strategy: "safe"`): download + verify → extract → rsync overlay excluding `drive_c/users/`, `user.reg`, `userdef.reg` → update DB.

**Full update** (`update_strategy: "full"`): warn user → remove prefix → reinstall.

**Launch:** `GAMEID=umu-<steam_appid>` (or `0`), `WINEPREFIX=<prefix>`, `PROTONPATH=<runner>`, exec `umu-run <entry_point>`.

**Linux apps:** extracted to `~/.local/share/cellar/native/<id>/`; launched directly via subprocess.

---

## Local database schema

`~/.local/share/cellar/cellar.db` — schema v3 with versioned migrations.

```sql
CREATE TABLE schema_version (version INTEGER PRIMARY KEY);

CREATE TABLE installed (
    id              TEXT PRIMARY KEY,
    prefix_dir      TEXT NOT NULL,
    platform        TEXT NOT NULL DEFAULT 'windows',
    version         TEXT,
    archive_crc32   TEXT,
    runner          TEXT,
    runner_override TEXT,
    steam_appid     INTEGER,
    install_path    TEXT,
    install_size    INTEGER,
    repo_source     TEXT,
    installed_at    TEXT,
    last_updated    TEXT
);

CREATE TABLE bases (
    runner       TEXT PRIMARY KEY,
    repo_source  TEXT,
    installed_at TEXT
);
```

Repos are managed via `config.json`, not the database.

---

## UI structure

Modelled on GNOME Software. libadwaita throughout.

- **Main window:** `AdwViewSwitcher` → Explore / Installed / Updates tabs. Category filter strip of linked `GtkToggleButton` pills. `GtkFlowBox` of 300×96 px `.card` app cards. `GtkSearchBar` + `set_key_capture_widget`.
- **Detail view:** Icon + name + category. Description, components, changelog. `AdwCarousel` screenshots. Install/Update/Remove action button. Edit pencil for writable repos.
- **Progress dialog:** `AdwDialog` + progress bar + cancel. `AdwToast` on completion.
- **Settings:** `Adw.PreferencesDialog` — Repositories group (add/remove, bearer token), Access Control group (token generator), Delta Base Images group.
- Tab icons in `data/icons/hicolor/symbolic/apps/` (CC0-1.0), registered via `Gtk.IconTheme.add_search_path()`.

---

## Project structure

```
cellar/
  cellar/
    main.py             # GApplication entry point
    window.py           # AdwApplicationWindow
    views/
      browse.py         # BrowseView (Explore/Installed/Updates tabs)
      detail.py         # App detail + Install/Update/Remove
      edit_app.py       # Edit/delete catalogue entry
      update_app.py     # Safe update dialog (backup + rsync overlay)
      install_runner.py # Runner download + install dialog
      settings.py       # Preferences dialog
      steam_picker.py   # Steam game search/picker dialog
      screenshot_grid.py # Screenshot browser, grid display, and importer
      widgets.py        # Shared UI widget helpers (progress page, etc.)
      builder/          # Package Builder subpackage
        view.py         # Main builder view — project list, detail panel, publish
        metadata.py     # Metadata editing — name, category, description, images
        dependencies.py # Winetricks dependency picker dialog
        pickers.py      # Runner, base image, and entry point pickers
        catalogue_import.py  # Import existing catalogue entries into projects
        progress.py     # Build/install progress tracking
    backend/
      repo.py           # Catalogue index fetch + per-app metadata fetch + transport fetchers
      packager.py       # import_to_repo / update_in_repo / remove_from_repo
      installer.py      # Download→verify→extract→import pipeline
      updater.py        # Safe rsync overlay + backup prefix
      base_store.py     # Delta base image store (is_base_installed, install_base, remove_base)
      umu.py            # umu-launcher detection, prefix/runner paths
      runners.py        # GE-Proton releases via GitHub API
      project.py        # Package Builder project persistence
      steam.py          # Steam Store API client
      database.py       # SQLite tracking (installed, bases)
      manifest.py       # File manifest (mtime + size) for safe-update diffing
      config.py         # JSON config + libsecret credential storage
    models/
      app_entry.py      # AppEntry + RunnerEntry + BaseEntry dataclasses
    utils/
      http.py           # requests.Session (User-Agent: Mozilla/5.0 compatible; Cellar/1.0)
      images.py         # Pillow helpers
      smb.py            # SmbPath — pathlib.Path-like SMB abstraction via smbprotocol
      ssh.py            # SshPath — pathlib.Path-like SSH/SFTP abstraction via paramiko
      _remote_path.py   # RemotePathMixin — shared base for SmbPath and SshPath
      async_work.py     # Threading helpers (run_in_background)
      desktop.py        # .desktop entry creation for installed apps
      progress.py       # Shared progress-formatting helpers (fmt_size, fmt_stats)
      paths.py          # ui_file / icons_dir resolution (source tree vs installed)
  data/
    icons/hicolor/symbolic/apps/
    ui/window.ui
  docs/
    PLAN_UMU_MIGRATION.md # umu migration plan and status
    GTK_ADWAITA_NOTES.md  # CSS gotchas, named colors, icon registration
  tests/
    fixtures/           # Sample catalogue.json for local dev
    test_*.py
  pyproject.toml
  meson.build
```

---

## Development priorities

1–9. ~~Repo backend, Browse UI, Network transports, Detail view, Wine/umu backend, Local DB, Update logic, HTTP(S) auth, Delta packages~~ ✅ all done
10. **Flatpak packaging** — `flatpak/io.github.cellar.json`
11. **KDE support** — GVFS/smbclient fallback, KWallet, `XDG_CURRENT_DESKTOP`

---

## Running in development

```bash
PYTHONPATH=. CELLAR_REPO=tests/fixtures python3 -m cellar.main
```

`CELLAR_REPO` accepts a local path or any supported URI. Tests: `PYTHONPATH=. python3 -m pytest tests/ -v`.

`paths.py` checks `data/ui/` first, then `/app/share/cellar/ui/` — no build step needed.

---

## Key constraints & gotchas

- **Async everything:** Archives are multi-GB. Use `threading.Thread` + `GLib.idle_add`. Never block the UI thread.
- **Flatpak sandbox:** umu-run may need `flatpak-spawn --host`. Handle both cases.
- **rsync fallback:** Check availability at runtime; fall back to Python dir merge.
- **Prefix name collisions:** Append suffix rather than silently overwrite.
- **Repo offline:** Show cached catalogue, don't block app launch.
- **HTTP User-Agent:** Use `User-Agent: Mozilla/5.0 (compatible; Cellar/1.0)` (`http.py`). Default Python UA is blocked by Cloudflare.
- **nginx image assets:** Use `location ^~` so prefix match beats `~* \.(png|jpg)` regex blocks.
- **Pillow + HTTP:** Can't load URLs or pass auth headers. Use `Repo._fetch_to_cache` temp-cache pattern.
- **Copy-on-write (delta):** Uses `cp --reflink=auto` for btrfs/XFS; falls back to regular copy on other filesystems.

# Cellar — Project Brief

## What this project is

A GNOME desktop application that acts as a software storefront for Wine/Bottles-managed Windows applications. Think GNOME Software, but the "packages" are Bottles full backups stored on a network share (SMB, NFS, or HTTP). The user browses a catalogue, clicks Install, and the app handles downloading the backup and importing it into Bottles. Component version management (runner, DXVK, VKD3D) is left to Bottles itself.

The project is called **Cellar**.

---

## Tech stack

- **Language:** Python 3.11+
- **UI toolkit:** GTK4 + libadwaita (target GNOME 46+)
- **Packaging:** Flatpak (target `io.github.cellar` or similar)
- **Local data:** SQLite via `sqlite3` stdlib
- **Network I/O:** `requests` for HTTP/HTTPS; `ssh` subprocess for SSH; GIO (`gi.repository.Gio`) for SMB/NFS via GVFS
- **Image handling:** Pillow (load, resize, crop, ICO→PNG)
- **Archive handling:** `tarfile` stdlib
- **File sync:** `rsync` subprocess; Python fallback if rsync absent
- **Bottles integration:** `bottles-cli` subprocess + `PyYAML` for `bottle.yml`

---

## Repo / catalogue format

`repo/catalogue.json` is the single source of truth. App assets live under `repo/apps/<id>/` (icon, cover, screenshots, archive). Delta base archives under `repo/bases/`. See `docs/CATALOGUE_FORMAT.md` for full schema.

### Supported URI schemes

| Scheme | Writable | Notes |
|---|---|---|
| Local path / `file://` | Yes | |
| `http://` / `https://` | **No** | Read-only; optional bearer token auth |
| `ssh://[user@]host[:port]/path` | Yes | `BatchMode=yes`; key auth via agent or `ssh_identity=` |
| `smb://` / `nfs://` | Yes | Via GIO/GVFS |

HTTP(S) image assets are downloaded to a per-session temp cache (`Repo._fetch_to_cache`) — GdkPixbuf can't pass auth headers. Archives return URLs (installer handles auth). Bearer token stored per-repo in `config.json`, sent as `Authorization: Bearer <token>`.

---

## Bottles integration

| Variant | Data path |
|---|---|
| Flatpak | `~/.var/app/com.usebottles.bottles/data/bottles/bottles/` |
| Native | `~/.local/share/bottles/bottles/` |

**Install:** download + verify CRC32 → extract to temp → copy to Bottles data path → write DB record.

**Safe update** (`update_strategy: "safe"`): download + verify → extract → rsync overlay excluding `drive_c/users/`, `user.reg`, `userdef.reg` → update DB.

**Full update** (`update_strategy: "full"`): warn user → remove bottle → reinstall.

`bottles-cli` used for: `list bottles`, `--json programs -b <name>`, `run`. Handle absence gracefully (show setup prompt, don't crash).

---

## Local database schema

`~/.local/share/cellar/cellar.db`

```sql
CREATE TABLE installed (
    id TEXT PRIMARY KEY, bottle_name TEXT NOT NULL,
    installed_version TEXT, installed_at TIMESTAMP, last_updated TIMESTAMP,
    repo_source TEXT, runner_override TEXT
);
CREATE TABLE repos (
    id INTEGER PRIMARY KEY, name TEXT, uri TEXT NOT NULL,
    last_refreshed TIMESTAMP, enabled INTEGER DEFAULT 1
);
CREATE TABLE bases (
    win_ver TEXT PRIMARY KEY, installed_at TIMESTAMP, repo_source TEXT
);
```

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
      add_app.py        # Add app to catalogue dialog
      edit_app.py       # Edit/delete catalogue entry
      update_app.py     # Safe update dialog (backup + rsync overlay)
      install_runner.py # Runner download + install dialog
      settings.py       # Preferences dialog
    backend/
      repo.py           # Catalogue fetch + transport fetchers (_Local, _Http, _Ssh, _Gio)
      packager.py       # import_to_repo / update_in_repo / remove_from_repo
      installer.py      # Download→verify→extract→import pipeline
      updater.py        # Safe rsync overlay + backup_bottle
      base_store.py     # Delta base image store (is_base_installed, install_base, remove_base)
      bottles.py        # Bottles detection + bottles-cli wrapper
      components.py     # bottlesdevs/components runner metadata
      database.py       # SQLite tracking (installed, repos, bases)
      config.py         # JSON config (~/.local/share/cellar/config.json)
    models/
      app_entry.py      # AppEntry + BuiltWith + BaseEntry dataclasses
    utils/
      gio_io.py         # GIO file/network helpers
      http.py           # requests.Session (User-Agent: Mozilla/5.0 compatible; Cellar/1.0)
      images.py         # Pillow helpers
      paths.py          # ui_file / icons_dir resolution (source tree vs installed)
  data/
    icons/hicolor/symbolic/apps/
    ui/window.ui
  docs/
    CATALOGUE_FORMAT.md # Full catalogue.json + repo layout reference
    DELTA_PACKAGES.md   # Delta package design, status, constraints
  tests/
    fixtures/           # Sample catalogue.json for local dev
    test_*.py
  pyproject.toml
  meson.build
```

---

## Development priorities

1–9. ~~Repo backend, Browse UI, Network transports, Detail view, Bottles backend, Local DB, Update logic, HTTP(S) auth, Delta packages~~ ✅ all done
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

- **Async everything:** Archives are multi-GB. Use `GLib.Thread` or asyncio+GLib. Never block the UI thread.
- **Flatpak sandbox:** `bottles-cli` may need `flatpak-spawn --host`. Handle both cases.
- **rsync fallback:** Check availability at runtime; fall back to Python dir merge.
- **Bottle name collisions:** Append suffix rather than silently overwrite.
- **Repo offline:** Show cached catalogue, don't block app launch.
- **HTTP User-Agent:** Use `User-Agent: Mozilla/5.0 (compatible; Cellar/1.0)` (`http.py`). Default Python UA is blocked by Cloudflare.
- **nginx image assets:** Use `location ^~` so prefix match beats `~* \.(png|jpg)` regex blocks.
- **Pillow + HTTP:** Can't load URLs or pass auth headers. Use `Repo._fetch_to_cache` temp-cache pattern.
- **Hardlinks (delta):** Require same filesystem as Bottles data dir. Fall back to copy otherwise.

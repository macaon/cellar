# Cellar

A GNOME desktop application that acts as a software storefront for
[Bottles](https://usebottles.com/)-managed Windows applications. Think GNOME
Software, but the "packages" are Bottles full backups stored on a network share
(SMB, NFS, or HTTP).

Browse a catalogue, click Install, and Cellar handles downloading the backup,
importing it into Bottles, and updating Wine components as needed.

> **Status: early development** — the browse UI and repo backend are working;
> install/update flows are not yet implemented.

---

## Tech stack

- **Language:** Python 3.11+
- **UI toolkit:** GTK4 + libadwaita (GNOME 46+)
- **Packaging:** Flatpak (`io.github.cellar`)
- **Local data:** SQLite via `sqlite3` stdlib
- **Network I/O:** GIO (`gi.repository.Gio`) for share access
- **Bottles integration:** `bottles-cli` subprocess + YAML manipulation

---

## Running in development

### Requirements

- Python 3.11+
- GTK 4 and libadwaita 1.x (`python3-gobject` / `pygobject`)
- A directory containing a `catalogue.json` (see below)

### Quick start

```bash
git clone <repo-url> cellar
cd cellar

# Point the app at the bundled test fixtures
PYTHONPATH=. CELLAR_REPO=tests/fixtures python3 -m cellar.main
```

### Using your own repository

Set `CELLAR_REPO` to any local directory (SMB/NFS/HTTP support comes later)
that follows the repository layout described in `CLAUDE.md`:

```
/repo/
  catalogue.json
  apps/
    appname/
      manifest.json
      icon.png
      appname-1.0.tar.gz
```

### Running tests

```bash
python3 -m pytest tests/ -v
```

---

## Project structure

```
cellar/
  cellar/
    main.py          GApplication entry point
    window.py        Main AdwApplicationWindow
    views/
      browse.py      Grid browse view (app cards + category filter + search)
      detail.py      App detail page (phase 3)
      installed.py   Installed apps view (phase 5)
      updates.py     Available updates view (phase 6)
      settings.py    Settings dialog (phase 9)
    backend/
      repo.py        Catalogue fetching and manifest parsing
      installer.py   Download, extract, import to Bottles (phase 4)
      updater.py     rsync-based update logic (phase 6)
      bottles.py     bottles-cli wrapper, path detection (phase 4)
      database.py    SQLite installed/repo tracking (phase 5)
    models/
      app_entry.py   Dataclass for catalogue entries
      manifest.py    Dataclass for full app manifests
    utils/
      paths.py       UI file path resolution (source tree + installed)
      gio_io.py      GIO-based file/network helpers (phase 7)
      checksum.py    SHA-256 verification (phase 4)
  data/
    ui/
      window.ui      Main window template
      app_card.ui    App card template
      detail_view.ui Detail view template
  tests/
    fixtures/        Sample catalogue.json and manifests for local testing
    test_repo.py     Backend unit tests
```

---

## Development roadmap

1. **Repo backend** — parse `catalogue.json` / `manifest.json` ✅
2. **Browse UI** — grid of app cards, category filter, search ✅
3. **Detail view** — app page driven by manifest data
4. **Bottles backend** — path detection, `bottles-cli` wrapper, basic install
5. **Local DB** — track installed apps, wire up Install button state
6. **Update logic** — safe rsync strategy, full replacement
7. **Network repo support** — GIO for SMB/NFS, HTTP fallback
8. **Component update UI** — post-install prompt to upgrade runner/DXVK
9. **Multi-repo support** — settings page, repo management
10. **Flatpak packaging**

---

## License

GPL-3.0-or-later

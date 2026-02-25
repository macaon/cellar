# Changelog

All notable changes to Cellar are documented here.
Versioning follows [Semantic Versioning](https://semver.org/) — while the major version is 0, minor bumps may include breaking changes.

---

## [0.5.0] — 2026-02-25

### Added
- **Settings dialog** (`cellar/views/settings.py`): `AdwPreferencesDialog` accessible via hamburger menu → Preferences
  - Repositories group lists configured sources as rows with individual remove buttons
  - `AdwEntryRow` to add a new repo by URI — accepts Enter key or the + button
  - On add, validates the URI and attempts to fetch `catalogue.json`:
    - Found → repo added immediately, main window refreshes
    - Missing + writable local path → "Initialise?" `AdwAlertDialog`; confirms creates the directory and writes an empty `catalogue.json`
    - Missing + HTTP(S) → explains the source is read-only and the catalogue must exist on the server
    - Missing + SSH/SMB/NFS → explains remote init is not yet supported and shows manual setup instructions
  - Duplicate URIs are rejected
- **About dialog** (`AdwAboutDialog`) wired to the `app.about` menu action
- **`cellar/backend/config.py`**: persists the repo list to `~/.local/share/cellar/config.json` (XDG_DATA_HOME-aware); `load_repos()` / `save_repos()` helpers

### Changed
- Hamburger menu (`data/ui/window.ui`) is now wired to a `GMenu` with Preferences (`win.preferences`) and About (`app.about`) items
- Window catalogue loading now merges repos from `config.json` **and** the `CELLAR_REPO` environment variable (env var acts as a dev/testing override on top of persisted config)
- "No repository configured" status page now directs users to Preferences instead of the `CELLAR_REPO` env var

---

## [0.4.0] — 2026-02-25

### Added
- `AppEntry.to_dict()` — serialises an entry back to a catalogue-compatible dict, omitting empty fields; required for the repo write/init path
- `Repo.fetch_entry_by_id(app_id)` — direct lookup without a separate manifest fetch
- `Repo.is_writable` property — `False` for HTTP(S) repos, `True` for all other transports; used by the UI to decide whether to show repo management actions

### Changed
- **`AppEntry` is now the single unified model** — browse grid, detail view, and install configuration all live in one dataclass. `manifest.py` and the `Manifest` class are removed.
- `BuiltWith` moved into `app_entry.py`. `built_with` is `None` when an entry has no packaged archive yet.
- **`catalogue.json` is now a fat file**: all metadata (display, attribution, media, install config) lives directly in catalogue entries rather than being split across a separate per-app `manifest.json`. Per-app manifest files are gone.
- `catalogue.json` gains a top-level wrapper: `{"cellar_version": 1, "generated_at": "…", "apps": […]}`. Bare JSON arrays are still accepted for backward compatibility.
- New fields on `AppEntry`: `description`, `tags`, `developer`, `publisher`, `release_year`, `content_rating`, `languages`, `website`, `store_links`, `cover`, `hero`, `install_size_estimate`, `entry_point`, `compatibility_notes`
- `Repo.fetch_manifest()` and `Repo.fetch_manifest_by_id()` removed (superseded by `fetch_catalogue()` + `fetch_entry_by_id()`)
- Test fixtures updated to wrapper format with full field coverage; 42 tests passing

### Removed
- `cellar/models/manifest.py` and the `Manifest` dataclass
- Per-app `manifest.json` fixture files

---

## [0.3.0] — 2026-02-25

### Added
- Multi-transport repo backend — `Repo` now accepts URIs beyond local paths:
  - `http://` / `https://` via `urllib` — primary transport, no extra dependencies; **read-only**
  - `ssh://[user@]host[:port]/path` via the system `ssh` client — key auth via SSH agent / `~/.ssh/config`; explicit identity file via `ssh_identity=` kwarg
  - `smb://` and `nfs://` via GIO/GVFS — GIO is lazily imported so the backend is testable headlessly
- `_Fetcher` protocol with `_LocalFetcher`, `_HttpFetcher`, `_SshFetcher`, `_GioFetcher` implementations
- GIO file helpers in `cellar/utils/gio_io.py`: `gio_read_bytes()` and `gio_file_exists()`
- 14 new tests: HTTP mock responses, HTTP error handling, SSH argument construction, SSH error propagation, identity file pass-through

### Changed
- `Repo.resolve_path()` renamed to `Repo.resolve_asset_uri()` — returns a `str` URI instead of a `pathlib.Path`
- `Repo.__init__` accepts `ssh_identity: str | None` for explicit SSH key configuration

### Fixed
- `_HttpFetcher` normalises trailing slashes on base URLs so asset URIs are always well-formed

---

## [0.2.0] — 2026-02-18

### Added
- Browse UI (`cellar/views/browse.py`): scrolling `GtkFlowBox` grid of app cards with `.card` Adwaita styling
- Horizontal category filter strip with linked `GtkToggleButton` pills (radio behaviour via `set_group`)
- `GtkSearchBar` wired to the header bar search toggle; typing anywhere in the window opens it automatically via `set_key_capture_widget`
- Empty and error states via `AdwStatusPage`
- `AppCard` widget with icon placeholder, name, and two-line summary

---

## [0.1.0] — 2026-02-18

### Added
- Initial project scaffold: `pyproject.toml`, `meson.build`, Flatpak manifest skeleton
- Repo backend (`cellar/backend/repo.py`): parses local `catalogue.json`
- `AppEntry` dataclass model and `RepoManager` for merging multiple sources (last-repo-wins)
- `Repo` supports bare local paths and `file://` URIs
- Test fixtures with two sample apps (`example-app`, `paint-clone`)
- Full test suite for local repo operations (`tests/test_repo.py`)
- `cellar/utils/paths.py`: resolves UI files from the source tree or installed location — no build step needed during development

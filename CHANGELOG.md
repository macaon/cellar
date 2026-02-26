# Changelog

All notable changes to Cellar are documented here.
Versioning follows [Semantic Versioning](https://semver.org/) — while the major version is 0, minor bumps may include breaking changes.

---

## [0.7.5] — 2026-02-26

### Added
- **`cellar/backend/installer.py`**: archive download, verification, extraction, and bottle import
  - `install_app(entry, archive_uri, bottles_install, *, progress_cb, cancel_event)` — full install pipeline; returns the `bottle_name` used; reports `(phase, fraction)` progress
  - `_acquire_archive()` — local archives (bare path or `file://`) used in-place; HTTP(S) streamed in 1 MB chunks with cancel support and partial-file cleanup; other schemes raise `InstallError`
  - `_verify_sha256()` — skipped when `archive_sha256` is empty
  - `_extract_archive()` — uses `filter="data"` on Python 3.12+ for safe extraction
  - `_find_bottle_dir()` — identifies single top-level bottle directory; resolves ambiguity by `bottle.yml` presence
  - `_safe_bottle_name()` — derives a non-colliding bottle name from the app ID (appends `-2`, `-3` … on collision)
  - `InstallError` / `InstallCancelled` exceptions; partial `copytree` destination cleaned up on failure
- **`cellar/backend/database.py`**: SQLite installed-app tracking
  - `mark_installed(app_id, bottle_name, version, repo_source)` — upsert; preserves `installed_at` on re-install
  - `get_installed(app_id)` / `is_installed(app_id)` / `remove_installed(app_id)` / `get_all_installed()`
  - Schema created on first use via `_ensure_schema()`; database at `~/.local/share/cellar/cellar.db`
- **`tests/test_installer.py`** / **`tests/test_database.py`**: 36 new tests; total 117 passing

---

## [0.7.4] — 2026-02-26

### Added
- **`cellar/backend/bottles.py`**: bottles-cli subprocess wrapper
  - `BottlesError` — single exception class for all CLI failures (not found, timeout, non-zero exit)
  - `list_bottles(install)` — calls `bottles-cli list bottles`, parses the `"Found N bottles:\n- Name\n"` output; returns `[]` when no bottles are installed
  - `edit_bottle(install, bottle_name, key, value)` — calls `bottles-cli edit -b … -k … -v …`; common keys: `Runner`, `DXVK`, `VKD3D`
  - `_run(install, args, *, timeout=60)` — shared helper; raises `BottlesError` with the stderr message on non-zero exit, or descriptive messages for `FileNotFoundError` / `TimeoutExpired`
  - `_parse_bottle_list(output)` — parses the confirmed bottles-cli text format; ignores header and non-`"- "` lines
- **`tests/test_bottles.py`**: 17 additional tests for the CLI wrapper (parser edge cases, `_run` error paths, `list_bottles`, `edit_bottle` command assembly for both native and Flatpak `cli_cmd`)

---

## [0.7.3] — 2026-02-26

### Added
- **`cellar/backend/bottles.py`**: Bottles installation detection
  - `BottlesInstall` dataclass — `data_path`, `variant` (`"flatpak"` / `"native"` / `"custom"`), `cli_cmd`
  - `is_cellar_sandboxed()` — checks `/.flatpak-info` to detect Flatpak sandbox
  - `detect_bottles(override_path=None)` — checks config override → Flatpak data path → native data path; returns `None` if Bottles is not found
  - `_build_cli_cmd(is_flatpak_bottles, sandboxed)` — resolves the correct base command for all four combinations (native/Flatpak Bottles × unsandboxed/sandboxed Cellar); Flatpak Bottles uses `flatpak run --command=bottles-cli com.usebottles.bottles`; sandboxed Cellar prefixes with `flatpak-spawn --host`
- **`cellar/backend/config.py`**: `load_bottles_data_path()` / `save_bottles_data_path(path)` — persist the user's Bottles data directory override in `config.json`; `None` removes the key (auto-detection resumes)
- **`tests/test_bottles.py`**: 22 new tests covering sandbox detection, all four CLI-command combinations, detection priority (Flatpak preferred over native), override path (valid/missing/string/bypasses auto-detect), and config round-trips

---

## [0.7.2] — 2026-02-25

### Added
- **EditAppDialog**: clear (×) button on each single-image row (Icon, Cover, Hero) — starts insensitive; becomes active when an image is set or picked; clicking sets the catalogue field to empty and shows "Will be removed" subtitle
- **EditAppDialog**: screenshots replaced by a per-item listbox — each existing or newly added screenshot is shown as its own row with a trash button for individual removal; an "Add Screenshots…" activatable row appends to the list via the file picker (multi-select); an empty state label "No screenshots" is shown when the list is empty
- **`cellar/backend/packager.py`**: `update_in_repo` now distinguishes `None` (keep existing screenshots), `[]` (clear all — deletes the screenshots directory), and `[…]` (replace — removes old dir, copies new files)

### Changed
- `EditAppDialog` state variables: `_icon_path`, `_cover_path`, `_hero_path` are now `str | None` (`None` = keep, `""` = clear, `str` = new file path); `_screenshots_dirty` flag replaces the old replace-all approach so unchanged screenshots are never re-written

---

## [0.7.1] — 2026-02-25

### Added
- **Edit catalogue entry** (`cellar/views/edit_app.py`): `EditAppDialog(Adw.Dialog)` — opened from a new Edit (pencil) button in the detail view header bar, visible only when the repo is writable
  - All metadata fields pre-filled from the existing `AppEntry`; App ID is read-only (displayed as a non-editable `ActionRow` subtitle — renaming an ID would break installed records)
  - **Archive** group: shows current filename; "Replace…" button opens a `.tar.gz` file chooser; on pick, the archive row subtitle updates and `bottle.yml` is read in a background thread to refresh the Wine Components rows
  - **Identity / Details / Attribution / Wine Components / Images / Install** groups — identical layout to the Add-app dialog
  - Images: "Change…" button per image; subtitle shows current filename as default; only picked images are overwritten on save
  - **Danger Zone** group at the bottom with a destructive "Delete Entry…" button
  - Save flow: background thread → `update_in_repo()`; progress view with determinate progress bar and Cancel; on success → "Entry updated" toast + browse reload; on error → form restored + `AdwAlertDialog`
  - Delete confirmation (`AdwAlertDialog`) offers three choices: Cancel, "Move Archive…" (folder picker, archive moved before directory removal), "Delete Archive" (archive removed with the rest)
  - Delete flow: spinner progress view; background thread → `remove_from_repo()`; on success → dialog closes, "Entry deleted" toast, nav pops back to browse, browse reloads; cancellable
- **`cellar/backend/packager.py`** additions:
  - `update_in_repo(repo_root, old_entry, new_entry, images, new_archive_src, ...)` — chunked archive copy (optional), selective image updates, full screenshot replacement, catalogue upsert; `cancel_event` supported throughout
  - `remove_from_repo(repo_root, entry, *, move_archive_to, cancel_event)` — optional archive move, `shutil.rmtree` of the app directory, catalogue entry removal; all steps are cancellable
  - Internal helpers `_upsert_catalogue()`, `_remove_from_catalogue()`, `_write_catalogue()` extracted from `import_to_repo()` to eliminate duplicated catalogue-write logic

### Changed
- `cellar/views/detail.py`: accepts two new keyword-only constructor parameters — `is_writable: bool` (default `False`) and `on_edit: Callable | None`; when both are set, a `document-edit-symbolic` button is packed into the header bar after the Install button
- `cellar/window.py`: `_on_app_selected()` now derives `can_write` from `self._first_repo.is_writable`, constructs an `_on_edit` closure, and passes both to `DetailView`; new `_on_entry_deleted()` method pops the nav view and reloads the catalogue; About dialog version bumped to 0.7.1

---

## [0.7.0] — 2026-02-25

### Added
- **Add-app dialog** (`cellar/views/add_app.py`): UI flow for adding a Bottles backup to a local Cellar repo directly from the main window
  - `+` button in the header bar, visible only when the first configured repo is writable (local path)
  - File chooser pre-filtered to `*.tar.gz` Bottles backup archives
  - `AddAppDialog` (`Adw.Dialog`) with full metadata form organised as `AdwPreferencesGroup` sections: Archive, Identity, Details, Attribution, Wine Components, Images, Install
  - Auto-extracts `bottle.yml` from the archive (no PyYAML required — simple line-by-line regex parser) to pre-fill Name, Runner, DXVK, VKD3D, and suggest "Games" category for `Environment: Game` bottles
  - App ID auto-generated from name via `slugify()` (e.g. `Notepad++` → `notepad-plus-plus`); ID field locks as soon as the user manually edits it
  - Image pickers for Icon, Cover, Hero, and multi-select Screenshots via `Gtk.FileChooserNative`
  - "Add to Catalogue" button enabled only when Name is non-empty; Category always has a default selection
  - Progress view replaces the form during import: determinate `GtkProgressBar`, status label, Cancel button
  - Archive copied in 1 MB chunks on a background thread so the UI stays responsive; cancellation via `threading.Event` cleans up the partial destination file
  - On success: dialog closes, `AdwToast` "App added to catalogue" shown on main window, browse view reloads
  - On error: form is restored and an `AdwAlertDialog` shows the failure message
- **`cellar/backend/packager.py`** (new): packaging helpers
  - `read_bottle_yml(archive_path)` — extracts and parses `bottle.yml` from inside a `.tar.gz`
  - `slugify(name)` — converts human app names to URL-safe IDs
  - `import_to_repo(repo_root, entry, archive_src, images, ...)` — copies archive + images into the repo tree and appends/updates the `catalogue.json` entry; accepts optional `progress_cb` and `cancel_event`
  - `CancelledError` exception for clean cancellation signalling
- `Repo.local_path(rel_path)` — returns the absolute `Path` for a repo-relative path; raises `RepoError` for non-local repos
- `AdwToastOverlay` (`toast_overlay`) wraps `main_content` in `window.ui`; `CellarWindow._show_toast()` helper method

### Changed
- `data/ui/window.ui`: `main_content` box is now wrapped in `AdwToastOverlay id="toast_overlay"`; `add_button` (hidden by default) added to the header bar left of the search toggle
- `cellar/window.py`: `add_button` and `toast_overlay` template children; `_load_catalogue()` shows the add button for writable repos; About dialog version bumped to 0.7.0

---

## [0.6.1] — 2026-02-25

### Fixed
- `DetailView` crashed on startup with `RuntimeError: could not create new GType` because `AdwToolbarView` is a final GType and cannot be subclassed in Python (PyGObject). `DetailView` now inherits `Gtk.Box` and embeds an `Adw.ToolbarView` instance internally.

---

## [0.6.0] — 2026-02-25

### Added
- **Detail view** (`cellar/views/detail.py`): full app page shown when an app card is activated
  - Hero banner image (full-width, only if a local file is available)
  - App header: icon (96 px), name (`title-1`), developer · publisher · year byline, category and content-rating chips
  - Description text
  - Screenshots carousel (`AdwCarousel` + `AdwCarouselIndicatorDots`); only shown when local screenshots exist
  - **Details** info group: developer, publisher, release year, languages, content rating, tags, website and store links (clickable, opens default browser)
  - **Wine Components** info group: runner, DXVK, VKD3D (only shown when `built_with` is set)
  - **Package** info group: download size, install size, update strategy (human-readable label)
  - **Compatibility Notes** and **What's New** (changelog) sections, shown only when content is present
  - Install button in the header bar — visible but insensitive, with a tooltip; placeholder until the installer backend lands
  - All sizes formatted as human-readable strings (B / KB / MB / GB / TB)
  - Asset images (icon, hero, screenshots) loaded from local files; remote URIs fall back to a generic placeholder — async remote loading is a future improvement
- `BrowseView` now emits an `app-selected` GObject signal when a card is activated
- Window navigation converted to `AdwNavigationView` — clicking a card pushes an `AdwNavigationPage` with the detail view; the back button is provided automatically

### Changed
- `data/ui/window.ui`: `AdwToolbarView` is now wrapped in `AdwNavigationView`; the browse toolbar and content sit inside an `AdwNavigationPage` with tag `browse`
- `window.py`: tracks the first successfully loaded `Repo` for asset URI resolution in the detail view; `About` dialog version bumped to 0.6.0

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

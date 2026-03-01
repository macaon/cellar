# Changelog

All notable changes to Cellar are documented here.

## [0.13.0] — 2026-03-01

### Added
- **Runner manager dialog** — replaces the simple `SelectRunnerDialog` with a
  new `RunnerManagerDialog` (500 × 500 px) that groups runners into collapsible
  family sections (Soda, Caffe, Wine GE, Kron4ek, Proton GE, …) using data
  from the bottlesdevs/components index.  Each row shows state-based suffix
  icons: installed + in-use → folder button only; installed + not in use →
  folder + trash; not installed → `folder-download-symbolic`.
- **Pre-install runner check** — when clicking **Install** and the required
  runner is missing, Cellar opens the runner manager so the user can download
  or select an alternative before proceeding.  Installation continues
  automatically once a runner is confirmed.
- **Warning icon on runner row** — a small warning icon appears next to the
  runner version in the Wine Components group when the required runner is not
  installed.
- `RunnerManagerDialog` can delete unused runners (confirmation dialog +
  background `shutil.rmtree`) and open their folder in the system file manager.
- **Installed checkmark on app cards** — a green GNOME
  `check-round-outline2-symbolic` icon (Adwaita `success` class) appears at
  the top-right corner (9 px margin) of app cards in the Explore view for
  installed apps, matching GNOME Software's style.
- **Card layout matching GNOME Software** — app icons are 52 × 52 px with
  22 px margins (left, top, bottom); cover images fill flush left, cropped to
  75 px wide × full card height.

### Changed
- **Runner family grouping** — runners are classified by name prefix (e.g.
  `soda-*` → Soda, `ge-proton*` → Proton GE) with version-aware natural sort
  (newest first within each name, alphabetical across names).  Locally
  installed runners not in the index are classified the same way.
- **Runner family organisation** — Lutris, Lutris GE and Vaniglia are folded
  into an "Other" group (always last); `sys-wine-*` runners appear under
  "Wine"; remaining families in alphabetical order after the preferred ones.
- **Change button accent color** — the "Change" button on the runner row uses
  `suggested-action` (system accent color) instead of flat styling.
- `components.py`: added `list_runners_by_category()`, `get_family_info()`,
  `family_display_order()`, and `_FAMILY_MAP` / `_FAMILY_DISPLAY_ORDER`
  constants for runner family grouping.
- `bottles.py`: added `get_runners_in_use(install)` that scans all
  `bottle.yml` files and returns the set of runner names currently in use
  (used to guard runner deletion).

### Fixed
- **Checksum verification** — runner downloads now read the correct
  `file_checksum` YAML field (was reading nonexistent `checksum`), and detect
  MD5 vs SHA-256 by hash length so verification actually works.

## [0.13.1] — 2026-03-01

### Added
- **Image optimisation at import** — covers are downscaled to 300×400, heroes
  to 1920×620, and screenshots to 1920×1080 (JPEG 85%) when imported via the
  packager.  Icons are copied as-is.  Images already within limits are not
  re-encoded.

### Fixed
- **Cover image sharpness** — card cover thumbnails now load at 4× target size
  before HYPER-downscaling, eliminating the blur from double-scaling.
- **Hero vertical centering** — hero banner now crops equally from top and
  bottom when the window resizes (was clipping from the top only).

---

## [0.12.0] — 2026-03-01

### Added
- **Downloadable runners in SelectRunnerDialog** — the runner picker now
  shows a second "Available to Download" group listing every runner in the
  bottlesdevs/components index that is not yet installed.  Clicking
  **Download** on any row opens `InstallRunnerDialog`, and on completion the
  runner is automatically selected.

### Changed
- **Runner "Change" button hidden for non-installed apps** — the Change
  button in the Wine Components group now only appears when the app is
  actually installed (i.e. a bottle exists on disk).  Before install,
  the runner info is shown as a plain read-only row.

### Fixed
- **Runner change via `bottle.yml`** — switching runners previously called
  `bottles-cli edit -k Runner -v <name>`, which recent versions of
  bottles-cli do not support (`unrecognized arguments`).  Runner changes
  now write the `Runner` field directly to the bottle's `bottle.yml`, which
  is both more reliable and requires no subprocess.

---

## [0.11.2] — 2026-03-01

### Fixed
- **sys-wine runner detection** — `list_runners()` now merges three sources:
  1. Subdirectory names inside the Bottles `runners/` directory.
  2. `wine --version` output mapped to the `sys-wine-X.Y` naming Bottles uses.
  3. `bottle.yml` scan — any `sys-wine-*` runner already referenced by an
     existing bottle; catches version-string mismatches between bottle-create
     time and the current Wine version.
- **Flatpak Bottles sys-wine** — `_detect_system_wine()` now probes the Wine
  binary bundled *inside* the Flatpak at
  `/var/lib/flatpak/app/com.usebottles.bottles/current/active/files/bin/wine`
  (system-wide install) and the per-user equivalent.  `wine` on `$PATH` is
  never used for Flatpak Bottles.
- New helper `_wine_version_cmds(install)` encapsulates per-variant Wine
  binary selection and is independently tested.

---

## [0.11.1] — 2026-03-01

### Added
- **Runner compatibility check** — the detail view asynchronously lists
  runners installed in Bottles and shows an `Adw.Banner` warning when the
  required runner is missing.  The banner offers either **Download** (if the
  runner is in the bottlesdevs/components index) or **Choose Runner** (if
  not, to pick an already-installed alternative).
- **InstallRunnerDialog** (`cellar/views/install_runner.py`) — two-phase
  dialog (confirmation → progress) that stream-downloads a runner tarball,
  verifies SHA-256, extracts it to the Bottles `runners/` directory.
  Cancel supported at any point.
- **SelectRunnerDialog** (inline in `cellar/views/detail.py`) — radio rows
  for every installed Bottles runner; an optional **Download original
  runner…** button when the built-with runner is in the index.
- **Runner override row** — the Wine Components group shows the runner as an
  interactive row with a **Change** button, so the user can switch runners at
  any time for installed apps.
- **Runner override persistence** — the chosen runner is stored in the SQLite
  `installed` table (`runner_override TEXT` column, additive `ALTER TABLE`
  migration) and applied immediately by writing `bottle.yml`.
- **`cellar/backend/components.py`** — new module; clones
  `bottlesdevs/components` on first run and pulls on subsequent startups
  (using `dulwich`, pure-Python git).  Public API: `sync_index()`,
  `is_available()`, `get_runner_info()`, `list_available_runners()`.
- **`list_runners(install)`** and **`runners_dir(install)`** added to
  `cellar/backend/bottles.py`: reads the Bottles `runners/` directory
  directly.
- **`get_runner_override()`** and **`set_runner_override()`** added to
  `cellar/backend/database.py`.
- **`dulwich`** added to `pyproject.toml` dependencies.

---

## [0.11.0] — 2026-03-01

### Added
- **Open / Trash buttons** — when an app is installed the primary action
  button becomes **Open**; a `user-trash-symbolic` destructive button
  appears beside it for uninstalling, matching GNOME Software's layout.
- **Smart Open flow** — reads `External_Programs` from `bottle.yml` and
  picks the right launch path automatically: 0 programs → entry_point or
  Bottles GUI; 1 program → direct launch; 2+ programs →
  `LaunchProgramDialog`.
- **LaunchProgramDialog** — small picker with radio rows (name + executable),
  Cancel / Open buttons in the header bar.
- **`launch_bottle()`** in `bottles.py` — handles all four
  sandbox × variant combinations; `-p` for registered External_Programs
  entries, `-e` for a raw `entry_point`.
- **`read_bottle_programs()`** in `bottles.py` — parses `External_Programs`
  from `bottle.yml` via `yaml.safe_load`; filters out `removed: true`
  entries.

### Changed
- **PyYAML adopted** for all `bottle.yml` parsing.
- **Source selector** — hidden when only one repo carries an entry; a
  `GtkMenuButton` popover with radio rows for multi-repo entries.
- **Action buttons** and source selector fixed at 105 × 34 px for a
  consistent right-column layout.
- **Settings redesign** — each configured repo is shown as a plain
  `ActionRow` (name + URI) with a pencil edit button and a trash remove
  button; **Add Repository** button lives in the group header suffix;
  `AddEditRepoDialog` for Name / URI / Token / SSL / CA cert upfront.

### Fixed
- **`bottles-cli` bottle name** — `launch_bottle()` now reads the `Name`
  field from `bottle.yml` and passes it as `-b`; the directory name could
  differ from the display name Bottles expects.
- **`bottles-cli run` flags** — corrected to `-p <name>` for registered
  External_Programs, `-e <path>` for a raw entry_point; the non-existent
  `-a` flag was removed.
- **Bottle directory name preserved from archive** — the installer uses the
  verbatim directory name from the archive (with a numeric suffix only on
  collision) instead of a slug derived from the catalogue ID; slug-based
  names caused `bottles-cli` program lookup failures.
- **Cross-machine `bottle.yml` path rewriting** — Linux absolute paths in
  `External_Programs` are rewritten to the actual install location after
  extraction; Windows-format paths (`C:\…`) are left untouched.

---

## [0.10.1] — 2026-02-28

### Fixed
- **403 Forbidden** now shows a clear error message with a hint to check the
  token and server configuration; previously shown as generic "Could Not
  Connect".
- **SSL AKI requirement** — "Missing Authority Key Identifier" with valid
  home CA certs resolved by clearing `X509_V_FLAG_X509_STRICT` when a
  user-supplied CA cert is provided; full chain validation still runs.
- **HTTPS image loading** — `Repo.resolve_asset_uri` now downloads image
  assets (png, jpg, gif, webp, svg, avif, ico) to a per-session temp cache
  and returns local paths so GdkPixbuf can load them without auth headers.
- **Stale installed entries** — bottle directories removed outside Cellar are
  now pruned from the DB on every catalogue load; they no longer appear on
  the Installed and Updates tabs.
- **Install/Remove state in same session** — `_installed_record` is now
  populated immediately after a successful install so Remove works without
  navigating away.
- **Package size** — `archive_size` is now written to `catalogue.json` when
  adding a package via the Add App dialog.

---

## [0.10.0] — 2026-02-28

### Added
- **Bearer token authentication for HTTP/HTTPS repos** — generate a 64-char
  random token in Settings → Access Control → Generate; stored per-repo in
  `config.json`; sent as `Authorization: Bearer` on every HTTP request
  (catalogue fetch, image download, archive download).
- **CA certificate support** — pick a `.crt` / `.pem` / `.cer` file when
  adding an HTTPS repo; copied to `~/.local/share/cellar/certs/`; full chain
  validation still applies, but the `X509_V_FLAG_X509_STRICT` AKI requirement
  is relaxed for home CAs.
- **SSL verification bypass** — destructive opt-in for networks where the CA
  cert is unobtainable; stored as `ssl_verify: false` in `config.json`.
- **Access Control group** in Settings — **Generate** button creates and
  displays a 64-char token for configuring the web server side.
- **ICO icon support** — `.ico` files display correctly in the browse grid
  and detail view; `new_from_file_at_size` picks the closest frame.
- **`User-Agent: Mozilla/5.0 (compatible; Cellar/1.0)`** on all outbound
  HTTP requests (avoids Cloudflare and CDN bot-protection blocks).

---

## [0.9.1] — 2026-02-28

### Fixed
- **Critical: Remove button deleted all bottles** — when `bottle_name` was
  empty, `Path(data_path) / ""` resolved to the Bottles data root itself,
  causing `shutil.rmtree` to wipe every bottle.  Now guards against empty
  `bottle_name` and refuses to delete the data root.
- **Card sizing** — `_FixedBox` now has `__gtype_name__ = "CellarFixedBox"`
  so PyGObject actually calls the `do_measure` override; all cards are
  strictly 300 × 96 px.
- **Install progress bar** — single bar with a phase label that switches from
  Downloading → Verifying → Installing; dialog resized to 360 × ~200 px.

---

## [0.9.0] — 2026-02-27

### Added
- **Explore / Installed / Updates view switcher** — `AdwViewSwitcher` +
  `AdwViewStack` replacing the window title.  The **Updates** tab shows a
  badge count.
- **Horizontal 300 × 96 app cards** — cover thumbnail (64 × 96, 2:3 ratio)
  or 48 px icon on the left; name + two-line summary on the right.
- **Screenshot carousel** — prev / next navigation arrows that fade in on
  hover; fullscreen viewer dialog.
- **Blue Updates badge** using `@accent_bg_color`; bundled GNOME Software
  tab icons (`software-explore-symbolic`, `-installed-`, `-updates-`) CC0-1.0.
- Search applies across all three views.

---

## [0.8.1] — 2026-02-27

### Fixed
- GVFS mount failure messages containing "no such file" no longer trigger the
  "Initialise repository?" dialog; the `"mount"` substring short-circuits to
  a "Could Not Connect" error instead.
- `Gtk.MountOperation(parent=self)` `TypeError` in `Adw.PreferencesDialog`
  fixed by using `get_root()`.
- SMB/NFS images (icon, cover, screenshots) now display correctly via the
  GVFS FUSE path returned by `Gio.File.get_path()`.

---

## [0.8.0] — 2026-02-26

### Added
- **SMB/NFS auto-mount with credential dialog** — `_GioFetcher` catches
  `NOT_MOUNTED` and calls `Gio.File.mount_enclosing_volume()` before
  retrying.  `Gtk.MountOperation` drives GNOME's standard credential dialog;
  credentials are stored in GNOME Keyring via libsecret.
- **Remote repo initialisation** for SMB/NFS (`gio_makedirs` +
  `gio_write_bytes`) and SSH (`ssh mkdir -p` + `ssh cat`).
- **`cellar/utils/gio_io.py`** — `gio_makedirs()` and `gio_write_bytes()`
  GIO write helpers.
- **Custom categories** — "Custom…" sentinel in the Add/Edit combo reveals an
  `AdwEntryRow`; on save, the new category is appended to the top-level
  `categories` array in `catalogue.json` and appears in all future combos.
- **Multi-repo Add App** — when more than one writable repo is configured, a
  "Repository" combo row lets the user choose the destination.
- **SMB/NFS install support** — `installer.py` uses the GVFS FUSE path
  (primary) or streams via `Gio.File.read()` (fallback).

---

## [0.7.1] — 2026-02-26

### Fixed
- Removed invalid `--no-delete` rsync flag (does not exist; rsync preserves
  destination-only files by default and `--delete` is the opt-in to remove
  them).

---

## [0.7.0] — 2026-02-26

### Added
- **Safe update flow** — **Update** button appears in the detail view when
  the installed version differs from the catalogue version.
- **`cellar/backend/updater.py`** — `update_app_safe`: optional backup →
  download → SHA-256 verify → extract → rsync overlay.  The overlay applies
  the new archive without `--delete`, excluding `drive_c/users/*/AppData/`,
  `Documents/`, `user.reg`, and `userdef.reg`.
- **`backup_bottle`** — tars the existing bottle with per-file progress and
  cancel support; destination chosen via `Gtk.FileChooserNative`.
- **`UpdateDialog`** (`cellar/views/update_app.py`) — two-phase dialog
  (confirm → progress); shows current → new version and a backup row.

---

## [0.6.1] — 2026-02-26

### Fixed
- Native Bottles detection now requires `bottles-cli` to be on `$PATH`
  (via `shutil.which`) in addition to the data directory existing; a stale
  `~/.local/share/bottles/bottles/` directory no longer triggers false
  detection.

---

## [0.6.0] — 2026-02-26

### Added
- **Bottles installation detection** (`cellar/backend/bottles.py`) —
  `BottlesInstall` dataclass (`data_path`, `variant`, `cli_cmd`); supports
  Flatpak Bottles, native Bottles, and custom override path.  Cellar sandbox
  detection (`/.flatpak-info`) adds `flatpak-spawn --host` prefix
  automatically.
- **Full install flow** (`cellar/backend/installer.py`) — download → SHA-256
  verify → extract → import to Bottles data directory.  `InstallProgressDialog`
  (confirm + progress phases, Cancel throughout).  When both Flatpak and
  native Bottles are present the user picks the target.
- **Remove/uninstall** — `Adw.AlertDialog` confirmation; `shutil.rmtree` on
  the bottle directory; DB record removed; Install button restored.
- **Local SQLite database** (`cellar/backend/database.py`) — tracks installed
  apps, bottle names, and versions at
  `~/.local/share/cellar/cellar.db`.
- Cover image in the browse grid; image quality improvements (`_FixedBox`
  exact allocation, HYPER pre-scaling for cover art).

---

## [0.5.0] — 2026-02-25

### Added
- **Add App dialog** (`cellar/views/add_app.py`) — picks a `.tar.gz` Bottles
  backup, auto-extracts `bottle.yml` to pre-fill metadata, shows a progress
  view during import.  App ID auto-generated via `slugify()` and locks on
  manual edit.
- **Edit App dialog** (`cellar/views/edit_app.py`) — edits existing catalogue
  entries; per-item screenshot management; Danger Zone for deleting the entry
  (with optional archive move).
- **`cellar/backend/packager.py`** — `import_to_repo`, `update_in_repo`,
  `remove_from_repo`; `read_bottle_yml` extracts the YAML from a `.tar.gz`
  with streaming progress.
- `AdwToastOverlay` in the main window for non-blocking notifications.

---

## [0.4.0] — 2026-02-25

### Added
- **Settings dialog** (`cellar/views/settings.py`) — `AdwPreferencesDialog`
  with repo management: add by URI (validates + fetches on add), remove,
  initialise empty repo for new locations.  About dialog wired to
  `app.about`.
- **`cellar/backend/config.py`** — persists repo list to
  `~/.local/share/cellar/config.json`.
- **Detail view** (`cellar/views/detail.py`) — full app page on card
  activation: hero banner, icon, name, byline, category/content-rating chips,
  description, screenshots carousel (`AdwCarousel`), Details / Wine
  Components / Package info groups.
- `AdwNavigationView` navigation — card activation pushes an
  `AdwNavigationPage`; Back button provided automatically.

---

## [0.3.0] — 2026-02-25

### Added
- **Multi-transport repo backend** — `_LocalFetcher`, `_HttpFetcher`
  (urllib), `_SshFetcher` (system `ssh` subprocess), `_GioFetcher`
  (SMB/NFS via GIO/GVFS).
- `Repo.is_writable` — `False` for HTTP/HTTPS, `True` for all other
  transports.
- `cellar/utils/gio_io.py` — `gio_read_bytes()` and `gio_file_exists()`.

### Changed
- **Fat `catalogue.json`** — all metadata lives in one file; per-app
  `manifest.json` files removed.  Top-level wrapper:
  `{"cellar_version": 1, "generated_at": "…", "apps": […]}`.
- `AppEntry` is now the single unified model for browse, detail, and install
  configuration.
- `Repo.resolve_path()` renamed to `Repo.resolve_asset_uri()`.

---

## [0.2.0] — 2026-02-18

### Added
- Browse UI (`cellar/views/browse.py`) — scrolling `GtkFlowBox` grid of app
  cards with `.card` Adwaita styling.
- Horizontal category filter strip — linked `GtkToggleButton` pills with
  radio behaviour.
- `GtkSearchBar` wired to the header bar search toggle; typing anywhere
  opens it automatically via `set_key_capture_widget`.
- Empty and error states via `AdwStatusPage`.

---

## [0.1.0] — 2026-02-18

### Added
- Initial project scaffold: `pyproject.toml`, `meson.build`, Flatpak manifest
  skeleton.
- Repo backend (`cellar/backend/repo.py`) — parses local `catalogue.json`;
  `AppEntry` dataclass model; `RepoManager` for merging multiple sources
  (last-repo-wins).
- `Repo` supports bare local paths and `file://` URIs.
- Test fixtures with sample apps; full test suite for local repo operations.
- `cellar/utils/paths.py` — resolves UI files from the source tree or the
  installed location; no build step needed during development.

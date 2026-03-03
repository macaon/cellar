# Changelog

All notable changes to Cellar are documented here.

## [0.23.0] — 2026-03-03

### Added
- **Delta install size reflects unique content** — for delta packages the
  "Install size" shown in the detail view now reports only the uncompressed
  size of files unique to the app (i.e. what the delta actually contributes),
  not the full bottle size including hardlinked base files.
  ``create_delta_archive`` returns the uncompressed delta size after diffing
  and ``AddAppDialog`` uses it to override ``install_size_estimate``.
- **zstd compression for delta archives** — delta archives are now written as
  `.tar.zst` (zstd level 3) instead of `.tar.gz`.  Full archives from Bottles
  are still copied verbatim as `.tar.gz` (no recompression).  The installer
  auto-detects the format by extension and decompresses via the `zstandard`
  Python package (added as a dependency), keeping everything self-contained
  with no subprocess dependency on the `zstd` binary.  Level 3 compresses
  faster than gzip while achieving a noticeably better ratio and decompress
  speed.

### Fixed
- **SMB/NFS download progress** — archives served via GVFS FUSE paths
  (e.g. `/run/user/1000/gvfs/smb-share:…`) were treated as local files,
  making the download phase appear instant while all network I/O happened
  during "Verifying download…" with no speed or progress display.  GVFS paths
  are now stream-copied to a temp file with full download progress and speed
  stats, matching the HTTP download experience.

### Changed
- **Base image download progress** — `InstallBaseFromRepoDialog` now shows
  proper multi-phase progress (download with MB/total/speed stats, verify,
  extract) matching the package install dialog, instead of a crude 0-50-100%
  split that sat on "Verifying" for the entire download.
- **Settings: Delta Base Images simplified** — removed the repo-scanning
  background thread and "Download" buttons from the base images section.
  Settings now only shows locally installed bases with a trash button for
  removal.  Base downloads are triggered contextually from the Add App dialog.
- **Add App dialog prompts for base download** — when uploading a package
  whose runner has a matching base in the repo but it is not installed locally,
  the delta status row now shows a "Download" button instead of blocking with
  a "go to Settings" message.  Clicking it opens the download dialog inline;
  on success the delta check re-runs and enables delta packaging automatically.

## [0.21.0] — 2026-03-03

### Added
- **Settings shows available base images from repos** — the Delta Base Images
  section now fetches bases from all configured repositories in the background
  and shows bases available for download alongside locally installed ones.
  Each available base gets a "Download" button that downloads, verifies, and
  installs the base image locally with a progress dialog.
- `Repo.ssl_verify` and `Repo.ca_cert` properties for accessing SSL settings
  from outside the fetcher layer (needed by the base download dialog).

## [0.20.9] — 2026-03-03

### Fixed
- **Delta archive creation now correctly excludes base files** — `_compute_delta`
  previously used rsync `--compare-dest` (which relies on mtime comparison) and
  a Python size-only fallback, both of which could include files that are
  byte-for-byte identical to the base.  The root cause: a base installed on one
  day and an app bottle created on another day share identical Windows system
  DLLs but with different mtimes, so rsync treated them as changed.  Both paths
  are replaced by a single BLAKE2b-128 content-hash comparison — a file is
  excluded from the delta only when a file at the same relative path in the base
  has identical bytes, regardless of timestamps or size.

## [0.20.8] — 2026-03-03

### Changed
- **Delta bases keyed by runner, not Windows version** — different Wine runners
  (`soda-9.0-1`, `ge-proton10-32`, `sys-wine`, …) each ship completely different
  `system32` DLL sets, so matching on Windows version produced useless deltas
  when the base and app bottles used different runners.  The base image system
  is now keyed by runner name throughout:
  - `AppEntry.base_win_ver` → `AppEntry.base_runner`
  - `BaseEntry.win_ver` → `BaseEntry.runner`
  - `bases` map in `catalogue.json` keyed by runner name (e.g. `"soda-9.0-1"`)
  - `_WIN_VER_LABELS` removed — runner names are already human-readable
  - `UploadBaseDialog` scans `Runner:` field instead of `Windows:`
  - Database migration renames `bases.win_ver` column to `bases.runner`
  - Backwards-compatible: `AppEntry.from_dict` still reads old `base_win_ver`

## [0.20.7] — 2026-03-02

### Fixed
- **Delta install no longer seeds superfluous base files** — `_compute_delta`
  now writes a `.cellar_delete` manifest into the delta archive listing every
  file that existed in the base image but was absent from the app backup (e.g.
  Windows setup temp files cleaned up during app installation).  `_overlay_delta`
  reads this manifest after seeding and removes the listed paths, so the
  installed bottle matches the original backup exactly rather than being inflated
  by base-only files.
- Added `_compute_delta_python` and `_overlay_delta_python` helpers so the
  rsync and Python paths are cleanly separated and the delete-manifest logic
  runs unconditionally regardless of which copy strategy was used.

## [0.20.6] — 2026-03-02

### Changed
- **Much faster archive scan** — `read_bottle_yml` now tries the system `tar`
  binary first (`tar -xOf --wildcards '*/bottle.yml'`), which uses C-speed gzip
  and stops reading as soon as the member is found.  Falls back to Python
  `tarfile` iteration when `tar` is not on PATH (Flatpak sandbox etc.).
- **Indeterminate progress bar** during archive scan in both the Add App and
  Upload Base Image dialogs — the old deterministic bar was misleading (jumped
  to 100% immediately due to gzip block buffering); a pulsing bar correctly
  signals "working" without implying a known fraction.

## [0.20.5] — 2026-03-02

### Changed
- Remove duplicate "Upload Base Image…" button from the empty-state row;
  the header "Add" button is the single entry point, matching Repositories.

## [0.20.4] — 2026-03-02

### Changed
- **Delta Base Images group layout** — removed the verbose description from the
  group header so the "Add" button is compact and normal-height (matching the
  Repositories group).  The empty-state row now shows an "Upload Base Image…"
  `suggested-action` button as its suffix, giving a clear call-to-action inline
  rather than relying on the header button alone.

## [0.20.3] — 2026-03-02

### Changed
- **Updater delta path** — `update_app_safe()` now accepts `base_entry` and
  `base_archive_uri` keyword args (API parity with `install_app`).  For delta
  packages the rsync overlay strategy is used unchanged: the delta archive
  contains only changed/new files and overlaying them directly on the existing
  bottle is correct (unchanged files stay, user data is preserved via the
  existing no-delete + exclusion rules).  No base reconstruction is needed.
- `_on_update_clicked` in `DetailView` resolves `base_entry` /
  `base_archive_uri` from source repos and passes them to `UpdateDialog`.
- `UpdateDialog` threads the base params through to `update_app_safe()`.

## [0.20.2] — 2026-03-02

### Changed
- **Install progress pulsing for delta phase** — `InstallProgressDialog` now
  triggers an indeterminate progress-bar pulse for "Applying delta…" phases
  in addition to the existing "Copying…" pulse, so the bar moves during rsync
  overlay operations.
- `_proceed_to_install()` in `DetailView` resolves `base_entry` and
  `base_archive_uri` from the entry's source repos and threads them through
  `InstallProgressDialog` → `install_app()`, so the "Downloading base image…" /
  "Verifying base image…" / "Installing base image…" phase labels are shown
  when a delta install needs to auto-download its base image.

## [0.20.1] — 2026-03-02

### Added
- **Delta Base Images group in Preferences** — new `AdwPreferencesGroup` lists
  all locally installed base images with their Windows version label, install date,
  and a remove button.  The "Upload Base Image…" button opens a new
  `UploadBaseDialog` that scans the selected archive for the `Windows:` field,
  installs the base locally, and optionally copies the archive to a writable
  repository (updating `catalogue.json` with CRC32 and size).  Remove
  confirms before deleting.
- `window.py` passes `writable_repos` to `SettingsDialog` so the upload dialog
  can show the repo selector only when writable repos are available.

## [0.20.0] — 2026-03-02

### Added
- **Delta archive creation in Add App dialog** — when a repo contains a base image
  for the bottle's Windows version, the Add App dialog now creates a delta archive
  automatically: only files that differ from the base are stored in the repo.
  Uses `rsync --checksum --compare-dest` for accurate content diffing; falls back
  to a Python size-based implementation when rsync is unavailable.
- `create_delta_archive(full_archive, base_dir, dest)` in `packager.py` — new
  helper that extracts a full backup, diffs against a local base directory, and
  produces a minimal delta `.tar.gz`.
- Delta status row in the Add App dialog — a new row in the Archive group shows
  whether delta packaging will be used for the current archive, with a warning and
  a disabled Add button if the required base is not installed locally.

## [0.19.11] — 2026-03-02

### Fixed
- **Browse view auto-refreshes after install, update, or remove** — the catalogue
  was previously only refreshed on manual refresh button click.  Install badges,
  the Updates tab count, and the Installed tab now update automatically as soon
  as each operation completes.

## [0.19.10] — 2026-03-02

### Changed
- **Zero layout shift on cached detail views** — `Repo.peek_asset_cache()` checks
  whether an image asset is already on disk without triggering a download.  All
  three asset loaders (`_make_hero`, `_make_icon`, `_make_screenshots`) now take
  a fast synchronous path when all images are cached, building the full page in
  one pass with no placeholders and no reflow.  The async placeholder path is
  kept as a fallback only for the very first open, when a network fetch is
  actually required.

## [0.19.9] — 2026-03-02

### Changed
- **Asset cache extended to SSH and SMB/NFS repos** — the persistent image
  cache (`~/.cache/cellar/assets/`) now applies to all non-local transports.
  SSH and SMB repos previously re-fetched images on every detail-view open;
  they now benefit from the same cache-hit fast path as HTTP(S) repos.
  Local repos are still served directly from disk and remain uncached.

## [0.19.8] — 2026-03-02

### Changed
- **Detail view opens instantly** — hero banner, app icon, and screenshots are
  now loaded asynchronously on background threads.  The page appears immediately
  with a generic icon placeholder and an empty screenshot area; each image fades
  in (150 ms crossfade for the icon) as it finishes downloading and decoding.
  Eliminates the multi-second freeze when opening an app detail page over a
  slow network connection.
- **Persistent asset cache** — HTTP(S) image assets (hero, cover, screenshots,
  icons) are cached to `~/.cache/cellar/assets/<repo-hash>/` instead of a
  per-session temp directory.  Re-opening the same app on subsequent visits
  shows images instantly from disk.  The cache is automatically invalidated
  when the repo's `catalogue.json` `generated_at` field changes.

## [0.19.7] — 2026-03-02

### Fixed
- **Desktop shortcut uses bottle display name** — the `-b` argument in the
  generated `Exec=` line now uses the bottle's `Name` field from `bottle.yml`
  (e.g. `Adobe Photoshop CC 2018`) rather than the directory name
  (e.g. `Adobe-Photoshop-CC-2018`).  bottles-cli matches by display name, so
  directory names with dashes instead of spaces were causing "Bottle not found"
  errors on launch.

## [0.19.6] — 2026-03-02

### Fixed
- **Desktop shortcut icon rendering** — icons are now stored at 512×512 (matching
  Bottles) using aspect-ratio scaling with upscaling support (`resize()` replaces
  `thumbnail()` which was shrink-only).  Transparent padding is cropped before
  scaling so the logo fills the canvas.  Icon filenames include a CRC32 of the
  processed PNG bytes (e.g. `inaudible_a3f2c1d4.png`), mirroring
  `xdg-desktop-portal`, so GNOME Shell always loads the latest icon without a
  shell restart.  `update-desktop-database` is called after every create/remove
  so the app grid picks up changes immediately.

## [0.19.5] — 2026-03-02

### Fixed
- **Desktop shortcut icon now displays immediately** — `Icon=` now contains the
  absolute path to `~/.local/share/icons/cellar/<id>.png` instead of a theme
  name, eliminating the need for an icon-cache refresh.  Icon storage moved
  from `~/.local/share/icons/hicolor/256x256/apps/` to the simpler flat
  directory `~/.local/share/icons/cellar/`.  Cleanup on shortcut or app
  removal covers the new path.

## [0.19.4] — 2026-03-02

### Added
- **Desktop shortcut creation** — a gear icon button now appears between Open
  and the trash button for installed apps.  Clicking it opens a popover menu
  with "Create Desktop Shortcut" / "Remove Desktop Shortcut".  Shortcuts are
  written to `~/.local/share/applications/cellar-<id>.desktop` and the app
  icon is copied (and converted to 256×256 PNG via Pillow) into
  `~/.local/share/icons/hicolor/256x256/apps/`.  The correct `bottles-cli` or
  `flatpak run --command=bottles-cli` exec line is generated automatically
  based on the detected Bottles variant.  Removing an app also silently cleans
  up any existing shortcut and icon.  New `cellar/utils/desktop.py` module.

## [0.19.3] — 2026-03-02

### Fixed
- **Non-square raster icons no longer squished** — `load_and_fit` now scales
  images uniformly to fit within the target square and centers them on a
  transparent canvas (letterbox/pillarbox), instead of center-cropping to a
  square first.  Covers and hero images (via `load_and_crop`) are unaffected —
  they still scale-to-fill as intended.

## [0.19.2] — 2026-03-02

### Fixed
- **SVG icons now display** in the browse grid and detail view.  Pillow has no
  SVG support, so `.svg` files were silently falling back to the generic
  placeholder icon.  `load_and_fit` and `load_and_crop` now detect `.svg` by
  extension and rasterise via `GdkPixbuf` (librsvg) before trying Pillow.
  Two new tests cover `load_and_fit` and `load_and_crop` with a real SVG file.

## [0.19.1] — 2026-03-02

### Fixed
- **Stay on detail page after editing** — saving changes in the Edit Catalogue
  Entry dialog no longer pops back to the browse grid.  The detail view is
  rebuilt in place with the updated `AppEntry` (hero, icon, name, etc. all
  refresh immediately); the browse grids are reloaded in the background.

## [0.19.0] — 2026-03-02

### Added
- **Upload size and speed display** — progress bars in the Add App and Edit
  Catalogue Entry dialogs now show `copied / total (speed)` text during the
  archive copy phase (e.g. `256 MB / 4.2 GB (1.3 GB/s)`).  Text is cleared
  automatically when the phase switches to "Writing catalogue…".
  `packager.py` gains `phase_cb(label)` and `stats_cb(copied, total, speed_bps)`
  parameters on both `import_to_repo` and `update_in_repo`.

### Changed
- **Better upload phase labels** — the archive copy phase now shows "Copying
  archive…" and the catalogue-write phase shows "Writing catalogue…", replacing
  the old fraction-threshold hack that changed the label at 90% progress.
- **Upload progress page layout** — margins and spacing tightened to match the
  download progress pages (12 px top/bottom, spacing 6); progress bars have
  `set_size_request(0, -1)` so the dialog width stays stable.

### Fixed
- **GTK focus assertion** (`gtk_list_box_row_grab_focus: assertion 'box != NULL'
  failed`) — the screenshot list in Edit Catalogue Entry now hides the ListBox
  before removing rows so GTK does not try to move keyboard focus to a row that
  is being unparented.

## [0.18.0] — 2026-03-02

### Added
- **Download size and speed display** — progress bars during package and runner
  downloads now show `downloaded / total (speed)` text (e.g.
  `2.6 MB / 349 MB (1.3 MB/s)`) instead of a bare percentage.  Text is cleared
  automatically when the phase switches to Verifying, Extracting, or Copying.
  Speed is computed as a running average from download start.  `installer.py`
  gains a `download_stats_cb(downloaded, total, speed_bps)` parameter;
  `_download_and_extract_runner` in `install_runner.py` gains the same.
- **Filename display during extraction** — the progress bar text shows the
  name of the file currently being extracted (e.g. `ntdll.dll`), updating at
  up to 12 fps (throttled to avoid flooding the main loop for archives with
  thousands of members).  Works for both package and runner extraction.
  `_extract_archive` in `installer.py` gains an `extract_name_cb(filename)`
  parameter; `_download_and_extract_runner` gains `name_cb`.

## [0.17.0] — 2026-03-02

### Changed
- **Streamlined install flow** — clicking Install on an app with a missing
  runner no longer opens the full `RunnerManagerDialog` as a separate step.
  Instead, `InstallProgressDialog` now shows a unified confirmation page with
  a Runner group ("Will be downloaded" + Change button) above the Bottles
  picker.  The flow is: confirm → download runner → install package, all in
  one dialog.  When no runner download is needed and only one Bottles install
  exists, the confirm page is skipped entirely and installation starts
  immediately.  The Change button still opens `RunnerManagerDialog` for
  picking an alternative; selecting an already-installed runner skips the
  download phase.

## [0.16.1] — 2026-03-02

### Fixed
- **Runner picker UX** — in picker mode, radio buttons are now enabled on all
  runners (installed and uninstalled) so any runner can be selected. Download
  arrow buttons are hidden in picker mode to avoid bypassing the selection flow.
  The required runner is pre-selected by default. Clicking Select on an
  uninstalled runner triggers the download flow automatically.
- **Runner install labels** — `InstallRunnerDialog` phase labels now read
  "Downloading runner…" and "Extracting runner…" for clarity.

## [0.16.0] — 2026-03-02

### Changed
- **Multi-phase install progress bars** — `InstallProgressDialog` now shows
  distinct phases (Downloading → Verifying → Extracting → Copying → Finishing),
  each resetting the bar to 0 → 100%.  The "Copying to Bottles" phase uses an
  indeterminate pulse since `shutil.copytree` does not report per-file progress.
  Eliminates the stalls at ~7%, ~40%, and ~70% caused by the verify and copy
  steps sharing a single bar with no progress feedback.
- **Multi-phase runner install progress** — `InstallRunnerDialog` now uses two
  distinct 0 → 100% phases (Downloading, Extracting) with per-member extract
  progress, replacing the previous 0–80%/80–100% split.
- **CRC32 replaces SHA-256** for archive integrity verification — ~4× faster on
  multi-gigabyte files.  `archive_sha256` field renamed to `archive_crc32` in
  `catalogue.json` and `AppEntry`; old `archive_sha256` keys in existing
  catalogues are read transparently for backward compatibility.
- **CRC32 computed on upload** — `import_to_repo` and `update_in_repo` now
  compute CRC32 inline during the chunked archive copy and write it to
  `catalogue.json`.  Previously the `archive_crc32` field was always empty
  for newly added apps, silently skipping verification on install.

### Fixed
- **urllib3 InsecureRequestWarning on startup** — suppressed the noisy
  "Unverified HTTPS request" warnings emitted by dulwich when `~/.gitconfig`
  has `http.sslVerify = false` (common on home-lab setups with a local proxy).

## [0.15.0] — 2026-03-01

### Added
- **Auto-discover programs via `bottles-cli --json programs`** — the Open
  button now finds all programs available in a bottle, including those
  auto-discovered by Bottles from `.lnk` shortcuts.  Previously only manually
  registered `External_Programs` from `bottle.yml` were shown.  New
  `list_bottle_programs()` calls `bottles-cli --json programs -b <name>` for
  structured JSON output (name, executable, path, auto_discovered flag) and
  falls back to `bottle.yml` parsing when `bottles-cli` is unavailable.

## [0.14.0] — 2026-03-01

### Changed
- **Pillow replaces GdkPixbuf for image handling** — all image loading,
  resizing, cropping, and ICO conversion now uses Pillow instead of GdkPixbuf.
  Fixes ICO files with BMP-encoded frames that the hand-rolled parser silently
  skipped.  New `cellar/utils/images.py` module with `load_and_crop`,
  `load_and_fit`, `optimize_image`, and `to_texture` helpers.
- **requests replaces urllib** — all HTTP(S) requests now go through
  `requests.Session` via the new `cellar/utils/http.py` session factory.
  User-Agent, bearer token, and SSL settings (verify/CA cert) are configured
  once per session.  Fixes archive downloads (`installer.py`) and runner
  downloads (`install_runner.py`) ignoring repo SSL settings.

### Removed
- `cellar/utils/checksum.py` — empty placeholder file.
- `_USER_AGENT` constant duplication between `repo.py` and `install_runner.py`.
- `_ico_to_png()` and `_optimize_image()` from `packager.py` (replaced by
  `cellar.utils.images.optimize_image`).
- `_load_cover_texture()` and `_load_icon_texture()` from `browse.py`
  (replaced by `cellar.utils.images` helpers).

## [0.13.4] — 2026-03-01

### Added
- **ICO → PNG conversion at import** — ICO files uploaded as icons are
  automatically converted to PNG (extracting the largest embedded frame,
  typically 256 px) when imported via Add App or Edit App.  This works around
  GdkPixbuf lacking an ICO loader on most Linux systems.

### Fixed
- **ICO icon display** — icons stored as `.ico` no longer silently fall back to
  the cover image or gear placeholder.  The packager now writes PNG, and the
  catalogue references the `.png` path.

## [0.13.3] — 2026-03-01

### Fixed
- **ICO icon rendering** — detail view now uses GdkPixbuf to load icons,
  picking the largest embedded frame from ICO files (typically 256 px) instead
  of the first/smallest one.  Fixes blurry icons when using ICO files from
  SteamGridDB.

## [0.13.2] — 2026-03-01

### Changed
- **Cover as icon fallback** — the detail view now shows the cover image
  (center-cropped to a square) next to the app title when no icon is available,
  instead of the generic gear icon.

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

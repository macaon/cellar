# Changelog

All notable changes to Cellar are documented here.

---

## [0.12.23] — 2026-02-28

### Fixed
- Add-to-Catalogue dialog width reduced from 560 to 360 to match the Install
  progress dialog.

---

## [0.12.22] — 2026-02-28

### Changed
- **Repo rows are now editable in-place** — existing repository rows in
  Settings are now `AdwEntryRow` widgets pre-filled with the URI, matching
  the add-repo row. Click the field, edit the URI, press Enter to save. Token
  and SSL status are shown via prefix icons; the "Token…" button remains as a
  suffix. The separate pencil-button-and-dialog approach is removed.

---

## [0.12.21] — 2026-02-28

### Changed
- **Reverted SMB/NFS transport changes** — Rolling back v0.12.19 and v0.12.20.
  The `smbclient`-based fetcher introduced credential-friction, a stdin hang
  on launch, and ambiguous write-capability detection (the `+` button was
  always visible for SMB repos regardless of actual permissions). The simpler
  GIO/GVFS approach from v0.12.18 is restored: `_GioFetcher` handles SMB and
  NFS reads, `utils/gio_io.py` is back, and `Gtk.MountOperation` / `mount_op`
  are re-wired through `Repo.__init__` and `window.py`. NFS support returns.

---

## [0.12.20] — 2026-02-28

### Changed
- **SMB reads no longer create a GVFS mount** — The previous `_GioFetcher`
  called `mount_enclosing_volume()` on every catalogue load, leaving a
  persistent mount entry in the Files app sidebar (reported by testers).
  Catalogue fetches, image assets, and archive downloads now use `smbclient`
  directly (samba-client package), which never creates a mount point.
  Write operations (add / edit / remove app via the packager) still use GIO
  to obtain a GVFS FUSE path, but the mount is created lazily — only when
  a write operation is actually triggered, not during browsing.
- **NFS support dropped** — NFS always requires a kernel-level mount (no
  smbclient equivalent). Anyone using NFS can expose the share via Samba,
  HTTP, or SSH instead.
- **`_GioFetcher` and `utils/gio_io.py` removed** — GIO is now only used in
  the thin `_smb_writable_path()` helper (for write operations) and is no
  longer part of the regular read path.
- **`mount_op` / `Gtk.MountOperation` removed** from `Repo.__init__` and
  `window.py` — not needed for the smbclient read path.
- **SMB authentication** — embed credentials in the URI:
  `smb://user:password@host/share`. The password travels via the `PASSWD`
  environment variable so it does not appear in `ps` output. When no password
  is given, a null/guest session is attempted (`--no-pass`).

### Fixed
- **smbclient never hangs waiting for a password** — `stdin` is now
  `DEVNULL` on all smbclient subprocess calls; previously, if the share
  required a password, the app would block indefinitely on the terminal
  password prompt.
- **SMB error messages now include smbclient stdout** — smbclient sometimes
  writes errors to stdout rather than stderr; both are now included in the
  reported error so the root cause is always visible.

---

## [0.12.18] — 2026-02-28

### Added
- **ICO icon support** — `.ico` files are now displayed correctly in the
  browse grid and detail view. `new_from_file_at_size` replaces
  `new_from_file` in both texture loaders so GdkPixbuf's ICO loader picks
  the frame closest to the target size rather than an arbitrary one. `.ico`
  is added to `_IMAGE_EXTENSIONS` so ICO assets are fetched through the
  auth-aware cache on HTTP(S) repos.

---

## [0.12.17] — 2026-02-28

### Fixed
- **Images not loading from authenticated HTTPS repos** — `GdkPixbuf` cannot
  pass an `Authorization` header when loading from an `http://` URL, and
  `os.path.isfile()` always returns `False` for HTTP URLs (filtering out
  hero images and screenshots in the detail view). `Repo.resolve_asset_uri`
  now downloads image assets (png, jpg, gif, webp, svg, avif) through the
  auth-aware Python fetcher into a per-session temporary cache directory and
  returns the local path. Archives are still returned as URLs so the
  installer's own token-aware downloader handles them.

---

## [0.12.16] — 2026-02-28

### Fixed
- **Cloudflare (and other CDN/WAF) blocking HTTP requests with 403** —
  Python's default `User-Agent: Python-urllib/3.x` is blocked by Cloudflare's
  bot protection before the request ever reaches the origin server, causing a
  silent 403. All outbound HTTP(S) requests (catalogue fetches and archive
  downloads) now send `User-Agent: Mozilla/5.0 (compatible; Cellar/1.0)`.

---

## [0.12.15] — 2026-02-28

### Fixed
- **README nginx example corrected again** — previous example used
  `root /srv/cellar` which still doubles the path segment. The correct
  rule is that `root` is prepended to the full request URI: with files
  at `/cellar/` use `root /`, with files at `/srv/data/cellar/` use
  `root /srv/data`, etc. Example and explanation updated accordingly.

---

## [0.12.14] — 2026-02-28

### Fixed
- **Generate button removed from the "Access token" input field** — token
  generation belongs in the Access Control section only; the input field is
  for pasting a token received from a repo owner.
- **403 Forbidden now shows a clear error** instead of the generic "Could Not
  Connect" message, with a hint to check the token and server configuration.
- **nginx example in README corrected** — using `alias` together with `if`
  inside a location block is a known nginx bug that returns 403 even when
  auth passes. The example now uses `root` instead, which works correctly.

---

## [0.12.13] — 2026-02-28

### Changed
- **Bearer token UX reworked** — token setup is now discoverable without
  triggering a 401 first.  The Repositories group gains a permanent
  "Access token (optional)" field below the URI row, with a generate button
  (↻) that fills it and copies to clipboard. A new **Access Control** group
  explains the concept and has a **Generate** button that shows the token in
  a dialog for copying to your web server config. Works for both HTTP and
  HTTPS repos. 401 responses now show clear inline alerts: "enter the token
  below and try again" (no token) or "token rejected, check your config"
  (wrong token) — no more mid-flow dialogs.

---

## [0.12.12] — 2026-02-28

### Added
- **Bearer token authentication for HTTPS repos** — when adding an HTTPS repo
  returns 401, Cellar now prompts for a bearer token. A **Generate** button
  creates a 64-character random token and copies it to the clipboard so you
  can paste it straight into your web server config. The token is stored in
  `config.json` alongside the repo and included in every HTTP request
  (`Authorization: Bearer …`), including archive downloads during install and
  update. Existing repos with a token show a masked "Access Token" row in
  Settings with a **Change…** button.
- **nginx and Caddy config examples in README** — shows exactly how to
  configure a `map`-based bearer token check in nginx and an equivalent Caddy
  block so users can restrict access to their repo with minimal effort.

---

## [0.12.11] — 2026-02-28

### Fixed
- **Preferences dialog too small** — `content_height=-1` collapsed the dialog
  because `Adw.PreferencesDialog` wraps content in a scrolled window whose
  natural height is near zero. Changed to an explicit `content_height=500` which
  gives a usable minimum without over-constraining the dialog.

---

## [0.12.10] — 2026-02-28

### Changed
- **Preferences dialog now auto-sizes** — `content_height=-1` lets the window
  grow vertically to fit its content rather than staying at a fixed small size.
  Width set to 560 px to comfortably accommodate long repository URIs.
- **Repo rows use `Adw.ExpanderRow` when SSL info is present** — instead of
  cramming the CA cert name or SSL warning into a one-line subtitle, repos with
  a custom CA cert or disabled verification expand to reveal a child row showing
  the certificate filename (with a shield icon) or a "disabled" warning. Plain
  repos without SSL overrides remain simple `Adw.ActionRow`s.

---

## [0.12.9] — 2026-02-28

### Fixed
- **"Missing Authority Key Identifier" with valid home CA certs** — Python 3.10+
  with OpenSSL 3.x sets `X509_V_FLAG_X509_STRICT` in the default SSL context,
  making the AKI certificate extension mandatory. Many home/self-signed CAs omit
  it; curl and browsers don't enforce it. Cellar now clears this flag when using
  a user-supplied CA cert so full chain validation (hostname, expiry, trust
  anchor) still runs, but the AKI requirement is relaxed.

---

## [0.12.8] — 2026-02-28

### Changed
- **CA certificates are now copied into `~/.local/share/cellar/certs/`** rather
  than referenced by their original path. `config.json` stores only the
  filename; `window.py` resolves it via `certs_dir()` at startup. This means
  the cert remains available even if the source file is moved or deleted, and
  the entire Cellar config directory is self-contained.

---

## [0.12.7] — 2026-02-28

### Added
- **CA certificate support for HTTPS repos** — when adding an HTTPS repo fails
  with an SSL error, the dialog now offers two options: "Add CA Certificate…"
  (opens a file chooser for `.crt`/`.pem`/`.cer`, validates the connection
  before saving) and "Disable Verification" (destructive, for networks where
  you cannot obtain the CA cert). The `ca_cert` path is stored in `config.json`
  and used for all subsequent fetches and archive downloads from that repo.
  Repo rows in Settings show the CA filename or "SSL verification disabled" as
  a subtitle hint when either option is active.

---

## [0.12.6] — 2026-02-28

### Fixed
- **HTTP repo add showed "No Catalogue Found" for all errors** — any failure
  on a non-writable repo (SSL error, auth failure, network timeout) was
  swallowed and shown as the generic read-only message instead of the real
  error. Now only 404-style errors show "No Catalogue Found"; everything else
  shows "Could Not Connect" with the actual message.

### Added
- **SSL certificate bypass for HTTPS repos** — when adding an HTTPS repo fails
  with an SSL/certificate error (common for self-signed or private-CA certs on
  home servers), a dialog now offers "Add Without Verification" which saves
  `ssl_verify: false` to `config.json` for that repo. All subsequent fetches
  and downloads skip certificate validation for that repo only.

---

## [0.12.5] — 2026-02-28

### Fixed
- **Installed tab shows stale entries** — catalogue load only checked the DB,
  never the filesystem. Apps removed from within Bottles or manually would
  stay on the Installed tab forever. Now reconciles against disk on every
  catalogue load: missing bottle directories are pruned from the DB and
  excluded from the Installed and Updates tabs immediately.

---

## [0.12.4] — 2026-02-28

### Fixed
- **Remove broken after install in same session** — `_installed_record` was
  never populated after a successful install, so clicking Remove without
  navigating away always found an empty `bottle_name` and refused to delete.
  Now `_on_install_success` sets `_installed_record = {"bottle_name": …}`
  immediately.

---

## [0.12.3] — 2026-02-28

### Changed
- **Install progress**: single progress bar with label that switches from
  "Downloading" to "Installing". Bar resets to 0% when the phase changes.
  Installer API now takes separate `download_cb` and `install_cb` callbacks
  instead of a combined `progress_cb(phase, fraction)`.

---

## [0.12.2] — 2026-02-28

### Fixed
- **Extraction stall at 0%**: `tarfile.getmembers()` was scanning the entire
  compressed archive before extracting anything, causing the progress bar to
  sit at "Installing… 0%" for the whole duration of large archives. Now
  iterates members incrementally and tracks compressed bytes consumed
  (`raw.tell()`) for smooth progress.

---

## [0.12.1] — 2026-02-28

### Fixed
- **SMB/NFS download progress**: GVFS FUSE paths were returned as "local",
  skipping the download phase entirely (bar jumped to 100% then stalled).
  Now stream-copies through the FUSE mount with real per-chunk progress.

### Changed
- **Install dialog**: replaced two progress bars with a single bar whose label
  changes between phases (Downloading → Verifying → Installing). Shrunk the
  dialog from 420 × ~300 px to 360 × ~200 px.

---

## [0.12.0] — 2026-02-28

### Fixed
- **CRITICAL: Remove deleting all bottles** — when the installed record had no
  `bottle_name` (empty string), `Path(data_path) / ""` resolved to the Bottles
  data root itself, causing `shutil.rmtree` to wipe every bottle. Now guards
  against empty `bottle_name` and adds a safety check refusing to delete the
  data root directory.

---

## [0.11.31] — 2026-02-28

### Changed
- **Install progress**: split single progress bar into two separate indicators —
  one for Downloading and one for Installing (extract + copy). Each phase
  reports its own 0–100% independently, so local installs no longer show a
  misleading jump and remote downloads get a dedicated bar.

---

## [0.11.30] — 2026-02-28

### Changed
- **Screenshot carousel spacing**: added 12 px gap between screenshots in both
  the inline carousel and the fullscreen dialog so page boundaries are visible.

---

## [0.11.29] — 2026-02-28

### Added
- **Screenshot carousel navigation arrows**: hovering over the screenshot
  carousel reveals prev/next arrow buttons (`.osd` + `.circular`) that fade
  in/out with a 150 ms CSS transition. First/last page hides the
  corresponding arrow.
- **Fullscreen screenshot dialog**: clicking any screenshot opens an
  `Adw.Dialog` with a full-size carousel viewer (1000×700), its own
  navigation arrows, and indicator dots.

### Fixed
- **Scroll conflict resolved**: set `allow_scroll_wheel=False` on the inline
  screenshot carousel so the mousewheel scrolls the page instead of advancing
  screenshots.

---

## [0.11.28] — 2026-02-28

### Changed
- **Blue updates badge**: the Updates tab badge now uses `@accent_bg_color`
  (GNOME's blue accent) instead of the default grey, matching GNOME Software.

---

## [0.11.27] — 2026-02-28

### Fixed
- **Card margin fix**: Right hand card margin was set at 12px for text content,
  while the left hand side had 6px padding before the image, and anoter 12px
  margin added to the image call. Right hand margin was set to 18px to compensate.

---

## [0.11.26] — 2026-02-27

### Changed
- **Two-line summary in app cards**: summary label now wraps to a second
  line (word/char wrap) and ellipsises only if the text exceeds two lines,
  matching GNOME Software's card layout.

---

## [0.11.25] — 2026-02-27

### Fixed
- **Uniform card width**: the `do_measure` override on `AppCard`
  (`Gtk.FlowBoxChild`) was silently ignored by PyGObject because the subclass
  lacked a `__gtype_name__`; card widths were still content-driven. Fixed by
  wrapping the card `Gtk.Box` in a `_FixedBox(_CARD_WIDTH, _CARD_HEIGHT)`
  (which *does* have `__gtype_name__ = "CellarFixedBox"` and a working
  `do_measure`). All cards are now strictly 300 × 96 px and long titles
  are ellipsised at the correct point.

---

## [0.11.24] — 2026-02-27

### Changed
- **GNOME Software-style horizontal cards**: replaced the portrait capsule
  grid with fixed 300 × 96 px horizontal cards. Each card shows a 64 × 96
  cover thumbnail (exact 2:3 Steam ratio) on the left when available,
  falling back to a 48 px app icon centred in the column. The right side
  shows the app name (bold) and summary (dimmed), both single-line with
  ellipsis. Cards are strictly uniform — `do_measure` returns exactly
  300 × 96 for every card regardless of content.
- **Capsule size preference removed**: cards are now fixed-size, so the
  Appearance section (and the underlying config helpers) are no longer
  needed and have been removed from Settings and `config.py`.

---

## [0.11.23] — 2026-02-27

### Fixed
- **Cards always the same width**: `AppCard` now overrides `do_measure` to
  report exactly `cover_width` for the horizontal dimension, the same pattern
  used by `_FixedBox` for the image area. Previously a label whose natural
  pixel width exceeded `cover_width` (possible at larger font sizes) would
  silently widen its card, producing an uneven grid.

### Changed
- **Capsule sizes rescaled to Steam spec**: sizes are now clean fractions of
  Steam's 600 × 900 capsule (2:3 portrait ratio) —
  Small 150 × 225 (¼), Medium 200 × 300 (⅓, new default), Large 300 × 450 (½).
  The old "compact / standard" keys are replaced by "small / medium / large".
  Icon sizes and spacing scale proportionally (`cover_width × ⅔`).

---

## [0.11.22] — 2026-02-27

### Fixed
- **App cards stay fixed size on resize**: `GtkFlowBox` was set to
  `homogeneous=True`, causing GTK to divide the full row width equally among
  columns and stretch cards as the window widened. Switching to
  `homogeneous=False` lets each card keep its natural width (as reported by
  `_FixedBox`). `halign=CENTER` on the FlowBox centres packed rows so partial
  rows don't pin to the left edge.

---

## [0.11.21] — 2026-02-27

### Fixed
- **View switcher icons**: bundle the three GNOME Software symbolic SVGs
  (`software-explore-symbolic`, `software-installed-symbolic`,
  `software-updates-symbolic`) under `data/icons/hicolor/symbolic/apps/` and
  register the search path with GTK's icon theme at startup, so the icons
  display correctly without GNOME Software being installed. Icons are CC0-1.0
  (GNOME Foundation) with `fill="currentColor"` for proper theme recolouring.

---

## [0.11.20] — 2026-02-27

### Changed
- **View switcher icons**: use GNOME Software's own icon set —
  `software-explore-symbolic`, `software-installed-symbolic`,
  `software-updates-symbolic` — matching the upstream visual style exactly.
  These icons ship with GNOME Software (GPL-2.0+, compatible with our
  GPL-3.0+) and will fall back gracefully on systems where they are absent.

---

## [0.11.19] — 2026-02-27

### Added
- **Explore / Installed / Updates view switcher**: the window title is replaced
  by an `AdwViewSwitcher` with three tabs, matching the GNOME Software layout.
  - **Explore** shows the full catalogue with the category strip and search.
  - **Installed** shows only apps that are recorded in the local database.
  - **Updates** shows apps whose installed version differs from the catalogue
    version; the tab displays a badge with the pending update count.
  - Search and capsule-size changes apply across all three views.

### Removed
- **Category-strip separator**: the thin horizontal rule between the category
  pills and the app grid has been removed; margins provide sufficient spacing.

---

## [0.11.18] — 2026-02-27

### Changed
- **Header bar**: search toggle button moved to the far left (start side), matching
  the GNOME Software layout.
- **Search bar**: entry is no longer full-width; constrained to ~40 characters
  (`max-width-chars=40`) and centred by `GtkSearchBar`, giving a narrower,
  more focused look consistent with GNOME Software.

---

## [0.11.17] — 2026-02-27

### Added
- **Detail view — version in byline**: the app version is now appended to the
  "Developer · Publisher · Year" byline using the same `·` separator.
- **Detail view — conditional Details group**: the "Details" preferences group
  is now hidden entirely when none of its fields (developer, publisher, release
  year, languages, content rating, tags, website, store links) are populated,
  keeping the page clean for minimal catalogue entries.

---

## [0.11.16] — 2026-02-27

### Fixed
- **`cellar/window.py`**: after saving an edited catalogue entry the nav stack
  now pops back to the browse grid automatically, so the updated icon (or any
  other change) is visible immediately without manually pressing Back.
  Previously `on_done` was wired directly to `_load_catalogue`, which rebuilt
  the grid in the background while leaving the user on the detail page.

---

## [0.11.15] — 2026-02-27

### Added
- ICO and SVG accepted in all image pickers (Add App and Edit App dialogs).
  GdkPixbuf handles ICO natively and SVG via librsvg (standard on GNOME).

---

## [0.11.14] — 2026-02-27

### Added
- **Multi-repo support for Add App**: when more than one writable repository is
  configured, the Add App form now shows a "Repository" combo row so the user
  can choose which repo to add the package to. Single-repo setups are unchanged.
  `AddAppDialog` now accepts `repos` (list) instead of a single `repo`.
  `window.py` collects all writable repos after each catalogue reload and passes
  them through; repo names from config are now forwarded to `Repo()` so the
  combo shows human-readable labels when available.

### Fixed
- **Images (icon, cover, screenshots) not displayed for SMB/NFS repos**:
  `_GioFetcher.resolve_uri` was returning the raw `smb://` URI, which
  `os.path.isfile()` and `GdkPixbuf` cannot use directly. It now calls
  `Gio.File.get_path()` first to get the GVFS FUSE path (a regular filesystem
  path under `/run/user/…/gvfs/`), which works transparently since the share is
  already mounted from the catalogue fetch.
- **Uploaded app icons too wide in capsule view**: icon PNGs were scaled to the
  full card width (`cover_width × cover_width`) and displayed with
  `ContentFit.CONTAIN`, filling the entire card. Now scaled to
  `cover_width × 2 // 3` and displayed with `ContentFit.SCALE_DOWN` (never
  enlarged beyond natural size), so the icon floats centred with padding on all
  sides — matching the existing fallback icon behaviour.

---

## [0.11.13] — 2026-02-27

### Added
- **`cellar/views/add_app.py`**: "Reading archive…" progress bar shown while
  `bottle.yml` is scanned from the backup. The dialog now opens on a scan page
  (progress bar, 0 → 100%) and transitions to the metadata form automatically
  when the read completes.

### Fixed
- **`cellar/backend/packager.py`**: `read_bottle_yml` previously called
  `tf.getmembers()`, which reads every member in the archive before searching —
  causing the full multi-GB stream to be decompressed even though `bottle.yml`
  is typically one of the first entries. Switched to iterating `tf` directly so
  the scan stops as soon as `bottle.yml` is found.
- `read_bottle_yml` now accepts an optional `progress_cb(fraction)` and uses a
  `_ProgressFileObj` wrapper to track compressed bytes read, giving an accurate
  fraction even when the file is found early.

---

## [0.11.12] — 2026-02-27

### Fixed
- **`cellar/views/add_app.py`**: opening the Add App dialog on a large archive
  froze the UI long enough to trigger the GNOME "force quit?" timeout dialog.
  `_prefill()` called `read_bottle_yml()` synchronously in `__init__`, blocking
  the main thread while the entire gzip stream was seeked to find `bottle.yml`.
  It now runs on a daemon background thread (matching the pattern already used
  in `edit_app.py`), with all UI updates posted back via `GLib.idle_add`.

---

## [0.11.11] — 2026-02-27

### Fixed
- **`cellar/backend/packager.py`**: importing or editing an app in an SMB/NFS
  repo failed with `[Errno 95] Operation not supported` when copying images
  (icon, cover, hero, screenshots) to the GVFS FUSE mount. `shutil.copy2`
  copies file metadata including timestamps via `copystat`/`utimes`, which
  GVFS FUSE does not support. Replaced all four call sites with
  `shutil.copyfile`, which copies only the file content.

---

## [0.11.10] — 2026-02-27

### Fixed
- **`cellar/backend/installer.py`**: progress bar jumped immediately to 55% and
  then stalled for the entire extraction phase. `_extract_archive` now iterates
  tar members one at a time and reports progress proportional to uncompressed
  bytes extracted, filling the 55–70% band smoothly. Previously it called
  `tarfile.extractall()` as a single blocking call with no feedback. The
  immediate jump was also caused by the download step being instant for
  local/SMB/NFS repos (file used in-place), so the first visible update was
  the hardcoded 55% at extraction start.

---

## [0.11.9] — 2026-02-27

### Removed
- `"Other"` from `BASE_CATEGORIES` — a lazy catch-all that discouraged proper categorisation.
  Existing entries whose `category` is `"Other"` will surface in the custom entry row when
  edited, prompting the user to assign a real category.

---

## [0.11.8] — 2026-02-27

### Added
- **Custom categories** in the Add App and Edit App dialogs.
  - The "Category" combo shows built-in categories (`Games`, `Productivity`,
    `Graphics`, `Utility`, `Other`) plus any custom ones stored in the repo,
    then a **"Custom…"** sentinel at the end.
  - Selecting "Custom…" reveals an `AdwEntryRow` below the combo and moves
    focus to it automatically.
  - On save, a custom category that isn't built-in is appended to a top-level
    `categories` array in `catalogue.json`; it appears as a first-class item
    in the combo on the next open.
  - Save/Add button requires both Name and a non-empty Category.
- **`cellar/backend/packager.py`**: `BASE_CATEGORIES` constant;
  `add_catalogue_category(repo_root, category)`; catalogue r/w helpers now
  preserve the top-level `categories` key on every rewrite.
- **`cellar/backend/repo.py`**: `Repo.fetch_categories()` returns
  `BASE_CATEGORIES` merged with stored custom categories, base-first.

---

## [0.11.7] — 2026-02-26

### Removed
- **Pre-install component selection UI** (v0.11.6) — reverted. DXVK and VKD3D
  are bundled inside the bottle prefix rather than installed to the Bottles
  components directory, so the directory scan reliably returned empty lists.
  Runner detection also proved unreliable in practice. Bottles itself warns about
  missing or mismatched runners when the user tries to launch an app, which is
  sufficient. The `InstalledComponents` dataclass, `list_installed_components()`,
  and `launch_bottles()` additions from v0.11.6 are removed; `InstallProgressDialog`
  returns to its v0.11.5 state.

---

## [0.11.6] — 2026-02-26

### Added
- Pre-install component selection UI (subsequently removed in v0.11.7).

---

## [0.11.5] — 2026-02-26

### Fixed
- **`cellar/backend/installer.py`**: installing an app from an SMB or NFS repository raised "Downloading from 'smb' repos is not yet supported". `_acquire_archive` now handles `smb://` and `nfs://` URIs:
  1. **FUSE path (primary)**: calls `Gio.File.get_path()` on the URI — the share is already mounted from the catalogue fetch, so `gvfsd-fuse` returns a local filesystem path and the archive is used in-place with no copy (same behaviour as a local repo).
  2. **GIO InputStream (fallback)**: if `gvfsd-fuse` is unavailable, streams the archive to a temp file via `Gio.File.read()` + `read_bytes()` in 1 MB chunks with cancel support. GIO synchronous I/O is thread-safe and designed for background threads.
- `updater.py` reuses `_acquire_archive` from `installer.py`, so the update flow gains SMB/NFS support automatically.
- The unsupported-scheme error message updated to list SMB and NFS as supported.

---

## [0.11.4] — 2026-02-26

### Fixed
- **`cellar/views/add_app.py`** and **`cellar/views/edit_app.py`**: "Add to Catalogue" and all edit/delete operations crashed with `RepoError: local_path() is only available for local repos` when the active repository was an SMB or NFS share.

### Added
- **`cellar/backend/repo.py`**: `Repo.writable_path(rel_path="")` — like `local_path()` but also works for SMB/NFS repos. GVFS exposes every mounted network share through `gvfsd-fuse` at `/run/user/<uid>/gvfs/`; `Gio.File.get_path()` returns that FUSE path transparently once the share is mounted. `packager.py` then operates on it via ordinary `pathlib.Path` with no GIO-specific changes. Raises `RepoError` for HTTP/SSH repos or when `gvfsd-fuse` is unavailable.
- All four `local_path()` call sites in `add_app.py` and `edit_app.py` updated to `writable_path()`.

---

## [0.11.3] — 2026-02-26

### Fixed
- **`cellar/views/settings.py`**: GVFS mount failures (e.g. "Failed to mount Windows share: No such file or directory") contain the phrase "no such file", causing `_looks_like_missing` to return `True` and incorrectly triggering the "Initialise repository?" dialog. The heuristic now short-circuits to `False` when the error string contains "mount", so genuine connectivity failures reach the "Could Not Connect" alert instead.

### Changed
- **`cellar/views/settings.py`**: the "No Catalogue Found" init dialog body now varies by URI scheme — for remote repositories (SMB, NFS, SSH) it adds a note that the directory will be created on the server if it does not already exist, making the intent of the Initialise action clear.

---

## [0.11.2] — 2026-02-26

### Added
- **Remote repo initialisation** — the "Initialise repository here?" dialog now works for SMB, NFS, and SSH locations, not just local paths.
  - **SMB / NFS** (`_init_gio_repo`): uses two new GIO write helpers — `gio_makedirs(uri)` creates the directory tree via `Gio.File.make_directory_with_parents()`, and `gio_write_bytes(uri, data)` writes the file via `Gio.File.replace()`. Both apply the same mount-and-retry logic as the read path (nested `GLib.MainLoop`, `Gtk.MountOperation` for credential prompts, `ALREADY_MOUNTED` silently ignored).
  - **SSH** (`_init_ssh_repo`): two sequential `subprocess` calls — `ssh … mkdir -p <path>` to create the directory, then `ssh … cat > <catalogue_path>` with the JSON piped to stdin. Uses `BatchMode=yes` consistent with the SSH fetcher (key-based auth required). Clear error dialogs for missing `ssh` binary, timeouts, and non-zero exits.
  - `_on_init_response` now dispatches on URI scheme (local / smb / nfs / ssh) instead of the local-or-bail pattern.
  - `_empty_catalogue()` module-level helper extracted from the duplicated inline dict.
- **`cellar/utils/gio_io.py`**: two new public functions:
  - `gio_makedirs(uri, *, mount_op=None)` — `mkdir -p` over GIO; `EXISTS` is not an error; mounts first if `NOT_MOUNTED`.
  - `gio_write_bytes(uri, data, *, mount_op=None)` — create-or-replace write over GIO; mounts first if `NOT_MOUNTED`.
  - `_ensure_mounted(gfile, mount_op)` — internal helper factored out of `_GioFetcher` to avoid duplication; shared by both new write helpers.

---

## [0.11.1] — 2026-02-26

### Fixed
- **`cellar/views/settings.py`**: `Gtk.MountOperation(parent=self)` raised `TypeError` because `Adw.PreferencesDialog` is not a `GtkWindow`. Fixed by using `self.get_root()` to walk up the widget tree to the enclosing window; falls back to `None` if the root is not a `GtkWindow` (mount dialog still works, just unparented).

---

## [0.11.0] — 2026-02-26

### Added
- **SMB/NFS auto-mount with credential prompt** — `smb://` and `nfs://` repositories now work when the share is not yet mounted by GVFS.
  - `_GioFetcher.fetch_bytes` catches `Gio.IOErrorEnum.NOT_MOUNTED` and calls `Gio.File.mount_enclosing_volume()` before retrying. Mounting blocks via a nested `GLib.MainLoop`, which keeps GTK event processing alive so any credential dialog can be displayed and interacted with.
  - `_GioFetcher._ensure_mounted()` — new helper that drives the async mount callback and translates GLib errors into `RepoError`. `ALREADY_MOUNTED` is silently ignored; `FAILED_HANDLED` (user cancelled the dialog) surfaces as "Authentication cancelled by user".
  - `_GioFetcher`, `_make_fetcher`, and `Repo.__init__` now accept an optional `mount_op` object (`Gio.MountOperation` or any subclass). The UI layer passes a `Gtk.MountOperation(parent=window)`, which renders GNOME's standard credential dialog (username, password, domain, workgroup) and — when the user ticks "Remember Password" — stores the credential in the GNOME Keyring via libsecret. Subsequent mounts retrieve the saved credential automatically with no prompt.
  - `cellar/window.py`: a single `Gtk.MountOperation(parent=self)` is created per catalogue-load pass and forwarded to every `Repo()` constructed in that pass.
  - `cellar/views/settings.py`: `Gtk.MountOperation(parent=self)` is passed to the `Repo` created during the "Add repository" validation step, so credential prompts appear immediately when the user first enters an SMB/NFS URI.

---

## [0.10.1] — 2026-02-26

### Fixed
- **`cellar/backend/updater.py`**: remove invalid `--no-delete` rsync flag. rsync preserves destination-only files by default; `--delete` is the opt-in to remove them. `--no-delete` does not exist and caused rsync to exit with code 1.

---

## [0.10.0] — 2026-02-26

### Added
- **Safe update flow** — when the catalogue version differs from the installed version and an archive is present, an **Update** button (suggested-action) appears in the detail-view header alongside the existing Remove button.
  - `cellar/backend/updater.py` (previously a stub) now implements `update_app_safe`: optional backup → download → SHA-256 verify → extract → overlay. The overlay applies the new archive over the existing bottle without `--delete`, skipping `drive_c/users/*/AppData/{Roaming,Local,LocalLow}/`, `drive_c/users/*/Documents/`, `user.reg`, and `userdef.reg`. Uses `rsync` when available; falls back to a pure-Python file-copy loop.
  - `backup_bottle` tars the existing bottle (preserving the top-level directory name) with per-file progress reporting and cancel support.
  - `cellar/views/update_app.py` — new `UpdateDialog` (two-phase `Adw.Dialog`): confirm page shows current → new version, a backup row (clicking it opens a `Gtk.FileChooserNative` save dialog so the user picks exactly where the backup lands), and a data-safety warning. Progress page shows phase label + progress bar + Cancel.
  - `cellar/views/detail.py` — `DetailView` gains `on_update_done` parameter; `_has_update` is computed from `installed_record.installed_version` vs `entry.version`; `_update_btn` is added to the header and hidden when not applicable.
  - `cellar/window.py` — `_on_update_done` callback updates the DB record with the new version and shows a success toast.

---

## [0.9.1] — 2026-02-26

### Fixed
- **`cellar/views/browse.py`**: icon cards now fill the full card width and render sharply. Previously the icon was capped at 64 px (`_ICON_SIZE_MAX`) and displayed as a tiny `Gtk.Image` centred in the much-larger `_FixedBox`, causing visible blur and wasted space. The new `_load_icon_texture` helper HYPER-pre-scales the icon to `cover_width × cover_width` (center-cropping non-square images); the texture is placed in a `Gtk.Picture` with `ContentFit.CONTAIN` inside the `_FixedBox`. Because the texture is already exactly `cover_width` wide and the box is `cover_width × cover_height`, GTK renders it 1:1 with no interpolation pass. The generic themed-icon fallback also scales to `cover_width * 2 // 3`.

---

## [0.9.0] — 2026-02-26

### Added
- **Remove/uninstall button in detail view**: when Cellar detects that an app's bottle is present on disk, the header bar now shows a "Remove" button (destructive style) instead of the inert "Installed" badge.
  - Clicking "Remove" opens an `Adw.AlertDialog` that names the bottle directory and warns that all data inside the prefix (saved games, registry, configuration) will be permanently deleted.
  - On confirmation: the bottle directory is deleted with `shutil.rmtree`, the database record is removed via `database.remove_installed`, the button reverts to "Install", and a toast confirms the removal.
  - `DetailView` gains two new optional constructor parameters: `installed_record` (the dict from `database.get_installed`) and `on_remove_done` (callback invoked after a successful removal).

---

## [0.8.8] — 2026-02-26

### Fixed
- **`cellar/views/browse.py`**: eliminate "Finalizing CellarFixedBox but it still has children left" GTK warning. `_FixedBox` previously stored `self._child` as a Python-level strong reference, which could interfere with GTK's C-level reference counting and prevent `do_dispose` from properly unparenting the child before finalization. All child access is now via `get_first_child()` (GTK's own child list), removing the Python reference entirely.

---

## [0.8.7] — 2026-02-26

### Fixed
- **`cellar/views/browse.py`**: combine `_FixedBox` (exact allocation) with HYPER pre-scaling (quality downscaling) for truly sharp cover art on 1× displays. The texture is pre-scaled to exactly `cover_width × cover_height` with `GdkPixbuf.InterpType.HYPER`; `_FixedBox` guarantees the picture is allocated exactly that size; `ContentFit.FILL` renders it 1:1 with no GTK scaling pass at all.

---

## [0.8.6] — 2026-02-26

### Fixed
- **Image sharpness (HiDPI root cause)**: on displays with a 2× (or fractional) scale factor, pre-scaled software textures get upscaled by GTK after the fact, making images blurry regardless of the scaling algorithm used. The fix is to let GTK's own renderer handle image scaling so it operates at native display pixel density.
  - `cellar/views/browse.py`: new `_FixedBox` custom widget (`Gtk.Widget` subclass) overrides `do_measure` to always report `cover_width × cover_height` as its natural size, preventing the child image's pixel dimensions from leaking into `FlowBox` layout. The cover `Gtk.Picture` is loaded with `new_for_filename()` (full resolution) and GTK scales it to the allocated area at physical pixel density.
  - `cellar/views/detail.py`: screenshots reverted to `Gtk.Picture.new_for_filename()` (full resolution, GTK-managed scaling); the previous HYPER pre-scaling was actually worse on HiDPI because it created textures at logical pixel dimensions that GTK then upscaled.
  - `cellar/utils/image.py` removed (no longer needed).

---

## [0.8.5] — 2026-02-26

### Fixed
- **Image quality**: replace `GL_LINEAR` / `GdkPixbuf.BILINEAR` scaling with `GdkPixbuf.HYPER` (high-quality Gaussian/bicubic resampling) for cover art and screenshots. At the large downscale ratios typical of these images (4–6×) the difference is significant.
  - New `cellar/utils/image.py`: `load_cover_texture(path, w, h)` (scale-to-cover + center-crop) and `load_fit_texture(path, target_h)` (scale to height preserving aspect ratio)
  - `cellar/views/browse.py`: cover thumbnails now use `load_cover_texture` (HYPER)
  - `cellar/views/detail.py`: screenshot carousel now uses `load_fit_texture` (HYPER) instead of full-resolution `Gtk.Picture.new_for_filename()`; falls back to filename loading on error

---

## [0.8.4] — 2026-02-26

### Fixed
- **`cellar/views/browse.py`**: cover images no longer bleed outside the card's rounded corners — `Overflow.HIDDEN` is now set on the card box itself (which owns the `.card` border-radius) rather than only on the inner `img_area`
- **`cellar/views/browse.py`**: capsule title text reduced from `title-4` to `heading` for better proportion at both capsule sizes

---

## [0.8.3] — 2026-02-26

### Changed
- **`cellar/backend/config.py`**: capsule size options renamed from Small/Medium to **Compact/Standard** to avoid implying a truncated scale

---

## [0.8.2] — 2026-02-26

### Changed
- **`cellar/backend/config.py`**: removed Large (400 × 600) and Original (600 × 900) capsule size options — anything above Medium is impractically large; default capsule size on a fresh install is now Small (100 × 150) instead of Medium

---

## [0.8.1] — 2026-02-26

### Fixed
- **`cellar/views/browse.py`**: capsule size preference is now strictly enforced for all cards
  - Replaced `Gtk.Picture.new_for_filename()` + `set_size_request()` with a `_load_cover_texture()` helper that uses `GdkPixbuf` to scale-to-cover and center-crop images to exactly `cover_width × cover_height` before creating a `Gdk.Texture`. Because the texture's intrinsic pixel dimensions equal the target, `Gtk.Picture` reports the correct natural size to `FlowBox` and no longer expands to the source image's full resolution.
  - Both cover and no-cover cards now share a fixed-size `img_area` box (`set_size_request(cover_width, cover_height)` + `set_overflow(HIDDEN)`), so all cards are identical in height regardless of whether a cover image is present.
  - Summary text removed from cards (it is already shown in the detail view).

---

## [0.8.0] — 2026-02-26

### Added
- **Capsule size preference** — Preferences → General → "Capsule Size" combo row lets the user choose between Small (100 × 150), Medium (200 × 300), Large (400 × 600), and Original (600 × 900); persisted in `config.json`; grid rebuilds live without a network fetch when the setting changes
- **Enforced 2:3 portrait ratio** — `AppCard` now takes `cover_width` and derives `cover_height = cover_width * 3 // 2`; any image (including wide ones) is cropped uniformly via `Gtk.ContentFit.COVER`, so all capsules are the same shape regardless of source dimensions
- **`cellar/backend/config.py`**: `CAPSULE_SIZES`, `CAPSULE_SIZE_LABELS`, `load_capsule_size()`, `save_capsule_size()`
- **`cellar/views/browse.py`**: `BrowseView` now stores the entry list, resolver, and capsule width; `set_capsule_width(width)` rebuilds from stored state without re-fetching; `load_entries()` accepts an optional `capsule_width` argument
- **`cellar/views/settings.py`**: new "Appearance" preferences group; `on_capsule_size_changed` callback wired directly to `BrowseView.set_capsule_width`
- **`cellar/window.py`**: passes `capsule_width` from config to `load_entries()`; passes `set_capsule_width` as the live-update callback to `SettingsDialog`

---

## [0.7.9] — 2026-02-26

### Added
- **`cellar/views/browse.py`**: cover image now shown in the browse grid
  - `AppCard` accepts an optional `resolve_asset` callable; when `entry.cover` resolves to an existing local file the portrait image is shown at the top of the card (`Gtk.ContentFit.COVER`, fixed 200 px height)
  - When no cover is available the card falls back to the app icon (now properly loaded from disk via `Gdk.Texture`, with a generic `application-x-executable` fallback) followed by name and summary text
  - Summary text is hidden for cover-art cards to keep the layout compact
  - `BrowseView.load_entries()` accepts an optional `resolve_asset` parameter and forwards it to every `AppCard`
- **`cellar/window.py`**: passes `_first_repo.resolve_asset_uri` to `load_entries()` so images are resolved from the active repository

---

## [0.7.8] — 2026-02-26

### Fixed
- **`cellar/backend/bottles.py`**: native Bottles detection now requires `bottles-cli` to be on `$PATH` (via `shutil.which`) in addition to the data directory existing. Previously, a stale or unrelated `~/.local/share/bottles/bottles/` directory would cause Cellar to falsely report a native Bottles installation.
- Updated module docstring to explain the stricter native detection criteria.
- **`tests/test_bottles.py`**: all native-detection tests now mock `shutil.which`; added `test_detect_native_ignored_when_no_binary` and `test_detect_all_native_ignored_without_binary` (126 passing).

---

## [0.7.7] — 2026-02-26

### Added
- **`cellar/backend/bottles.py`**: `detect_all_bottles(override_path=None) -> list[BottlesInstall]` — returns every detected Bottles installation (Flatpak + native) in preference order; `detect_bottles()` is now a thin wrapper around it
- **`cellar/views/detail.py`**: `InstallProgressDialog` now has a two-phase flow:
  - **Confirm page** — shows the detected Bottles installation with its variant label and path; when both Flatpak and native Bottles are present, a radio-button list lets the user choose which to use; header has "Cancel" (start) and "Install" (end)
  - **Progress page** — existing progress bar + Cancel; header buttons are hidden, install runs with the user-selected `BottlesInstall`
- `DetailView` constructor param renamed `bottles_install` → `bottles_installs` (now accepts a list)
- **`cellar/window.py`**: reconciliation now checks all detected installations when testing whether a bottle directory still exists
- **`tests/test_bottles.py`**: 7 new tests for `detect_all_bottles` (empty, flatpak-only, native-only, both, override exclusivity, CLI commands, consistency with `detect_bottles`)

---

## [0.7.6] — 2026-02-26

### Added
- **`cellar/views/detail.py`**: Install button is now fully wired up
  - `DetailView` accepts `bottles_install`, `is_installed`, and `on_install_done` constructor params
  - Button shows **"Install"** (active, suggested-action) when Bottles is detected and the app is not yet installed
  - Button shows **"Installed"** (success style, insensitive) when already installed
  - Button is insensitive with tooltip "Bottles is not installed" when Bottles is not detected
  - Clicking Install opens the new `InstallProgressDialog` with a progress bar and Cancel button
  - On success, the button transitions to "Installed" and the `on_install_done` callback is invoked
- **`InstallProgressDialog`** (new class in `detail.py`): modal progress dialog for the install flow
  - Runs `install_app()` on a background thread; reports `(phase, fraction)` progress via `GLib.idle_add`
  - Cancel button (or any dialog dismissal) sets the cancel event for clean abort
  - Shows an `AdwAlertDialog` on error; closes quietly on cancellation
- **`cellar/window.py`**: `_on_app_selected` now detects Bottles, checks the DB, and passes both to `DetailView`
  - `_on_install_done` callback writes the DB record via `database.mark_installed` and shows an `AdwToast`

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

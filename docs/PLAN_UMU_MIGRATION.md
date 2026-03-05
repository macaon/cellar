# Cellar — umu-launcher Migration Plan

## Goal

Remove the Bottles dependency entirely for Windows app management. Replace it
with umu-launcher + Cellar-owned storage. Add a Package Builder tool so
maintainers can create, configure, and package WINEPREFIXes from within Cellar
itself. Add `steam_appid` to the data model to enable protonfixes via umu.

Users should never need to install anything beyond Cellar (and eventually just
the Cellar Flatpak). No Bottles, no separate umu install, no pip, no dnf.

---

## Current state (what exists)

- **Bottles** manages runners and bottle storage. Cellar installs apps into
  Bottles' data directory and launches via `bottles-cli`.
- **`backend/components.py`** syncs the `bottlesdevs/components` runner index
  via dulwich. Runner install dialog (`views/install_runner.py`) takes a
  `target_dir: Path` parameter — already caller-determined, not hardcoded.
- **`backend/installer.py`** extracts Bottles backup archives (which are
  WINEPREFIXes with a `bottle.yml` on top) into the Bottles data path.
- **`backend/bottles.py`** handles Bottles detection, bottles-cli invocation,
  bottle.yml reading, runner listing, and launch.
- **`backend/igdb.py`** and **`views/igdb_picker.py`** provide IGDB metadata
  lookup. `steam_appid` is not yet in the data model.
- **`database.py`** schema has `installed.bottle_name`, `installed.install_path`
  (added for Linux apps), `installed.platform`, `installed.runner_override`.
- **`models/app_entry.py`** `AppEntry` has `platform`, `entry_point`,
  `built_with.runner`, `store_links` — no `steam_appid` field.

---

## Target architecture

### Storage layout

```
~/.local/share/cellar/
  cellar.db
  config.json
  components/            ← dulwich clone of bottlesdevs/components (unchanged)
  runners/
    ge-proton10-32/      ← Cellar-owned; was Bottles' runners/ dir
    soda-9.0-1/
  prefixes/
    <app-id>/            ← one WINEPREFIX per installed app
  projects/
    <slug>/              ← Package Builder working area
      project.json
      prefix/            ← the WINEPREFIX being built
~/.cache/cellar/
  assets/                ← image cache (unchanged)
```

### Launch (umu-launcher)

```
WINEPREFIX=~/.local/share/cellar/prefixes/<id>
PROTONPATH=~/.local/share/cellar/runners/<runner>
GAMEID=umu-<steam_appid>   # or 0 if no steam_appid
EXE=drive_c/path/to/app.exe
umu-run
```

umu is either bundled in the Flatpak (for build/configure operations) or
invoked on the host via `flatpak-spawn --host` for game launch (same pattern
as the current Bottles flatpak-spawn handling).

---

## Archive format

**Bottles archives are still valid** — a Bottles bottle is a WINEPREFIX with
`bottle.yml` on top. umu ignores `bottle.yml`. Existing archives work as-is
for extraction; `bottle.yml` is simply not used. Rebuilding is optional but
recommended for cleanliness.

**New Cellar-native archives** (produced by the Package Builder) use a fixed
structure with no Bottles metadata:

```
prefix/              ← always this exact name as the single top-level dir
  drive_c/
  system.reg
  user.reg
  userdef.reg
```

`drive_c/users/` symlinks (which point to the original creator's home
directory) are **stripped before archiving** — replaced with empty directories.
umu/Proton recreates the correct symlinks on first launch automatically.

The installer identifies the archive as Cellar-native by the absence of
`bottle.yml` in the top-level directory. Both formats extract to
`~/.local/share/cellar/prefixes/<app-id>/`.

---

## Data model changes

### `AppEntry` (models/app_entry.py)

Add one field:

```python
steam_appid: int | None = None
```

Serialised as `"steam_appid": 12345` in `catalogue.json`. Omitted when `None`.
Used to set `GAMEID=umu-<steam_appid>` at launch; falls back to `GAMEID=0`.

### `database.py`

SQLite is retained. It provides atomic writes during install/uninstall
(important when an operation on a multi-GB prefix could be interrupted).
Drift between DB and filesystem is not a concern in practice — Cellar already
reconciles on directory scan and exposes a manual refresh. Cross-app queries
(e.g. "apps using runner X") hit the remote `catalogue.json`, not local state.

The old schema used Bottles-specific terminology (`bottle_name`,
`installed_version`) and accumulated columns via `ALTER TABLE` over time.
The new schema is created clean. A versioned migration handles existing
installs. This matters for code review (e.g. Flathub acceptance).

#### Schema versioning

Add a `schema_version` table as the standard versioning mechanism:

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
```

On every DB open: read `schema_version`, run any pending migrations in order,
update version. A fresh install gets the current schema directly (no
migrations). `_ensure_schema()` is replaced by `_open_db()` which handles
both paths.

#### Target schema (version 1)

```sql
CREATE TABLE schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE installed (
    id              TEXT PRIMARY KEY,
    prefix_dir      TEXT NOT NULL,       -- subdir under install_path; = app_id for umu apps
    platform        TEXT NOT NULL DEFAULT 'windows',  -- 'windows' | 'linux'
    version         TEXT,                -- installed catalogue version string
    runner          TEXT,                -- runner the prefix was built/installed with
    runner_override TEXT,                -- user-selected runner override (NULL = use runner)
    steam_appid     INTEGER,             -- umu GAMEID; NULL means GAMEID=0
    install_path    TEXT,                -- base dir containing prefix_dir
    repo_source     TEXT,                -- URI of the source repo
    installed_at    TEXT,                -- ISO-8601 UTC timestamp
    last_updated    TEXT                 -- ISO-8601 UTC timestamp
);

CREATE TABLE bases (
    runner       TEXT PRIMARY KEY,       -- e.g. "ge-proton10-32"
    repo_source  TEXT,
    installed_at TEXT                    -- ISO-8601 UTC timestamp
);
```

Changes from the old schema:
- `bottle_name` → `prefix_dir` (accurate name for the new architecture)
- `installed_version` → `version` (simpler)
- `runner` added as a first-class column (was only derivable from `bottle.yml`)
- `steam_appid` added
- `platform` promoted from `ALTER TABLE` addition to base schema column
- `install_path` promoted from `ALTER TABLE` addition to base schema column
- `TIMESTAMP` type annotation → `TEXT` (SQLite stores timestamps as text
  regardless; `TEXT` is honest about this)
- `bases.win_ver` is gone — the column was already renamed to `runner` in a
  previous migration; target schema uses `runner` throughout

#### Migration v0 → v1

Detected by: `schema_version` table absent **and** `installed` table has a
`bottle_name` column (confirmed via `PRAGMA table_info(installed)`).

```sql
-- 1. Migrate installed
CREATE TABLE installed_v1 (
    id              TEXT PRIMARY KEY,
    prefix_dir      TEXT NOT NULL,
    platform        TEXT NOT NULL DEFAULT 'windows',
    version         TEXT,
    runner          TEXT,
    runner_override TEXT,
    steam_appid     INTEGER,
    install_path    TEXT,
    repo_source     TEXT,
    installed_at    TEXT,
    last_updated    TEXT
);

INSERT INTO installed_v1
    (id, prefix_dir, platform, version, runner_override, install_path,
     repo_source, installed_at, last_updated)
SELECT
    id,
    bottle_name,                          -- prefix_dir = old bottle_name
    COALESCE(platform, 'windows'),
    installed_version,                    -- version = old installed_version
    runner_override,
    install_path,
    repo_source,
    installed_at,
    last_updated
FROM installed;
-- runner column left NULL: no reliable source in old schema (was in bottle.yml)
-- steam_appid column left NULL: did not exist

DROP TABLE installed;
ALTER TABLE installed_v1 RENAME TO installed;

-- 2. bases table: structure unchanged; runner column already clean.
--    No data migration needed.

-- 3. Stamp version
INSERT INTO schema_version (version) VALUES (1);
```

The migration runs inside a single transaction. On failure it rolls back and
Cellar starts read-only against the old schema rather than crashing.

### IGDB integration

Extend `igdb.py::_normalise()` to extract Steam App ID from `external_games`:

```
# In Apicalypse query, add:
external_games.uid,external_games.category;

# external_games.category = 1 is Steam
# external_games.uid is the Steam App ID (string → cast to int)
```

Add `steam_appid: int | None` to the dict returned by `search_games()`. The
IGDB picker pre-fills `steam_appid` in the Add/Edit dialog when available.
Maintainers can also enter it manually for apps IGDB doesn't cover.

---

## New module: `backend/umu.py`

Responsibilities:
- `detect_umu() -> str | None` — find `umu-run` binary (`shutil.which`, then
  check `/app/bin/umu-run` for bundled Flatpak location)
- `runners_dir() -> Path` — `~/.local/share/cellar/runners/`
- `prefixes_dir() -> Path` — `~/.local/share/cellar/prefixes/`
- `projects_dir() -> Path` — `~/.local/share/cellar/projects/`
- `resolve_runner_path(runner_name: str) -> Path | None` — looks up runner in
  `runners_dir()`
- `build_env(app_id, runner_name, steam_appid) -> dict` — returns the env var
  dict for a umu invocation
- `launch_app(app_id, entry_point, runner_name, steam_appid) -> None` —
  fire-and-forget `Popen`; uses `flatpak-spawn --host` when Cellar is
  sandboxed (same logic as current `bottles.py::is_cellar_sandboxed()`)
- `run_in_prefix(prefix_path, runner_name, exe_or_verb, *, gameid=0) -> subprocess.CompletedProcess`
  — blocking; used by Package Builder for wineboot, winetricks, installers

---

## Changes to existing modules

### `backend/installer.py`

- Remove `_fix_program_paths()` entirely (no bottle.yml, no path fixup needed)
- Remove `_find_bottle_source()` heuristic (or keep simplified: just find the
  single top-level dir, prefer `prefix/` name)
- Change install destination from Bottles data path to
  `umu.prefixes_dir() / app_id`
- Pass `install_path=str(umu.prefixes_dir())` and `bottle_name=app_id` to
  `database.mark_installed()`
- Drop all `bottle.yml` read/write steps
- Drop delta base seeding that references Bottles paths (update to use
  `umu.prefixes_dir()`)

### `backend/updater.py`

- Update prefix path resolution to use `umu.prefixes_dir() / app_id` instead
  of Bottles data path

### `backend/packager.py`

- `import_to_repo()` now accepts any WINEPREFIX path (not just a Bottles
  bottle path)
- Before archiving: strip `drive_c/users/` (replace with empty dirs), remove
  `bottle.yml` if present
- Archive with `prefix/` as the fixed top-level directory name
- Remove Bottles-specific logic

### `views/install_runner.py`

- Subtitle text: change "installed into Bottles" → "installed for Cellar"
- Callers: change `target_dir` to `umu.runners_dir() / runner_name`

### `views/add_app.py` / `views/edit_app.py`

- Add `steam_appid` field (integer entry, optional)
- Pre-fill from IGDB picker when available
- Remove "select Bottles bottle" picker; replace with "select prefix directory"
  file chooser (initial path: `umu.prefixes_dir()`) — or "from project" button
  that opens the project picker

### `views/detail.py`

- Launch: call `umu.launch_app()` instead of `bottles.launch_bottle()`
- "Open Install Folder": open `umu.prefixes_dir() / app_id / "drive_c"` in
  file manager

### `views/settings.py`

- Remove Bottles data path override setting
- Add umu-run binary path override (for non-standard installs)

### `backend/bottles.py`

- Keep as optional legacy module for a transitional period
- Long-term: delete once Package Builder covers all creation workflows

---

## New feature: Package Builder

A dedicated view for maintainers. Shown only when a writable repo is
configured. Accessible from the main window sidebar/switcher alongside
Browse / Installed / Updates.

### Project storage

```
~/.local/share/cellar/projects/<slug>/
  project.json    ← metadata: name, runner, entry_point, dep_log, notes, steam_appid
  prefix/         ← the WINEPREFIX
```

`project.json` schema:
```json
{
  "name": "My App",
  "slug": "my-app",
  "runner": "ge-proton10-32",
  "entry_point": "drive_c/Program Files/MyApp/myapp.exe",
  "steam_appid": 12345,
  "deps_installed": ["dotnet48", "vcrun2022", "corefonts"],
  "notes": ""
}
```

### Project types

The builder supports two project types selected at creation time:

- **App** — installs an app into the prefix and publishes to `catalogue.json::apps`
- **Base** — installs only shared dependencies; publishes to `catalogue.json::bases`
  keyed by runner name. One base per runner per repo.

A base is the foundation for delta packages: a clean prefix + common deps
(fonts, .NET, vcredist, etc.) with no app installed. Its identity is its runner
name, so the runner picker is fixed once a base project is initialised.

### Package Builder view layout

Left panel: project list (`GtkListBox`) showing both App and Base projects,
distinguished by a type badge. Toolbar: New App Project, New Base Project,
Delete Project.

Right panel: project detail. Sections differ slightly by project type.

**1. Prefix**
- Runner selector (dropdown from `umu.runners_dir()`) — triggers runner
  install if selected runner absent; locked after initialisation for Base
  projects (runner = base identity)
- "Initialise prefix" button — runs `wineboot --init` in prefix via
  `umu.run_in_prefix()`; disabled once initialised
- Status row: Wine version, prefix path

**2. Dependencies**
- List of installed verbs (from `project.json::deps_installed`)
- "Add dependency" button → opens a searchable verb picker or free-text entry
  for winetricks verbs; runs `umu-run winetricks <verb>` in prefix on
  background thread with progress dialog
- "Run custom command" — free-text wine command entry for edge cases
- *For Base projects this section is the primary work — fonts, .NET,
  vcredist, and other universally needed components go here*

**3. Files** *(App projects only)*
- "Run installer" — file chooser for `.exe`; runs it inside the prefix via
  `umu.run_in_prefix()` with progress/log output
- "Browse prefix" — opens `prefix/drive_c/` in the system file manager
  (`gio open` / `xdg-open`); primary path for no-installer games where the
  maintainer manually copies game files into `drive_c/`
- "Set entry point" — file chooser rooted at `prefix/drive_c/`, stores the
  selected path relative to `drive_c/` in `project.json::entry_point`
- *For Base projects: "Browse prefix" only (inspection); no installer or
  entry point — a base has no app*

**4. Package**

*App projects:*
- Steam App ID field (pre-fill from IGDB lookup button)
- "Test launch" — launches entry point via `umu.launch_app()` using project
  runner and steam_appid; confirms it works before packaging
- "Publish app" button — pipeline:
  1. Strip `prefix/drive_c/users/` symlinks → empty dirs
  2. Tar as `prefix/` top-level
  3. Compute CRC32
  4. If base for this runner exists in repo: produce delta archive
     (same BLAKE2b diff as current `create_delta_archive()`); set `base_runner`
  5. Otherwise: produce full archive
  6. Open Add App dialog pre-filled with project metadata

*Base projects:*
- "Publish base" button — pipeline:
  1. Strip `prefix/drive_c/users/` symlinks → empty dirs
  2. Tar as `prefix/` top-level
  3. Compute CRC32
  4. Upload to `repo/bases/<runner>/`
  5. Write entry to `catalogue.json::bases` keyed by runner name
  6. Install extracted base to `~/.local/share/cellar/bases/<runner>/`
     so future app projects on this machine can immediately produce deltas

### No-installer game workflow

For games distributed as a bare directory of files (no `.exe` installer):

1. Create project, initialise prefix
2. Install any needed dependencies (DirectX, vcredist, etc.) via the
   Dependencies section
3. Hit "Browse prefix" → system file manager opens at `prefix/drive_c/`
4. Manually copy/drag game files into `drive_c/` (e.g. into
   `drive_c/Games/MyGame/`)
5. Return to Cellar, "Set entry point" → browse to the game executable
6. "Test launch" → confirm it works
7. "Package to repo"

---

## Delta packages — retained and adapted

Delta packages are a core feature of Cellar's repo hosting model and are
retained in the new architecture. Without deltas, every app archive is a full
WINEPREFIX (~2-3 GB). With a shared base, each app archive contains only its
unique files, typically an order-of-magnitude smaller. For a catalogue of 20
apps this is the difference between ~50 GB of repo storage and ~7 GB.

The delta algorithm (BLAKE2b-128 content hashing, `.cellar_delete` manifest,
seed + overlay installation) is entirely format-agnostic and carries forward
without changes. The only adaptations needed for the new architecture are:

- **Base archives** use the new clean prefix format (no `bottle.yml`)
- **Base storage** stays at `~/.local/share/cellar/bases/<runner>/` — unchanged
- **Seeding** targets `umu.prefixes_dir() / app_id` instead of the Bottles
  data path
- **`AppEntry.base_runner`** retained; meaning unchanged

The `bases` table in the DB schema is retained. The `bases` section of
`catalogue.json` is retained. The Settings "Delta Base Images" group is
retained. `base_store.py` is retained with only path updates.

---

## Implementation phases

### Phase 1 — Core migration (Bottles removed)

1. Create `backend/umu.py` (detection, path helpers, launch, run_in_prefix)
2. Update `backend/installer.py` (new install path, drop bottle.yml steps;
   update delta seed/overlay paths to use `umu.prefixes_dir()`)
3. Update `backend/base_store.py` (paths only — base storage dir unchanged)
4. Update `backend/updater.py` (new prefix path)
5. Update `backend/packager.py` (accept any WINEPREFIX, clean archive format;
   delta creation logic unchanged)
6. Update `views/install_runner.py` callers (new runner target dir)
7. Update `views/detail.py` (umu launch, open folder path)
8. Update `views/add_app.py` / `edit_app.py` (steam_appid field, prefix picker)
9. Update `views/settings.py` (remove Bottles setting, add umu path override;
   Delta Base Images section unchanged)
10. Rebuild DB schema (version 1): clean `installed` table, `bases` unchanged
11. `AppEntry`: add `steam_appid` field, serialisation

### Phase 2 — IGDB → steam_appid

1. Extend `igdb.py::search_games()` to fetch `external_games.uid/category`
2. Extend `_normalise()` to extract Steam App ID (category=1)
3. IGDB picker pre-fills steam_appid in Add/Edit dialogs

### Phase 3 — Package Builder

1. `backend/project.py` — project CRUD, `project.json` read/write
2. `views/package_builder.py` — project list + detail view
3. Dependency installer (winetricks verb picker + umu invocation)
4. Run installer / Browse prefix / Set entry point
5. Test launch + Package to repo pipeline
6. Wire into main window navigation

### Phase 4 — Flatpak

1. `flatpak/io.github.cellar.json` manifest
2. Bundle umu-launcher via pip in manifest
3. Bundle winetricks
4. Permissions: `--device=dri`, `--socket=wayland`, `--socket=x11`,
   `--filesystem=home` (or targeted paths)
5. `flatpak-spawn --host` for game launch when sandboxed
6. Test on clean system (no Bottles, no system umu)

---

## Runner management — components.py replaced

`backend/components.py` (dulwich-based sync of `bottlesdevs/components`) is
replaced by `backend/runners.py` sourcing GE-Proton releases directly from
the GitHub Releases API.

**Why:**
- umu requires Proton, not Wine. The components index is predominantly Wine
  runners (Soda, Caffe, Kron4ek, Vaniglia, Lutris) — irrelevant in the new
  architecture.
- GE-Proton is the only supported runner family for Cellar + umu. It is
  self-contained (no Steam Runtime dependency at the runner level — umu manages
  the Steam Linux Runtime itself, downloading it automatically to
  `~/.local/share/umu/`).
- The GitHub Releases API gives download URLs, file sizes, and checksums
  directly. No git clone, no YAML parsing, no dulwich required.

**umu-launcher runtime notes (from upstream docs):**
- umu auto-downloads the Steam Linux Runtime to `~/.local/share/umu/` —
  not Cellar's concern.
- Default `PROTONPATH` (unset) = UMU-Proton (Valve Proton + umu patches).
  Cellar always sets `PROTONPATH` explicitly to pin the version recorded in
  `built_with.runner`.
- Official Valve Proton technically works but depends on Steam being installed.
  GE-Proton is self-contained and the correct choice for Cellar.

**`backend/runners.py` public API:**

```python
def fetch_releases(limit: int = 20) -> list[dict]:
    """Return recent GE-Proton releases from the GitHub Releases API.

    Each dict: name (str), tag (str), url (str), size (int), checksum (str).
    Response cached in memory for one hour (well within GitHub's 60 req/hr
    unauthenticated rate limit).
    """

def is_installed(runner_name: str) -> bool:
    """True if runner_name directory exists in umu.runners_dir()."""

def installed_runners() -> list[str]:
    """Names of all runners in runners_dir(), newest-first."""
```

Runner download and extraction reuses `InstallRunnerDialog` unchanged —
`target_dir` was already a parameter; callers now pass
`umu.runners_dir() / runner_name`.

**dulwich removed** as a project dependency. The startup background thread
that cloned/pulled the components repo is also removed.

---

## What does NOT change

- All repo/transport infrastructure (`backend/repo.py`, `_Local`, `_Http`,
  `_Ssh`, `_GioFetcher`)
- Catalogue format (`catalogue.json`, `AppEntry` shape) — `steam_appid` is
  purely additive
- IGDB client and picker (Phase 2 is an extension, not a rewrite)
- Delta packages / base store — algorithm and format unchanged; path updates
  only (`umu.prefixes_dir()` replaces Bottles data path in seed/overlay)
- Linux native app support — completely separate path, unaffected
- Image caching (`utils/images.py`, `Repo.resolve_asset_uri`)
- Browse / Installed / Updates UI
- rsync safe update logic
- All 221 existing tests (runner/path references may need updating)

---

## Key constraints to carry forward

- **Never block the UI thread** — all umu invocations on background threads
  with `GLib.Thread` / `threading.Thread`; progress via `GLib.idle_add`
- **Flatpak sandbox detection** — `Path("/.flatpak-info").exists()`; prefix
  game-launch umu calls with `flatpak-spawn --host` when true
- **Runner absent** — if `built_with.runner` not in `umu.runners_dir()`,
  prompt to install it (existing `InstallRunnerDialog` pattern) before
  allowing install or test launch
- **`GAMEID=0` fallback** — always valid for umu; protonfixes just won't apply
- **Archive backwards compatibility** — installer detects Bottles-format
  archives (presence of `bottle.yml`) and handles them identically to
  Cellar-native archives; the only difference is `bottle.yml` is ignored

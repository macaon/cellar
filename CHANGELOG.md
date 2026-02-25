# Changelog

All notable changes to Cellar will be documented here.

---

## [Unreleased]

### Added
- Multi-transport repo backend (`cellar/backend/repo.py`):
  - HTTP/HTTPS support via `urllib` (primary transport, no extra dependencies)
  - SSH support via the system `ssh` client — key auth handled by SSH agent / `~/.ssh/config`; explicit identity file configurable per-repo
  - SMB/CIFS and NFS support via GIO/GVFS (`smb://`, `nfs://`)
  - `_Fetcher` protocol with `_LocalFetcher`, `_HttpFetcher`, `_SshFetcher`, `_GioFetcher` implementations
- GIO file helpers (`cellar/utils/gio_io.py`): `gio_read_bytes()` and `gio_file_exists()`
- 14 new tests covering HTTP mock responses, HTTP error handling, SSH argument construction, SSH error propagation, and identity file pass-through

### Changed
- `Repo.resolve_path()` renamed to `Repo.resolve_asset_uri()` — now returns a `str` URI instead of a `pathlib.Path`, since remote transports have no local path equivalent
- `Repo.__init__` accepts a new keyword argument `ssh_identity: str | None` for specifying an SSH private key file

### Fixed
- `_HttpFetcher` normalises trailing slashes on the base URL so asset URIs are always well-formed regardless of how the repo URI was entered

---

## [0.2.0] — 2026-02-18

### Added
- Browse UI (`cellar/views/browse.py`): scrolling `GtkFlowBox` grid of app cards with `.card` Adwaita styling
- Horizontal category filter strip with linked `GtkToggleButton` pills (radio behaviour)
- `GtkSearchBar` wired to the header bar search toggle; typing anywhere in the window opens it automatically via `set_key_capture_widget`
- Empty and error states via `AdwStatusPage`
- `AppCard` widget with icon placeholder, name, and two-line summary

---

## [0.1.0] — 2026-02-18

### Added
- Initial project scaffold: `pyproject.toml`, `meson.build`, Flatpak manifest skeleton
- Repo backend (`cellar/backend/repo.py`): parses local `catalogue.json` and `manifest.json`
- `AppEntry` and `Manifest` dataclass models
- `RepoManager` for merging catalogues from multiple sources (last-repo-wins deduplication)
- `Repo` supports bare local paths and `file://` URIs
- Test fixtures with two sample apps (`example-app`, `paint-clone`)
- Full test suite for local repo operations (`tests/test_repo.py`)
- `cellar/utils/paths.py`: resolves UI files from source tree or installed location (no build step needed during development)

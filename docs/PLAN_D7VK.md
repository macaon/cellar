# D7VK Integration Plan

## Background

[d7vk](https://github.com/WinterSnowfall/d7vk) is a Vulkan-based translation layer for Direct3D 7/6/5/3 - the oldest 3D DirectX APIs (~1996–2001 era). It's a DXVK spin-off providing a replacement `ddraw.dll` that routes old D3D calls through D3D9 → Vulkan. Useful for titles like HoMM3, Sacrifice, GTA 2, Midtown Madness 2, etc.

GE-Proton does **not** bundle d7vk, so Cellar needs to manage it separately.

## Design Decisions

- **Install-time injection**, not runtime - users who launch via `.desktop` shortcuts bypass Cellar's launch flow, so the DLL and registry override must be baked into the prefix.
- **Shared download, per-prefix copy** - d7vk releases are downloaded once to `~/.local/share/cellar/d7vk/<version>/`, then the DLL is copied into each prefix that needs it.
- **Per-game version tracking** - each installed game records which d7vk version it has. Updates are offered individually per game in the Updates tab, so a broken release can be skipped for specific titles.
- **Registry override** - `HKCU\Software\Wine\DllOverrides` → `ddraw` = `native,builtin` written into the prefix so it works without `WINEDLLOVERRIDES` env var. Cellar-launched games also set `WINEDLLOVERRIDES` for belt-and-suspenders.

## Changes Required

### 1. Data Model (`cellar/models/app_entry.py`)
- Add `d7vk: bool` field to `AppEntry` (default `False`), same pattern as `dxvk`/`vkd3d`.
- Add to `INDEX_FIELDS` if it should appear in catalogue index.

### 2. Database (`cellar/backend/database.py`)
- Add `d7vk INTEGER` and `d7vk_version TEXT` columns to `launch_overrides` table.
- Schema migration (next version bump).

### 3. D7VK Store (new: `cellar/backend/d7vk.py`)
- Fetch releases from GitHub API (`https://api.github.com/repos/WinterSnowfall/d7vk/releases`).
- Download and extract release assets to `~/.local/share/cellar/d7vk/<version>/`.
- Provide `get_latest_version()`, `is_installed(version)`, `install(version)`, `get_dll_path(version)`.
- Follow the pattern established by `runners.py` for GE-Proton.

### 4. Prefix Injection (`cellar/backend/installer.py`)
- After prefix extraction, if d7vk is enabled: download d7vk if needed, copy `ddraw.dll` into prefix `system32/` (and `syswow64/` for 64-bit prefixes), write `ddraw` registry override.
- Record d7vk version in `launch_overrides`.

### 5. Launch Flow (`cellar/backend/umu.py`)
- Add `d7vk` param to `dll_overrides()` → appends `ddraw=n,b`.
- Add `d7vk` to `merge_launch_params()`.

### 6. Launch Params Toggle (`cellar/views/launch_params.py`)
- Add `Adw.SwitchRow` for D7VK in the compatibility group (copy DXVK row pattern).
- On enable (post-install): inject DLL + registry override into existing prefix, record version.
- On disable: remove DLL and registry override from prefix.

### 7. Detail View (`cellar/views/detail.py`)
- Pass `d7vk` through merged params to `dll_overrides()`.

### 8. Updates Tab
- Check installed d7vk_version against latest GitHub release for each game with d7vk enabled.
- Show per-game update entries - user can update individually.
- Update = replace DLL in prefix, bump version in DB.

### 9. Builder Metadata (`cellar/views/builder/metadata.py`)
- Add D7VK toggle for catalogue defaults alongside existing DXVK/VKD3D toggles.

## File Summary

| File | Change |
|------|--------|
| `cellar/models/app_entry.py` | Add `d7vk` field |
| `cellar/backend/database.py` | Two columns + migration |
| `cellar/backend/d7vk.py` | **New** - GitHub release fetch, download, version management |
| `cellar/backend/installer.py` | Post-extract DLL injection |
| `cellar/backend/umu.py` | `dll_overrides()` + `merge_launch_params()` |
| `cellar/views/launch_params.py` | SwitchRow toggle |
| `cellar/views/detail.py` | Pass param through |
| `cellar/views/browse.py` | Update entries for d7vk |
| `cellar/views/builder/metadata.py` | Catalogue default toggle |

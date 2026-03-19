# Repository Format

A Cellar repo is a directory (local or remote) containing a `catalogue.json`
master index and per-app assets:

```
repo/
  catalogue.json
  apps/
    <id>/
      metadata.json                full app metadata (fetched on demand)
      icon.png                     square icon — PNG, JPG, ICO, or SVG
      cover.png                    portrait cover (2:3) — shown in browse cards
      logo.png                     transparent logo — replaces icon in detail view
      screenshots/
        01.png
      <id>-1.0.tar.zst.001         chunked archive (1 GB per chunk)
      <id>-1.0.tar.zst.002
  bases/
    <base-name>-base.tar.zst.001   shared base image (chunked)
    <base-name>-base.tar.zst.002
  runners/
    <runner>.tar.zst.001           GE-Proton runner archive (chunked)
```

Archives contain a single top-level `prefix/` directory holding the WINEPREFIX
(or app contents for Linux apps).

## Two-tier catalogue

The catalogue is split into a **slim index** and **per-app metadata**:

- `catalogue.json` — contains only the fields needed for the browse grid and
  update detection (id, name, category, summary, icon, cover, platform,
  archive_crc32, base_image). Also holds runners, bases, categories, and
  category_icons.
- `apps/<id>/metadata.json` — the full `AppEntry` with all fields. Fetched on
  demand when the user opens the detail view. Cached locally for offline use.

This keeps the initial catalogue fetch small even for repos with many apps.

## `catalogue.json`

```json
{
  "cellar_version": 2,
  "generated_at": "2026-03-09T12:00:00Z",
  "category_icons": {
    "Games": "applications-games-symbolic",
    "Productivity": "applications-office-symbolic"
  },
  "categories": ["My Custom Category"],
  "runners": {
    "GE-Proton10-32": {
      "archive": "runners/GE-Proton10-32.tar.zst",
      "archive_size": 524288000,
      "archive_crc32": "aabbccdd",
      "archive_chunks": [
        {"size": 524288000, "crc32": "aabbccdd"}
      ]
    }
  },
  "bases": {
    "GE-Proton10-32-allfonts": {
      "runner": "GE-Proton10-32",
      "archive": "bases/GE-Proton10-32-allfonts-base.tar.zst",
      "archive_size": 2684354560,
      "archive_crc32": "eeff0011",
      "archive_chunks": [
        {"size": 1073741824, "crc32": "eeff0011"},
        {"size": 1073741824, "crc32": "22334455"},
        {"size": 536870912,  "crc32": "66778899"}
      ]
    }
  },
  "apps": [
    {
      "id": "my-app",
      "name": "My App",
      "category": "Games",
      "summary": "One-line description shown in the browse grid",
      "icon": "apps/my-app/icon.png",
      "cover": "apps/my-app/cover.png",
      "platform": "windows",
      "archive_crc32": "11223344",
      "base_image": "GE-Proton10-32-allfonts"
    }
  ]
}
```

## Key fields

All fields except `id`, `name`, `version`, and `category` are optional.

| Field | Description |
|---|---|
| `base_image` | The base image this delta is built against. The installer seeds the prefix from this base before overlaying. Runner derived via `bases[base_image].runner`. Required for all Windows apps. |
| `archive_chunks` | Per-chunk size and CRC32. Empty or absent for legacy single-file archives. |
| `steam_appid` | Sets `GAMEID=umu-<id>` for umu-launcher, enabling [protonfixes](https://github.com/Open-Wine-Components/umu-protonfixes). Omit or `null` for `GAMEID=0`. |
| `update_strategy` | `"safe"` (rsync overlay, preserves user data) or `"full"` (complete replacement with warning). |
| `launch_targets` | Array of `{"name", "path", "args"}`. First target is primary (desktop shortcut, single-click launch). Multiple targets show a picker dialog. |
| `platform` | `"windows"` (default, omitted from JSON), `"linux"` for native apps, or `"dos"` for DOS games (early testing). |
| `hide_title` | Suppress app name in detail view when a transparent `logo` image is provided. |
| `lock_runner` | Prevent the user from changing the runner for this app. |
| `category_icons` | Top-level map of category name → Adwaita symbolic icon name. |
| `categories` | Custom categories beyond the built-in set (Games, Productivity, Graphics, Utility). |

# Internals

This document covers the internal mechanisms behind Cellar's packaging,
installation, and update systems.

## Chunked archives

All archives - app packages, runner binaries, and base images - are split into
independent ~1 GB `.tar.zst` chunks during compression. Each chunk is a
self-contained tar.zst file that can be downloaded, extracted, and deleted
independently.

**Compression** - the `_ChunkWriter` in `packager.py` monitors output size.
When a chunk reaches the 1 GiB threshold it closes the current zstd
compressor and tarfile, rotates to a new output file, and reopens both.
Each chunk gets its own CRC32 checksum; a cumulative CRC32 is tracked across
all chunks.

**Chunk naming** - files follow the pattern `archive.tar.zst.001`, `.002`,
etc. (1-based). The `archive_chunks` field on each entry stores per-chunk
metadata: `[{"size": 1073741824, "crc32": "aabb0011"}, ...]`.

**Download** - the installer iterates through chunks sequentially. For each:
download to a temp file → verify CRC32 → extract in a single streaming pass →
delete the temp file → proceed to next chunk. Peak temporary disk usage is one
chunk (~1 GB) rather than the full compressed archive size.

## Delta packages

Delta archives contain only files that differ from a shared base image,
dramatically reducing download size. A typical base image is 2–3 GB; app
deltas are 50–500 MB each.

**At publish time** (`compress_prefix_delta_zst`):
1. Walk every file in the app prefix and compute a BLAKE2b-128 hash.
2. For each file that also exists in the base directory, hash the base copy.
   If hashes match, exclude the file from the delta.
3. Files present in the base but absent from the app prefix are recorded in a
   `.cellar_delete` manifest (a text file listing relative paths).
4. The remaining delta files and the delete manifest are packed into chunked
   `.tar.zst` archives.
5. Symlinks under `drive_c/users/` are stripped - umu recreates them on first
   launch.

**At install time** (`installer.py`):
1. Ensure the runner is installed (download if not).
2. Ensure the base image is installed (download + extract if not).
3. Download and extract the app delta to a temporary directory (on the same
   filesystem as `prefixes/` for efficient moves).
4. Seed the destination prefix from the base via copy-on-write:
   `cp -a --reflink=auto <base>/. <dest>/`. On btrfs and XFS this creates
   reflinks - file blocks are shared with the base until modified, using zero
   extra disk. On other filesystems `--reflink=auto` silently falls back to a
   regular copy. A pure-Python fallback handles environments without `cp`.
5. Overlay the delta onto the seeded prefix using rsync (Python fallback if
   unavailable). Each destination file is unlinked before copying to avoid
   modifying shared base inodes.
6. Apply the `.cellar_delete` manifest - remove each listed path.
7. Write a file manifest (`.cellar-manifest.json`) for future safe updates.

## Safe updates

When an app uses `update_strategy: "safe"`, Cellar updates the prefix
in-place while preserving user data (saves, configs, settings).

**Manifest tracking** - after every fresh install or update, Cellar writes
`.cellar-manifest.json` at the prefix root. This records the mtime and file
size of every file:

```json
{"version": 1, "files": {"drive_c/game/data.pak": [1048576, 1709251200], ...}}
```

**Change detection** (`scan_user_files`) - before applying an update, Cellar
re-stats every file:
- Files whose size or mtime changed since the manifest was written are flagged
  as *modified package files* (the user or the app changed them at runtime).
- Files on disk that are not in the manifest and not under Wine system paths
  are flagged as *user-created files* (saves, screenshots, custom configs).

**Update flow:**
1. Optionally back up the existing prefix to a `.tar.gz` archive.
2. Download and verify the new app archive (chunked, CRC32-checked).
3. Extract to a temporary directory.
4. Overlay via rsync (or Python fallback), skipping protected paths.
5. For delta updates: apply the `.cellar_delete` manifest.
6. Restore stashed user-modified and user-created files on top.
7. Rewrite the manifest to establish a new baseline.

**Protected paths** - the overlay never touches:
- `drive_c/users/*/AppData/Roaming/`, `Local/`, `LocalLow/`
- `drive_c/users/*/Documents/`
- `user.reg`, `userdef.reg`
- Wine system directories (`drive_c/windows/`, `drive_c/Program Files*/`, etc.)

A full update (`update_strategy: "full"`) warns the user, removes the entire
prefix, and reinstalls from scratch.

## Install pipeline summary

**Windows app:**
ensure runner → ensure base → download app delta (chunked, streaming CRC32) →
extract to temp → seed prefix from base (CoW) → overlay delta → apply delete
manifest → write file manifest → record in SQLite database

**Linux app:**
download archive (chunked) → extract to `native/<id>/` → `chmod +x` entry
point → write manifest → record in database

**DOS game:**
download archive (chunked) → extract to `dos/<id>/` (includes bundled DOSBox
Staging binary, configs, and game files) → record in database

## Local data layout

```
~/.local/share/cellar/
  cellar.db             SQLite database (installed apps, base images)
  config.json           Repos, bearer tokens, preferences
  runners/
    GE-Proton10-32/     GE-Proton runner (managed by Cellar)
  prefixes/
    <app-id>/           WINEPREFIX for each installed Windows app
  native/
    <app-id>/           Installed Linux native apps
  dos/
    <app-id>/           Installed DOS games (bundled DOSBox Staging + configs)
  dosbox-staging/       Shared DOSBox Staging runtime (auto-downloaded)
  projects/
    <slug>/             Package Builder working area
      project.json
      prefix/           WINEPREFIX being built
  bases/
    <runner>/           Installed base images (for delta seeding)

~/.cache/cellar/
  assets/               Persistent image asset cache (HTTP(S)/SSH/SMB repos)
  catalogues/           Cached catalogue.json per repo (offline fallback)
  metadata/             Cached per-app metadata.json
```

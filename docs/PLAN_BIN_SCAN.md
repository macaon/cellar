# Plan: Scan CUE/BIN Data Tracks for Installer Detection

## Problem

`find_dos_installer()` only works on ISOs via `pycdlib`. CUE/BIN discs skip installer detection entirely, falling back to the DOSBox prompt.

## Approach

Extract ISO 9660 data from the BIN's data track by stripping raw CD sector headers, then scan with `pycdlib`.

Raw CD sectors (MODE1/2352) are 2352 bytes each:
- 12 bytes sync pattern
- 4 bytes header
- 2048 bytes user data (ISO 9660)
- 288 bytes ECC/EDC

Strip the non-data bytes → write a temporary ISO → pass to `scan_iso()`.

## Implementation

Add `scan_cue_bin(cue_sheet: CueSheet) -> list[str]` to `disc_image.py`:

1. Find the first data track in `cue_sheet.tracks` (not `is_audio`)
2. Open the BIN file, seek to the data track's `index1_offset`
3. Read sectors, extract 2048-byte user data from each (skip 16-byte header, ignore 288-byte tail)
4. Write to a temp file in `~/.cache/cellar/`
5. Pass to `scan_iso()` → returns file list
6. Clean up temp file

Then update `_install_from_disc` to call `scan_cue_bin()` when the disc is a CUE/BIN pair.

## Scope

- ~20 lines of Python in `disc_image.py`
- No new dependencies (reuses `pycdlib`)
- Only needs the data track, not audio tracks

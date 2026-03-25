# Fix: Import User Data from App Configuration dialog (#32)

## Problem

Importing user files from the "User Data" section inside the App
Configuration dialog fails silently. The file chooser opens, the user
selects a `.tar.zst` archive, but nothing happens.

## Root cause

Same class of bug as the backup issue we fixed in v0.93.0. The import
flow in `detail.py` (`_on_import_user_files` / `_on_import_file_chosen`)
presents its progress dialog via `self._dialog_parent()`, which should
now correctly target the config dialog. However, the import flow was
never actually tested after the move — the backup fix confirmed the
pattern works, so the import issue is likely something else.

## Where to look

1. `cellar/views/detail.py` — `_on_import_user_files()` (line ~1465)
   and `_on_import_file_chosen()` (line ~1479)

2. The `self._chooser` GC fix was added for both backup and import,
   so that's not it.

3. The `_dialog_parent()` fix was applied to the import progress dialog
   too, so that should be fine.

4. Most likely candidate: the `_on_import_file_chosen` callback might
   not be firing at all, or the `import_user_files` function in
   `cellar/backend/updater.py` might be failing for a different reason.
   Add the same debug logging pattern used for backup:

   ```python
   log.info("Import response: %s", response)
   log.info("Import archive: %s", archive_path)
   # inside _run:
   log.info("Import thread started, cancel_event=%s", cancel_event.is_set())
   ```

5. Check if the `_on_done` in the import flow has the same
   `cancel_event` bug (call `dlg.close()` before checking
   `cancel_event.is_set()`). It likely does — apply the same
   `was_cancelled = cancel_event.is_set()` fix before `dlg.close()`.

## Quick fix checklist

- [ ] Add debug logging to import flow
- [ ] Reproduce and read logs
- [ ] Apply `was_cancelled` fix to import `_on_done` if needed
- [ ] Fix whatever else the logs reveal
- [ ] Remove debug logging
- [ ] Bump to v0.93.1, commit, tag, release

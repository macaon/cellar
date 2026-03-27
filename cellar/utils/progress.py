"""Shared progress-reporting helpers for all dialogs."""

from __future__ import annotations

import re


def user_facing_error(exc: Exception) -> str:
    """Return a user-friendly error string that strips internal file paths.

    Raw exception strings from ``OSError``, ``FileNotFoundError``, etc. often
    contain absolute paths like ``/home/user/.local/share/cellar/…`` that are
    confusing in a GUI alert.  This helper strips path prefixes while keeping
    the human-readable tail (e.g. ``No space left on device``).
    """
    msg = str(exc)
    # Strip Unix absolute paths (keep the trailing description after ': ').
    msg = re.sub(r"/[^\s:]+", "\u2026", msg)
    return msg.strip() or type(exc).__name__


def fmt_size(n: int) -> str:
    """Human-readable byte count using SI units: '1.4 MB', '3.20 GB', etc."""
    if n < 1000:
        return f"{n} B"
    if n < 1000 ** 2:
        return f"{n / 1000:.1f} KB"
    if n < 1000 ** 3:
        return f"{n / 1000 ** 2:.1f} MB"
    return f"{n / 1000 ** 3:.2f} GB"


def fmt_stats(done: int, total: int, speed: float) -> str:
    """Format transfer progress compactly, e.g. ``'103/355 MB (~1:30 left)'``.

    When *total* is 0 or unknown, shows only the done amount.
    When *speed* is 0, omits the ETA.
    """
    if total > 0:
        # Use the total's unit for both values to keep it tight.
        if total < 1000 ** 2:
            size_str = f"{done / 1000:.0f}/{total / 1000:.0f} KB"
        elif total < 1000 ** 3:
            size_str = f"{done / 1000 ** 2:.0f}/{total / 1000 ** 2:.0f} MB"
        else:
            size_str = f"{done / 1000 ** 3:.1f}/{total / 1000 ** 3:.1f} GB"
    else:
        size_str = fmt_size(done)

    eta = ""
    if total > 0 and speed > 0 and done < total:
        secs = int((total - done) / speed)
        if secs < 60:
            eta = f" (~{max(1, secs)}s left)"
        else:
            mins, s = divmod(secs, 60)
            eta = f" (~{mins}:{s:02d} left)"

    return f"{size_str}{eta}"


def fmt_file_count(current: int, total: int) -> str:
    """Format a file-count position as e.g. 'File 42 / 156' or 'File 42' when total unknown."""
    if total > 0:
        return f"File {current} / {total}"
    return f"File {current}"


def fmt_compress_stats(done_files: int, total_files: int, speed_bps: float) -> str:
    """Format compress progress as e.g. '42 / 156 files  (48.3 MB/s)'.

    *speed_bps* is the uncompressed source read rate in bytes/second.
    """
    count = f"{done_files} / {total_files} files" if total_files else f"{done_files} files"
    if speed_bps >= 0.1 * 1000 ** 2:
        spd = f"{speed_bps / 1000 ** 2:.1f} MB/s"
    elif speed_bps > 0:
        spd = f"{speed_bps / 1000:.0f} KB/s"
    else:
        spd = "\u2026"
    return f"{count}  ({spd})"


def trunc_middle(name: str, max_chars: int = 40) -> str:
    """Middle-truncate *name* so it fits in a progress bar without reflowing."""
    if len(name) <= max_chars:
        return name
    half = (max_chars - 1) // 2
    return f"{name[:half]}\u2026{name[-(max_chars - half - 1):]}"

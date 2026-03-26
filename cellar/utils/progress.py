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
    """Format transfer progress as e.g. '2.6 MB / 349 MB (1.3 MB/s) — about 4 min left'.

    When *total* is 0 or unknown, omits the '/ total' part and the ETA.
    When *speed* is 0, shows '…' instead of a speed value.
    """
    size_str = f"{fmt_size(done)} / {fmt_size(total)}" if total > 0 else fmt_size(done)
    speed_str = f"{fmt_size(int(speed))}/s" if speed > 0 else "\u2026"
    eta = ""
    if total > 0 and speed > 0 and done < total:
        secs = (total - done) / speed
        if secs < 60:
            eta = " \u2014 about %d sec left" % max(1, int(secs))
        else:
            mins = secs / 60
            eta = " \u2014 about %d min left" % max(1, int(mins + 0.5))
    return f"{size_str} ({speed_str}){eta}"


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

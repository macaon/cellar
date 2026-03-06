"""Shared progress-reporting helpers for all dialogs."""

from __future__ import annotations


def fmt_size(n: int) -> str:
    """Human-readable byte count: '1.4 MB', '3.20 GB', etc."""
    if n < 1024:
        return f"{n} B"
    if n < 1024 ** 2:
        return f"{n / 1024:.1f} KB"
    if n < 1024 ** 3:
        return f"{n / 1024 ** 2:.1f} MB"
    return f"{n / 1024 ** 3:.2f} GB"


def fmt_stats(done: int, total: int, speed: float) -> str:
    """Format transfer progress as e.g. '2.6 MB / 349 MB (1.3 MB/s)'.

    When *total* is 0 or unknown, omits the '/ total' part.
    When *speed* is 0, shows '…' instead of a speed value.
    """
    size_str = f"{fmt_size(done)} / {fmt_size(total)}" if total > 0 else fmt_size(done)
    speed_str = f"{fmt_size(int(speed))}/s" if speed > 0 else "\u2026"
    return f"{size_str} ({speed_str})"


def fmt_file_count(current: int, total: int) -> str:
    """Format a file-count position as e.g. 'File 42 / 156' or 'File 42' when total unknown."""
    if total > 0:
        return f"File {current} / {total}"
    return f"File {current}"


def fmt_compress_stats(done_files: int, total_files: int, speed_bps: float) -> str:
    """Format compress progress as e.g. '42 / 156 files  (48.3 MiB/s)'.

    *speed_bps* is the uncompressed source read rate in bytes/second.
    """
    count = f"{done_files} / {total_files} files" if total_files else f"{done_files} files"
    if speed_bps >= 0.1 * 1024 ** 2:
        spd = f"{speed_bps / 1024 ** 2:.1f} MiB/s"
    elif speed_bps > 0:
        spd = f"{speed_bps / 1024:.0f} KiB/s"
    else:
        spd = "\u2026"
    return f"{count}  ({spd})"


def trunc_middle(name: str, max_chars: int = 40) -> str:
    """Middle-truncate *name* so it fits in a progress bar without reflowing."""
    if len(name) <= max_chars:
        return name
    half = (max_chars - 1) // 2
    return f"{name[:half]}\u2026{name[-(max_chars - half - 1):]}"

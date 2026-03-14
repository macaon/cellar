"""Helpers for running background work from GTK views.

All GTK widget operations must happen on the main thread.  These helpers
wrap the standard ``threading.Thread`` + ``GLib.idle_add`` pattern used
throughout the codebase, eliminating boilerplate while keeping the
threading model explicit.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable

from gi.repository import GLib

log = logging.getLogger(__name__)


def run_in_background(
    work: Callable[[], object],
    on_done: Callable[[object], None] | None = None,
    on_error: Callable[[str], None] | None = None,
) -> None:
    """Run *work* on a daemon thread; dispatch results to the main thread.

    *work* is called with no arguments on a background thread.  Its return
    value is passed to *on_done* (called via ``GLib.idle_add``).  If *work*
    raises, the stringified exception is passed to *on_error* instead.

    If *on_done* or *on_error* are ``None`` the result / error is silently
    discarded.

    Example::

        run_in_background(
            work=lambda: expensive_io(),
            on_done=lambda result: label.set_text(str(result)),
            on_error=lambda msg: show_alert("Error", msg),
        )
    """
    def _target() -> None:
        try:
            result = work()
        except Exception as exc:
            if on_error is not None:
                GLib.idle_add(on_error, str(exc))
            else:
                log.debug("Background task raised unhandled exception: %s", exc)
        else:
            if on_done is not None:
                GLib.idle_add(on_done, result)

    threading.Thread(target=_target, daemon=True).start()

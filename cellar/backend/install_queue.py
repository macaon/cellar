"""FIFO install queue — one active install at a time, others queued.

All callbacks are dispatched via ``GLib.idle_add`` so UI code can safely
update widgets without thread guards.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from gi.repository import GLib

log = logging.getLogger(__name__)


@dataclass
class InstallJob:
    """Immutable description of a pending install."""

    app_id: str
    app_name: str
    entry: object               # AppEntry
    archive_uri: str
    platform: str               # "windows", "linux", "dos"
    token: str | None = None
    ssh_identity: str | None = None
    base_entry: object | None = None
    base_archive_uri: str = ""
    runner_entry: object | None = None
    runner_archive_uri: str = ""
    cancel_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class InstallResult:
    """Outcome of a successful install — passed to on_complete."""

    app_id: str
    prefix_dir: str
    install_path: str = ""
    runner: str = ""
    install_size: int = 0
    delta_size: int = 0


class InstallQueue:
    """Serialises installs: one runs at a time, others wait in a FIFO queue.

    Callbacks
    ---------
    on_progress(app_id, phase, fraction)
        Fired from the install thread (via idle_add) for download/extract phases.
    on_download_stats(app_id, downloaded, total, speed)
        Fired from the install thread (via idle_add) with byte-level download stats.
    on_complete(result: InstallResult)
        Fired on the UI thread when an install succeeds.
    on_error(app_id, message)
        Fired on the UI thread when an install fails.
    on_cancelled(app_id)
        Fired on the UI thread when an install is cancelled.
    on_queue_changed()
        Fired on the UI thread whenever the queue or active job changes.
    """

    def __init__(
        self,
        *,
        on_complete: Callable[[InstallResult], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
        on_cancelled: Callable[[str], None] | None = None,
        on_progress: Callable[[str, str, float], None] | None = None,
        on_download_stats: Callable[[str, int, int, float], None] | None = None,
        on_queue_changed: Callable[[], None] | None = None,
    ) -> None:
        self._queue: deque[InstallJob] = deque()
        self._active: InstallJob | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._on_complete = on_complete
        self._on_error = on_error
        self._on_cancelled = on_cancelled
        self._on_progress = on_progress
        self._on_download_stats = on_download_stats
        self._on_queue_changed = on_queue_changed
        # Recently completed (app_id, app_name) pairs for the queue dialog.
        self._completed: list[tuple[str, str]] = []
        # Latest download stats for the active job (for tooltip / queue dialog).
        self._active_phase: str = ""
        self._active_fraction: float = 0.0
        self._active_dl_done: int = 0
        self._active_dl_total: int = 0
        self._active_dl_speed: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────

    def enqueue(self, job: InstallJob) -> None:
        """Add a job.  Starts processing immediately if idle."""
        with self._lock:
            # Don't double-enqueue the same app.
            if self._active and self._active.app_id == job.app_id:
                return
            if any(j.app_id == job.app_id for j in self._queue):
                return
            self._queue.append(job)
        self._notify_queue_changed()
        self._maybe_start()

    def cancel(self, app_id: str) -> None:
        """Cancel an active or queued install."""
        with self._lock:
            # If active, signal cancellation.
            if self._active and self._active.app_id == app_id:
                self._active.cancel_event.set()
                return
            # If queued, just remove it.
            self._queue = deque(j for j in self._queue if j.app_id != app_id)
        self._notify_queue_changed()

    def is_active(self, app_id: str) -> bool:
        with self._lock:
            return self._active is not None and self._active.app_id == app_id

    def is_queued(self, app_id: str) -> bool:
        with self._lock:
            return any(j.app_id == app_id for j in self._queue)

    def is_pending(self, app_id: str) -> bool:
        """Return True if app is active or queued."""
        with self._lock:
            if self._active and self._active.app_id == app_id:
                return True
            return any(j.app_id == app_id for j in self._queue)

    @property
    def active_job(self) -> InstallJob | None:
        with self._lock:
            return self._active

    @property
    def active_phase(self) -> str:
        return self._active_phase

    @property
    def active_fraction(self) -> float:
        return self._active_fraction

    @property
    def active_stats_text(self) -> str:
        """Human-readable download stats for the active job, or empty string."""
        from cellar.utils.progress import fmt_stats
        if self._active_dl_total > 0 or self._active_dl_done > 0:
            return fmt_stats(self._active_dl_done, self._active_dl_total, self._active_dl_speed)
        return ""

    @property
    def queued_jobs(self) -> list[InstallJob]:
        with self._lock:
            return list(self._queue)

    @property
    def completed_items(self) -> list[tuple[str, str]]:
        """Recently completed (app_id, app_name) pairs."""
        return list(self._completed)

    @property
    def completed_ids(self) -> list[str]:
        return [cid for cid, _ in self._completed]

    @property
    def is_empty(self) -> bool:
        with self._lock:
            return self._active is None and not self._queue

    def clear_completed(self) -> None:
        self._completed.clear()

    # ── Internal ──────────────────────────────────────────────────────────

    def _maybe_start(self) -> None:
        """Start the next job if nothing is running."""
        with self._lock:
            if self._active is not None:
                return
            if not self._queue:
                return
            self._active = self._queue.popleft()
        self._active_phase = ""
        self._active_fraction = 0.0
        self._active_dl_done = 0
        self._active_dl_total = 0
        self._active_dl_speed = 0.0
        self._notify_queue_changed()
        self._thread = threading.Thread(
            target=self._run_job, args=(self._active,), daemon=True,
        )
        self._thread.start()

    def _run_job(self, job: InstallJob) -> None:
        """Execute a single install job on a background thread."""
        from cellar.backend.installer import InstallCancelled
        from cellar.utils.progress import user_facing_error

        def _phase(label: str) -> None:
            if self._on_progress:
                GLib.idle_add(self._on_progress, job.app_id, label, 0.0)

        def _dl_progress(fraction: float) -> None:
            if self._on_progress:
                GLib.idle_add(self._on_progress, job.app_id, "", fraction)

        def _dl_stats(downloaded: int, total: int, speed: float) -> None:
            if self._on_download_stats:
                GLib.idle_add(self._on_download_stats, job.app_id, downloaded, total, speed)

        try:
            result = self._do_install(job, _phase, _dl_progress, _dl_stats)
            self._completed.append((job.app_id, job.app_name))
            GLib.idle_add(self._on_job_done, job, result)
        except InstallCancelled:
            GLib.idle_add(self._on_job_cancelled, job)
        except Exception as exc:  # noqa: BLE001
            log.error("Install of %s failed: %s", job.app_id, exc, exc_info=True)
            msg = user_facing_error(exc)
            GLib.idle_add(self._on_job_error, job, msg)

    def _do_install(
        self, job: InstallJob,
        phase_cb: Callable, progress_cb: Callable, stats_cb: Callable,
    ) -> InstallResult:
        """Run the actual install — returns an InstallResult."""
        from cellar.backend.installer import install_app, install_dos_app, install_linux_app

        if job.platform in ("linux", "dos"):
            _installer = install_dos_app if job.platform == "dos" else install_linux_app
            _app_id, install_dest = _installer(
                job.entry,
                job.archive_uri,
                download_cb=progress_cb,
                download_stats_cb=stats_cb,
                install_cb=progress_cb,
                phase_cb=phase_cb,
                cancel_event=job.cancel_event,
                token=job.token,
                ssh_identity=job.ssh_identity,
            )
            from cellar.utils.paths import dir_size_bytes
            _install_size = dir_size_bytes(install_dest)
            return InstallResult(
                app_id=job.app_id,
                prefix_dir=_app_id,
                install_path=str(install_dest.parent),
                install_size=_install_size,
            )

        # Windows app.
        prefix_dir = install_app(
            job.entry,
            job.archive_uri,
            base_entry=job.base_entry,
            base_archive_uri=job.base_archive_uri,
            runner_entry=job.runner_entry,
            runner_archive_uri=job.runner_archive_uri,
            download_cb=progress_cb,
            download_stats_cb=stats_cb,
            install_cb=progress_cb,
            phase_cb=phase_cb,
            cancel_event=job.cancel_event,
            token=job.token,
            ssh_identity=job.ssh_identity,
        )
        from cellar.backend.umu import prefixes_dir
        from cellar.utils.paths import dir_size_bytes
        _install_size = dir_size_bytes(prefixes_dir() / prefix_dir)
        _runner = job.base_entry.runner if job.base_entry else ""
        _delta_size = job.entry.delta_size if hasattr(job.entry, "delta_size") else 0
        return InstallResult(
            app_id=job.app_id,
            prefix_dir=prefix_dir,
            install_path=str(prefixes_dir()),
            runner=_runner,
            install_size=_install_size,
            delta_size=_delta_size,
        )

    def _on_job_done(self, job: InstallJob, result: InstallResult) -> None:
        with self._lock:
            self._active = None
        if self._on_complete:
            self._on_complete(result)
        self._notify_queue_changed()
        self._maybe_start()

    def _on_job_cancelled(self, job: InstallJob) -> None:
        with self._lock:
            self._active = None
        if self._on_cancelled:
            self._on_cancelled(job.app_id)
        self._notify_queue_changed()
        self._maybe_start()

    def _on_job_error(self, job: InstallJob, message: str) -> None:
        with self._lock:
            self._active = None
        if self._on_error:
            self._on_error(job.app_id, message)
        self._notify_queue_changed()
        self._maybe_start()

    def _notify_queue_changed(self) -> None:
        if self._on_queue_changed:
            GLib.idle_add(self._on_queue_changed)

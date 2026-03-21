"""FIFO publish queue — one active publish at a time, others queued.

Mirrors the :class:`InstallQueue` pattern: background thread per job,
all UI callbacks dispatched via ``GLib.idle_add``.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from gi.repository import GLib

log = logging.getLogger(__name__)


@dataclass
class PublishJob:
    """Description of a pending publish."""

    app_id: str
    app_name: str
    project: Any               # cellar.backend.project.Project
    repo: Any                  # Repo object with .writable_path()
    all_repos: list[Any]       # All configured repos (for base/runner lookup)
    entry: Any                 # AppEntry
    images: dict[str, Any]     # icon/cover/logo/screenshots paths
    delete_after: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class PublishResult:
    """Outcome of a successful publish."""

    app_id: str
    app_name: str
    repo_name: str
    delete_after: bool = False
    project_slug: str = ""


class PublishQueue:
    """Serialises publishes: one runs at a time, others wait in a FIFO queue.

    Callbacks
    ---------
    on_complete(result: PublishResult)
        Fired on the UI thread when a publish succeeds.
    on_error(app_id, message)
        Fired on the UI thread when a publish fails.
    on_cancelled(app_id)
        Fired on the UI thread when a publish is cancelled.
    on_progress(app_id, phase, fraction)
        Fired (via idle_add) for phase changes.
    on_bytes(app_id, total_bytes, stats_text)
        Fired (via idle_add) with human-readable byte stats.
    on_queue_changed()
        Fired on the UI thread whenever the queue or active job changes.
    """

    def __init__(
        self,
        *,
        on_complete: Callable[[PublishResult], None] | None = None,
        on_error: Callable[[str, str], None] | None = None,
        on_cancelled: Callable[[str], None] | None = None,
        on_progress: Callable[[str, str, float], None] | None = None,
        on_bytes: Callable[[str, int, str], None] | None = None,
        on_queue_changed: Callable[[], None] | None = None,
    ) -> None:
        self._queue: deque[PublishJob] = deque()
        self._active: PublishJob | None = None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._on_complete = on_complete
        self._on_error = on_error
        self._on_cancelled = on_cancelled
        self._on_progress = on_progress
        self._on_bytes = on_bytes
        self._on_queue_changed = on_queue_changed
        self._completed: list[tuple[str, str]] = []
        self._active_phase: str = ""
        self._active_fraction: float = 0.0
        self._active_stats: str = ""

    # ── Public API ────────────────────────────────────────────────────────

    def enqueue(self, job: PublishJob) -> None:
        """Add a job.  Starts processing immediately if idle."""
        with self._lock:
            if self._active and self._active.app_id == job.app_id:
                return
            if any(j.app_id == job.app_id for j in self._queue):
                return
            self._queue.append(job)
        self._notify_queue_changed()
        self._maybe_start()

    def cancel(self, app_id: str) -> None:
        """Cancel an active or queued publish."""
        with self._lock:
            if self._active and self._active.app_id == app_id:
                self._active.cancel_event.set()
                return
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
    def active_job(self) -> PublishJob | None:
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
        return self._active_stats

    @property
    def queued_jobs(self) -> list[PublishJob]:
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
        with self._lock:
            if self._active is not None:
                return
            if not self._queue:
                return
            self._active = self._queue.popleft()
        self._active_phase = ""
        self._active_fraction = 0.0
        self._active_stats = ""
        self._notify_queue_changed()
        self._thread = threading.Thread(
            target=self._run_job, args=(self._active,), daemon=True,
        )
        self._thread.start()

    def _run_job(self, job: PublishJob) -> None:
        """Execute a single publish job on a background thread."""
        from cellar.backend.packager import CancelledError
        from cellar.utils.progress import user_facing_error

        try:
            self._do_publish(job)
            self._completed.append((job.app_id, job.app_name))
            GLib.idle_add(self._on_job_done, job)
        except CancelledError:
            GLib.idle_add(self._on_job_cancelled, job)
        except Exception as exc:  # noqa: BLE001
            log.error("Publish of %s failed: %s", job.app_id, exc, exc_info=True)
            msg = user_facing_error(exc)
            GLib.idle_add(self._on_job_error, job, msg)

    def _do_publish(self, job: PublishJob) -> None:
        """Run the full compress → upload → import pipeline."""
        from dataclasses import replace as _dc_replace

        from cellar.backend.packager import (
            CancelledError,
            _cleanup_chunks,
            _cleanup_old_archive,
            compress_prefix_delta_zst,
            compress_prefix_zst,
            compress_runner_zst,
            import_to_repo,
            upsert_base,
            upsert_runner,
        )

        project = job.project
        entry = job.entry
        images = dict(job.images)

        from cellar.utils.progress import fmt_size

        _prev: list[tuple[float, int]] = []

        def _bytes_cb(n: int) -> None:
            now = time.monotonic()
            _prev.append((now, n))
            cutoff = now - 2.0
            while _prev and _prev[0][0] < cutoff:
                _prev.pop(0)
            if len(_prev) >= 2:
                dt = _prev[-1][0] - _prev[0][0]
                db = _prev[-1][1] - _prev[0][1]
                speed = db / dt if dt > 0 else 0
                spd = f" ({fmt_size(int(speed))}/s)" if speed > 0 else ""
            else:
                spd = ""
            stats = fmt_size(n) + " written" + spd
            self._active_stats = stats
            if self._on_bytes:
                GLib.idle_add(self._on_bytes, job.app_id, n, stats)

        def _reset_phase(label: str) -> None:
            _prev.clear()
            self._active_phase = label
            self._active_fraction = 0.0
            self._active_stats = ""
            if self._on_progress:
                GLib.idle_add(self._on_progress, job.app_id, label, 0.0)

        # ── Download Steam screenshots if selected ────────────────────
        if project.selected_steam_urls:
            _reset_phase("Downloading screenshots\u2026")
            from cellar.utils.http import make_session as _make_session

            _session = _make_session()
            dl_dir = project.project_dir / "screenshots"
            dl_dir.mkdir(parents=True, exist_ok=True)
            _selected = set(project.selected_steam_urls)
            _downloaded: list[str] = []
            _steam_url_for_path: dict[str, str] = {}
            _slug = project.slug
            for i, ss in enumerate(project.steam_screenshots):
                if ss.get("full") not in _selected:
                    continue
                try:
                    _resp = _session.get(ss["full"], timeout=30)
                    if _resp.ok:
                        _suffix = ".jpg" if ss["full"].lower().endswith(".jpg") else ".png"
                        _dest = dl_dir / f"steam_{i:02d}{_suffix}"
                        _dest.write_bytes(_resp.content)
                        _downloaded.append(str(_dest))
                        _steam_url_for_path[str(_dest)] = ss["full"]
                except Exception as _exc:  # noqa: BLE001
                    log.warning("Screenshot download failed: %s", _exc)
            if _downloaded:
                _n_existing = len(project.screenshot_paths)
                project.screenshot_paths = list(project.screenshot_paths) + _downloaded
                _new_rels = tuple(
                    f"apps/{_slug}/screenshots/{j + 1:02d}{Path(p).suffix}"
                    for j, p in enumerate(project.screenshot_paths)
                )
                _ss_sources = {
                    _new_rels[_n_existing + k]: _steam_url_for_path[_downloaded[k]]
                    for k in range(len(_downloaded))
                }
                entry = _dc_replace(
                    entry, screenshots=_new_rels, screenshot_sources=_ss_sources,
                )
                images["screenshots"] = list(project.screenshot_paths)
            project.steam_screenshots = []
            project.selected_steam_urls = []
            from cellar.backend.project import save_project as _save_project

            _save_project(project)

        # ── Prepare archive destination ───────────────────────────────
        repo_root = job.repo.writable_path()
        archive_dest = repo_root / entry.archive
        archive_dest.parent.mkdir(parents=True, exist_ok=True)
        _cleanup_old_archive(repo_root, entry)

        _written_archive = False
        _runner_upserted: tuple[str, object] | None = None
        _base_upserted: tuple[str, object] | None = None
        _partial_dest = None
        _app_imported = False

        _src_path = (
            Path(project.source_dir)
            if project.project_type in ("linux", "dos")
            else project.content_path
        )

        try:
            _reset_phase("Compressing and uploading\u2026")
            _delta_uncompressed = 0
            if project.project_type in ("linux", "dos"):
                _partial_dest = archive_dest
                size, crc32, chunks = compress_prefix_zst(
                    _src_path,
                    archive_dest,
                    cancel_event=job.cancel_event,
                    bytes_cb=_bytes_cb,
                )
                base_image = ""
            else:
                from cellar.backend.base_store import base_path, is_base_installed

                if not is_base_installed(project.runner):
                    raise RuntimeError(
                        f"Base image \u201c{project.runner}\u201d is not installed locally. "
                        "Install the base image before publishing."
                    )
                _reset_phase("Scanning files\u2026")
                _partial_dest = archive_dest
                size, crc32, chunks, _delta_uncompressed = compress_prefix_delta_zst(
                    _src_path,
                    base_path(project.runner),
                    archive_dest,
                    cancel_event=job.cancel_event,
                    phase_cb=_reset_phase,
                    bytes_cb=_bytes_cb,
                )
                base_image = project.runner
            _partial_dest = None
            _written_archive = True

            # ── Auto-publish base image + runner if missing ───────────
            if base_image:
                import json as _json

                from cellar.backend.base_store import base_path
                from cellar.backend.umu import runners_dir

                _target_bases: dict[str, str] = {}
                _target_runners: dict[str, str] = {}
                _cat_path = repo_root / "catalogue.json"
                try:
                    if _cat_path.exists():
                        with _cat_path.open("r") as _f:
                            _cat_raw = _json.load(_f)
                        if isinstance(_cat_raw, dict):
                            _target_bases = _cat_raw.get("bases", {})
                            _target_runners = _cat_raw.get("runners", {})
                except Exception:
                    pass

                _need_base = base_image not in _target_bases
                if _need_base:
                    _runner_name = base_image
                    for _r in job.all_repos:
                        try:
                            _rb = _r.fetch_bases()
                            if base_image in _rb:
                                _runner_name = _rb[base_image].runner
                                break
                        except Exception:
                            continue

                    _need_runner = _runner_name not in _target_runners

                    if _need_runner:
                        _runner_src = runners_dir() / _runner_name
                        _runner_rel = f"runners/{_runner_name}.tar.zst"
                        _runner_dest = repo_root / _runner_rel
                        _runner_dest.parent.mkdir(parents=True, exist_ok=True)
                        _reset_phase("Uploading runner\u2026")
                        _partial_dest = _runner_dest
                        _rs, _rc, _rch = compress_runner_zst(
                            _runner_src,
                            _runner_dest,
                            cancel_event=job.cancel_event,
                            bytes_cb=_bytes_cb,
                        )
                        _partial_dest = None
                        upsert_runner(repo_root, _runner_name, _runner_rel, _rc, _rs, _rch)
                        _runner_upserted = (_runner_name, _runner_dest)

                    _base_rel = f"bases/{base_image}-base.tar.zst"
                    _base_dest = repo_root / _base_rel
                    _base_dest.parent.mkdir(parents=True, exist_ok=True)
                    _reset_phase("Uploading base image\u2026")
                    _partial_dest = _base_dest
                    _bs, _bc, _bch = compress_prefix_zst(
                        base_path(base_image),
                        _base_dest,
                        cancel_event=job.cancel_event,
                        bytes_cb=_bytes_cb,
                    )
                    _partial_dest = None
                    upsert_base(repo_root, base_image, _runner_name, _base_rel, _bc, _bs, _bch)
                    _base_upserted = (base_image, _base_dest)

            _reset_phase("Finalizing\u2026")
            final_entry = _dc_replace(
                entry,
                archive_crc32=crc32,
                archive_size=size,
                archive_chunks=chunks,
                base_image=base_image,
                delta_size=_delta_uncompressed,
            )
            import_to_repo(
                repo_root,
                final_entry,
                "",
                images,
                archive_in_place=True,
                phase_cb=_reset_phase,
            )
            _app_imported = True

        except CancelledError:
            from cellar.backend.packager import (
                _remove_from_catalogue,
                _rmtree,
                remove_base,
                remove_runner,
            )

            if _partial_dest is not None:
                try:
                    _cleanup_chunks(_partial_dest)
                except Exception:
                    pass
            if _app_imported:
                try:
                    _remove_from_catalogue(repo_root, entry.id)
                except Exception:
                    pass
                try:
                    _rmtree(repo_root / "apps" / entry.id, ignore_errors=True)
                except Exception:
                    pass
            if _base_upserted:
                try:
                    remove_base(repo_root, _base_upserted[0])
                except Exception:
                    pass
            if _runner_upserted:
                try:
                    remove_runner(repo_root, _runner_upserted[0])
                except Exception:
                    pass
            if _written_archive:
                try:
                    _cleanup_chunks(archive_dest)
                except Exception:
                    pass
            raise

    def _on_job_done(self, job: PublishJob) -> None:
        with self._lock:
            self._active = None
        if job.delete_after:
            try:
                from cellar.backend.project import delete_project

                delete_project(job.project.slug)
            except Exception:  # noqa: BLE001
                log.warning("Failed to delete project %s after publish", job.project.slug)
        if self._on_complete:
            self._on_complete(PublishResult(
                app_id=job.app_id,
                app_name=job.app_name,
                repo_name=job.repo.name or job.repo.uri,
                delete_after=job.delete_after,
                project_slug=job.project.slug,
            ))
        self._notify_queue_changed()
        self._maybe_start()

    def _on_job_cancelled(self, job: PublishJob) -> None:
        with self._lock:
            self._active = None
        if self._on_cancelled:
            self._on_cancelled(job.app_id)
        self._notify_queue_changed()
        self._maybe_start()

    def _on_job_error(self, job: PublishJob, message: str) -> None:
        with self._lock:
            self._active = None
        if self._on_error:
            self._on_error(job.app_id, message)
        self._notify_queue_changed()
        self._maybe_start()

    def _notify_queue_changed(self) -> None:
        if self._on_queue_changed:
            GLib.idle_add(self._on_queue_changed)

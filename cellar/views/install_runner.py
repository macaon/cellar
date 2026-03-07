"""Runner download and install dialog.

Follows the same two-phase stack pattern as ``update_app.py``:

* **Confirmation page** — shows the runner name and a size note.
  Header: Cancel (start) and Install (end, suggested-action).
* **Progress page** — phase label + ``Gtk.ProgressBar`` + body Cancel.
  Phases: *Downloading…* (0 → 0.8) → *Extracting…* (0.8 → 1.0).

Usage::

    dialog = InstallRunnerDialog(
        runner_name="ge-proton10-32",
        url="https://github.com/.../GE-Proton10-32.tar.gz",
        checksum="sha256:abc123...",
        target_dir=Path("~/.var/.../runners/ge-proton10-32"),
        on_done=lambda name: ...,
    )
    dialog.present(parent)
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.views.widgets import make_progress_page

from cellar.utils.http import DEFAULT_TIMEOUT, make_session
from cellar.utils.progress import fmt_stats as _fmt_dl_stats, trunc_middle as _trunc_filename

log = logging.getLogger(__name__)


class InstallRunnerDialog(Adw.Dialog):
    """Two-phase dialog: confirmation → download/extract progress."""

    def __init__(
        self,
        *,
        runner_name: str,
        url: str,
        checksum: str,
        target_dir: Path,
        on_done: Callable[[str], None],
    ) -> None:
        super().__init__(title=f"Install {runner_name}", content_width=360)
        self._runner_name = runner_name
        self._url = url
        self._checksum = checksum
        self._target_dir = target_dir
        self._on_done = on_done
        self._cancel_event = threading.Event()

        self._build_ui()
        self.connect("closed", lambda _d: self._cancel_event.set())

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()

        self._header = Adw.HeaderBar()
        self._header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        self._header.pack_start(cancel_btn)

        self._install_btn = Gtk.Button(label="Install")
        self._install_btn.add_css_class("suggested-action")
        self._install_btn.connect("clicked", self._on_proceed_clicked)
        self._header.pack_end(self._install_btn)

        toolbar_view.add_top_bar(self._header)

        self._stack = Gtk.Stack()
        self._stack.add_named(self._build_confirm_page(), "confirm")
        self._stack.add_named(self._build_progress_page(), "progress")
        self._stack.set_visible_child_name("confirm")

        toolbar_view.set_content(self._stack)
        self.set_child(toolbar_view)

    def _build_confirm_page(self) -> Gtk.Widget:
        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        scroll.set_propagate_natural_height(True)

        page = Adw.PreferencesPage()
        scroll.set_child(page)

        group = Adw.PreferencesGroup(title="Runner")
        row = Adw.ActionRow(
            title=self._runner_name,
            subtitle=(
                "This runner will be downloaded and installed for Cellar. "
                "The download size may be several hundred megabytes."
            ),
        )
        row.add_prefix(Gtk.Image.new_from_icon_name("media-playback-start-symbolic"))
        group.add(row)
        page.add(group)

        return scroll

    def _build_progress_page(self) -> Gtk.Widget:
        box, self._phase_label, self._progress_bar, self._cancel_body_btn = (
            make_progress_page("Downloading runner\u2026", self._on_cancel_progress_clicked)
        )
        return box

    # ── Signal handlers ───────────────────────────────────────────────────

    def _on_proceed_clicked(self, _btn) -> None:
        self._stack.set_visible_child_name("progress")
        self._header.set_visible(False)
        self._start_download()

    def _on_cancel_progress_clicked(self, _btn) -> None:
        self._cancel_event.set()
        self._phase_label.set_text("Cancelling…")
        self._cancel_body_btn.set_sensitive(False)

    # ── Download thread ───────────────────────────────────────────────────

    def _start_download(self) -> None:
        def _progress(fraction: float) -> None:
            GLib.idle_add(self._progress_bar.set_fraction, fraction)

        def _stats(downloaded: int, total: int, speed: float) -> None:
            text = _fmt_dl_stats(downloaded, total, speed)
            GLib.idle_add(self._progress_bar.set_text, text)

        def _phase(text: str) -> None:
            GLib.idle_add(self._phase_label.set_text, text)
            # Clear stats/name text when moving to extract phase.
            GLib.idle_add(self._progress_bar.set_text, "")

        _last_name_t = [0.0]

        def _name(filename: str) -> None:
            now = time.monotonic()
            if now - _last_name_t[0] >= 0.08:
                _last_name_t[0] = now
                GLib.idle_add(self._progress_bar.set_text, _trunc_filename(filename))

        def _run() -> None:
            try:
                _download_and_extract_runner(
                    url=self._url,
                    checksum=self._checksum,
                    target_dir=self._target_dir,
                    progress_cb=_progress,
                    stats_cb=_stats,
                    phase_cb=_phase,
                    name_cb=_name,
                    cancel_event=self._cancel_event,
                )
                GLib.idle_add(self._on_done_ui, self._runner_name)
            except _Cancelled:
                GLib.idle_add(self.close)
            except Exception as exc:  # noqa: BLE001
                GLib.idle_add(self._on_error, str(exc))

        threading.Thread(target=_run, daemon=True).start()

    def _on_done_ui(self, runner_name: str) -> None:
        self.close()
        self._on_done(runner_name)

    def _on_error(self, message: str) -> None:
        self._cancel_body_btn.set_sensitive(False)
        alert = Adw.AlertDialog(heading="Install Failed", body=message)
        alert.add_response("ok", "OK")
        alert.connect("response", lambda _d, _r: self.close())
        alert.present(self)


# ---------------------------------------------------------------------------
# Download + extract helper (private)
# ---------------------------------------------------------------------------


class _Cancelled(Exception):
    """Raised when the user cancels the operation."""


def _download_and_extract_runner(
    *,
    url: str,
    checksum: str,
    target_dir: Path,
    progress_cb: Callable[[float], None],
    phase_cb: Callable[[str], None],
    cancel_event: threading.Event,
    stats_cb: Callable[[int, int, float], None] | None = None,
    name_cb: Callable[[str], None] | None = None,
) -> None:
    """Download the runner archive, verify it, and extract it to *target_dir*.

    Progress is reported via *progress_cb* in distinct phases, each 0 → 1:
      - **Downloading…** — HTTP stream
      - **Extracting…** — per-member tarfile extraction

    *stats_cb*, when provided, is called as ``stats_cb(downloaded, total, speed_bps)``
    during the download phase so the UI can show size/speed text.

    *name_cb*, when provided, is called as ``name_cb(filename)`` before each
    member is extracted so the UI can show the current file name.

    Raises ``_Cancelled`` if *cancel_event* is set during the operation.
    """
    # Determine hash algorithm from checksum format.
    if checksum:
        raw = checksum.removeprefix("sha256:").removeprefix("md5:")
        if len(raw) == 32:
            hash_algo = "md5"
        else:
            hash_algo = "sha256"
        expected_hash: str | None = raw
    else:
        hash_algo = "sha256"
        expected_hash = None

    # ── Download ──────────────────────────────────────────────────────────
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".tar.gz")
    tmp_path = Path(tmp_name)
    try:
        session = make_session()
        hasher = hashlib.new(hash_algo)
        import os
        try:
            resp = session.get(url, stream=True, timeout=DEFAULT_TIMEOUT)
            resp.raise_for_status()
            total = int(resp.headers.get("Content-Length", 0) or 0)
            downloaded = 0
            start = time.monotonic()
            with os.fdopen(tmp_fd, "wb") as f:
                tmp_fd = -1  # ownership transferred
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if cancel_event.is_set():
                        raise _Cancelled
                    f.write(chunk)
                    hasher.update(chunk)
                    downloaded += len(chunk)
                    elapsed = time.monotonic() - start
                    speed = downloaded / elapsed if elapsed > 0.1 else 0.0
                    if stats_cb:
                        stats_cb(downloaded, total, speed)
                    if total:
                        progress_cb(min(downloaded / total, 1.0))
        finally:
            if tmp_fd >= 0:
                os.close(tmp_fd)

        if expected_hash and hasher.hexdigest() != expected_hash:
            raise ValueError(
                f"{hash_algo.upper()} mismatch for {url!r}: "
                f"expected {expected_hash}, got {hasher.hexdigest()}"
            )

        progress_cb(1.0)
        if cancel_event.is_set():
            raise _Cancelled

        # ── Extract ───────────────────────────────────────────────────────
        phase_cb("Extracting runner\u2026")
        progress_cb(0.0)
        use_filter = sys.version_info >= (3, 12)
        with tempfile.TemporaryDirectory() as extract_dir:
            archive_size = tmp_path.stat().st_size or 1
            with open(tmp_path, "rb") as raw:
                with tarfile.open(fileobj=raw, mode="r:gz") as tar:
                    for member in tar:
                        if cancel_event.is_set():
                            raise _Cancelled
                        if name_cb:
                            name_cb(Path(member.name).name or member.name)
                        if use_filter:
                            tar.extract(member, extract_dir, filter="data")
                        else:
                            tar.extract(member, extract_dir)  # noqa: S202
                        progress_cb(min(raw.tell() / archive_size, 1.0))

            if cancel_event.is_set():
                raise _Cancelled

            # Find the single top-level directory produced by the tarball.
            entries = list(Path(extract_dir).iterdir())
            if len(entries) == 1 and entries[0].is_dir():
                extracted_dir = entries[0]
            else:
                # Tarball extracted flat — use the temp dir itself.
                extracted_dir = Path(extract_dir)

            target_dir.parent.mkdir(parents=True, exist_ok=True)
            if target_dir.exists():
                shutil.rmtree(target_dir)
            shutil.move(str(extracted_dir), str(target_dir))

        progress_cb(1.0)

    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

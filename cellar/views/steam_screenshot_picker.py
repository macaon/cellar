"""Steam screenshot selection dialog.

Shows thumbnails from a Steam ``fetch_details`` result and lets the user
choose which full-resolution screenshots to include.  Also provides an
"Add local files" button for manually selected images.

``on_confirmed`` is called with
``(selected_full_urls: list[str], local_paths: list[str])``.
The caller is responsible for downloading the Steam URLs and running them
through ``optimize_image`` before adding them to the project.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk

log = logging.getLogger(__name__)


class SteamScreenshotPickerDialog(Adw.Dialog):
    """Modal dialog for picking screenshots from a Steam lookup result.

    *screenshots_data* is a list of ``{"thumbnail": url, "full": url}`` dicts
    as returned by :func:`cellar.backend.steam.fetch_details`.
    All screenshots start unselected; the user checks the ones they want.
    """

    def __init__(
        self,
        *,
        screenshots_data: list[dict],
        on_confirmed,
    ) -> None:
        super().__init__(title="Select Screenshots", content_width=560, content_height=520)
        self._data = screenshots_data
        self._on_confirmed = on_confirmed
        self._selected: set[int] = set()
        self._local_paths: list[str] = []
        self._tmp_dir: Path | None = None
        self._chooser = None

        self._pictures: list[Gtk.Picture] = []
        self._checks: list[Gtk.CheckButton] = []

        self._build_ui()
        self._load_thumbnails()
        self.connect("closed", self._cleanup)

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _: self.close())
        header.pack_start(cancel_btn)

        done_btn = Gtk.Button(label="Done")
        done_btn.add_css_class("suggested-action")
        done_btn.connect("clicked", self._on_done)
        header.pack_end(done_btn)

        toolbar.add_top_bar(header)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        scroll = Gtk.ScrolledWindow(
            hscrollbar_policy=Gtk.PolicyType.NEVER,
            vscrollbar_policy=Gtk.PolicyType.AUTOMATIC,
        )
        scroll.set_vexpand(True)

        flow = Gtk.FlowBox()
        flow.set_valign(Gtk.Align.START)
        flow.set_max_children_per_line(2)
        flow.set_min_children_per_line(1)
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_margin_top(8)
        flow.set_margin_bottom(8)
        flow.set_margin_start(8)
        flow.set_margin_end(8)
        flow.set_row_spacing(8)
        flow.set_column_spacing(8)
        scroll.set_child(flow)
        outer.append(scroll)

        for i in range(len(self._data)):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.set_margin_top(4)
            card.set_margin_bottom(4)
            card.set_margin_start(4)
            card.set_margin_end(4)

            pic = Gtk.Picture()
            pic.set_size_request(240, 135)
            pic.set_content_fit(Gtk.ContentFit.CONTAIN)
            card.append(pic)
            self._pictures.append(pic)

            cb = Gtk.CheckButton(label=f"Screenshot {i + 1}", active=False)
            cb.connect("toggled", self._on_toggle, i)
            card.append(cb)
            self._checks.append(cb)

            flow.append(card)

        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bottom.set_margin_top(8)
        bottom.set_margin_bottom(12)
        bottom.set_margin_start(12)
        bottom.set_margin_end(12)

        self._local_label = Gtk.Label(label="No additional local files", xalign=0)
        self._local_label.set_hexpand(True)
        self._local_label.add_css_class("dim-label")
        bottom.append(self._local_label)

        add_btn = Gtk.Button(label="Add local files\u2026")
        add_btn.connect("clicked", self._on_add_local)
        bottom.append(add_btn)

        outer.append(bottom)

        toolbar.set_content(outer)
        self.set_child(toolbar)

    def _cleanup(self, _dialog=None) -> None:
        if self._tmp_dir and self._tmp_dir.exists():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)
            self._tmp_dir = None

    # ------------------------------------------------------------------
    # Thumbnail loading
    # ------------------------------------------------------------------

    def _load_thumbnails(self) -> None:
        from cellar.utils.async_work import run_in_background

        self._tmp_dir = Path(tempfile.mkdtemp(prefix="cellar-ss-"))
        for i, item in enumerate(self._data):
            url = item["thumbnail"]
            dest = self._tmp_dir / f"thumb_{i}.jpg"

            def _work(url=url, dest=dest) -> str | None:
                from cellar.utils.http import make_session
                resp = make_session().get(url, timeout=15)
                if resp.ok:
                    dest.write_bytes(resp.content)
                    return str(dest)
                return None

            def _done(path: str | None, idx=i) -> None:
                if path is not None:
                    self._on_thumbnail_ready(idx, path)

            run_in_background(work=_work, on_done=_done)

    def _on_thumbnail_ready(self, idx: int, path: str) -> None:
        if 0 <= idx < len(self._pictures):
            self._pictures[idx].set_filename(path)

    # ------------------------------------------------------------------
    # Interaction
    # ------------------------------------------------------------------

    def _on_toggle(self, cb: Gtk.CheckButton, idx: int) -> None:
        if cb.get_active():
            self._selected.add(idx)
        else:
            self._selected.discard(idx)

    def _on_add_local(self, _btn) -> None:
        chooser = Gtk.FileChooserNative(
            title="Add Local Screenshot Files",
            transient_for=self.get_root(),
            action=Gtk.FileChooserAction.OPEN,
            select_multiple=True,
        )
        img_filter = Gtk.FileFilter()
        img_filter.set_name("Images (PNG, JPG)")
        img_filter.add_mime_type("image/png")
        img_filter.add_mime_type("image/jpeg")
        chooser.add_filter(img_filter)
        chooser.connect("response", self._on_local_chosen, chooser)
        chooser.show()
        self._chooser = chooser

    def _on_local_chosen(self, _c, response: int, chooser) -> None:
        if response != Gtk.ResponseType.ACCEPT:
            return
        files = chooser.get_files()
        paths = [files.get_item(i).get_path() for i in range(files.get_n_items())]
        self._local_paths.extend(p for p in paths if p)
        count = len(self._local_paths)
        self._local_label.set_text(
            f"{count} local file{'s' if count != 1 else ''} added"
        )

    def _on_done(self, _btn) -> None:
        selected_urls = [
            self._data[i]["full"]
            for i in sorted(self._selected)
            if i < len(self._data)
        ]
        self.close()
        self._on_confirmed(selected_urls, self._local_paths)

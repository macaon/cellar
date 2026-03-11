"""Progress dialog widgets used by multiple builder dialogs."""

from __future__ import annotations

import re

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.views.widgets import set_margins


class ProgressDialog(Adw.Dialog):
    """Simple blocking progress dialog for long-running operations.

    When *cancel_event* is provided, a Cancel button is shown that sets the
    event when clicked, allowing the background thread to abort gracefully.
    """

    def __init__(self, label: str, cancel_event: "threading.Event | None" = None) -> None:
        super().__init__(content_width=340, content_height=120)
        self.set_can_close(False)
        self._cancel_event = cancel_event

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        set_margins(box, 24)
        box.set_valign(Gtk.Align.CENTER)
        box.set_vexpand(True)

        self._label = Gtk.Label(label=label, xalign=0.5)
        self._label.add_css_class("dim-label")

        self._bar = Gtk.ProgressBar()
        self._bar.set_show_text(True)
        self._bar.set_text("")
        self._pulse_id = GLib.timeout_add(80, self._pulse)

        box.append(self._label)
        box.append(self._bar)

        if cancel_event is not None:
            self._cancel_btn = Gtk.Button(label="Cancel")
            self._cancel_btn.set_halign(Gtk.Align.CENTER)
            self._cancel_btn.add_css_class("pill")
            self._cancel_btn.connect("clicked", self._on_cancel)
            box.append(self._cancel_btn)

        self.set_child(box)

    def _on_cancel(self, _btn) -> None:
        if self._cancel_event is not None:
            self._cancel_event.set()
        self._cancel_btn.set_sensitive(False)
        self._cancel_btn.set_label("Cancelling\u2026")

    def _pulse(self) -> bool:
        self._bar.pulse()
        return True

    def set_label(self, text: str) -> None:
        self._label.set_text(text)

    def set_fraction(self, fraction: float) -> None:
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self._bar.set_fraction(fraction)

    def set_stats(self, text: str) -> None:
        self._bar.set_text(text)

    def start_pulse(self) -> None:
        """Switch back to indeterminate pulse (e.g. after compress phase ends)."""
        if self._pulse_id is None:
            self._bar.set_fraction(0.0)
            self._pulse_id = GLib.timeout_add(80, self._pulse)

    def force_close(self) -> None:
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self.set_can_close(True)
        self.close()


class WinetricksProgressDialog(Adw.Dialog):
    """Progress dialog for winetricks verb installation.

    Streams output from the winetricks subprocess line-by-line via
    :meth:`push_line`.  Parses key lines to show a human-readable
    "current operation" label and appends all output to a scrollable log.
    """

    # Regex patterns for line classification
    _RE_DOWNLOADING = re.compile(
        r"Downloading https?://\S+\s+to\s+.*/winetricks/(\w+)", re.IGNORECASE
    )
    _RE_SKIP = re.compile(r"^(\S+) already installed, skipping")
    _RE_RUNNING = re.compile(
        r"Running winetricks verbs[^:]*:\s*(.+)$", re.IGNORECASE
    )
    # curl progress lines: start with whitespace+digits or the header row
    _RE_CURL = re.compile(r"^\s*(%\s+Total|[\d\s]+%)")

    _MAX_LOG_LINES = 200

    def __init__(self, verbs: list[str]) -> None:
        super().__init__(content_width=480)
        self.set_can_close(False)

        toolbar = Adw.ToolbarView()
        header = Adw.HeaderBar(show_start_title_buttons=False, show_end_title_buttons=False)
        header.set_title_widget(Gtk.Label(label="Installing Dependencies"))
        toolbar.add_top_bar(header)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        set_margins(box, 16)

        verb_label = Gtk.Label(label="Running: " + ", ".join(verbs))
        verb_label.set_xalign(0)
        verb_label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        verb_label.add_css_class("caption")
        verb_label.add_css_class("dim-label")
        box.append(verb_label)

        self._bar = Gtk.ProgressBar()
        self._bar.set_show_text(True)
        self._bar.set_text("")
        self._pulse_id = GLib.timeout_add(80, self._pulse)
        box.append(self._bar)

        self._status_label = Gtk.Label(label="Starting\u2026")
        self._status_label.set_xalign(0)
        self._status_label.set_ellipsize(3)  # Pango.EllipsizeMode.END
        box.append(self._status_label)

        # Scrollable log view
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_min_content_height(200)
        scroll.set_vexpand(True)
        scroll.add_css_class("card")

        self._text_buffer = Gtk.TextBuffer()
        text_view = Gtk.TextView(buffer=self._text_buffer, editable=False, cursor_visible=False)
        text_view.set_monospace(True)
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_margin_top(6)
        text_view.set_margin_bottom(6)
        text_view.set_margin_start(8)
        text_view.set_margin_end(8)
        scroll.set_child(text_view)
        box.append(scroll)

        self._log_lines: list[str] = []
        self._scroll = scroll

        toolbar.set_content(box)
        self.set_child(toolbar)

    def _pulse(self) -> bool:
        self._bar.pulse()
        return True

    def push_line(self, line: str) -> None:
        """Called from GLib.idle_add with each output line from winetricks."""
        # Update status label from meaningful lines
        m = self._RE_DOWNLOADING.search(line)
        if m:
            self._status_label.set_text(f"Downloading {m.group(1)}\u2026")
        elif self._RE_RUNNING.search(line):
            self._status_label.set_text("Running winetricks\u2026")
        elif self._RE_SKIP.match(line):
            verb = self._RE_SKIP.match(line).group(1)
            self._status_label.set_text(f"{verb} already installed, skipping")

        # Append to log (skip noisy curl progress header rows)
        if not self._RE_CURL.match(line):
            self._log_lines.append(line)
            if len(self._log_lines) > self._MAX_LOG_LINES:
                self._log_lines.pop(0)
            self._text_buffer.set_text("\n".join(self._log_lines))
            # Auto-scroll to bottom
            end_iter = self._text_buffer.get_end_iter()
            self._text_buffer.place_cursor(end_iter)
            adj = self._scroll.get_vadjustment()
            adj.set_value(adj.get_upper())

    def force_close(self) -> None:
        if self._pulse_id is not None:
            GLib.source_remove(self._pulse_id)
            self._pulse_id = None
        self.set_can_close(True)
        self.close()

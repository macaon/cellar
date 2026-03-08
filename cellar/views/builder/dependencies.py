"""Dependency picker dialog — browse and install winetricks verbs."""

from __future__ import annotations

import html
import logging
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk

from cellar.backend.project import Project, save_project
from cellar.utils.async_work import run_in_background
from cellar.views.builder.progress import WinetricksProgressDialog

log = logging.getLogger(__name__)

# Curated winetricks verbs grouped by category and sorted alphabetically.
# Format: (category_name, [(verb, description), ...])
_VERB_CATALOGUE: list[tuple[str, list[tuple[str, str]]]] = [
    ("Visual C++ Runtimes", [
        ("vcrun2003", "Visual C++ 2003 Redistributable"),
        ("vcrun2005", "Visual C++ 2005 Redistributable"),
        ("vcrun2008", "Visual C++ 2008 Redistributable"),
        ("vcrun2010", "Visual C++ 2010 Redistributable"),
        ("vcrun2012", "Visual C++ 2012 Redistributable"),
        ("vcrun2013", "Visual C++ 2013 Redistributable"),
        ("vcrun2015", "Visual C++ 2015 Redistributable"),
        ("vcrun2017", "Visual C++ 2017 Redistributable"),
        ("vcrun2019", "Visual C++ 2019 Redistributable"),
        ("vcrun2022", "Visual C++ 2022 Redistributable"),
        ("vcrun6",    "Visual C++ 6.0 SP6 runtime"),
    ]),
    (".NET Framework", [
        ("dotnet11",  ".NET Framework 1.1"),
        ("dotnet20",  ".NET Framework 2.0"),
        ("dotnet30",  ".NET Framework 3.0"),
        ("dotnet35",  ".NET Framework 3.5"),
        ("dotnet40",  ".NET Framework 4.0"),
        ("dotnet45",  ".NET Framework 4.5"),
        ("dotnet452", ".NET Framework 4.5.2"),
        ("dotnet46",  ".NET Framework 4.6"),
        ("dotnet461", ".NET Framework 4.6.1"),
        ("dotnet462", ".NET Framework 4.6.2"),
        ("dotnet471", ".NET Framework 4.7.1"),
        ("dotnet472", ".NET Framework 4.7.2"),
        ("dotnet48",  ".NET Framework 4.8"),
        ("dotnet6",   ".NET 6.0 desktop runtime"),
        ("dotnet7",   ".NET 7.0 desktop runtime"),
        ("dotnet8",   ".NET 8.0 desktop runtime"),
    ]),
    ("DirectX", [
        ("d3dcompiler_43", "D3DCompiler 43"),
        ("d3dcompiler_47", "D3DCompiler 47"),
        ("d3dx10",         "DirectX 10 DLLs"),
        ("d3dx11_42",      "DirectX 11 DLL (d3dx11_42)"),
        ("d3dx11_43",      "DirectX 11 DLL (d3dx11_43)"),
        ("d3dx9",          "DirectX 9 DLLs (all versions)"),
        ("dinput8",        "DirectInput 8"),
        ("xact",           "XACT Engine"),
        ("xactengine3_7",  "XACT Engine 3.7"),
    ]),
    ("Media & Codecs", [
        ("amstream",   "DirectShow amstream.dll"),
        ("devenum",    "DirectShow devenum.dll"),
        ("lavfilters", "LAV Filters (open-source media codecs)"),
        ("openal",     "OpenAL audio library"),
        ("quartz",     "DirectShow quartz.dll"),
        ("wmp10",      "Windows Media Player 10"),
        ("wmp11",      "Windows Media Player 11"),
        ("wmp9",       "Windows Media Player 9"),
        ("wmv9vcm",    "MS WMV9 Video Codec"),
    ]),
    ("Fonts", [
        ("allfonts",   "All winetricks fonts"),
        ("corefonts",  "Microsoft Core Fonts (Arial, Times New Roman\u2026)"),
        ("liberation", "Liberation fonts (free Arial/Times/Courier)"),
        ("tahoma",     "MS Tahoma"),
    ]),
    ("System DLLs", [
        ("gdiplus",  "Microsoft GDI+"),
        ("mfc100",   "Microsoft Foundation Classes 10.0"),
        ("mfc110",   "Microsoft Foundation Classes 11.0"),
        ("mfc120",   "Microsoft Foundation Classes 12.0"),
        ("mfc140",   "Microsoft Foundation Classes 14.0"),
        ("mfc42",    "Microsoft Foundation Classes 4.2"),
        ("msvcirt",  "MS VC++ 6.0 C++ runtime (msvcirt.dll)"),
        ("msxml3",   "MS XML 3.0"),
        ("msxml4",   "MS XML 4.0"),
        ("msxml6",   "MS XML 6.0 SP1"),
    ]),
    ("Game Runtimes", [
        ("gfw",   "Games for Windows LIVE"),
        ("physx", "NVIDIA PhysX"),
        ("xna31", "Microsoft XNA Framework 3.1"),
        ("xna40", "Microsoft XNA Framework 4.0"),
    ]),
]


class DependencyPickerDialog(Adw.Dialog):
    """Browse and install winetricks dependencies.

    Presents verbs grouped in collapsible Adw.ExpanderRow sections (one per
    category).  Each verb row has a per-row install (download icon) or remove
    (trash icon) button; a spinner replaces the button while installing.
    The search entry in the header bar auto-expands matching sections and hides
    non-matching verbs.
    """

    def __init__(self, project: Project, on_dep_changed: Callable, runner_name: str = "") -> None:
        super().__init__(title="Dependencies", content_width=500)
        self._project = project
        # Allow callers to pass the resolved GE-Proton runner name directly.
        # For app projects project.runner is the base image name, not the runner.
        self._runner_name = runner_name or project.runner
        self._on_dep_changed = on_dep_changed
        # list of (ExpanderRow, [ActionRow, ...]) for search visibility control
        self._category_rows: list[tuple[Adw.ExpanderRow, list[Adw.ActionRow]]] = []

        toolbar = Adw.ToolbarView()

        # Header with search entry as title widget
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda _: self.close())
        header.pack_start(close_btn)

        self._search_entry = Gtk.SearchEntry()
        self._search_entry.set_placeholder_text("Search\u2026")
        self._search_entry.set_hexpand(True)
        self._search_entry.connect("search-changed", self._on_search_changed)
        header.set_title_widget(self._search_entry)
        toolbar.add_top_bar(header)

        # Main scroll area
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_min_content_height(420)
        scroll.set_vexpand(True)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._list_box.add_css_class("boxed-list")
        self._list_box.set_margin_top(12)
        self._list_box.set_margin_bottom(12)
        self._list_box.set_margin_start(12)
        self._list_box.set_margin_end(12)

        for category, verbs in _VERB_CATALOGUE:
            exp_row = Adw.ExpanderRow(title=html.escape(category))
            verb_rows: list[Adw.ActionRow] = []

            for verb, description in verbs:
                verb_row = Adw.ActionRow(title=verb, subtitle=html.escape(description))
                verb_row._verb = verb  # type: ignore[attr-defined]
                verb_row._search_key = f"{verb} {description} {category}".lower()  # type: ignore[attr-defined]
                suffix = self._make_suffix(verb)
                verb_row._suffix_stack = suffix  # type: ignore[attr-defined]
                verb_row.add_suffix(suffix)
                exp_row.add_row(verb_row)
                verb_rows.append(verb_row)

            self._list_box.append(exp_row)
            self._category_rows.append((exp_row, verb_rows))

        scroll.set_child(self._list_box)
        toolbar.set_content(scroll)
        self.set_child(toolbar)

    # ── Suffix stack: idle / installing / installed ─────────────────────────

    def _make_suffix(self, verb: str) -> Gtk.Stack:
        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)

        # idle: download button
        install_btn = Gtk.Button(icon_name="folder-download-symbolic")
        install_btn.set_valign(Gtk.Align.CENTER)
        install_btn.set_tooltip_text(f"Install {verb}")
        install_btn.add_css_class("flat")
        install_btn.connect("clicked", self._on_install_clicked, verb, stack)
        stack.add_named(install_btn, "idle")

        # installing: spinner
        spinner = Gtk.Spinner(spinning=True)
        spinner.set_valign(Gtk.Align.CENTER)
        spinner.set_size_request(16, 16)
        stack.add_named(spinner, "installing")

        # installed: check icon only (winetricks has no reliable uninstall)
        check = Gtk.Image.new_from_icon_name("check-round-outline2-symbolic")
        check.set_valign(Gtk.Align.CENTER)
        check.add_css_class("success")
        stack.add_named(check, "installed")

        state = "installed" if verb in self._project.deps_installed else "idle"
        stack.set_visible_child_name(state)
        return stack

    # ── Search ──────────────────────────────────────────────────────────────

    def _on_search_changed(self, _entry) -> None:
        query = self._search_entry.get_text().lower().strip()
        if not query:
            for exp_row, verb_rows in self._category_rows:
                exp_row.set_visible(True)
                exp_row.set_expanded(False)
                for vr in verb_rows:
                    vr.set_visible(True)
            return

        for exp_row, verb_rows in self._category_rows:
            has_match = False
            for vr in verb_rows:
                match = query in vr._search_key  # type: ignore[attr-defined]
                vr.set_visible(match)
                if match:
                    has_match = True
            exp_row.set_visible(has_match)
            if has_match:
                exp_row.set_expanded(True)

    # ── Install handlers ────────────────────────────────────────────────────

    def _on_install_clicked(self, _btn, verb: str, stack: Gtk.Stack) -> None:
        self._install_verbs([verb], stack)

    def _install_verbs(self, verbs: list[str], stack: Gtk.Stack) -> None:
        dlg = WinetricksProgressDialog(verbs)
        dlg.present(self)

        def _work():
            from cellar.backend.umu import run_winetricks
            result = run_winetricks(
                self._project.content_path,
                self._runner_name,
                verbs,
                line_cb=lambda line: GLib.idle_add(dlg.push_line, line),
            )
            return result.returncode == 0

        def _finish(ok: bool) -> None:
            dlg.force_close()
            if ok:
                for v in verbs:
                    if v not in self._project.deps_installed:
                        self._project.deps_installed.append(v)
                save_project(self._project)
                stack.set_visible_child_name("installed")
                self._on_dep_changed()
            else:
                stack.set_visible_child_name("idle")
                log.warning("winetricks install failed for: %s", verbs)

        def _on_err(msg: str) -> None:
            log.error("run_winetricks failed: %s", msg)
            _finish(False)

        run_in_background(_work, on_done=_finish, on_error=_on_err)


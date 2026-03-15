"""Tests for cellar.backend.dosbox — GOG DOSBox detection, config parsing, conversion."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from cellar.backend.dosbox import (
    AutoexecInfo,
    GogDosboxInfo,
    MountCmd,
    detect_gog_dosbox,
    detect_gog_dosbox_in_prefix,
    generate_overrides_conf,
    parse_gog_confs,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_goggame_info(
    folder: Path,
    game_id: str = "1435829353",
    game_name: str = "The Elder Scrolls II: Daggerfall",
    dosbox_path: str = "DOSBOX\\dosbox.exe",
    arguments: str = '-conf "..\\dosbox_daggerfall.conf" -conf "..\\dosbox_daggerfall_single.conf" -noconsole -c "exit"',
    working_dir: str = "DOSBOX",
    is_primary: bool = True,
    category: str = "game",
) -> Path:
    """Write a synthetic goggame-*.info file."""
    data = {
        "gameId": game_id,
        "name": game_name,
        "playTasks": [
            {
                "path": dosbox_path,
                "arguments": arguments,
                "category": category,
                "isPrimary": is_primary,
                "type": "FileTask",
                "workingDir": working_dir,
            }
        ],
    }
    info_path = folder / f"goggame-{game_id}.info"
    info_path.write_text(json.dumps(data), encoding="utf-8")
    return info_path


def _write_conf(folder: Path, name: str, content: str) -> Path:
    """Write a DOSBox .conf file."""
    conf = folder / name
    conf.write_text(textwrap.dedent(content), encoding="utf-8")
    return conf


# ---------------------------------------------------------------------------
# detect_gog_dosbox
# ---------------------------------------------------------------------------


class TestDetectGogDosbox:
    """Tests for GOG DOSBox detection via goggame-*.info parsing."""

    def test_detects_dosbox_game(self, tmp_path: Path) -> None:
        _write_goggame_info(tmp_path)
        result = detect_gog_dosbox(tmp_path)
        assert result is not None
        assert result.game_id == "1435829353"
        assert result.game_name == "The Elder Scrolls II: Daggerfall"
        assert result.dosbox_dir == "DOSBOX"
        assert len(result.conf_args) == 2
        assert "../dosbox_daggerfall.conf" in result.conf_args
        assert "../dosbox_daggerfall_single.conf" in result.conf_args

    def test_returns_none_for_non_dosbox_game(self, tmp_path: Path) -> None:
        _write_goggame_info(
            tmp_path,
            game_id="999",
            dosbox_path="Game\\game.exe",
            arguments="",
            working_dir="Game",
        )
        result = detect_gog_dosbox(tmp_path)
        assert result is None

    def test_returns_none_for_no_info_files(self, tmp_path: Path) -> None:
        assert detect_gog_dosbox(tmp_path) is None

    def test_returns_none_for_malformed_json(self, tmp_path: Path) -> None:
        (tmp_path / "goggame-123.info").write_text("not json")
        assert detect_gog_dosbox(tmp_path) is None

    def test_case_insensitive_dosbox_detection(self, tmp_path: Path) -> None:
        _write_goggame_info(tmp_path, dosbox_path="dosbox\\DOSBox.EXE")
        result = detect_gog_dosbox(tmp_path)
        assert result is not None

    def test_multiple_play_tasks(self, tmp_path: Path) -> None:
        data = {
            "gameId": "123",
            "name": "Test Game",
            "playTasks": [
                {
                    "path": "DOSBOX\\dosbox.exe",
                    "arguments": '-conf "..\\game.conf"',
                    "category": "game",
                    "isPrimary": True,
                    "type": "FileTask",
                    "workingDir": "DOSBOX",
                },
                {
                    "path": "DOSBOX\\GOGDOSConfig.exe",
                    "arguments": "123",
                    "category": "tool",
                    "type": "FileTask",
                    "workingDir": "DOSBOX",
                },
                {
                    "path": "Manual.pdf",
                    "category": "document",
                    "type": "FileTask",
                },
            ],
        }
        (tmp_path / "goggame-123.info").write_text(json.dumps(data))
        result = detect_gog_dosbox(tmp_path)
        assert result is not None
        # Only game-category tasks are collected
        assert len(result.play_tasks) == 1

    def test_no_primary_falls_back_to_first_game_task(self, tmp_path: Path) -> None:
        _write_goggame_info(tmp_path, is_primary=False)
        result = detect_gog_dosbox(tmp_path)
        assert result is not None
        assert result.game_name == "The Elder Scrolls II: Daggerfall"

    def test_detects_dosbox_in_wineprefix(self, tmp_path: Path) -> None:
        """detect_gog_dosbox_in_prefix should find DOSBox games inside drive_c."""
        prefix = tmp_path / "prefix"
        game_dir = prefix / "drive_c" / "GOG Games" / "Daggerfall"
        game_dir.mkdir(parents=True)
        _write_goggame_info(game_dir)
        result = detect_gog_dosbox_in_prefix(prefix)
        assert result is not None
        folder, info = result
        assert folder == game_dir
        assert info.game_name == "The Elder Scrolls II: Daggerfall"

    def test_prefix_without_dosbox_returns_none(self, tmp_path: Path) -> None:
        """Non-DOSBox games in a prefix should return None."""
        prefix = tmp_path / "prefix"
        game_dir = prefix / "drive_c" / "GOG Games" / "SomeGame"
        game_dir.mkdir(parents=True)
        _write_goggame_info(
            game_dir, game_id="999", dosbox_path="Game\\game.exe",
            arguments="", working_dir="Game",
        )
        assert detect_gog_dosbox_in_prefix(prefix) is None

    def test_prefix_no_drive_c_returns_none(self, tmp_path: Path) -> None:
        assert detect_gog_dosbox_in_prefix(tmp_path) is None

    def test_url_tasks_ignored(self, tmp_path: Path) -> None:
        """URLTask entries (like support links) should not be considered."""
        data = {
            "gameId": "456",
            "name": "Test",
            "playTasks": [
                {
                    "link": "http://example.com",
                    "category": "game",
                    "type": "URLTask",
                },
            ],
        }
        (tmp_path / "goggame-456.info").write_text(json.dumps(data))
        assert detect_gog_dosbox(tmp_path) is None


# ---------------------------------------------------------------------------
# parse_gog_confs
# ---------------------------------------------------------------------------


class TestParseGogConfs:
    """Tests for DOSBox config file parsing."""

    def test_extracts_settings(self, tmp_path: Path) -> None:
        _write_conf(
            tmp_path,
            "game.conf",
            """\
            [cpu]
            core = auto
            cycles = fixed 50000

            [sblaster]
            sbtype = sb16
            irq = 7

            [dosbox]
            machine = svga_s3
            memsize = 63
            """,
        )
        result = parse_gog_confs([tmp_path / "game.conf"])
        settings = result["settings"]
        assert settings["cpu"]["cycles"] == "fixed 50000"
        assert settings["sblaster"]["sbtype"] == "sb16"
        assert settings["dosbox"]["memsize"] == "63"

    def test_extracts_autoexec(self, tmp_path: Path) -> None:
        _write_conf(
            tmp_path,
            "game.conf",
            """\
            [autoexec]
            @echo off
            mount C ".."
            mount C "..\\cloud_saves" -t overlay
            c:
            fall.exe z.cfg
            exit
            """,
        )
        result = parse_gog_confs([tmp_path / "game.conf"])
        autoexec = result["autoexec"]
        assert len(autoexec.mounts) == 2
        assert autoexec.mounts[0].drive == "C"
        assert autoexec.mounts[0].path == ".."
        assert autoexec.mounts[1].flags == "-t overlay"
        assert "fall.exe z.cfg" in autoexec.game_commands

    def test_later_conf_wins_autoexec(self, tmp_path: Path) -> None:
        _write_conf(
            tmp_path,
            "base.conf",
            """\
            [autoexec]
            mount C ".."
            c:
            setup.exe
            """,
        )
        _write_conf(
            tmp_path,
            "game.conf",
            """\
            [autoexec]
            mount C ".."
            c:
            fall.exe z.cfg
            """,
        )
        result = parse_gog_confs(
            [tmp_path / "base.conf", tmp_path / "game.conf"]
        )
        autoexec = result["autoexec"]
        assert "fall.exe z.cfg" in autoexec.game_commands
        assert "setup.exe" not in autoexec.game_commands

    def test_settings_merge_across_confs(self, tmp_path: Path) -> None:
        _write_conf(
            tmp_path,
            "base.conf",
            """\
            [cpu]
            cycles = auto

            [sblaster]
            sbtype = sb16
            """,
        )
        _write_conf(
            tmp_path,
            "game.conf",
            """\
            [cpu]
            cycles = fixed 50000
            """,
        )
        result = parse_gog_confs(
            [tmp_path / "base.conf", tmp_path / "game.conf"]
        )
        # Later conf wins for the same key
        assert result["settings"]["cpu"]["cycles"] == "fixed 50000"
        # Earlier conf's unique keys are preserved
        assert result["settings"]["sblaster"]["sbtype"] == "sb16"

    def test_missing_conf_file_warns(self, tmp_path: Path, caplog) -> None:
        result = parse_gog_confs([tmp_path / "nonexistent.conf"])
        assert result["settings"] == {}

    def test_empty_autoexec(self, tmp_path: Path) -> None:
        _write_conf(
            tmp_path,
            "game.conf",
            """\
            [cpu]
            cycles = auto

            [autoexec]
            """,
        )
        result = parse_gog_confs([tmp_path / "game.conf"])
        assert result["autoexec"].game_commands == ()

    def test_gog_launcher_menu_parsing(self, tmp_path: Path) -> None:
        """Real GOG launcher menu structure (Daggerfall-style)."""
        _write_conf(
            tmp_path,
            "game.conf",
            """\
            [ipx]
            ipx=false

            [autoexec]
            @ECHO OFF
            cls
            mount C ".."
            mount C "..\\cloud_saves" -t overlay
            c:
            goto launcher

            :launcher
            cls
            ECHO  The Elder Scrolls II: Daggerfall Launcher
            ECHO  1) The Elder Scrolls II: Daggerfall
            ECHO  2) Game DOS Settings
            ECHO  4) exit program

            choice /c1234 /s Which program do you want to run? [1-4]: /n
            if errorlevel 4 goto exit
            if errorlevel 2 goto setup
            if errorlevel 1 goto game

            :game
            cls
            fall.exe z.cfg
            goto exit

            :setup
            cls
            setup.exe
            goto launcher

            :exit
            exit
            """,
        )
        result = parse_gog_confs([tmp_path / "game.conf"])
        autoexec = result["autoexec"]
        assert len(autoexec.mounts) == 2
        # Game commands include the actual executables (fall.exe and setup.exe)
        assert "fall.exe z.cfg" in autoexec.game_commands
        assert "setup.exe" in autoexec.game_commands


# ---------------------------------------------------------------------------
# generate_overrides_conf
# ---------------------------------------------------------------------------


class TestGenerateOverridesConf:
    """Tests for DOSBox overrides config generation."""

    def test_rewrites_parent_paths(self, tmp_path: Path) -> None:
        _write_conf(
            tmp_path,
            "game.conf",
            """\
            [autoexec]
            mount C ".."
            mount C "..\\cloud_saves" -t overlay
            c:
            fall.exe z.cfg
            """,
        )
        result = parse_gog_confs([tmp_path / "game.conf"])
        overrides = generate_overrides_conf(result, "DOSBOX")
        assert 'mount C "."' in overrides
        assert 'mount C "cloud_saves" -t overlay' in overrides
        # Game commands are NOT in the autoexec — they're passed via CLI
        assert "fall.exe z.cfg" not in overrides

    def test_drops_exit_commands(self, tmp_path: Path) -> None:
        _write_conf(
            tmp_path,
            "game.conf",
            """\
            [autoexec]
            mount C ".."
            c:
            fall.exe z.cfg
            exit
            """,
        )
        result = parse_gog_confs([tmp_path / "game.conf"])
        overrides = generate_overrides_conf(result, "DOSBOX")
        assert "exit" not in overrides.lower().split("\n")

    def test_includes_extracted_settings(self, tmp_path: Path) -> None:
        _write_conf(
            tmp_path,
            "game.conf",
            """\
            [cpu]
            cycles = fixed 50000

            [sblaster]
            sbtype = sb16

            [autoexec]
            mount C ".."
            c:
            game.exe
            """,
        )
        result = parse_gog_confs([tmp_path / "game.conf"])
        overrides = generate_overrides_conf(result, "DOSBOX")
        assert "cycles = fixed 50000" in overrides
        assert "sbtype = sb16" in overrides

    def test_strips_gog_launcher_menu(self, tmp_path: Path) -> None:
        """GOG launcher batch menu should be stripped; game commands excluded from autoexec."""
        _write_conf(
            tmp_path,
            "game.conf",
            """\
            [autoexec]
            @ECHO OFF
            cls
            mount C ".."
            c:
            goto launcher
            :launcher
            ECHO menu text
            choice /c1234
            if errorlevel 1 goto game
            :game
            cls
            fall.exe z.cfg
            goto exit
            :exit
            exit
            """,
        )
        result = parse_gog_confs([tmp_path / "game.conf"])
        overrides = generate_overrides_conf(result, "DOSBOX")

        # Menu scaffolding should be gone
        assert "goto" not in overrides.lower()
        assert "choice" not in overrides.lower()
        assert "ECHO" not in overrides
        assert "errorlevel" not in overrides.lower()

        # Game commands are passed via CLI, not in autoexec
        assert "fall.exe" not in overrides
        # Mount commands should be preserved
        assert 'mount C "."' in overrides

    def test_quiet_launch(self, tmp_path: Path) -> None:
        """Overrides should include startup_verbosity = quiet."""
        _write_conf(
            tmp_path,
            "game.conf",
            """\
            [autoexec]
            mount C ".."
            c:
            game.exe
            """,
        )
        result = parse_gog_confs([tmp_path / "game.conf"])
        overrides = generate_overrides_conf(result, "DOSBOX")
        assert "startup_verbosity = quiet" in overrides

    def test_nounivbe_injection(self, tmp_path: Path) -> None:
        """NoUniVBE should be injected after mounts in autoexec."""
        _write_conf(
            tmp_path,
            "game.conf",
            """\
            [autoexec]
            mount C ".."
            c:
            game.exe
            """,
        )
        result = parse_gog_confs([tmp_path / "game.conf"])
        overrides = generate_overrides_conf(result, "DOSBOX")
        lines = overrides.splitlines()
        # NoUniVBE should be present after mount commands
        nounivbe_idx = next(
            (i for i, l in enumerate(lines) if "NOUNIVBE" in l.upper()), None
        )
        mount_idx = next(
            (i for i, l in enumerate(lines) if l.strip().lower().startswith("mount ")), None
        )
        assert nounivbe_idx is not None, "NoUniVBE not found in overrides"
        assert mount_idx is not None, "mount command not found in overrides"
        assert nounivbe_idx > mount_idx, "NoUniVBE should come after mount commands"
        # Game command should NOT be in autoexec
        assert "game.exe" not in overrides


# ---------------------------------------------------------------------------
# convert_gog_dosbox (structure test — no actual download)
# ---------------------------------------------------------------------------


class TestConvertGogDosbox:
    """Integration tests for the conversion pipeline.

    These tests mock ``ensure_dosbox_staging`` to avoid network calls.
    """

    def test_conversion_produces_correct_structure(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        # Set up a fake GOG game folder
        source = tmp_path / "source"
        source.mkdir()

        # Create Windows DOSBOX dir
        dosbox_win = source / "DOSBOX"
        dosbox_win.mkdir()
        (dosbox_win / "dosbox.exe").write_bytes(b"fake")
        (dosbox_win / "SDL.dll").write_bytes(b"fake")

        # Create game files
        (source / "FALL.EXE").write_bytes(b"game")
        (source / "Z.CFG").write_text("config")
        arena = source / "arena2"
        arena.mkdir()
        (arena / "data.bsa").write_bytes(b"data")

        # Create GOG configs
        _write_conf(
            source,
            "dosbox_daggerfall.conf",
            """\
            [cpu]
            cycles = fixed 50000

            [sblaster]
            sbtype = sb16
            """,
        )
        _write_conf(
            source,
            "dosbox_daggerfall_single.conf",
            """\
            [autoexec]
            mount C ".."
            c:
            fall.exe z.cfg
            exit
            """,
        )

        # Write goggame info
        _write_goggame_info(source)

        # Set up fake DOSBox Staging installation
        fake_staging = tmp_path / "staging"
        fake_staging.mkdir()
        (fake_staging / "dosbox").write_bytes(b"ELF_BINARY")
        (fake_staging / "dosbox").chmod(0o755)
        resources = fake_staging / "resources"
        resources.mkdir()
        (resources / "CP_437.TXT").write_text("codepage")
        soundfonts = fake_staging / "soundfonts"
        soundfonts.mkdir()
        (soundfonts / "default.sf2").write_bytes(b"soundfont")

        # Create portable config in staging dir (DOSBox generates this)
        (fake_staging / "dosbox-staging.conf").write_text(
            "[sdl]\nfullscreen = false\n"
        )
        # Create NoUniVBE in staging dir
        nounivbe_dir = fake_staging / "nounivbe"
        nounivbe_dir.mkdir()
        (nounivbe_dir / "NOUNIVBE.EXE").write_bytes(b"nounivbe")

        # Mock ensure_dosbox_staging
        monkeypatch.setattr(
            "cellar.backend.dosbox.ensure_dosbox_staging",
            lambda progress_cb=None: fake_staging / "dosbox",
        )

        from cellar.backend.dosbox import convert_gog_dosbox

        info = detect_gog_dosbox(source)
        assert info is not None

        dest = tmp_path / "dest"
        entry_points = convert_gog_dosbox(source, dest, info)

        # Verify structure — no launch.sh (DOSBox invoked at launch time)
        assert not (dest / "launch.sh").exists()
        assert (dest / "dosbox" / "dosbox").is_file()
        assert (dest / "dosbox" / "resources" / "CP_437.TXT").is_file()
        assert (dest / "dosbox" / "soundfonts" / "default.sf2").is_file()
        assert (dest / "config" / "dosbox-staging.conf").is_file()
        assert (dest / "config" / "dosbox-overrides.conf").is_file()

        # Game files preserved
        assert (dest / "FALL.EXE").is_file()
        assert (dest / "Z.CFG").is_file()
        assert (dest / "arena2" / "data.bsa").is_file()

        # Windows DOSBOX directory removed
        assert not (dest / "DOSBOX").exists()

        # Entry points — DOS exe files, not launch.sh
        assert len(entry_points) >= 1
        assert entry_points[0]["path"] == "fall.exe"
        assert entry_points[0]["args"] == "z.cfg"
        assert entry_points[0]["name"] == "The Elder Scrolls II: Daggerfall"

        # NoUniVBE copied
        assert (dest / "nounivbe" / "NOUNIVBE.EXE").is_file()

        # Overrides content — mounts and NoUniVBE, but NOT game commands
        overrides = (dest / "config" / "dosbox-overrides.conf").read_text()
        assert "cycles = fixed 50000" in overrides
        assert 'mount C "."' in overrides
        assert "startup_verbosity = quiet" in overrides
        assert "NOUNIVBE" in overrides
        assert "fall.exe" not in overrides


# Needed for the conversion test
import os

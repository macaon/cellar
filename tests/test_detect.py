"""Tests for cellar.backend.detect."""
from __future__ import annotations

from pathlib import Path

import pytest

from cellar.backend.detect import (
    _ELF_MAGIC,
    detect_platform,
    find_exe_files,
    find_gameinfo,
    find_linux_executables,
    parse_app_name,
    parse_version_hint,
    unsupported_reason,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_elf(path: Path) -> Path:
    """Write a minimal ELF magic header to *path*."""
    path.write_bytes(_ELF_MAGIC + b"\x00" * 12)
    return path


def make_file(path: Path, content: bytes = b"") -> Path:
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# detect_platform — single files
# ---------------------------------------------------------------------------


class TestDetectPlatformFile:
    def test_exe(self, tmp_path):
        f = make_file(tmp_path / "game.exe")
        assert detect_platform(f) == "windows"

    def test_msi(self, tmp_path):
        f = make_file(tmp_path / "installer.msi")
        assert detect_platform(f) == "windows"

    def test_bat(self, tmp_path):
        f = make_file(tmp_path / "launch.bat")
        assert detect_platform(f) == "windows"

    def test_cmd(self, tmp_path):
        f = make_file(tmp_path / "run.cmd")
        assert detect_platform(f) == "windows"

    def test_lnk(self, tmp_path):
        f = make_file(tmp_path / "shortcut.lnk")
        assert detect_platform(f) == "windows"

    def test_elf_binary(self, tmp_path):
        f = make_elf(tmp_path / "game")
        assert detect_platform(f) == "linux"

    def test_sh_is_unsupported(self, tmp_path):
        f = make_file(tmp_path / "install.sh")
        assert detect_platform(f) == "unsupported"

    def test_run_is_unsupported(self, tmp_path):
        f = make_file(tmp_path / "setup.run")
        assert detect_platform(f) == "unsupported"

    def test_zip_is_unsupported(self, tmp_path):
        f = make_file(tmp_path / "game.zip")
        assert detect_platform(f) == "unsupported"

    def test_tar_gz_is_unsupported(self, tmp_path):
        f = make_file(tmp_path / "game.tar.gz")
        assert detect_platform(f) == "unsupported"

    def test_tar_zst_is_unsupported(self, tmp_path):
        f = make_file(tmp_path / "game.tar.zst")
        assert detect_platform(f) == "unsupported"

    def test_7z_is_unsupported(self, tmp_path):
        f = make_file(tmp_path / "game.7z")
        assert detect_platform(f) == "unsupported"

    def test_unknown_extension_is_unsupported(self, tmp_path):
        f = make_file(tmp_path / "game.xyz", b"\x00\x01\x02")
        assert detect_platform(f) == "unsupported"

    def test_pdf_is_unsupported(self, tmp_path):
        f = make_file(tmp_path / "readme.pdf")
        assert detect_platform(f) == "unsupported"


# ---------------------------------------------------------------------------
# detect_platform — folders
# ---------------------------------------------------------------------------


class TestDetectPlatformFolder:
    def test_windows_folder(self, tmp_path):
        make_file(tmp_path / "game.exe")
        make_file(tmp_path / "launcher.exe")
        assert detect_platform(tmp_path) == "windows"

    def test_linux_folder(self, tmp_path):
        make_elf(tmp_path / "game")
        make_file(tmp_path / "start.sh")
        assert detect_platform(tmp_path) == "linux"

    def test_mixed_majority_windows(self, tmp_path):
        make_file(tmp_path / "a.exe")
        make_file(tmp_path / "b.exe")
        make_file(tmp_path / "launch.sh")
        assert detect_platform(tmp_path) == "windows"

    def test_mixed_majority_linux(self, tmp_path):
        make_elf(tmp_path / "game")
        make_file(tmp_path / "start.sh")
        make_file(tmp_path / "a.exe")
        assert detect_platform(tmp_path) == "linux"

    def test_empty_folder_is_ambiguous(self, tmp_path):
        assert detect_platform(tmp_path) == "ambiguous"

    def test_tie_is_ambiguous(self, tmp_path):
        make_file(tmp_path / "game.exe")
        make_file(tmp_path / "start.sh")
        assert detect_platform(tmp_path) == "ambiguous"

    def test_recursive_detection(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        make_file(sub / "game.exe")
        assert detect_platform(tmp_path) == "windows"

    def test_deep_recursive_detection(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        make_file(deep / "game.exe")
        assert detect_platform(tmp_path) == "windows"


# ---------------------------------------------------------------------------
# parse_app_name
# ---------------------------------------------------------------------------


class TestParseAppName:
    @pytest.mark.parametrize(
        "filename, expected",
        [
            # GoG installer style
            (
                "setup_songs_of_conquest_1.9.1_a10783a599_4055_(89220).exe",
                "Songs Of Conquest",
            ),
            # Simple setup prefix
            ("setup_cyberpunk_2077.exe", "Cyberpunk 2077"),
            # install_ prefix
            ("install_witcher3.exe", "Witcher3"),
            # No prefix
            ("Hollow Knight.exe", "Hollow Knight"),
            # Folder name with spaces
            ("Shadow of the Tomb Raider", "Shadow Of The Tomb Raider"),
            # Trailing version only
            ("doom_eternal_1.0.exe", "Doom Eternal"),
            # Version with build
            ("game_1.2.3_build456.exe", "Game"),
            # GOG prefix variant
            ("gog_witcher_3_1.31.exe", "Witcher 3"),
            # GoG bare version + parenthesised build ID
            ("setup_wingspan_295_(88309).exe", "Wingspan"),
            # Version + short hex + arch tag + build ID
            (
                "setup_core_keeper_1.2.0.5-1c0f_(64bit)_(89175).exe",
                "Core Keeper",
            ),
        ],
    )
    def test_parse(self, tmp_path, filename, expected):
        # Use a file path for .exe entries, folder path otherwise
        if "." in filename:
            p = tmp_path / filename
            p.write_bytes(b"")
        else:
            p = tmp_path / filename
            p.mkdir()
        assert parse_app_name(p) == expected

    def test_fallback_on_empty_result(self, tmp_path):
        # Pathological name that strips to nothing
        p = tmp_path / "setup_1.0.exe"
        p.write_bytes(b"")
        result = parse_app_name(p)
        assert result  # must return something non-empty


# ---------------------------------------------------------------------------
# parse_version_hint
# ---------------------------------------------------------------------------


class TestParseVersionHint:
    def test_semver(self, tmp_path):
        p = tmp_path / "game_1.9.1.exe"
        p.write_bytes(b"")
        assert parse_version_hint(p) == "1.9.1"

    def test_two_part(self, tmp_path):
        p = tmp_path / "game_2.0.exe"
        p.write_bytes(b"")
        assert parse_version_hint(p) == "2.0"

    def test_no_version(self, tmp_path):
        p = tmp_path / "game.exe"
        p.write_bytes(b"")
        assert parse_version_hint(p) is None

    def test_gog_style(self, tmp_path):
        p = tmp_path / "setup_game_1.9.1_a10783a599.exe"
        p.write_bytes(b"")
        assert parse_version_hint(p) == "1.9.1"

    def test_folder_version(self, tmp_path):
        p = tmp_path / "MyGame_3.14.15"
        p.mkdir()
        assert parse_version_hint(p) == "3.14.15"


# ---------------------------------------------------------------------------
# find_exe_files
# ---------------------------------------------------------------------------


class TestFindExeFiles:
    def test_top_level(self, tmp_path):
        make_file(tmp_path / "a.exe")
        make_file(tmp_path / "b.msi")
        make_file(tmp_path / "c.txt")
        found = find_exe_files(tmp_path)
        names = {p.name for p in found}
        assert names == {"a.exe", "b.msi"}

    def test_recursive(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        make_file(sub / "game.exe")
        found = find_exe_files(tmp_path)
        assert any(p.name == "game.exe" for p in found)

    def test_finds_nested_and_top_level(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        make_file(sub / "nested.exe")
        make_file(tmp_path / "top.exe")
        found = find_exe_files(tmp_path)
        names = {p.name for p in found}
        assert names == {"top.exe", "nested.exe"}

    def test_deep_recursive(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        deep.mkdir(parents=True)
        make_file(deep / "deep.exe")
        found = find_exe_files(tmp_path)
        assert any(p.name == "deep.exe" for p in found)

    def test_no_duplicates(self, tmp_path):
        make_file(tmp_path / "game.exe")
        found = find_exe_files(tmp_path)
        assert len(found) == len(set(found))


# ---------------------------------------------------------------------------
# find_linux_executables
# ---------------------------------------------------------------------------


class TestFindLinuxExecutables:
    def test_sh_script(self, tmp_path):
        make_file(tmp_path / "start.sh")
        found = find_linux_executables(tmp_path)
        assert any(p.name == "start.sh" for p in found)

    def test_run_script(self, tmp_path):
        make_file(tmp_path / "install.run")
        found = find_linux_executables(tmp_path)
        assert any(p.name == "install.run" for p in found)

    def test_elf_binary(self, tmp_path):
        make_elf(tmp_path / "game")
        found = find_linux_executables(tmp_path)
        assert any(p.name == "game" for p in found)

    def test_non_elf_no_extension(self, tmp_path):
        make_file(tmp_path / "notelf", b"\x00\x01\x02\x03")
        found = find_linux_executables(tmp_path)
        assert not any(p.name == "notelf" for p in found)

    def test_recursive(self, tmp_path):
        sub = tmp_path / "bin"
        sub.mkdir()
        make_file(sub / "launch.sh")
        found = find_linux_executables(tmp_path)
        assert any(p.name == "launch.sh" for p in found)


# ---------------------------------------------------------------------------
# find_gameinfo
# ---------------------------------------------------------------------------


class TestFindGameinfo:
    def _make_prefix(self, tmp_path: Path) -> Path:
        """Create a minimal WINEPREFIX structure."""
        drive_c = tmp_path / "drive_c"
        drive_c.mkdir()
        return tmp_path

    def test_found(self, tmp_path):
        prefix = self._make_prefix(tmp_path)
        game_dir = prefix / "drive_c" / "GOG Games" / "Songs of Conquest"
        game_dir.mkdir(parents=True)
        (game_dir / "gameinfo").write_text("Songs of Conquest\n1.9.1\nen-US\n")
        result = find_gameinfo(prefix)
        assert result == {"name": "Songs of Conquest", "version": "1.9.1"}

    def test_not_found(self, tmp_path):
        prefix = self._make_prefix(tmp_path)
        assert find_gameinfo(prefix) is None

    def test_no_drive_c(self, tmp_path):
        assert find_gameinfo(tmp_path) is None

    def test_case_insensitive_filename(self, tmp_path):
        prefix = self._make_prefix(tmp_path)
        (prefix / "drive_c" / "GameInfo").write_text("My Game\n2.0\n")
        result = find_gameinfo(prefix)
        assert result == {"name": "My Game", "version": "2.0"}

    def test_name_only(self, tmp_path):
        prefix = self._make_prefix(tmp_path)
        (prefix / "drive_c" / "gameinfo").write_text("Only Name\n")
        result = find_gameinfo(prefix)
        assert result == {"name": "Only Name", "version": ""}

    def test_empty_file_skipped(self, tmp_path):
        prefix = self._make_prefix(tmp_path)
        (prefix / "drive_c" / "gameinfo").write_text("")
        assert find_gameinfo(prefix) is None


# ---------------------------------------------------------------------------
# unsupported_reason
# ---------------------------------------------------------------------------


class TestUnsupportedReason:
    def test_sh(self, tmp_path):
        f = tmp_path / "install.sh"
        f.write_bytes(b"")
        msg = unsupported_reason(f)
        assert "installer scripts" in msg.lower()

    def test_zip(self, tmp_path):
        f = tmp_path / "game.zip"
        f.write_bytes(b"")
        msg = unsupported_reason(f)
        assert "archive" in msg.lower()

    def test_tar_gz(self, tmp_path):
        f = tmp_path / "game.tar.gz"
        f.write_bytes(b"")
        msg = unsupported_reason(f)
        assert "archive" in msg.lower()

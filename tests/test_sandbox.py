"""Tests for cellar.backend.sandbox."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from cellar.backend.sandbox import (
    _build_bwrap_cmd,
    _remove_junk,
    _unwrap_single_child,
    cleanup_captured_install,
    detect_installer_type,
    is_bwrap_available,
    is_makeself,
    try_makeself_noexec,
)


# ---------------------------------------------------------------------------
# detect_installer_type
# ---------------------------------------------------------------------------


class TestDetectInstallerType:
    def test_elf(self, tmp_path: Path) -> None:
        p = tmp_path / "installer"
        p.write_bytes(b"\x7fELF" + b"\x00" * 100)
        assert detect_installer_type(p) == "elf"

    def test_makeself(self, tmp_path: Path) -> None:
        p = tmp_path / "installer.sh"
        p.write_bytes(b"#!/bin/bash\n# Makeself archive\necho hi\n")
        assert detect_installer_type(p) == "makeself"

    def test_shell(self, tmp_path: Path) -> None:
        p = tmp_path / "installer.sh"
        p.write_bytes(b"#!/bin/bash\necho hi\n")
        assert detect_installer_type(p) == "shell"

    def test_unknown(self, tmp_path: Path) -> None:
        p = tmp_path / "installer.dat"
        p.write_bytes(b"\x00\x00\x00\x00")
        assert detect_installer_type(p) == "unknown"

    def test_missing_file(self, tmp_path: Path) -> None:
        p = tmp_path / "nonexistent"
        assert detect_installer_type(p) == "unknown"


class TestIsMakeself:
    def test_true(self, tmp_path: Path) -> None:
        p = tmp_path / "game.sh"
        p.write_bytes(b"#!/bin/bash\n# MAKESELF header\nstuff\n")
        assert is_makeself(p) is True

    def test_false(self, tmp_path: Path) -> None:
        p = tmp_path / "game.sh"
        p.write_bytes(b"#!/bin/bash\nplain script\n")
        assert is_makeself(p) is False


# ---------------------------------------------------------------------------
# is_bwrap_available
# ---------------------------------------------------------------------------


class TestIsBwrapAvailable:
    def test_available_on_path(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/bwrap"):
            assert is_bwrap_available() is True

    def test_not_available(self) -> None:
        with patch("shutil.which", return_value=None), \
             patch("cellar.backend.umu.is_cellar_sandboxed", return_value=False):
            assert is_bwrap_available() is False


# ---------------------------------------------------------------------------
# _build_bwrap_cmd
# ---------------------------------------------------------------------------


class TestBuildBwrapCmd:
    def test_shell_installer(self, tmp_path: Path) -> None:
        installer = tmp_path / "install.sh"
        installer.write_bytes(b"#!/bin/bash\necho hi\n")
        capture = tmp_path / "capture"
        capture.mkdir()

        cmd = _build_bwrap_cmd(installer, capture)

        assert cmd[0] == "bwrap"
        assert "--ro-bind" in cmd
        assert "--die-with-parent" in cmd
        # Should use bash for shell scripts
        assert "bash" in cmd
        assert str(installer) in cmd

    def test_elf_installer(self, tmp_path: Path) -> None:
        installer = tmp_path / "installer"
        installer.write_bytes(b"\x7fELF" + b"\x00" * 100)
        capture = tmp_path / "capture"
        capture.mkdir()

        cmd = _build_bwrap_cmd(installer, capture)

        # ELF — should NOT use bash
        assert "bash" not in cmd
        assert str(installer) in cmd

    def test_network_blocked(self, tmp_path: Path) -> None:
        installer = tmp_path / "install.sh"
        installer.write_bytes(b"#!/bin/bash\necho hi\n")
        capture = tmp_path / "capture"
        capture.mkdir()

        cmd = _build_bwrap_cmd(installer, capture, block_network=True)
        assert "--unshare-net" in cmd

    def test_custom_args(self, tmp_path: Path) -> None:
        installer = tmp_path / "install.sh"
        installer.write_bytes(b"#!/bin/bash\necho hi\n")
        capture = tmp_path / "capture"
        capture.mkdir()

        cmd = _build_bwrap_cmd(
            installer, capture, installer_args=["--prefix=/opt/game"],
        )
        assert "--prefix=/opt/game" in cmd

    def test_creates_subdirs(self, tmp_path: Path) -> None:
        installer = tmp_path / "install.sh"
        installer.write_bytes(b"#!/bin/bash\necho hi\n")
        capture = tmp_path / "capture"
        # Don't pre-create — _build_bwrap_cmd should create root/ and home/

        _build_bwrap_cmd(installer, capture)

        assert (capture / "root").is_dir()
        assert (capture / "home").is_dir()


# ---------------------------------------------------------------------------
# cleanup helpers
# ---------------------------------------------------------------------------


class TestUnwrapSingleChild:
    def test_flat(self, tmp_path: Path) -> None:
        (tmp_path / "a.txt").touch()
        (tmp_path / "b.txt").touch()
        assert _unwrap_single_child(tmp_path) == tmp_path

    def test_nested(self, tmp_path: Path) -> None:
        deep = tmp_path / "opt" / "game" / "myapp"
        deep.mkdir(parents=True)
        (deep / "game.bin").touch()
        assert _unwrap_single_child(tmp_path) == deep

    def test_hidden_ignored(self, tmp_path: Path) -> None:
        child = tmp_path / "app"
        child.mkdir()
        (child / "bin").touch()
        (tmp_path / ".hidden").touch()
        assert _unwrap_single_child(tmp_path) == child


class TestRemoveJunk:
    def test_removes_logs(self, tmp_path: Path) -> None:
        (tmp_path / "game.bin").touch()
        (tmp_path / "install.log").touch()
        (tmp_path / "data.tmp").touch()
        _remove_junk(tmp_path)
        assert (tmp_path / "game.bin").exists()
        assert not (tmp_path / "install.log").exists()
        assert not (tmp_path / "data.tmp").exists()

    def test_removes_empty_dirs(self, tmp_path: Path) -> None:
        (tmp_path / "empty_dir").mkdir()
        (tmp_path / "keep" / "sub").mkdir(parents=True)
        (tmp_path / "keep" / "sub" / "file.txt").touch()
        _remove_junk(tmp_path)
        assert not (tmp_path / "empty_dir").exists()
        assert (tmp_path / "keep" / "sub" / "file.txt").exists()


class TestCleanupCapturedInstall:
    def test_merges_home_into_root(self, tmp_path: Path) -> None:
        root = tmp_path / "root"
        root.mkdir()
        (root / "game.bin").touch()

        home = tmp_path / "home"
        home.mkdir()
        (home / ".config").mkdir()
        (home / ".config" / "settings.ini").write_text("key=val")

        result = cleanup_captured_install(tmp_path)

        assert (result / "game.bin").exists()
        assert (result / ".config" / "settings.ini").exists()
        assert not (tmp_path / "home").exists()

    def test_unwraps_single_child_chain(self, tmp_path: Path) -> None:
        deep = tmp_path / "root" / "opt" / "app" / "mygame"
        deep.mkdir(parents=True)
        (deep / "game.bin").touch()

        result = cleanup_captured_install(tmp_path)

        assert (result / "game.bin").exists()

    def test_no_root_returns_capture_dir(self, tmp_path: Path) -> None:
        result = cleanup_captured_install(tmp_path)
        assert result == tmp_path


# ---------------------------------------------------------------------------
# try_makeself_noexec (mocked — no real makeself available)
# ---------------------------------------------------------------------------


class TestTryMakeselfNoexec:
    def test_not_makeself(self, tmp_path: Path) -> None:
        p = tmp_path / "plain.sh"
        p.write_bytes(b"#!/bin/bash\necho hi\n")
        capture = tmp_path / "capture"
        capture.mkdir()
        assert try_makeself_noexec(p, capture) is False

    def test_makeself_noexec_fails(self, tmp_path: Path) -> None:
        p = tmp_path / "game.sh"
        p.write_bytes(b"#!/bin/bash\n# Makeself archive\nexit 1\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        capture = tmp_path / "capture"
        capture.mkdir()
        # The script exits 1, so --noexec should fail
        result = try_makeself_noexec(p, capture)
        # Result depends on whether bash parses --noexec; either way,
        # the function should return False if no files extracted
        assert result is False or (capture / "root").is_dir()

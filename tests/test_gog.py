"""Tests for cellar.utils.gog — GOG Linux installer detection and extraction."""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from cellar.utils.gog import (
    extract_gog_game_data,
    is_gog_installer,
    list_game_files,
    read_gog_gameinfo,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gog_sh(
    tmp_path: Path,
    gameinfo: str = "",
    files: dict[str, bytes] | None = None,
    extra_noarch: dict[str, bytes] | None = None,
) -> Path:
    """Create a synthetic GOG Linux .sh installer.

    Writes a shell header with GOG/Makeself markers, then appends a ZIP
    containing the provided gameinfo, game files under data/noarch/game/,
    and any *extra_noarch* files directly under data/noarch/.
    """
    sh_path = tmp_path / "setup_test_game_1.0_(12345).sh"

    # Shell header with GOG/Makeself markers (first 4KB)
    header = b"#!/bin/sh\n# Makeself archive -- GOG.com installer\n"
    header += b"# MojoSetup installer\n"
    header += b"\x00" * (4096 - len(header))  # pad to 4KB

    # Build ZIP in memory
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        if gameinfo:
            zf.writestr("data/noarch/gameinfo", gameinfo)
        if files:
            for name, content in files.items():
                zf.writestr(f"data/noarch/game/{name}", content)
        if extra_noarch:
            for name, content in extra_noarch.items():
                zf.writestr(f"data/noarch/{name}", content)

    sh_path.write_bytes(header + buf.getvalue())
    return sh_path


def _make_plain_sh(tmp_path: Path) -> Path:
    """Create a plain shell script (not a GOG installer)."""
    sh_path = tmp_path / "install.sh"
    sh_path.write_text("#!/bin/sh\necho hello\n")
    return sh_path


# ---------------------------------------------------------------------------
# is_gog_installer
# ---------------------------------------------------------------------------


class TestIsGogInstaller:
    def test_valid_gog_installer(self, tmp_path):
        sh = _make_gog_sh(tmp_path, gameinfo="Test Game\n1.0\n99999\n")
        assert is_gog_installer(sh) is True

    def test_plain_sh_not_gog(self, tmp_path):
        sh = _make_plain_sh(tmp_path)
        assert is_gog_installer(sh) is False

    def test_nonexistent_file(self, tmp_path):
        assert is_gog_installer(tmp_path / "nope.sh") is False

    def test_empty_file(self, tmp_path):
        sh = tmp_path / "empty.sh"
        sh.write_bytes(b"")
        assert is_gog_installer(sh) is False

    def test_markers_but_no_zip(self, tmp_path):
        """Has GOG markers in header but no appended ZIP."""
        sh = tmp_path / "fake.sh"
        sh.write_bytes(b"#!/bin/sh\n# Makeself GOG\n")
        assert is_gog_installer(sh) is False


# ---------------------------------------------------------------------------
# read_gog_gameinfo
# ---------------------------------------------------------------------------


class TestReadGogGameinfo:
    def test_full_gameinfo(self, tmp_path):
        sh = _make_gog_sh(tmp_path, gameinfo="SOMA\n1.31\ngog-3\n")
        result = read_gog_gameinfo(sh)
        assert result == {"name": "SOMA", "version": "1.31 (gog-3)"}

    def test_version_without_build(self, tmp_path):
        sh = _make_gog_sh(tmp_path, gameinfo="My Game\n2.0\n")
        result = read_gog_gameinfo(sh)
        assert result == {"name": "My Game", "version": "2.0"}

    def test_name_only(self, tmp_path):
        sh = _make_gog_sh(tmp_path, gameinfo="Only Name\n")
        result = read_gog_gameinfo(sh)
        assert result == {"name": "Only Name", "version": ""}

    def test_no_gameinfo(self, tmp_path):
        sh = _make_gog_sh(tmp_path, gameinfo="")
        result = read_gog_gameinfo(sh)
        assert result is None

    def test_empty_name_returns_none(self, tmp_path):
        sh = _make_gog_sh(tmp_path, gameinfo="\n1.0\n")
        result = read_gog_gameinfo(sh)
        assert result is None

    def test_plain_sh_returns_none(self, tmp_path):
        sh = _make_plain_sh(tmp_path)
        assert read_gog_gameinfo(sh) is None


# ---------------------------------------------------------------------------
# list_game_files
# ---------------------------------------------------------------------------


class TestListGameFiles:
    def test_lists_files(self, tmp_path):
        sh = _make_gog_sh(tmp_path, files={
            "start.sh": b"#!/bin/sh\n",
            "lib/libfoo.so": b"\x7fELF",
            "game_Data/data.unity3d": b"data",
        })
        result = list_game_files(sh)
        # Files under data/noarch/game/ appear as game/...
        assert set(result) == {
            "game/start.sh", "game/lib/libfoo.so", "game/game_Data/data.unity3d",
        }

    def test_empty_zip(self, tmp_path):
        sh = _make_gog_sh(tmp_path)
        assert list_game_files(sh) == []

    def test_plain_sh_returns_empty(self, tmp_path):
        sh = _make_plain_sh(tmp_path)
        assert list_game_files(sh) == []


# ---------------------------------------------------------------------------
# extract_gog_game_data
# ---------------------------------------------------------------------------


class TestExtractGogGameData:
    def test_extracts_to_dest(self, tmp_path):
        sh = _make_gog_sh(
            tmp_path,
            gameinfo="Test\n1.0\n99\n",
            files={
                "start.sh": b"#!/bin/sh\nexec ./game\n",
                "game_bin": b"\x7fELF" + b"\x00" * 12,
                "lib/libfoo.so": b"\x7fELF" + b"\x00" * 12,
            },
        )
        dest = tmp_path / "output"
        extract_gog_game_data(sh, dest)

        # data/noarch/ prefix stripped — game/ subdir preserved
        assert (dest / "game" / "start.sh").exists()
        assert (dest / "game" / "game_bin").exists()
        assert (dest / "game" / "lib" / "libfoo.so").exists()
        # gameinfo extracted at root
        assert (dest / "gameinfo").exists()

    def test_strips_data_noarch_prefix(self, tmp_path):
        """Files should land under dest without data/noarch/ prefix."""
        sh = _make_gog_sh(tmp_path, files={"hello.txt": b"world"})
        dest = tmp_path / "output"
        extract_gog_game_data(sh, dest)

        assert (dest / "game" / "hello.txt").read_bytes() == b"world"
        assert not (dest / "data").exists()

    def test_preserves_executable_bits(self, tmp_path):
        """Files marked executable in the ZIP should be executable after extraction."""
        # Shell header
        header = b"#!/bin/sh\n# Makeself GOG\n"
        header += b"\x00" * (4096 - len(header))

        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w") as zf:
            info = zipfile.ZipInfo("data/noarch/game/run.sh")
            info.external_attr = 0o755 << 16  # rwxr-xr-x
            zf.writestr(info, "#!/bin/sh\n")

        sh = tmp_path / "game.sh"
        sh.write_bytes(header + zip_buf.getvalue())

        dest = tmp_path / "output"
        extract_gog_game_data(sh, dest)

        run_sh = dest / "game" / "run.sh"
        assert run_sh.exists()
        assert run_sh.stat().st_mode & 0o111  # executable bits set

    def test_progress_callback(self, tmp_path):
        sh = _make_gog_sh(
            tmp_path,
            gameinfo="Test\n1.0\n99\n",
            files={"a.txt": b"aaa", "b.txt": b"bbb"},
        )
        dest = tmp_path / "output"
        calls: list[tuple[int, int]] = []
        extract_gog_game_data(sh, dest, progress_cb=lambda e, t: calls.append((e, t)))

        # gameinfo + 2 game files = 3 entries
        assert len(calls) == 3
        # Final call should have extracted == total
        assert calls[-1][0] == calls[-1][1]

    def test_creates_dest_dir(self, tmp_path):
        sh = _make_gog_sh(tmp_path, files={"x.txt": b"x"})
        dest = tmp_path / "nonexistent" / "deep" / "dir"
        extract_gog_game_data(sh, dest)
        assert (dest / "game" / "x.txt").exists()

    def test_runs_postinst(self, tmp_path):
        """postinst.sh is executed after extraction."""
        sh = _make_gog_sh(
            tmp_path,
            files={"game_bin": b"\x7fELF"},
            extra_noarch={
                "support/postinst.sh": (
                    b"#!/bin/bash\ntouch \"${BASH_SOURCE[0]%/*}/../game/postinst_ran\"\n"
                ),
            },
        )
        dest = tmp_path / "output"
        extract_gog_game_data(sh, dest)

        # The script should have created this marker file
        assert (dest / "game" / "postinst_ran").exists()

    def test_no_postinst_no_crash(self, tmp_path):
        """No crash when support/postinst.sh doesn't exist."""
        sh = _make_gog_sh(tmp_path, files={"game_bin": b"\x7fELF"})
        dest = tmp_path / "output"
        extract_gog_game_data(sh, dest)
        assert (dest / "game" / "game_bin").exists()


# ---------------------------------------------------------------------------
# detect_platform integration
# ---------------------------------------------------------------------------


class TestDetectPlatformGogInstaller:
    """Verify that detect_platform recognises GOG .sh installers as linux."""

    def test_gog_sh_detected_as_linux(self, tmp_path):
        from cellar.backend.detect import detect_platform

        sh = _make_gog_sh(tmp_path, gameinfo="Test Game\n1.0\n99999\n")
        assert detect_platform(sh) == "linux"

    def test_plain_sh_unsupported_without_bwrap(self, tmp_path):
        from unittest.mock import patch

        from cellar.backend.detect import detect_platform

        sh = _make_plain_sh(tmp_path)
        with patch("cellar.backend.sandbox.is_bwrap_available", return_value=False):
            assert detect_platform(sh) == "unsupported"

    def test_plain_sh_linux_with_bwrap(self, tmp_path):
        from unittest.mock import patch

        from cellar.backend.detect import detect_platform

        sh = _make_plain_sh(tmp_path)
        with patch("cellar.backend.sandbox.is_bwrap_available", return_value=True):
            assert detect_platform(sh) == "linux"

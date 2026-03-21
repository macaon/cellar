"""Tests for cellar.backend.prefix_fixup."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from cellar.backend.prefix_fixup import fixup_prefix


@pytest.fixture()
def prefix_dir(tmp_path: Path) -> Path:
    """Create a minimal fake WINEPREFIX at a temporary location."""
    pfx = tmp_path / "prefixes" / "my-game"
    pfx.mkdir(parents=True)
    return pfx


class TestFixSymlinks:
    """Absolute symlinks inside the prefix are repointed."""

    def test_absolute_symlink_rewritten(self, prefix_dir: Path, tmp_path: Path) -> None:
        old = str(prefix_dir)
        new_parent = tmp_path / "new-loc"
        new_prefix = new_parent / "My Game"

        # Create a symlink that points back into the old prefix.
        (prefix_dir / "pfx").symlink_to(old)

        # Simulate the move.
        new_parent.mkdir()
        prefix_dir.rename(new_prefix)

        fixup_prefix(new_prefix, old)

        target = os.readlink(str(new_prefix / "pfx"))
        assert target == str(new_prefix)

    def test_relative_symlink_untouched(self, prefix_dir: Path) -> None:
        old = str(prefix_dir)
        (prefix_dir / "drive_c").mkdir()
        (prefix_dir / "link").symlink_to("drive_c")

        fixup_prefix(prefix_dir, "/some/nonexistent/old/path")

        assert os.readlink(str(prefix_dir / "link")) == "drive_c"

    def test_nested_symlink_rewritten(self, prefix_dir: Path, tmp_path: Path) -> None:
        old = "/old/prefix/my-game"
        users = prefix_dir / "drive_c" / "users"
        users.mkdir(parents=True)
        (users / "steamuser").symlink_to(f"{old}/drive_c/users/marcus")

        fixup_prefix(prefix_dir, old)

        target = os.readlink(str(users / "steamuser"))
        assert old not in target
        assert target == f"{prefix_dir}/drive_c/users/marcus"


class TestFixRegistryFiles:
    """Wine .reg files have stale paths replaced."""

    def test_registry_paths_rewritten(self, prefix_dir: Path) -> None:
        old = str(prefix_dir)
        reg_content = (
            'WINE REGISTRY Version 2\n'
            f'"default"="{old}/drive_c/Program Files/app"\n'
            f'"escaped"="{old.replace("/", chr(92) + "/")}"\n'
        )
        (prefix_dir / "user.reg").write_text(reg_content)

        new_path = "/home/user/games/My Game"
        fixup_prefix(prefix_dir, old)
        # old_path == new_path (prefix didn't actually move in this test),
        # so nothing changes.  Let's do a proper test:

    def test_registry_old_replaced_with_new(self, prefix_dir: Path, tmp_path: Path) -> None:
        old = "/old/prefix/path"
        reg_content = (
            'WINE REGISTRY Version 2\n'
            f'"InstallDir"="{old}/drive_c/Program Files/MyApp"\n'
            f'"FontDir"="{old.replace("/", chr(92) + "/")}/drive_c/windows/fonts"\n'
        )
        (prefix_dir / "system.reg").write_text(reg_content)

        fixup_prefix(prefix_dir, old)

        text = (prefix_dir / "system.reg").read_text()
        assert old not in text
        assert str(prefix_dir) in text

    def test_no_reg_files_is_noop(self, prefix_dir: Path) -> None:
        # Should not raise.
        fixup_prefix(prefix_dir, "/old/path")

    def test_pfx_subdirectory_registry(self, prefix_dir: Path) -> None:
        old = "/old/prefix"
        pfx = prefix_dir / "pfx"
        pfx.mkdir()
        (pfx / "user.reg").write_text(f'"path"="{old}/drive_c/test"\n')

        fixup_prefix(prefix_dir, old)

        text = (pfx / "user.reg").read_text()
        assert old not in text
        assert str(prefix_dir) in text


class TestFixTrackedFiles:
    """Proton tracked_files manifest is updated."""

    def test_tracked_files_rewritten(self, prefix_dir: Path) -> None:
        old = "/old/prefix"
        content = f"{old}/drive_c/file1.dll\n{old}/drive_c/file2.dll\n"
        (prefix_dir / "tracked_files").write_text(content)

        fixup_prefix(prefix_dir, old)

        text = (prefix_dir / "tracked_files").read_text()
        assert old not in text
        assert text.startswith(str(prefix_dir))

    def test_tracked_files_in_pfx(self, prefix_dir: Path) -> None:
        old = "/old/prefix"
        pfx = prefix_dir / "pfx"
        pfx.mkdir()
        (pfx / "tracked_files").write_text(f"{old}/file.dll\n")

        fixup_prefix(prefix_dir, old)

        text = (pfx / "tracked_files").read_text()
        assert old not in text


class TestNoop:
    """Edge cases that should be harmless no-ops."""

    def test_same_path_is_noop(self, prefix_dir: Path) -> None:
        fixup_prefix(prefix_dir, str(prefix_dir))

    def test_empty_prefix_is_noop(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        fixup_prefix(empty, "/old/path")

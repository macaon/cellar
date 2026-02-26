"""Tests for Bottles detection (cellar/backend/bottles.py) and
the bottles_data_path config helpers (cellar/backend/config.py)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cellar.backend import bottles as b


# ---------------------------------------------------------------------------
# is_cellar_sandboxed
# ---------------------------------------------------------------------------

def test_sandboxed_when_flatpak_info_exists(tmp_path):
    info = tmp_path / ".flatpak-info"
    info.touch()
    with patch.object(b, "_FLATPAK_INFO", info):
        assert b.is_cellar_sandboxed() is True


def test_not_sandboxed_when_flatpak_info_absent(tmp_path):
    info = tmp_path / ".flatpak-info"   # does not exist
    with patch.object(b, "_FLATPAK_INFO", info):
        assert b.is_cellar_sandboxed() is False


# ---------------------------------------------------------------------------
# _build_cli_cmd — all four combinations
# ---------------------------------------------------------------------------

def test_cli_native_unsandboxed():
    assert b._build_cli_cmd(is_flatpak_bottles=False, sandboxed=False) == [
        "bottles-cli",
    ]


def test_cli_native_sandboxed():
    assert b._build_cli_cmd(is_flatpak_bottles=False, sandboxed=True) == [
        "flatpak-spawn", "--host", "bottles-cli",
    ]


def test_cli_flatpak_bottles_unsandboxed():
    assert b._build_cli_cmd(is_flatpak_bottles=True, sandboxed=False) == [
        "flatpak", "run", "--command=bottles-cli", "com.usebottles.bottles",
    ]


def test_cli_flatpak_bottles_sandboxed():
    assert b._build_cli_cmd(is_flatpak_bottles=True, sandboxed=True) == [
        "flatpak-spawn", "--host",
        "flatpak", "run", "--command=bottles-cli", "com.usebottles.bottles",
    ]


# ---------------------------------------------------------------------------
# detect_bottles — auto-detection order
# ---------------------------------------------------------------------------

def _patch_paths(tmp_path, *, flatpak_exists=False, native_exists=False, sandboxed=False):
    """Return a dict of patches for the three module-level Path constants."""
    flatpak_dir = tmp_path / "flatpak"
    native_dir = tmp_path / "native"
    info = tmp_path / ".flatpak-info"
    if flatpak_exists:
        flatpak_dir.mkdir()
    if native_exists:
        native_dir.mkdir()
    if sandboxed:
        info.touch()
    return flatpak_dir, native_dir, info


def test_detect_prefers_flatpak_over_native(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(
        tmp_path, flatpak_exists=True, native_exists=True
    )
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info), \
         patch("shutil.which", return_value="/usr/bin/bottles-cli"):
        result = b.detect_bottles()
    assert result is not None
    assert result.variant == "flatpak"
    assert result.data_path == flatpak_dir


def test_detect_falls_back_to_native(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(tmp_path, native_exists=True)
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info), \
         patch("shutil.which", return_value="/usr/bin/bottles-cli"):
        result = b.detect_bottles()
    assert result is not None
    assert result.variant == "native"
    assert result.data_path == native_dir


def test_detect_native_ignored_when_no_binary(tmp_path):
    """Native data dir alone is not enough — bottles-cli must be on PATH."""
    flatpak_dir, native_dir, info = _patch_paths(tmp_path, native_exists=True)
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info), \
         patch("shutil.which", return_value=None):
        result = b.detect_bottles()
    assert result is None


def test_detect_returns_none_when_neither_present(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(tmp_path)
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_bottles()
    assert result is None


def test_detect_flatpak_bottles_sets_flatpak_cli(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(tmp_path, flatpak_exists=True)
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_bottles()
    assert result.cli_cmd == [
        "flatpak", "run", "--command=bottles-cli", "com.usebottles.bottles",
    ]


def test_detect_native_bottles_sets_bare_cli(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(tmp_path, native_exists=True)
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info), \
         patch("shutil.which", return_value="/usr/bin/bottles-cli"):
        result = b.detect_bottles()
    assert result.cli_cmd == ["bottles-cli"]


def test_detect_sandboxed_cellar_prefixes_flatpak_spawn(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(
        tmp_path, native_exists=True, sandboxed=True
    )
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info), \
         patch("shutil.which", return_value="/usr/bin/bottles-cli"):
        result = b.detect_bottles()
    assert result.cli_cmd[:2] == ["flatpak-spawn", "--host"]


def test_detect_sandboxed_flatpak_bottles_full_command(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(
        tmp_path, flatpak_exists=True, sandboxed=True
    )
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_bottles()
    assert result.cli_cmd == [
        "flatpak-spawn", "--host",
        "flatpak", "run", "--command=bottles-cli", "com.usebottles.bottles",
    ]


# ---------------------------------------------------------------------------
# detect_bottles — override path
# ---------------------------------------------------------------------------

def test_detect_override_valid_path(tmp_path):
    override = tmp_path / "my-bottles"
    override.mkdir()
    info = tmp_path / "no-info"
    with patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_bottles(override_path=override)
    assert result is not None
    assert result.variant == "custom"
    assert result.data_path == override


def test_detect_override_accepts_string(tmp_path):
    override = tmp_path / "my-bottles"
    override.mkdir()
    info = tmp_path / "no-info"
    with patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_bottles(override_path=str(override))
    assert result is not None
    assert result.data_path == override


def test_detect_override_missing_returns_none(tmp_path):
    override = tmp_path / "nonexistent"
    info = tmp_path / "no-info"
    with patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_bottles(override_path=override)
    assert result is None


def test_detect_override_skips_auto_detection(tmp_path):
    """A valid override must be used even when the Flatpak path also exists."""
    override = tmp_path / "custom"
    override.mkdir()
    flatpak_dir = tmp_path / "flatpak"
    flatpak_dir.mkdir()
    info = tmp_path / "no-info"
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_bottles(override_path=override)
    assert result.variant == "custom"


def test_detect_override_uses_native_cli_cmd(tmp_path):
    """Custom path always uses the native/bare bottles-cli (no Flatpak wrapper)."""
    override = tmp_path / "custom"
    override.mkdir()
    info = tmp_path / "no-info"
    with patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_bottles(override_path=override)
    assert result.cli_cmd == ["bottles-cli"]


# ---------------------------------------------------------------------------
# detect_all_bottles — returns every installation
# ---------------------------------------------------------------------------

def test_detect_all_empty(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(tmp_path)
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_all_bottles()
    assert result == []


def test_detect_all_flatpak_only(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(tmp_path, flatpak_exists=True)
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_all_bottles()
    assert len(result) == 1
    assert result[0].variant == "flatpak"


def test_detect_all_native_only(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(tmp_path, native_exists=True)
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info), \
         patch("shutil.which", return_value="/usr/bin/bottles-cli"):
        result = b.detect_all_bottles()
    assert len(result) == 1
    assert result[0].variant == "native"


def test_detect_all_native_ignored_without_binary(tmp_path):
    """detect_all_bottles must not return native when bottles-cli is absent."""
    flatpak_dir, native_dir, info = _patch_paths(tmp_path, native_exists=True)
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info), \
         patch("shutil.which", return_value=None):
        result = b.detect_all_bottles()
    assert result == []


def test_detect_all_both_returns_flatpak_first(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(
        tmp_path, flatpak_exists=True, native_exists=True
    )
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info), \
         patch("shutil.which", return_value="/usr/bin/bottles-cli"):
        result = b.detect_all_bottles()
    assert len(result) == 2
    assert result[0].variant == "flatpak"
    assert result[1].variant == "native"


def test_detect_all_both_correct_cli_cmds(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(
        tmp_path, flatpak_exists=True, native_exists=True
    )
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info), \
         patch("shutil.which", return_value="/usr/bin/bottles-cli"):
        result = b.detect_all_bottles()
    assert result[0].cli_cmd == [
        "flatpak", "run", "--command=bottles-cli", "com.usebottles.bottles",
    ]
    assert result[1].cli_cmd == ["bottles-cli"]


def test_detect_all_override_returns_single(tmp_path):
    override = tmp_path / "custom"
    override.mkdir()
    flatpak_dir = tmp_path / "flatpak"
    flatpak_dir.mkdir()  # also exists — must be ignored
    info = tmp_path / "no-info"
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_all_bottles(override_path=override)
    assert len(result) == 1
    assert result[0].variant == "custom"


def test_detect_bottles_uses_first_of_detect_all(tmp_path):
    """detect_bottles() must return the first element of detect_all_bottles()."""
    flatpak_dir, native_dir, info = _patch_paths(
        tmp_path, flatpak_exists=True, native_exists=True
    )
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info), \
         patch("shutil.which", return_value="/usr/bin/bottles-cli"):
        single = b.detect_bottles()
        all_ = b.detect_all_bottles()
    assert single == all_[0]


# ---------------------------------------------------------------------------
# config helpers — load/save bottles_data_path
# ---------------------------------------------------------------------------

def test_load_bottles_data_path_unset(tmp_path):
    cfg_file = tmp_path / "config.json"
    with patch("cellar.backend.config._config_path", new=lambda: cfg_file):
        from cellar.backend import config as cfg
        assert cfg.load_bottles_data_path() is None


def test_save_and_load_bottles_data_path(tmp_path):
    cfg_file = tmp_path / "config.json"
    with patch("cellar.backend.config._config_path", new=lambda: cfg_file):
        from cellar.backend import config as cfg
        cfg.save_bottles_data_path("/some/path")
        assert cfg.load_bottles_data_path() == "/some/path"


def test_save_bottles_data_path_none_clears_key(tmp_path):
    cfg_file = tmp_path / "config.json"
    with patch("cellar.backend.config._config_path", new=lambda: cfg_file):
        from cellar.backend import config as cfg
        cfg.save_bottles_data_path("/some/path")
        cfg.save_bottles_data_path(None)
        assert cfg.load_bottles_data_path() is None


def test_save_bottles_data_path_preserves_repos(tmp_path):
    cfg_file = tmp_path / "config.json"
    with patch("cellar.backend.config._config_path", new=lambda: cfg_file):
        from cellar.backend import config as cfg
        cfg.save_repos([{"uri": "file:///some/repo"}])
        cfg.save_bottles_data_path("/bottles")
        assert cfg.load_repos() == [{"uri": "file:///some/repo"}]
        assert cfg.load_bottles_data_path() == "/bottles"


# ---------------------------------------------------------------------------
# _parse_bottle_list
# ---------------------------------------------------------------------------

def test_parse_bottle_list_typical_output():
    output = "Found 3 bottles:\n- MyGame\n- WorkApp\n- Testing\n"
    assert b._parse_bottle_list(output) == ["MyGame", "WorkApp", "Testing"]


def test_parse_bottle_list_empty_output():
    assert b._parse_bottle_list("") == []


def test_parse_bottle_list_ignores_header_line():
    output = "Found 1 bottles:\n- OnlyOne\n"
    assert b._parse_bottle_list(output) == ["OnlyOne"]


def test_parse_bottle_list_handles_extra_whitespace():
    output = "Found 2 bottles:\n  - Padded Name  \n  - Another  \n"
    assert b._parse_bottle_list(output) == ["Padded Name", "Another"]


def test_parse_bottle_list_no_dashes_returns_empty():
    # Output with no "- " lines (unexpected format) returns empty list gracefully
    assert b._parse_bottle_list("Nothing here\nNo bottles\n") == []


# ---------------------------------------------------------------------------
# _run helper
# ---------------------------------------------------------------------------

def _fake_install() -> b.BottlesInstall:
    return b.BottlesInstall(
        data_path=Path("/fake/bottles"),
        variant="native",
        cli_cmd=["bottles-cli"],
    )


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_run_assembles_correct_command():
    install = _fake_install()
    with patch("cellar.backend.bottles.subprocess.run", return_value=_completed()) as mock_run:
        b._run(install, ["list", "bottles"])
    mock_run.assert_called_once()
    cmd = mock_run.call_args[0][0]
    assert cmd == ["bottles-cli", "list", "bottles"]


def test_run_raises_on_file_not_found():
    install = _fake_install()
    with patch("cellar.backend.bottles.subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(b.BottlesError, match="not found"):
            b._run(install, ["list", "bottles"])


def test_run_raises_on_timeout():
    install = _fake_install()
    with patch(
        "cellar.backend.bottles.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=[], timeout=60),
    ):
        with pytest.raises(b.BottlesError, match="timed out"):
            b._run(install, ["list", "bottles"])


def test_run_raises_on_nonzero_exit_with_stderr():
    install = _fake_install()
    with patch(
        "cellar.backend.bottles.subprocess.run",
        return_value=_completed(stderr="Bottle not found", returncode=1),
    ):
        with pytest.raises(b.BottlesError, match="Bottle not found"):
            b._run(install, ["list", "bottles"])


def test_run_raises_on_nonzero_exit_no_stderr():
    install = _fake_install()
    with patch(
        "cellar.backend.bottles.subprocess.run",
        return_value=_completed(returncode=2),
    ):
        with pytest.raises(b.BottlesError, match="exited with code 2"):
            b._run(install, ["list", "bottles"])


# ---------------------------------------------------------------------------
# list_bottles
# ---------------------------------------------------------------------------

def test_list_bottles_returns_names():
    install = _fake_install()
    output = "Found 2 bottles:\n- GameOne\n- GameTwo\n"
    with patch("cellar.backend.bottles.subprocess.run", return_value=_completed(stdout=output)):
        result = b.list_bottles(install)
    assert result == ["GameOne", "GameTwo"]


def test_list_bottles_empty_when_no_bottles():
    install = _fake_install()
    with patch("cellar.backend.bottles.subprocess.run", return_value=_completed(stdout="")):
        result = b.list_bottles(install)
    assert result == []


def test_list_bottles_uses_correct_subcommand():
    install = _fake_install()
    with patch("cellar.backend.bottles.subprocess.run", return_value=_completed()) as mock_run:
        b.list_bottles(install)
    cmd = mock_run.call_args[0][0]
    assert cmd == ["bottles-cli", "list", "bottles"]


# ---------------------------------------------------------------------------
# edit_bottle
# ---------------------------------------------------------------------------

def test_edit_bottle_assembles_correct_command():
    install = _fake_install()
    with patch("cellar.backend.bottles.subprocess.run", return_value=_completed()) as mock_run:
        b.edit_bottle(install, "MyGame", "Runner", "proton-ge-9-1")
    cmd = mock_run.call_args[0][0]
    assert cmd == ["bottles-cli", "edit", "-b", "MyGame", "-k", "Runner", "-v", "proton-ge-9-1"]


def test_edit_bottle_dxvk():
    install = _fake_install()
    with patch("cellar.backend.bottles.subprocess.run", return_value=_completed()) as mock_run:
        b.edit_bottle(install, "MyGame", "DXVK", "2.3")
    cmd = mock_run.call_args[0][0]
    assert cmd == ["bottles-cli", "edit", "-b", "MyGame", "-k", "DXVK", "-v", "2.3"]


def test_edit_bottle_raises_on_failure():
    install = _fake_install()
    with patch(
        "cellar.backend.bottles.subprocess.run",
        return_value=_completed(stderr="Unknown bottle", returncode=1),
    ):
        with pytest.raises(b.BottlesError, match="Unknown bottle"):
            b.edit_bottle(install, "Nonexistent", "Runner", "proton-ge-9-1")


def test_edit_bottle_with_flatpak_cli_cmd():
    install = b.BottlesInstall(
        data_path=Path("/fake/bottles"),
        variant="flatpak",
        cli_cmd=["flatpak", "run", "--command=bottles-cli", "com.usebottles.bottles"],
    )
    with patch("cellar.backend.bottles.subprocess.run", return_value=_completed()) as mock_run:
        b.edit_bottle(install, "MyGame", "DXVK", "2.3")
    cmd = mock_run.call_args[0][0]
    assert cmd[:4] == ["flatpak", "run", "--command=bottles-cli", "com.usebottles.bottles"]
    assert cmd[4:] == ["edit", "-b", "MyGame", "-k", "DXVK", "-v", "2.3"]

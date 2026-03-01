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


# ---------------------------------------------------------------------------
# runners_dir
# ---------------------------------------------------------------------------

def test_runners_dir_flatpak(tmp_path):
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir()
    install = b.BottlesInstall(
        data_path=bottles_dir,
        variant="flatpak",
        cli_cmd=[],
    )
    result = b.runners_dir(install)
    assert result == tmp_path / "runners"


def test_runners_dir_native(tmp_path):
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir()
    install = b.BottlesInstall(
        data_path=bottles_dir,
        variant="native",
        cli_cmd=[],
    )
    result = b.runners_dir(install)
    assert result == tmp_path / "runners"


# ---------------------------------------------------------------------------
# list_runners
# ---------------------------------------------------------------------------

def _install_with_data_path(data_path: Path) -> b.BottlesInstall:
    return b.BottlesInstall(
        data_path=data_path,
        variant="native",
        cli_cmd=["bottles-cli"],
    )


def _no_sys_wine():
    """Patch _detect_system_wine to return [] so tests are isolated."""
    return patch("cellar.backend.bottles._detect_system_wine", return_value=[], autospec=True)


def test_list_runners_empty_when_no_runners_dir(tmp_path):
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir()
    install = _install_with_data_path(bottles_dir)
    with _no_sys_wine():
        assert b.list_runners(install) == []


def test_list_runners_returns_subdirectory_names(tmp_path):
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir()
    runners = tmp_path / "runners"
    (runners / "ge-proton10-32").mkdir(parents=True)
    (runners / "soda-9.0-1").mkdir()
    install = _install_with_data_path(bottles_dir)
    with _no_sys_wine():
        result = b.list_runners(install)
    assert "ge-proton10-32" in result
    assert "soda-9.0-1" in result


def test_list_runners_excludes_files(tmp_path):
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir()
    runners = tmp_path / "runners"
    runners.mkdir()
    (runners / "ge-proton10-32").mkdir()
    (runners / "readme.txt").write_text("not a runner")
    install = _install_with_data_path(bottles_dir)
    with _no_sys_wine():
        result = b.list_runners(install)
    assert "ge-proton10-32" in result
    assert "readme.txt" not in result


def test_list_runners_sorted(tmp_path):
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir()
    runners = tmp_path / "runners"
    (runners / "z-runner").mkdir(parents=True)
    (runners / "a-runner").mkdir()
    (runners / "m-runner").mkdir()
    install = _install_with_data_path(bottles_dir)
    with _no_sys_wine():
        result = b.list_runners(install)
    assert result == sorted(result)


# ---------------------------------------------------------------------------
# _wine_version_cmds
# ---------------------------------------------------------------------------

def _native_install(tmp_path):
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir(exist_ok=True)
    return b.BottlesInstall(data_path=bottles_dir, variant="native", cli_cmd=[])


def _flatpak_install(tmp_path):
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir(exist_ok=True)
    return b.BottlesInstall(data_path=bottles_dir, variant="flatpak", cli_cmd=[])


def test_wine_version_cmds_native_unsandboxed(tmp_path):
    install = _native_install(tmp_path)
    with patch.object(b, "_FLATPAK_INFO", tmp_path / "no-info"):
        cmds = b._wine_version_cmds(install)
    assert cmds == [["wine", "--version"]]


def test_wine_version_cmds_native_sandboxed(tmp_path):
    info = tmp_path / ".flatpak-info"
    info.touch()
    install = _native_install(tmp_path)
    with patch.object(b, "_FLATPAK_INFO", info):
        cmds = b._wine_version_cmds(install)
    assert cmds == [["flatpak-spawn", "--host", "wine", "--version"]]


def test_wine_version_cmds_flatpak_uses_bundle_paths(tmp_path):
    install = _flatpak_install(tmp_path)
    with patch.object(b, "_FLATPAK_INFO", tmp_path / "no-info"):
        cmds = b._wine_version_cmds(install)
    # Both Flatpak Wine paths should be present, ending with "--version"
    assert len(cmds) == 2
    for cmd in cmds:
        assert cmd[-1] == "--version"
        assert any("com.usebottles.bottles" in part for part in cmd)


def test_wine_version_cmds_flatpak_sandboxed_prefixes_spawn(tmp_path):
    info = tmp_path / ".flatpak-info"
    info.touch()
    install = _flatpak_install(tmp_path)
    with patch.object(b, "_FLATPAK_INFO", info):
        cmds = b._wine_version_cmds(install)
    for cmd in cmds:
        assert cmd[:2] == ["flatpak-spawn", "--host"]


# ---------------------------------------------------------------------------
# _detect_system_wine
# ---------------------------------------------------------------------------

def test_detect_system_wine_returns_sys_wine_name(tmp_path):
    install = _native_install(tmp_path)
    with patch("cellar.backend.bottles.subprocess.run",
               return_value=_completed(stdout="wine-11.0\n")), \
         patch.object(b, "_FLATPAK_INFO", tmp_path / "no-info"):
        result = b._detect_system_wine(install)
    assert result == ["sys-wine-11.0"]


def test_detect_system_wine_strips_trailing_whitespace(tmp_path):
    install = _native_install(tmp_path)
    with patch("cellar.backend.bottles.subprocess.run",
               return_value=_completed(stdout="wine-9.0  \n")), \
         patch.object(b, "_FLATPAK_INFO", tmp_path / "no-info"):
        result = b._detect_system_wine(install)
    assert result == ["sys-wine-9.0"]


def test_detect_system_wine_returns_empty_when_not_found(tmp_path):
    install = _native_install(tmp_path)
    with patch("cellar.backend.bottles.subprocess.run",
               side_effect=FileNotFoundError), \
         patch.object(b, "_FLATPAK_INFO", tmp_path / "no-info"):
        result = b._detect_system_wine(install)
    assert result == []


def test_detect_system_wine_returns_empty_on_timeout(tmp_path):
    install = _native_install(tmp_path)
    with patch("cellar.backend.bottles.subprocess.run",
               side_effect=subprocess.TimeoutExpired(cmd=[], timeout=5)), \
         patch.object(b, "_FLATPAK_INFO", tmp_path / "no-info"):
        result = b._detect_system_wine(install)
    assert result == []


def test_detect_system_wine_returns_empty_on_nonzero_exit(tmp_path):
    install = _native_install(tmp_path)
    with patch("cellar.backend.bottles.subprocess.run",
               return_value=_completed(returncode=1)), \
         patch.object(b, "_FLATPAK_INFO", tmp_path / "no-info"):
        result = b._detect_system_wine(install)
    assert result == []


def test_detect_system_wine_native_prefixes_flatpak_spawn_when_sandboxed(tmp_path):
    info = tmp_path / ".flatpak-info"
    info.touch()
    install = _native_install(tmp_path)
    with patch("cellar.backend.bottles.subprocess.run",
               return_value=_completed(stdout="wine-11.0\n")) as mock_run, \
         patch.object(b, "_FLATPAK_INFO", info):
        b._detect_system_wine(install)
    cmd = mock_run.call_args[0][0]
    assert cmd[:2] == ["flatpak-spawn", "--host"]
    assert "wine" in cmd


def test_detect_system_wine_native_no_flatpak_spawn_when_unsandboxed(tmp_path):
    install = _native_install(tmp_path)
    with patch("cellar.backend.bottles.subprocess.run",
               return_value=_completed(stdout="wine-11.0\n")) as mock_run, \
         patch.object(b, "_FLATPAK_INFO", tmp_path / "no-info"):
        b._detect_system_wine(install)
    cmd = mock_run.call_args[0][0]
    assert "flatpak-spawn" not in cmd


def test_detect_system_wine_flatpak_uses_bundle_wine_path(tmp_path):
    """For Flatpak Bottles, the command must reference the Bottles Flatpak Wine binary."""
    install = _flatpak_install(tmp_path)
    with patch("cellar.backend.bottles.subprocess.run",
               return_value=_completed(stdout="wine-11.0\n")) as mock_run, \
         patch.object(b, "_FLATPAK_INFO", tmp_path / "no-info"):
        result = b._detect_system_wine(install)
    assert result == ["sys-wine-11.0"]
    cmd = mock_run.call_args[0][0]
    assert any("com.usebottles.bottles" in part for part in cmd)


def test_detect_system_wine_flatpak_falls_back_to_second_path(tmp_path):
    """If the first Flatpak Wine path fails, the second is tried."""
    install = _flatpak_install(tmp_path)
    call_count = 0

    def _side_effect(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise FileNotFoundError
        return _completed(stdout="wine-11.0\n")

    with patch("cellar.backend.bottles.subprocess.run", side_effect=_side_effect), \
         patch.object(b, "_FLATPAK_INFO", tmp_path / "no-info"):
        result = b._detect_system_wine(install)
    assert result == ["sys-wine-11.0"]
    assert call_count == 2


# ---------------------------------------------------------------------------
# list_runners — sys-wine integration
# ---------------------------------------------------------------------------

def test_list_runners_includes_system_wine(tmp_path):
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir()
    install = _install_with_data_path(bottles_dir)
    with patch("cellar.backend.bottles._detect_system_wine",
               return_value=["sys-wine-11.0"]):
        result = b.list_runners(install)
    assert "sys-wine-11.0" in result


def test_list_runners_deduplicates_sys_wine(tmp_path):
    """sys-wine detected by both wine --version and bottle.yml scan must appear once."""
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir()
    # Create a bottle that references sys-wine-11.0
    bottle_dir = bottles_dir / "Photoshop"
    bottle_dir.mkdir()
    (bottle_dir / "bottle.yml").write_text("Runner: sys-wine-11.0\n")
    install = _install_with_data_path(bottles_dir)
    with patch("cellar.backend.bottles._detect_system_wine",
               return_value=["sys-wine-11.0"]):
        result = b.list_runners(install)
    assert result.count("sys-wine-11.0") == 1


def test_list_runners_picks_up_sys_wine_from_bottle_yml(tmp_path):
    """A sys-wine runner referenced in bottle.yml is included even without wine binary."""
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir()
    bottle_dir = bottles_dir / "Photoshop"
    bottle_dir.mkdir()
    (bottle_dir / "bottle.yml").write_text("Runner: sys-wine-11.0\n")
    install = _install_with_data_path(bottles_dir)
    with _no_sys_wine():
        result = b.list_runners(install)
    assert "sys-wine-11.0" in result


def test_list_runners_ignores_non_syswine_from_bottle_yml(tmp_path):
    """Non-sys-wine runner names in bottle.yml must not be added (they'd be in runners/ dir)."""
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir()
    bottle_dir = bottles_dir / "SomeApp"
    bottle_dir.mkdir()
    (bottle_dir / "bottle.yml").write_text("Runner: ge-proton10-32\n")
    install = _install_with_data_path(bottles_dir)
    with _no_sys_wine():
        result = b.list_runners(install)
    # ge-proton10-32 is NOT in runners/ dir so it must not appear
    assert "ge-proton10-32" not in result


def test_list_runners_bottle_yml_scan_handles_invalid_yaml(tmp_path):
    """Malformed bottle.yml must not crash list_runners."""
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir()
    bottle_dir = bottles_dir / "Broken"
    bottle_dir.mkdir()
    (bottle_dir / "bottle.yml").write_text("{{{{ not yaml ::::")
    install = _install_with_data_path(bottles_dir)
    with _no_sys_wine():
        result = b.list_runners(install)   # must not raise
    assert isinstance(result, list)


def test_list_runners_combined_sources_are_sorted(tmp_path):
    bottles_dir = tmp_path / "bottles"
    bottles_dir.mkdir()
    runners = tmp_path / "runners"
    (runners / "soda-9.0-1").mkdir(parents=True)
    install = _install_with_data_path(bottles_dir)
    with patch("cellar.backend.bottles._detect_system_wine",
               return_value=["sys-wine-11.0"]):
        result = b.list_runners(install)
    assert result == sorted(result)
    assert "soda-9.0-1" in result
    assert "sys-wine-11.0" in result



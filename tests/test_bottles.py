"""Tests for Bottles detection (cellar/backend/bottles.py) and
the bottles_data_path config helpers (cellar/backend/config.py)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

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
         patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_bottles()
    assert result is not None
    assert result.variant == "flatpak"
    assert result.data_path == flatpak_dir


def test_detect_falls_back_to_native(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(tmp_path, native_exists=True)
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_bottles()
    assert result is not None
    assert result.variant == "native"
    assert result.data_path == native_dir


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
         patch.object(b, "_FLATPAK_INFO", info):
        result = b.detect_bottles()
    assert result.cli_cmd == ["bottles-cli"]


def test_detect_sandboxed_cellar_prefixes_flatpak_spawn(tmp_path):
    flatpak_dir, native_dir, info = _patch_paths(
        tmp_path, native_exists=True, sandboxed=True
    )
    with patch.object(b, "_FLATPAK_DATA", flatpak_dir), \
         patch.object(b, "_NATIVE_DATA", native_dir), \
         patch.object(b, "_FLATPAK_INFO", info):
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

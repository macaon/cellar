"""Tests for cellar.backend.dosbox_profiles — game profile detection and conf writing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cellar.backend.dosbox_profiles import (
    _content_root,
    _find_file_casefold,
    _find_file_recursive,
    _match_files,
    _match_gog_ids,
    apply_profile,
    detect_profile,
    load_profiles,
    read_profile_name,
    remove_profile_conf,
    write_profile_conf,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def game_dir(tmp_path: Path) -> Path:
    """Create a minimal DOS game directory structure."""
    d = tmp_path / "game"
    d.mkdir()
    (d / "config").mkdir()
    return d


@pytest.fixture()
def profiles_db(tmp_path: Path, monkeypatch) -> Path:
    """Write a test profiles database and patch load_profiles to use it."""
    db = {
        "schema_version": 1,
        "profiles": {
            "steel-sky": {
                "name": "Beneath a Steel Sky",
                "match": {
                    "gog_ids": ["1207658695"],
                    "files": ["SKY.BAT", "SKY.CFG"],
                },
                "settings": {
                    "sblaster": {"sbtype": "sbpro2"},
                    "mixer": {"reverb": "large", "chorus": "strong"},
                },
            },
            "doom": {
                "name": "DOOM",
                "match": {
                    "gog_ids": ["1435848814"],
                    "files": ["DOOM.EXE", "DOOM.WAD"],
                },
                "settings": {
                    "sblaster": {"sbtype": "sb16"},
                },
            },
        },
    }
    db_path = tmp_path / "dosbox-profiles.json"
    db_path.write_text(json.dumps(db), encoding="utf-8")

    # Patch the cache path so load_profiles finds our test file.
    monkeypatch.setattr(
        "cellar.backend.dosbox_profiles._PROFILES_CACHE", db_path
    )
    return db_path


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


class TestLoadProfiles:
    def test_loads_from_cache(self, profiles_db):
        db = load_profiles()
        assert "steel-sky" in db["profiles"]
        assert "doom" in db["profiles"]

    def test_fallback_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "cellar.backend.dosbox_profiles._PROFILES_CACHE",
            tmp_path / "nonexistent.json",
        )
        monkeypatch.setattr(
            "cellar.backend.dosbox_profiles._bundled_profiles_path",
            lambda: None,
        )
        db = load_profiles()
        assert db == {"profiles": {}}


# ---------------------------------------------------------------------------
# Content root detection
# ---------------------------------------------------------------------------


class TestContentRoot:
    def test_flat_layout(self, game_dir):
        assert _content_root(game_dir) == game_dir

    def test_hdd_layout(self, game_dir):
        (game_dir / "hdd").mkdir()
        assert _content_root(game_dir) == game_dir / "hdd"


# ---------------------------------------------------------------------------
# Case-insensitive file matching
# ---------------------------------------------------------------------------


class TestFindFileCasefold:
    def test_exact_case(self, game_dir):
        (game_dir / "SKY.BAT").touch()
        assert _find_file_casefold(game_dir, "SKY.BAT") is not None

    def test_different_case(self, game_dir):
        (game_dir / "sky.bat").touch()
        assert _find_file_casefold(game_dir, "SKY.BAT") is not None

    def test_mixed_case(self, game_dir):
        (game_dir / "Sky.Bat").touch()
        assert _find_file_casefold(game_dir, "SKY.BAT") is not None

    def test_nested_path(self, game_dir):
        subdir = game_dir / "DATA"
        subdir.mkdir()
        (subdir / "game.dat").touch()
        assert _find_file_casefold(game_dir, "data/GAME.DAT") is not None

    def test_missing_file(self, game_dir):
        assert _find_file_casefold(game_dir, "NOTHERE.EXE") is None

    def test_directory_not_file(self, game_dir):
        (game_dir / "SKY.BAT").mkdir()
        assert _find_file_casefold(game_dir, "SKY.BAT") is None


# ---------------------------------------------------------------------------
# Recursive file search
# ---------------------------------------------------------------------------


class TestFindFileRecursive:
    def test_finds_in_subdirectory(self, game_dir):
        subdir = game_dir / "SKY"
        subdir.mkdir()
        (subdir / "SKY.BAT").touch()
        assert _find_file_recursive(game_dir, "SKY.BAT") is not None

    def test_case_insensitive(self, game_dir):
        subdir = game_dir / "sky"
        subdir.mkdir()
        (subdir / "sky.bat").touch()
        assert _find_file_recursive(game_dir, "SKY.BAT") is not None

    def test_finds_deeply_nested(self, game_dir):
        deep = game_dir / "a" / "b" / "c"
        deep.mkdir(parents=True)
        (deep / "game.cfg").touch()
        assert _find_file_recursive(game_dir, "GAME.CFG") is not None

    def test_not_found(self, game_dir):
        assert _find_file_recursive(game_dir, "MISSING.EXE") is None


# ---------------------------------------------------------------------------
# GOG ID matching
# ---------------------------------------------------------------------------


class TestMatchGogIds:
    def test_match(self, game_dir):
        info = {
            "gameId": "1207658695",
            "name": "Beneath a Steel Sky",
            "playTasks": [],
        }
        (game_dir / "goggame-1207658695.info").write_text(
            json.dumps(info), encoding="utf-8"
        )
        assert _match_gog_ids(game_dir, ["1207658695"]) is True

    def test_no_match(self, game_dir):
        info = {"gameId": "9999", "name": "Other", "playTasks": []}
        (game_dir / "goggame-9999.info").write_text(
            json.dumps(info), encoding="utf-8"
        )
        assert _match_gog_ids(game_dir, ["1207658695"]) is False

    def test_no_info_files(self, game_dir):
        assert _match_gog_ids(game_dir, ["1207658695"]) is False

    def test_empty_ids_list(self, game_dir):
        assert _match_gog_ids(game_dir, []) is False


# ---------------------------------------------------------------------------
# File fingerprint matching
# ---------------------------------------------------------------------------


class TestMatchFiles:
    def test_bare_name_found_in_subdir(self, game_dir):
        """Bare filenames (no /) are searched recursively."""
        subdir = game_dir / "SKY"
        subdir.mkdir()
        (subdir / "SKY.BAT").touch()
        (subdir / "SKY.CFG").touch()
        assert _match_files(game_dir, ["SKY.BAT", "SKY.CFG"]) is True

    def test_bare_name_partial(self, game_dir):
        subdir = game_dir / "SKY"
        subdir.mkdir()
        (subdir / "SKY.BAT").touch()
        assert _match_files(game_dir, ["SKY.BAT", "SKY.CFG"]) is False

    def test_path_with_slash_exact(self, game_dir):
        """Paths containing / use exact (non-recursive) matching."""
        subdir = game_dir / "SKY"
        subdir.mkdir()
        (subdir / "SKY.BAT").touch()
        (subdir / "SKY.CFG").touch()
        assert _match_files(game_dir, ["SKY/SKY.BAT", "SKY/SKY.CFG"]) is True

    def test_path_with_slash_wrong_dir(self, game_dir):
        subdir = game_dir / "OTHER"
        subdir.mkdir()
        (subdir / "SKY.BAT").touch()
        (subdir / "SKY.CFG").touch()
        assert _match_files(game_dir, ["SKY/SKY.BAT", "SKY/SKY.CFG"]) is False

    def test_rejects_single_file(self, game_dir):
        """Must have at least 2 fingerprint files to reduce false positives."""
        (game_dir / "DOOM.EXE").touch()
        assert _match_files(game_dir, ["DOOM.EXE"]) is False

    def test_empty_list(self, game_dir):
        assert _match_files(game_dir, []) is False


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


class TestDetectProfile:
    def test_gog_id_priority(self, game_dir, profiles_db):
        """GOG ID match should take priority over file fingerprint."""
        # Create files matching DOOM
        (game_dir / "DOOM.EXE").touch()
        (game_dir / "DOOM.WAD").touch()
        # But GOG ID matches Steel Sky
        info = {"gameId": "1207658695", "name": "BASS", "playTasks": []}
        (game_dir / "goggame-1207658695.info").write_text(
            json.dumps(info), encoding="utf-8"
        )
        assert detect_profile(game_dir) == "steel-sky"

    def test_file_fingerprint_recursive(self, game_dir, profiles_db):
        """Bare filenames found recursively in subdirectories."""
        subdir = game_dir / "sky"
        subdir.mkdir()
        (subdir / "sky.bat").touch()
        (subdir / "sky.cfg").touch()
        assert detect_profile(game_dir) == "steel-sky"

    def test_no_match(self, game_dir, profiles_db):
        (game_dir / "RANDOM.EXE").touch()
        assert detect_profile(game_dir) is None

    def test_hdd_layout(self, game_dir, profiles_db):
        hdd = game_dir / "hdd"
        hdd.mkdir()
        sky = hdd / "SKY"
        sky.mkdir()
        (sky / "SKY.BAT").touch()
        (sky / "SKY.CFG").touch()
        assert detect_profile(game_dir) == "steel-sky"


# ---------------------------------------------------------------------------
# Conf writing
# ---------------------------------------------------------------------------


class TestWriteProfileConf:
    def test_writes_correct_ini(self, game_dir, profiles_db):
        path = write_profile_conf(game_dir, "steel-sky")
        assert path is not None
        assert path.name == "dosbox-profile.conf"

        text = path.read_text(encoding="utf-8")
        assert "Beneath a Steel Sky" in text
        assert "[sblaster]" in text
        assert "sbtype = sbpro2" in text
        assert "[mixer]" in text
        assert "reverb = large" in text
        assert "chorus = strong" in text

    def test_unknown_profile(self, game_dir, profiles_db):
        assert write_profile_conf(game_dir, "nonexistent") is None

    def test_creates_config_dir(self, tmp_path, profiles_db):
        game = tmp_path / "no_config_yet"
        game.mkdir()
        path = write_profile_conf(game, "doom")
        assert path is not None
        assert (game / "config").is_dir()


# ---------------------------------------------------------------------------
# Read / remove
# ---------------------------------------------------------------------------


class TestReadProfileName:
    def test_reads_name(self, game_dir, profiles_db):
        write_profile_conf(game_dir, "steel-sky")
        assert read_profile_name(game_dir) == "Beneath a Steel Sky"

    def test_no_profile(self, game_dir):
        assert read_profile_name(game_dir) is None


class TestRemoveProfileConf:
    def test_removes(self, game_dir, profiles_db):
        write_profile_conf(game_dir, "steel-sky")
        conf = game_dir / "config" / "dosbox-profile.conf"
        assert conf.is_file()
        remove_profile_conf(game_dir)
        assert not conf.exists()

    def test_noop_if_missing(self, game_dir):
        remove_profile_conf(game_dir)  # should not raise


# ---------------------------------------------------------------------------
# Convenience
# ---------------------------------------------------------------------------


class TestApplyProfile:
    def test_detect_and_write(self, game_dir, profiles_db):
        (game_dir / "DOOM.EXE").touch()
        (game_dir / "DOOM.WAD").touch()
        slug = apply_profile(game_dir)
        assert slug == "doom"
        assert (game_dir / "config" / "dosbox-profile.conf").is_file()

    def test_no_match_no_write(self, game_dir, profiles_db):
        slug = apply_profile(game_dir)
        assert slug is None
        assert not (game_dir / "config" / "dosbox-profile.conf").exists()

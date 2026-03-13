"""Tests for cellar/backend/database.py."""

from __future__ import annotations

from unittest.mock import patch

from cellar.backend import database as db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_db(tmp_path):
    """Context manager that redirects the DB to a temp file."""
    db_file = tmp_path / "cellar.db"
    return patch("cellar.backend.database._db_path", new=lambda: db_file)


# ---------------------------------------------------------------------------
# mark_installed / get_installed / is_installed
# ---------------------------------------------------------------------------

def test_mark_and_get_installed(tmp_path):
    with _patch_db(tmp_path):
        db.mark_installed("my-app", "my-app", "1.0", "file:///repo")
        rec = db.get_installed("my-app")
    assert rec is not None
    assert rec["id"] == "my-app"
    assert rec["prefix_dir"] == "my-app"
    assert rec["version"] == "1.0"
    assert rec["repo_source"] == "file:///repo"


def test_get_installed_returns_none_when_absent(tmp_path):
    with _patch_db(tmp_path):
        assert db.get_installed("nonexistent") is None


def test_is_installed_true(tmp_path):
    with _patch_db(tmp_path):
        db.mark_installed("app", "app", "1.0")
        assert db.is_installed("app") is True


def test_is_installed_false(tmp_path):
    with _patch_db(tmp_path):
        assert db.is_installed("app") is False


# ---------------------------------------------------------------------------
# upsert behaviour
# ---------------------------------------------------------------------------

def test_mark_installed_upsert_updates_version(tmp_path):
    with _patch_db(tmp_path):
        db.mark_installed("app", "app", "1.0", "file:///repo")
        db.mark_installed("app", "app-renamed", "2.0", "file:///repo")
        rec = db.get_installed("app")
    assert rec["version"] == "2.0"
    assert rec["prefix_dir"] == "app-renamed"


def test_mark_installed_upsert_preserves_installed_at(tmp_path):
    with _patch_db(tmp_path):
        db.mark_installed("app", "app", "1.0")
        first = db.get_installed("app")["installed_at"]
        db.mark_installed("app", "app", "2.0")
        second = db.get_installed("app")["installed_at"]
    # installed_at must not change on update
    assert first == second


# ---------------------------------------------------------------------------
# remove_installed
# ---------------------------------------------------------------------------

def test_remove_installed(tmp_path):
    with _patch_db(tmp_path):
        db.mark_installed("app", "app", "1.0")
        db.remove_installed("app")
        assert db.get_installed("app") is None


def test_remove_installed_noop_when_absent(tmp_path):
    with _patch_db(tmp_path):
        db.remove_installed("nonexistent")   # must not raise


# ---------------------------------------------------------------------------
# get_all_installed
# ---------------------------------------------------------------------------

def test_get_all_installed_empty(tmp_path):
    with _patch_db(tmp_path):
        assert db.get_all_installed() == []


def test_get_all_installed_returns_all(tmp_path):
    with _patch_db(tmp_path):
        db.mark_installed("app-a", "app-a", "1.0")
        db.mark_installed("app-b", "app-b", "2.0")
        rows = db.get_all_installed()
    ids = {r["id"] for r in rows}
    assert ids == {"app-a", "app-b"}


def test_get_all_installed_ordered_by_installed_at(tmp_path):
    with _patch_db(tmp_path):
        db.mark_installed("app-a", "app-a", "1.0")
        db.mark_installed("app-b", "app-b", "1.0")
        rows = db.get_all_installed()
    # First inserted should come first (both timestamps are close but sequential)
    assert rows[0]["id"] == "app-a"


# ---------------------------------------------------------------------------
# launch_overrides helpers
# ---------------------------------------------------------------------------

def test_get_launch_overrides_empty_for_uninstalled(tmp_path):
    with _patch_db(tmp_path):
        assert db.get_launch_overrides("nonexistent") == {}


def test_set_and_get_launch_overrides_runner(tmp_path):
    with _patch_db(tmp_path):
        db.mark_installed("app", "app", "1.0")
        db.set_launch_overrides("app", {"runner": "ge-proton10-32"})
        overrides = db.get_launch_overrides("app")
    assert overrides.get("runner") == "ge-proton10-32"


def test_set_and_get_launch_overrides_booleans(tmp_path):
    with _patch_db(tmp_path):
        db.set_launch_overrides("app", {"dxvk": False, "vkd3d": True, "debug": True})
        overrides = db.get_launch_overrides("app")
    assert overrides["dxvk"] is False
    assert overrides["vkd3d"] is True
    assert overrides["debug"] is True


def test_set_and_get_launch_overrides_targets(tmp_path):
    targets = [{"name": "Main", "path": "C:\\game.exe", "args": "-fullscreen"}]
    with _patch_db(tmp_path):
        db.set_launch_overrides("app", {"launch_targets": targets})
        overrides = db.get_launch_overrides("app")
    assert overrides["launch_targets"] == targets


def test_clear_launch_overrides(tmp_path):
    with _patch_db(tmp_path):
        db.set_launch_overrides("app", {"runner": "ge-proton10-32"})
        db.clear_launch_overrides("app")
        overrides = db.get_launch_overrides("app")
    assert overrides == {}


def test_set_launch_overrides_upsert(tmp_path):
    with _patch_db(tmp_path):
        db.set_launch_overrides("app", {"runner": "ge-proton9"})
        db.set_launch_overrides("app", {"runner": "ge-proton10-32", "dxvk": False})
        overrides = db.get_launch_overrides("app")
    assert overrides["runner"] == "ge-proton10-32"
    assert overrides["dxvk"] is False


# ---------------------------------------------------------------------------
# Base image tracking
# ---------------------------------------------------------------------------

def test_mark_and_get_installed_base(tmp_path):
    with _patch_db(tmp_path):
        db.mark_base_installed("soda-9.0-1", "smb://server/repo")
        rec = db.get_installed_base("soda-9.0-1")
    assert rec is not None
    assert rec["runner"] == "soda-9.0-1"
    assert rec["repo_source"] == "smb://server/repo"
    assert rec["installed_at"]


def test_get_installed_base_missing(tmp_path):
    with _patch_db(tmp_path):
        assert db.get_installed_base("ge-proton10-32") is None


def test_mark_base_installed_upsert(tmp_path):
    with _patch_db(tmp_path):
        db.mark_base_installed("soda-9.0-1", "smb://old")
        db.mark_base_installed("soda-9.0-1", "smb://new")
        rec = db.get_installed_base("soda-9.0-1")
    assert rec["repo_source"] == "smb://new"


def test_get_all_installed_bases(tmp_path):
    with _patch_db(tmp_path):
        db.mark_base_installed("soda-9.0-1")
        db.mark_base_installed("ge-proton10-32")
        bases = db.get_all_installed_bases()
    assert {b["runner"] for b in bases} == {"soda-9.0-1", "ge-proton10-32"}


def test_remove_base_record(tmp_path):
    with _patch_db(tmp_path):
        db.mark_base_installed("soda-9.0-1")
        db.remove_base_record("soda-9.0-1")
        assert db.get_installed_base("soda-9.0-1") is None


def test_remove_base_record_noop_when_missing(tmp_path):
    with _patch_db(tmp_path):
        db.remove_base_record("soda-9.0-1")  # should not raise

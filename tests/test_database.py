"""Tests for cellar/backend/database.py."""

from __future__ import annotations

from unittest.mock import patch

import pytest

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
    assert rec["bottle_name"] == "my-app"
    assert rec["installed_version"] == "1.0"
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
    assert rec["installed_version"] == "2.0"
    assert rec["bottle_name"] == "app-renamed"


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

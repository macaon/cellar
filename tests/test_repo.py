"""Tests for cellar.backend.repo using local fixture data."""

import sys
from pathlib import Path

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from cellar.backend.repo import Repo, RepoError, RepoManager
from cellar.models.app_entry import AppEntry
from cellar.models.manifest import Manifest

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Repo.fetch_catalogue
# ---------------------------------------------------------------------------

def test_fetch_catalogue_returns_all_entries():
    repo = Repo(str(FIXTURES))
    entries = repo.fetch_catalogue()
    assert len(entries) == 2
    ids = {e.id for e in entries}
    assert ids == {"example-app", "paint-clone"}


def test_catalogue_entry_fields():
    repo = Repo(str(FIXTURES))
    entries = {e.id: e for e in repo.fetch_catalogue()}
    ea = entries["example-app"]
    assert ea.name == "Example App"
    assert ea.category == "Productivity"
    assert ea.version == "1.0"
    assert ea.manifest == "apps/example-app/manifest.json"


# ---------------------------------------------------------------------------
# Repo.fetch_manifest
# ---------------------------------------------------------------------------

def test_fetch_manifest_fields():
    repo = Repo(str(FIXTURES))
    entries = {e.id: e for e in repo.fetch_catalogue()}
    m = repo.fetch_manifest(entries["example-app"])
    assert isinstance(m, Manifest)
    assert m.id == "example-app"
    assert m.update_strategy == "safe"
    assert m.built_with.runner == "proton-ge-9-1"
    assert m.built_with.dxvk == "2.3"
    assert m.built_with.vkd3d == "2.11"
    assert m.archive_size == 104857600
    assert len(m.screenshots) == 2


def test_fetch_manifest_full_strategy():
    repo = Repo(str(FIXTURES))
    m = repo.fetch_manifest_by_id("paint-clone")
    assert m.update_strategy == "full"
    assert m.built_with.vkd3d == ""  # not specified in fixture


# ---------------------------------------------------------------------------
# Repo error cases
# ---------------------------------------------------------------------------

def test_missing_catalogue_raises():
    repo = Repo.__new__(Repo)
    repo.uri = "/nonexistent"
    repo.name = "/nonexistent"
    from pathlib import Path
    repo._root = Path("/nonexistent")
    with pytest.raises(RepoError, match="not found"):
        repo.fetch_catalogue()


def test_invalid_uri_scheme_raises():
    with pytest.raises(RepoError, match="not yet supported"):
        Repo("smb://server/share")


def test_nonexistent_root_raises():
    with pytest.raises(RepoError, match="does not exist"):
        Repo("/this/path/does/not/exist/at/all")


# ---------------------------------------------------------------------------
# iter_categories
# ---------------------------------------------------------------------------

def test_iter_categories():
    repo = Repo(str(FIXTURES))
    cats = list(repo.iter_categories())
    assert set(cats) == {"Productivity", "Graphics"}


# ---------------------------------------------------------------------------
# RepoManager
# ---------------------------------------------------------------------------

def test_repo_manager_merges_catalogues():
    mgr = RepoManager()
    mgr.add(Repo(str(FIXTURES)))
    entries = mgr.fetch_all_catalogues()
    assert len(entries) == 2


def test_repo_manager_last_repo_wins():
    """If two repos have the same app ID, the later repo's entry wins."""
    mgr = RepoManager()
    mgr.add(Repo(str(FIXTURES)))
    mgr.add(Repo(str(FIXTURES)))  # same repo twice — last entry for same id wins
    entries = mgr.fetch_all_catalogues()
    # Still deduplicated by id
    assert len(entries) == 2


def test_repo_manager_skips_bad_repo(tmp_path):
    """An unreachable repo is skipped; others still load."""
    mgr = RepoManager()
    good = Repo(str(FIXTURES))
    mgr.add(good)
    # Manually add a broken repo by bypassing the constructor check
    bad = Repo.__new__(Repo)
    bad.uri = str(tmp_path / "nonexistent")
    bad.name = "bad"
    bad._root = tmp_path / "nonexistent"
    mgr.add(bad)
    entries = mgr.fetch_all_catalogues()
    assert len(entries) == 2  # bad repo skipped gracefully

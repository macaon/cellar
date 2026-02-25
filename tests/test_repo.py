"""Tests for cellar.backend.repo using local fixture data."""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow running from the repo root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from cellar.backend.repo import (
    Repo,
    RepoError,
    RepoManager,
    _HttpFetcher,
    _LocalFetcher,
    _SshFetcher,
)
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
# Repo.resolve_asset_uri
# ---------------------------------------------------------------------------

def test_resolve_asset_uri_local():
    repo = Repo(str(FIXTURES))
    uri = repo.resolve_asset_uri("apps/example-app/icon.png")
    assert uri.endswith("apps/example-app/icon.png")


# ---------------------------------------------------------------------------
# Repo error cases
# ---------------------------------------------------------------------------

def test_missing_catalogue_raises():
    repo = Repo.__new__(Repo)
    repo.uri = "/nonexistent"
    repo.name = "/nonexistent"
    repo._fetcher = _LocalFetcher(Path("/nonexistent"))
    with pytest.raises(RepoError, match="not found"):
        repo.fetch_catalogue()


def test_unsupported_uri_scheme_raises():
    with pytest.raises(RepoError, match="Unsupported URI scheme"):
        Repo("ftp://server/path")


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
    bad._fetcher = _LocalFetcher(tmp_path / "nonexistent")
    mgr.add(bad)
    entries = mgr.fetch_all_catalogues()
    assert len(entries) == 2  # bad repo skipped gracefully


# ---------------------------------------------------------------------------
# _HttpFetcher
# ---------------------------------------------------------------------------

def _make_mock_response(body: bytes, status: int = 200):
    """Return a mock context manager that urlopen would yield."""
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_http_fetcher_fetch_bytes():
    payload = b'{"key": "value"}'
    with patch("urllib.request.urlopen", return_value=_make_mock_response(payload)):
        fetcher = _HttpFetcher("https://example.com/repo")
        data = fetcher.fetch_bytes("catalogue.json")
    assert data == payload


def test_http_fetcher_resolve_uri():
    fetcher = _HttpFetcher("https://example.com/repo/")
    assert fetcher.resolve_uri("apps/foo/icon.png") == "https://example.com/repo/apps/foo/icon.png"


def test_http_fetcher_trailing_slash_normalised():
    """Base URL with or without trailing slash should produce the same asset URI."""
    f1 = _HttpFetcher("https://example.com/repo")
    f2 = _HttpFetcher("https://example.com/repo/")
    assert f1.resolve_uri("catalogue.json") == f2.resolve_uri("catalogue.json")


def test_http_fetcher_http_error_raises():
    import urllib.error
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            "https://example.com/404", 404, "Not Found", {}, None
        ),
    ):
        fetcher = _HttpFetcher("https://example.com/repo")
        with pytest.raises(RepoError, match="HTTP 404"):
            fetcher.fetch_bytes("missing.json")


def test_http_fetcher_network_error_raises():
    import urllib.error
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("Connection refused"),
    ):
        fetcher = _HttpFetcher("https://example.com/repo")
        with pytest.raises(RepoError, match="Network error"):
            fetcher.fetch_bytes("catalogue.json")


def test_repo_http_catalogue(tmp_path):
    """End-to-end: Repo backed by an HTTP fetcher reads catalogue correctly."""
    catalogue = (FIXTURES / "catalogue.json").read_bytes()
    repo = Repo.__new__(Repo)
    repo.uri = "https://example.com/repo"
    repo.name = "test"
    with patch("urllib.request.urlopen", return_value=_make_mock_response(catalogue)):
        repo._fetcher = _HttpFetcher("https://example.com/repo")
        entries = repo.fetch_catalogue()
    assert len(entries) == 2


# ---------------------------------------------------------------------------
# _SshFetcher
# ---------------------------------------------------------------------------

def test_ssh_fetcher_resolve_uri():
    fetcher = _SshFetcher("myhost.example.com", "/srv/repo", user="alice", port=2222)
    uri = fetcher.resolve_uri("catalogue.json")
    assert uri == "ssh://alice@myhost.example.com:2222/srv/repo/catalogue.json"


def test_ssh_fetcher_resolve_uri_no_user_no_port():
    fetcher = _SshFetcher("myhost.example.com", "/srv/repo")
    uri = fetcher.resolve_uri("apps/foo/icon.png")
    assert uri == "ssh://myhost.example.com/srv/repo/apps/foo/icon.png"


def test_ssh_fetcher_missing_ssh_raises(monkeypatch):
    """If ssh is not installed, a clear RepoError is raised."""
    import subprocess
    fetcher = _SshFetcher("myhost.example.com", "/srv/repo", user="alice")
    with patch("subprocess.run", side_effect=FileNotFoundError("ssh not found")):
        with pytest.raises(RepoError, match="ssh executable not found"):
            fetcher.fetch_bytes("catalogue.json")


def test_ssh_fetcher_nonzero_exit_raises():
    """Non-zero SSH exit code surfaces stderr as a RepoError."""
    result = MagicMock()
    result.returncode = 255
    result.stderr = b"Connection refused"
    with patch("subprocess.run", return_value=result):
        fetcher = _SshFetcher("myhost.example.com", "/srv/repo", user="alice")
        with pytest.raises(RepoError, match="SSH fetch failed"):
            fetcher.fetch_bytes("catalogue.json")


def test_ssh_fetcher_success():
    payload = b'[{"id":"x","name":"X","category":"C","summary":"s","icon":"i","version":"1","manifest":"m"}]'
    result = MagicMock()
    result.returncode = 0
    result.stdout = payload
    with patch("subprocess.run", return_value=result):
        fetcher = _SshFetcher("myhost.example.com", "/srv/repo", user="alice")
        data = fetcher.fetch_bytes("catalogue.json")
    assert data == payload


def test_ssh_fetcher_identity_file_passed():
    """An explicit identity file should appear in the ssh command arguments."""
    result = MagicMock()
    result.returncode = 0
    result.stdout = b"data"
    with patch("subprocess.run", return_value=result) as mock_run:
        fetcher = _SshFetcher(
            "myhost.example.com",
            "/srv/repo",
            user="alice",
            identity="/home/alice/.ssh/cellar_ed25519",
        )
        fetcher.fetch_bytes("catalogue.json")
    cmd = mock_run.call_args[0][0]
    assert "-i" in cmd
    assert "/home/alice/.ssh/cellar_ed25519" in cmd


def test_repo_ssh_uri_creates_ssh_fetcher():
    """Passing an ssh:// URI to Repo should create an _SshFetcher."""
    from cellar.backend.repo import _SshFetcher as SF
    repo = Repo("ssh://bob@builds.example.com/srv/cellar-repo")
    assert isinstance(repo._fetcher, SF)


def test_repo_ssh_uri_invalid_no_host_raises():
    with pytest.raises(RepoError, match="no host"):
        Repo("ssh:///path/without/host")

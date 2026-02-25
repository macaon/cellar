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
from cellar.models.app_entry import AppEntry, BuiltWith

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Repo.fetch_catalogue — basic parsing
# ---------------------------------------------------------------------------

def test_fetch_catalogue_returns_all_entries():
    repo = Repo(str(FIXTURES))
    entries = repo.fetch_catalogue()
    assert len(entries) == 2
    assert {e.id for e in entries} == {"example-app", "paint-clone"}


def test_catalogue_entries_are_app_entry_instances():
    repo = Repo(str(FIXTURES))
    for entry in repo.fetch_catalogue():
        assert isinstance(entry, AppEntry)


def test_catalogue_wrapper_format_parsed():
    """The cellar_version wrapper dict is accepted."""
    repo = Repo(str(FIXTURES))
    entries = repo.fetch_catalogue()
    assert len(entries) == 2  # wrapper format is the fixture default


def test_catalogue_bare_array_fallback(tmp_path):
    """A bare JSON array is still accepted for backwards compatibility."""
    bare = tmp_path / "catalogue.json"
    bare.write_text(
        '[{"id":"x","name":"X","version":"1","category":"C"}]', encoding="utf-8"
    )
    repo = Repo(str(tmp_path))
    entries = repo.fetch_catalogue()
    assert len(entries) == 1
    assert entries[0].id == "x"


# ---------------------------------------------------------------------------
# AppEntry field coverage
# ---------------------------------------------------------------------------

def test_example_app_identity_fields():
    entries = {e.id: e for e in Repo(str(FIXTURES)).fetch_catalogue()}
    e = entries["example-app"]
    assert e.name == "Example App"
    assert e.version == "1.0"
    assert e.category == "Productivity"


def test_example_app_display_fields():
    entries = {e.id: e for e in Repo(str(FIXTURES)).fetch_catalogue()}
    e = entries["example-app"]
    assert e.summary == "A sample Windows app managed by Bottles."
    assert "longer description" in e.description
    assert "Productivity" in e.tags
    assert "Office" in e.tags


def test_example_app_attribution_fields():
    entries = {e.id: e for e in Repo(str(FIXTURES)).fetch_catalogue()}
    e = entries["example-app"]
    assert e.developer == "Example Corp"
    assert e.publisher == "Example Corp"
    assert e.release_year == 2022
    assert e.content_rating == "PEGI 3"
    assert "English" in e.languages
    assert "German" in e.languages
    assert e.website == "https://example.com"
    assert e.store_links.get("gog") == "https://www.gog.com/game/example"


def test_example_app_media_fields():
    entries = {e.id: e for e in Repo(str(FIXTURES)).fetch_catalogue()}
    e = entries["example-app"]
    assert e.icon == "apps/example-app/icon.png"
    assert e.cover == "apps/example-app/cover.png"
    assert e.hero == "apps/example-app/hero.png"
    assert len(e.screenshots) == 2


def test_example_app_install_fields():
    entries = {e.id: e for e in Repo(str(FIXTURES)).fetch_catalogue()}
    e = entries["example-app"]
    assert e.archive == "apps/example-app/example-app-1.0.tar.gz"
    assert e.archive_size == 104857600
    assert e.archive_sha256 == "deadbeef1234567890abcdef1234567890abcdef1234567890abcdef12345678"
    assert e.install_size_estimate == 524288000
    assert e.update_strategy == "safe"
    assert e.entry_point == "Program Files/ExampleApp/example.exe"
    assert e.changelog == "Initial release."


def test_example_app_built_with():
    entries = {e.id: e for e in Repo(str(FIXTURES)).fetch_catalogue()}
    e = entries["example-app"]
    assert isinstance(e.built_with, BuiltWith)
    assert e.built_with.runner == "proton-ge-9-1"
    assert e.built_with.dxvk == "2.3"
    assert e.built_with.vkd3d == "2.11"


def test_paint_clone_full_strategy():
    entries = {e.id: e for e in Repo(str(FIXTURES)).fetch_catalogue()}
    e = entries["paint-clone"]
    assert e.update_strategy == "full"
    assert e.built_with is not None
    assert e.built_with.vkd3d == ""  # not specified in fixture


def test_optional_fields_default_gracefully(tmp_path):
    """An entry with only required fields should parse without error."""
    minimal = tmp_path / "catalogue.json"
    minimal.write_text(
        '{"cellar_version":1,"apps":[{"id":"m","name":"Min","version":"0","category":"Other"}]}',
        encoding="utf-8",
    )
    repo = Repo(str(tmp_path))
    entries = repo.fetch_catalogue()
    e = entries[0]
    assert e.id == "m"
    assert e.summary == ""
    assert e.tags == ()
    assert e.developer == ""
    assert e.store_links == {}
    assert e.built_with is None
    assert e.update_strategy == "safe"


# ---------------------------------------------------------------------------
# AppEntry.to_dict round-trip
# ---------------------------------------------------------------------------

def test_to_dict_round_trip():
    entries = {e.id: e for e in Repo(str(FIXTURES)).fetch_catalogue()}
    original = entries["example-app"]
    restored = AppEntry.from_dict(original.to_dict())
    assert restored == original


def test_to_dict_omits_empty_fields():
    e = AppEntry(id="x", name="X", version="1", category="C")
    d = e.to_dict()
    assert "summary" not in d
    assert "developer" not in d
    assert "built_with" not in d
    assert "archive" not in d


# ---------------------------------------------------------------------------
# Repo.fetch_entry_by_id
# ---------------------------------------------------------------------------

def test_fetch_entry_by_id_found():
    repo = Repo(str(FIXTURES))
    entry = repo.fetch_entry_by_id("paint-clone")
    assert entry.id == "paint-clone"


def test_fetch_entry_by_id_missing_raises():
    repo = Repo(str(FIXTURES))
    with pytest.raises(RepoError, match="not found"):
        repo.fetch_entry_by_id("does-not-exist")


# ---------------------------------------------------------------------------
# Repo.resolve_asset_uri
# ---------------------------------------------------------------------------

def test_resolve_asset_uri_local():
    repo = Repo(str(FIXTURES))
    uri = repo.resolve_asset_uri("apps/example-app/icon.png")
    assert uri.endswith("apps/example-app/icon.png")


# ---------------------------------------------------------------------------
# Repo.is_writable
# ---------------------------------------------------------------------------

def test_local_repo_is_writable():
    assert Repo(str(FIXTURES)).is_writable is True


def test_http_repo_is_not_writable():
    repo = Repo.__new__(Repo)
    repo.uri = "http://example.com/repo"
    repo.name = "test"
    repo._fetcher = _HttpFetcher("http://example.com/repo")
    assert repo.is_writable is False


def test_https_repo_is_not_writable():
    repo = Repo.__new__(Repo)
    repo.uri = "https://example.com/repo"
    repo.name = "test"
    repo._fetcher = _HttpFetcher("https://example.com/repo")
    assert repo.is_writable is False


def test_ssh_repo_is_writable():
    repo = Repo("ssh://bob@builds.example.com/srv/cellar-repo")
    assert repo.is_writable is True


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
    assert set(repo.iter_categories()) == {"Productivity", "Graphics"}


# ---------------------------------------------------------------------------
# RepoManager
# ---------------------------------------------------------------------------

def test_repo_manager_merges_catalogues():
    mgr = RepoManager()
    mgr.add(Repo(str(FIXTURES)))
    assert len(mgr.fetch_all_catalogues()) == 2


def test_repo_manager_last_repo_wins():
    mgr = RepoManager()
    mgr.add(Repo(str(FIXTURES)))
    mgr.add(Repo(str(FIXTURES)))
    assert len(mgr.fetch_all_catalogues()) == 2


def test_repo_manager_skips_bad_repo(tmp_path):
    mgr = RepoManager()
    mgr.add(Repo(str(FIXTURES)))
    bad = Repo.__new__(Repo)
    bad.uri = str(tmp_path / "nonexistent")
    bad.name = "bad"
    bad._fetcher = _LocalFetcher(tmp_path / "nonexistent")
    mgr.add(bad)
    assert len(mgr.fetch_all_catalogues()) == 2


# ---------------------------------------------------------------------------
# _HttpFetcher
# ---------------------------------------------------------------------------

def _mock_response(body: bytes):
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def test_http_fetcher_fetch_bytes():
    payload = b'{"key": "value"}'
    with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
        assert _HttpFetcher("https://example.com/repo").fetch_bytes("catalogue.json") == payload


def test_http_fetcher_resolve_uri():
    f = _HttpFetcher("https://example.com/repo/")
    assert f.resolve_uri("apps/foo/icon.png") == "https://example.com/repo/apps/foo/icon.png"


def test_http_fetcher_trailing_slash_normalised():
    f1 = _HttpFetcher("https://example.com/repo")
    f2 = _HttpFetcher("https://example.com/repo/")
    assert f1.resolve_uri("catalogue.json") == f2.resolve_uri("catalogue.json")


def test_http_fetcher_http_error_raises():
    import urllib.error
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError("https://example.com/404", 404, "Not Found", {}, None),
    ):
        with pytest.raises(RepoError, match="HTTP 404"):
            _HttpFetcher("https://example.com/repo").fetch_bytes("missing.json")


def test_http_fetcher_network_error_raises():
    import urllib.error
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        with pytest.raises(RepoError, match="Network error"):
            _HttpFetcher("https://example.com/repo").fetch_bytes("catalogue.json")


def test_repo_http_catalogue():
    catalogue = (FIXTURES / "catalogue.json").read_bytes()
    with patch("urllib.request.urlopen", return_value=_mock_response(catalogue)):
        repo = Repo.__new__(Repo)
        repo.uri = "https://example.com/repo"
        repo.name = "test"
        repo._fetcher = _HttpFetcher("https://example.com/repo")
        entries = repo.fetch_catalogue()
    assert len(entries) == 2


# ---------------------------------------------------------------------------
# _SshFetcher
# ---------------------------------------------------------------------------

def test_ssh_fetcher_resolve_uri():
    f = _SshFetcher("host.example.com", "/srv/repo", user="alice", port=2222)
    assert f.resolve_uri("catalogue.json") == "ssh://alice@host.example.com:2222/srv/repo/catalogue.json"


def test_ssh_fetcher_resolve_uri_no_user_no_port():
    f = _SshFetcher("host.example.com", "/srv/repo")
    assert f.resolve_uri("apps/foo/icon.png") == "ssh://host.example.com/srv/repo/apps/foo/icon.png"


def test_ssh_fetcher_missing_ssh_raises():
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(RepoError, match="ssh executable not found"):
            _SshFetcher("host.example.com", "/srv/repo", user="alice").fetch_bytes("catalogue.json")


def test_ssh_fetcher_nonzero_exit_raises():
    result = MagicMock()
    result.returncode = 255
    result.stderr = b"Connection refused"
    with patch("subprocess.run", return_value=result):
        with pytest.raises(RepoError, match="SSH fetch failed"):
            _SshFetcher("host.example.com", "/srv/repo", user="alice").fetch_bytes("catalogue.json")


def test_ssh_fetcher_success():
    payload = b'{"cellar_version":1,"apps":[]}'
    result = MagicMock()
    result.returncode = 0
    result.stdout = payload
    with patch("subprocess.run", return_value=result):
        assert _SshFetcher("host.example.com", "/srv/repo", user="alice").fetch_bytes("catalogue.json") == payload


def test_ssh_fetcher_identity_file_in_command():
    result = MagicMock()
    result.returncode = 0
    result.stdout = b"data"
    with patch("subprocess.run", return_value=result) as mock_run:
        _SshFetcher(
            "host.example.com", "/srv/repo", user="alice",
            identity="/home/alice/.ssh/cellar_ed25519",
        ).fetch_bytes("catalogue.json")
    cmd = mock_run.call_args[0][0]
    assert "-i" in cmd
    assert "/home/alice/.ssh/cellar_ed25519" in cmd


def test_repo_ssh_uri_creates_ssh_fetcher():
    from cellar.backend.repo import _SshFetcher as SF
    repo = Repo("ssh://bob@builds.example.com/srv/cellar-repo")
    assert isinstance(repo._fetcher, SF)


def test_repo_ssh_uri_invalid_no_host_raises():
    with pytest.raises(RepoError, match="no host"):
        Repo("ssh:///path/without/host")

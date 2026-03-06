"""Tests for cellar/backend/runners.py."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import cellar.backend.runners as runners


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_release(tag: str, name: str = "", with_checksum: bool = False) -> dict:
    """Build a minimal GitHub Releases API release dict."""
    aname = f"{tag}.tar.gz"
    assets = [
        {
            "name": aname,
            "browser_download_url": f"https://github.com/GloriousEggroll/proton-ge-custom/releases/download/{tag}/{aname}",
            "size": 1234567,
        }
    ]
    if with_checksum:
        assets.append({
            "name": f"{aname}.sha512sum",
            "browser_download_url": f"https://github.com/GloriousEggroll/proton-ge-custom/releases/download/{tag}/{aname}.sha512sum",
            "size": 200,
        })
    return {
        "tag_name": tag,
        "name": name or tag,
        "assets": assets,
    }


def _mock_get(releases: list[dict]):
    """Return a mock requests.Session.get that returns the given releases JSON."""
    resp = Mock()
    resp.status_code = 200
    resp.raise_for_status = Mock()
    resp.json = Mock(return_value=releases)
    return Mock(return_value=resp)


# ---------------------------------------------------------------------------
# fetch_releases
# ---------------------------------------------------------------------------

def _clear_cache():
    runners._cache = None


def test_fetch_releases_returns_list(monkeypatch):
    _clear_cache()
    api_data = [_fake_release("GE-Proton10-32"), _fake_release("GE-Proton10-31")]
    with patch("requests.Session.get", _mock_get(api_data)):
        result = runners.fetch_releases()
    assert len(result) == 2
    assert result[0]["tag"] == "GE-Proton10-32"
    assert result[0]["url"].endswith(".tar.gz")
    assert result[0]["size"] == 1234567


def test_fetch_releases_includes_checksum(monkeypatch):
    _clear_cache()
    api_data = [_fake_release("GE-Proton10-32", with_checksum=True)]
    with patch("requests.Session.get", _mock_get(api_data)):
        result = runners.fetch_releases()
    assert result[0]["checksum"].startswith("sha512:https://")


def test_fetch_releases_no_checksum_is_empty_string(monkeypatch):
    _clear_cache()
    api_data = [_fake_release("GE-Proton10-32", with_checksum=False)]
    with patch("requests.Session.get", _mock_get(api_data)):
        result = runners.fetch_releases()
    assert result[0]["checksum"] == ""


def test_fetch_releases_skips_non_tarball_assets():
    _clear_cache()
    api_data = [{
        "tag_name": "GE-Proton10-32",
        "name": "GE-Proton10-32",
        "assets": [
            {"name": "GE-Proton10-32.tar.gz.sha512sum", "browser_download_url": "https://x/hash", "size": 200},
            {"name": "GE-Proton10-32.tar.gz", "browser_download_url": "https://x/archive.tar.gz", "size": 999},
        ],
    }]
    with patch("requests.Session.get", _mock_get(api_data)):
        result = runners.fetch_releases()
    assert len(result) == 1
    assert result[0]["url"].endswith(".tar.gz")


def test_fetch_releases_respects_limit():
    _clear_cache()
    api_data = [_fake_release(f"GE-Proton10-{i}") for i in range(20)]
    with patch("requests.Session.get", _mock_get(api_data)):
        result = runners.fetch_releases(limit=5)
    assert len(result) == 5


def test_fetch_releases_uses_cache():
    _clear_cache()
    api_data = [_fake_release("GE-Proton10-32")]
    mock_get = _mock_get(api_data)
    with patch("requests.Session.get", mock_get):
        runners.fetch_releases()
        runners.fetch_releases()
    # Second call should use cache; the mock's session.get only called once.
    assert mock_get.call_count == 1


def test_fetch_releases_network_failure_returns_cache():
    _clear_cache()
    api_data = [_fake_release("GE-Proton10-32")]
    with patch("requests.Session.get", _mock_get(api_data)):
        first = runners.fetch_releases()

    # Force cache to be stale so a new fetch is attempted.
    runners._cache = (0.0, runners._cache[1])

    failing_get = Mock(side_effect=OSError("network error"))
    with patch("requests.Session.get", failing_get):
        result = runners.fetch_releases()
    assert result == first


def test_fetch_releases_network_failure_no_cache_returns_empty():
    _clear_cache()
    failing_get = Mock(side_effect=OSError("network error"))
    with patch("requests.Session.get", failing_get):
        result = runners.fetch_releases()
    assert result == []


def test_fetch_releases_empty_assets_skipped():
    _clear_cache()
    api_data = [
        {"tag_name": "GE-Proton10-32", "name": "GE-Proton10-32", "assets": []},
        _fake_release("GE-Proton10-31"),
    ]
    with patch("requests.Session.get", _mock_get(api_data)):
        result = runners.fetch_releases()
    assert len(result) == 1
    assert result[0]["tag"] == "GE-Proton10-31"


# ---------------------------------------------------------------------------
# get_release_info
# ---------------------------------------------------------------------------

def test_get_release_info_found_by_tag():
    _clear_cache()
    api_data = [_fake_release("GE-Proton10-32"), _fake_release("GE-Proton10-31")]
    with patch("requests.Session.get", _mock_get(api_data)):
        info = runners.get_release_info("GE-Proton10-31")
    assert info is not None
    assert info["tag"] == "GE-Proton10-31"


def test_get_release_info_not_found_returns_none():
    _clear_cache()
    api_data = [_fake_release("GE-Proton10-32")]
    with patch("requests.Session.get", _mock_get(api_data)):
        info = runners.get_release_info("GE-Proton9-99")
    assert info is None


def test_get_release_info_found_by_name():
    _clear_cache()
    api_data = [_fake_release("GE-Proton10-32", name="GE-Proton 10-32")]
    with patch("requests.Session.get", _mock_get(api_data)):
        info = runners.get_release_info("GE-Proton 10-32")
    assert info is not None


# ---------------------------------------------------------------------------
# is_installed / installed_runners
# ---------------------------------------------------------------------------

def test_is_installed_true(tmp_path):
    (tmp_path / "GE-Proton10-32").mkdir()
    with patch("cellar.backend.umu.runners_dir", return_value=tmp_path):
        assert runners.is_installed("GE-Proton10-32") is True


def test_is_installed_false(tmp_path):
    with patch("cellar.backend.umu.runners_dir", return_value=tmp_path):
        assert runners.is_installed("GE-Proton10-32") is False


def test_installed_runners_sorted_descending(tmp_path):
    for name in ["GE-Proton10-30", "GE-Proton10-32", "GE-Proton10-31"]:
        (tmp_path / name).mkdir()
    with patch("cellar.backend.umu.runners_dir", return_value=tmp_path):
        result = runners.installed_runners()
    assert result == ["GE-Proton10-32", "GE-Proton10-31", "GE-Proton10-30"]


def test_installed_runners_empty_dir(tmp_path):
    with patch("cellar.backend.umu.runners_dir", return_value=tmp_path):
        assert runners.installed_runners() == []


def test_installed_runners_missing_dir(tmp_path):
    missing = tmp_path / "runners"
    with patch("cellar.backend.umu.runners_dir", return_value=missing):
        assert runners.installed_runners() == []


def test_installed_runners_ignores_files(tmp_path):
    (tmp_path / "GE-Proton10-32").mkdir()
    (tmp_path / "README.txt").write_text("not a runner")
    with patch("cellar.backend.umu.runners_dir", return_value=tmp_path):
        result = runners.installed_runners()
    assert result == ["GE-Proton10-32"]

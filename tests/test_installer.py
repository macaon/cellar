"""Tests for cellar/backend/installer.py."""

from __future__ import annotations

import hashlib
import tarfile
import threading
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import pytest

from cellar.backend import installer as ins
from cellar.backend.bottles import BottlesInstall
from cellar.models.app_entry import AppEntry


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _entry(**kwargs) -> AppEntry:
    defaults = dict(
        id="test-app",
        name="Test App",
        version="1.0",
        category="Games",
        archive="apps/test-app/test-app-1.0.tar.gz",
        archive_size=0,
        archive_sha256="",
    )
    defaults.update(kwargs)
    return AppEntry(**defaults)


def _bottles(tmp_path: Path) -> BottlesInstall:
    data = tmp_path / "bottles"
    data.mkdir()
    return BottlesInstall(data_path=data, variant="native", cli_cmd=["bottles-cli"])


def _make_archive(tmp_path: Path, bottle_name: str = "TestBottle") -> Path:
    """Create a minimal .tar.gz containing a fake bottle directory."""
    src = tmp_path / "_bottle_src" / bottle_name
    src.mkdir(parents=True)
    (src / "bottle.yml").write_text("Name: TestBottle\nRunner: wine-7.0\n")
    (src / "drive_c").mkdir()
    archive = tmp_path / "test-app-1.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(src, arcname=bottle_name)
    return archive


# ---------------------------------------------------------------------------
# _safe_bottle_name
# ---------------------------------------------------------------------------

def test_safe_bottle_name_no_collision(tmp_path):
    data = tmp_path / "bottles"
    data.mkdir()
    assert ins._safe_bottle_name("my-app", data) == "my-app"


def test_safe_bottle_name_first_collision(tmp_path):
    data = tmp_path / "bottles"
    data.mkdir()
    (data / "my-app").mkdir()
    assert ins._safe_bottle_name("my-app", data) == "my-app-2"


def test_safe_bottle_name_multiple_collisions(tmp_path):
    data = tmp_path / "bottles"
    data.mkdir()
    (data / "my-app").mkdir()
    (data / "my-app-2").mkdir()
    assert ins._safe_bottle_name("my-app", data) == "my-app-3"


# ---------------------------------------------------------------------------
# _find_bottle_dir
# ---------------------------------------------------------------------------

def test_find_bottle_dir_single_dir(tmp_path):
    extract = tmp_path / "extracted"
    bottle = extract / "MyBottle"
    bottle.mkdir(parents=True)
    assert ins._find_bottle_dir(extract) == bottle


def test_find_bottle_dir_picks_bottle_yml(tmp_path):
    extract = tmp_path / "extracted"
    (extract / "dir1").mkdir(parents=True)
    bottle = extract / "dir2"
    bottle.mkdir()
    (bottle / "bottle.yml").write_text("")
    assert ins._find_bottle_dir(extract) == bottle


def test_find_bottle_dir_no_dirs_raises(tmp_path):
    extract = tmp_path / "extracted"
    extract.mkdir()
    (extract / "somefile.txt").write_text("x")
    with pytest.raises(ins.InstallError, match="no directories"):
        ins._find_bottle_dir(extract)


def test_find_bottle_dir_ambiguous_raises(tmp_path):
    extract = tmp_path / "extracted"
    (extract / "dir1").mkdir(parents=True)
    (extract / "dir2").mkdir()
    # Neither has bottle.yml
    with pytest.raises(ins.InstallError, match="Cannot identify"):
        ins._find_bottle_dir(extract)


# ---------------------------------------------------------------------------
# _verify_sha256
# ---------------------------------------------------------------------------

def test_verify_sha256_correct(tmp_path):
    data = b"hello world"
    f = tmp_path / "file.bin"
    f.write_bytes(data)
    expected = hashlib.sha256(data).hexdigest()
    ins._verify_sha256(f, expected)  # must not raise


def test_verify_sha256_mismatch_raises(tmp_path):
    f = tmp_path / "file.bin"
    f.write_bytes(b"hello world")
    with pytest.raises(ins.InstallError, match="SHA256"):
        ins._verify_sha256(f, "a" * 64)


# ---------------------------------------------------------------------------
# _extract_archive
# ---------------------------------------------------------------------------

def test_extract_archive_creates_contents(tmp_path):
    archive = _make_archive(tmp_path, "MyBottle")
    dest = tmp_path / "extracted"
    dest.mkdir()
    ins._extract_archive(archive, dest, None)
    assert (dest / "MyBottle" / "bottle.yml").exists()
    assert (dest / "MyBottle" / "drive_c").is_dir()


def test_extract_archive_bad_file_raises(tmp_path):
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tarball")
    dest = tmp_path / "extracted"
    dest.mkdir()
    with pytest.raises(ins.InstallError, match="extract"):
        ins._extract_archive(bad, dest, None)


# ---------------------------------------------------------------------------
# _acquire_archive — local
# ---------------------------------------------------------------------------

def test_acquire_local_bare_path_returns_original(tmp_path):
    archive = _make_archive(tmp_path)
    dest = tmp_path / "download.tar.gz"
    result = ins._acquire_archive(
        str(archive), dest, expected_size=0, progress_cb=None, cancel_event=None
    )
    assert result == archive


def test_acquire_local_file_uri_returns_original(tmp_path):
    archive = _make_archive(tmp_path)
    dest = tmp_path / "download.tar.gz"
    result = ins._acquire_archive(
        archive.as_uri(), dest, expected_size=0, progress_cb=None, cancel_event=None
    )
    assert result == archive


def test_acquire_unsupported_scheme_raises(tmp_path):
    dest = tmp_path / "download.tar.gz"
    with pytest.raises(ins.InstallError, match="not yet supported"):
        ins._acquire_archive(
            "ssh://host/path/archive.tar.gz", dest,
            expected_size=0, progress_cb=None, cancel_event=None,
        )


# ---------------------------------------------------------------------------
# _acquire_archive — HTTP
# ---------------------------------------------------------------------------

class _FakeResponse:
    """Minimal urllib response mock for streaming tests."""
    def __init__(self, data: bytes) -> None:
        self._buf = BytesIO(data)

    def read(self, n: int) -> bytes:
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


def test_acquire_http_downloads_to_dest(tmp_path):
    archive = _make_archive(tmp_path)
    data = archive.read_bytes()
    dest = tmp_path / "download.tar.gz"
    with patch("cellar.backend.installer.urllib.request.urlopen",
               return_value=_FakeResponse(data)):
        result = ins._acquire_archive(
            "https://example.com/archive.tar.gz", dest,
            expected_size=len(data), progress_cb=None, cancel_event=None,
        )
    assert result == dest
    assert dest.read_bytes() == data


def test_acquire_http_cancel_cleans_up(tmp_path):
    archive = _make_archive(tmp_path)
    data = archive.read_bytes()
    dest = tmp_path / "download.tar.gz"
    cancel = threading.Event()
    cancel.set()
    with patch("cellar.backend.installer.urllib.request.urlopen",
               return_value=_FakeResponse(data)):
        with pytest.raises(ins.InstallCancelled):
            ins._acquire_archive(
                "https://example.com/archive.tar.gz", dest,
                expected_size=len(data), progress_cb=None, cancel_event=cancel,
            )
    assert not dest.exists()


def test_acquire_http_progress_reported(tmp_path):
    archive = _make_archive(tmp_path)
    data = archive.read_bytes()
    dest = tmp_path / "download.tar.gz"
    reported: list[float] = []
    with patch("cellar.backend.installer.urllib.request.urlopen",
               return_value=_FakeResponse(data)):
        ins._acquire_archive(
            "https://example.com/archive.tar.gz", dest,
            expected_size=len(data), progress_cb=reported.append, cancel_event=None,
        )
    # At least one progress update with a value in [0, 1]
    assert reported
    assert all(0.0 <= f <= 1.0 for f in reported)


# ---------------------------------------------------------------------------
# install_app — integration
# ---------------------------------------------------------------------------

def test_install_app_local_happy_path(tmp_path):
    archive = _make_archive(tmp_path, "TestBottle")
    bottles = _bottles(tmp_path)
    entry = _entry()
    bottle_name = ins.install_app(entry, str(archive), bottles)
    assert bottle_name == "test-app"
    assert (bottles.data_path / "test-app" / "bottle.yml").exists()
    assert (bottles.data_path / "test-app" / "drive_c").is_dir()


def test_install_app_collision_suffix(tmp_path):
    archive = _make_archive(tmp_path)
    bottles = _bottles(tmp_path)
    (bottles.data_path / "test-app").mkdir()   # pre-existing
    entry = _entry()
    bottle_name = ins.install_app(entry, str(archive), bottles)
    assert bottle_name == "test-app-2"
    assert (bottles.data_path / "test-app-2").is_dir()


def test_install_app_sha256_verified(tmp_path):
    archive = _make_archive(tmp_path)
    bottles = _bottles(tmp_path)
    entry = _entry(archive_sha256="a" * 64)  # wrong hash
    with pytest.raises(ins.InstallError, match="SHA256"):
        ins.install_app(entry, str(archive), bottles)


def test_install_app_sha256_correct_passes(tmp_path):
    archive = _make_archive(tmp_path)
    bottles = _bottles(tmp_path)
    sha = hashlib.sha256(archive.read_bytes()).hexdigest()
    entry = _entry(archive_sha256=sha)
    bottle_name = ins.install_app(entry, str(archive), bottles)
    assert bottle_name == "test-app"


def test_install_app_cancel_before_start(tmp_path):
    archive = _make_archive(tmp_path)
    bottles = _bottles(tmp_path)
    entry = _entry()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ins.InstallCancelled):
        ins.install_app(entry, str(archive), bottles, cancel_event=cancel)


def test_install_app_progress_reported(tmp_path):
    archive = _make_archive(tmp_path)
    bottles = _bottles(tmp_path)
    entry = _entry()
    calls: list[tuple[str, float]] = []
    ins.install_app(entry, str(archive), bottles, progress_cb=lambda p, f: calls.append((p, f)))
    phases = [c[0] for c in calls]
    assert "Downloading" in phases
    assert "Installing" in phases
    # Each phase should have its own 0–1 range
    assert all(0.0 <= f <= 1.0 for _, f in calls)
    # Download should complete (reach 1.0) before Installing starts
    dl_fracs = [f for p, f in calls if p == "Downloading"]
    inst_fracs = [f for p, f in calls if p == "Installing"]
    assert dl_fracs[-1] == 1.0
    assert inst_fracs[-1] == 1.0


def test_install_app_unsupported_scheme_raises(tmp_path):
    bottles = _bottles(tmp_path)
    entry = _entry()
    with pytest.raises(ins.InstallError, match="not yet supported"):
        ins.install_app(entry, "ssh://host/path/archive.tar.gz", bottles)


def test_install_app_partial_copy_cleaned_up_on_error(tmp_path):
    """If copytree fails mid-way, the partial destination is removed."""
    archive = _make_archive(tmp_path)
    bottles = _bottles(tmp_path)
    entry = _entry()
    with patch("cellar.backend.installer.shutil.copytree",
               side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            ins.install_app(entry, str(archive), bottles)
    # Destination must not exist (cleaned up)
    assert not (bottles.data_path / "test-app").exists()

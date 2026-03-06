"""Tests for cellar/backend/installer.py."""

from __future__ import annotations

import tarfile
import threading
import zlib
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from cellar.backend import installer as ins
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
        archive_crc32="",
    )
    defaults.update(kwargs)
    return AppEntry(**defaults)


def _make_archive(
    tmp_path: Path,
    prefix_name: str = "prefix",
    extra_content: str = "",
) -> Path:
    """Create a minimal .tar.gz containing a fake umu prefix directory."""
    src = tmp_path / "_archive_src" / prefix_name
    src.mkdir(parents=True)
    (src / "drive_c").mkdir()
    if extra_content:
        (src / "extra.txt").write_text(extra_content)
    archive = tmp_path / "test-app-1.0.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(src, arcname=prefix_name)
    return archive


def _patch_prefixes_dir(tmp_path: Path):
    """Patch umu.prefixes_dir to use a temp directory."""
    prefixes = tmp_path / "prefixes"
    prefixes.mkdir(exist_ok=True)
    return patch("cellar.backend.umu.prefixes_dir", return_value=prefixes)


# ---------------------------------------------------------------------------
# _find_bottle_dir
# ---------------------------------------------------------------------------

def test_find_bottle_dir_single_dir(tmp_path):
    extract = tmp_path / "extracted"
    bottle = extract / "MyBottle"
    bottle.mkdir(parents=True)
    assert ins._find_bottle_dir(extract) == bottle


def test_find_bottle_dir_prefers_prefix_name(tmp_path):
    """A directory named 'prefix' is preferred (Cellar-native umu archive)."""
    extract = tmp_path / "extracted"
    (extract / "dir1").mkdir(parents=True)
    prefix = extract / "prefix"
    prefix.mkdir()
    assert ins._find_bottle_dir(extract) == prefix


def test_find_bottle_dir_picks_bottle_yml(tmp_path):
    """Legacy Bottles archive: directory with bottle.yml is preferred."""
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
    # Neither is named 'prefix' nor has bottle.yml
    with pytest.raises(ins.InstallError, match="Cannot identify"):
        ins._find_bottle_dir(extract)


# ---------------------------------------------------------------------------
# _verify_crc32
# ---------------------------------------------------------------------------

def test_verify_crc32_correct(tmp_path):
    data = b"hello world"
    f = tmp_path / "file.bin"
    f.write_bytes(data)
    expected = format(zlib.crc32(data) & 0xFFFFFFFF, "08x")
    ins._verify_crc32(f, expected)  # must not raise


def test_verify_crc32_mismatch_raises(tmp_path):
    f = tmp_path / "file.bin"
    f.write_bytes(b"hello world")
    with pytest.raises(ins.InstallError, match="CRC32"):
        ins._verify_crc32(f, "deadbeef")


# ---------------------------------------------------------------------------
# _extract_archive
# ---------------------------------------------------------------------------

def test_extract_archive_creates_contents(tmp_path):
    archive = _make_archive(tmp_path, "prefix")
    dest = tmp_path / "extracted"
    dest.mkdir()
    ins._extract_archive(archive, dest, None)
    assert (dest / "prefix" / "drive_c").is_dir()


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

def _fake_streaming_response(data: bytes):
    """Create a mock requests.Response that supports iter_content."""
    resp = Mock()
    resp.status_code = 200
    resp.raise_for_status = Mock()
    # iter_content yields the data in chunks
    chunk_size = 1024 * 1024
    chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
    resp.iter_content = Mock(return_value=iter(chunks))
    return resp


def test_acquire_http_downloads_to_dest(tmp_path):
    archive = _make_archive(tmp_path)
    data = archive.read_bytes()
    dest = tmp_path / "download.tar.gz"
    with patch("requests.Session.get",
               return_value=_fake_streaming_response(data)):
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
    with patch("requests.Session.get",
               return_value=_fake_streaming_response(data)):
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
    with patch("requests.Session.get",
               return_value=_fake_streaming_response(data)):
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
    archive = _make_archive(tmp_path, "prefix")
    entry = _entry()
    with _patch_prefixes_dir(tmp_path):
        prefix_dir = ins.install_app(entry, str(archive))
    # Returns entry.id; prefix installed at prefixes_dir() / entry.id
    assert prefix_dir == "test-app"
    assert (tmp_path / "prefixes" / "test-app" / "drive_c").is_dir()


def test_install_app_crc32_verified(tmp_path):
    archive = _make_archive(tmp_path)
    entry = _entry(archive_crc32="deadbeef")  # wrong hash
    with _patch_prefixes_dir(tmp_path):
        with pytest.raises(ins.InstallError, match="CRC32"):
            ins.install_app(entry, str(archive))


def test_install_app_crc32_correct_passes(tmp_path):
    archive = _make_archive(tmp_path)
    crc = format(zlib.crc32(archive.read_bytes()) & 0xFFFFFFFF, "08x")
    entry = _entry(archive_crc32=crc)
    with _patch_prefixes_dir(tmp_path):
        prefix_dir = ins.install_app(entry, str(archive))
    assert prefix_dir == "test-app"


def test_install_app_cancel_before_start(tmp_path):
    archive = _make_archive(tmp_path)
    entry = _entry()
    cancel = threading.Event()
    cancel.set()
    with _patch_prefixes_dir(tmp_path):
        with pytest.raises(ins.InstallCancelled):
            ins.install_app(entry, str(archive), cancel_event=cancel)


def test_install_app_progress_reported(tmp_path):
    archive = _make_archive(tmp_path)
    entry = _entry()
    dl_calls: list[float] = []
    inst_calls: list[float] = []
    with _patch_prefixes_dir(tmp_path):
        ins.install_app(
            entry, str(archive),
            download_cb=lambda f: dl_calls.append(f),
            install_cb=lambda f: inst_calls.append(f),
        )
    # Download bar: 0 → 1
    assert len(dl_calls) >= 2
    assert dl_calls[0] == 0.0
    assert dl_calls[-1] == 1.0
    assert all(0.0 <= f <= 1.0 for f in dl_calls)
    # Install bar: 0 → 1
    assert len(inst_calls) >= 2
    assert inst_calls[0] == 0.0
    assert inst_calls[-1] == 1.0
    assert all(0.0 <= f <= 1.0 for f in inst_calls)


def test_install_app_unsupported_scheme_raises(tmp_path):
    entry = _entry()
    with _patch_prefixes_dir(tmp_path):
        with pytest.raises(ins.InstallError, match="not yet supported"):
            ins.install_app(entry, "ssh://host/path/archive.tar.gz")


def test_install_app_partial_copy_cleaned_up_on_error(tmp_path):
    """If the file copy loop fails mid-way, the partial destination is removed."""
    archive = _make_archive(tmp_path, "prefix", extra_content="content")
    entry = _entry()
    with _patch_prefixes_dir(tmp_path):
        with patch("cellar.backend.installer.shutil.copy2",
                   side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                ins.install_app(entry, str(archive))
    assert not (tmp_path / "prefixes" / "test-app").exists()


# ---------------------------------------------------------------------------
# Delta helpers: _seed_from_base / _overlay_delta
# ---------------------------------------------------------------------------

def test_seed_from_base_copies_files(tmp_path):
    """Files seeded from base are present in the destination with correct content."""
    base = tmp_path / "base"
    dest = tmp_path / "bottle"
    dest.mkdir()
    (base / "drive_c" / "windows").mkdir(parents=True)
    (base / "drive_c" / "windows" / "notepad.exe").write_bytes(b"notepad")

    ins._seed_from_base(base, dest)

    seeded = dest / "drive_c" / "windows" / "notepad.exe"
    assert seeded.exists()
    assert seeded.read_bytes() == b"notepad"


def test_overlay_delta_updates_changed_file(tmp_path):
    """Overlaying a changed file must not modify the base content."""
    base = tmp_path / "base"
    dest = tmp_path / "bottle"
    delta = tmp_path / "delta"
    dest.mkdir()

    (base / "drive_c").mkdir(parents=True)
    base_file = base / "drive_c" / "app.exe"
    base_file.write_bytes(b"original_version_one")

    ins._seed_from_base(base, dest)

    (delta / "drive_c").mkdir(parents=True)
    (delta / "drive_c" / "app.exe").write_bytes(b"updated_version_two_data")

    ins._overlay_delta(delta, dest)

    dest_file = dest / "drive_c" / "app.exe"
    assert dest_file.read_bytes() == b"updated_version_two_data"
    assert base_file.read_bytes() == b"original_version_one"


def test_overlay_delta_adds_new_files(tmp_path):
    """Files present only in the delta are added to the bottle."""
    base = tmp_path / "base"
    dest = tmp_path / "bottle"
    delta = tmp_path / "delta"
    dest.mkdir()
    base.mkdir()

    ins._seed_from_base(base, dest)

    (delta / "drive_c" / "myapp").mkdir(parents=True)
    (delta / "drive_c" / "myapp" / "myapp.exe").write_bytes(b"myapp")

    ins._overlay_delta(delta, dest)

    assert (dest / "drive_c" / "myapp" / "myapp.exe").read_bytes() == b"myapp"


def test_seed_from_base_unchanged_files_preserved(tmp_path):
    """Files not touched by the delta remain in the bottle with correct content."""
    base = tmp_path / "base"
    dest = tmp_path / "bottle"
    delta = tmp_path / "delta"
    dest.mkdir()

    (base / "drive_c" / "windows").mkdir(parents=True)
    shared = base / "drive_c" / "windows" / "system.dll"
    shared.write_bytes(b"system")

    ins._seed_from_base(base, dest)
    delta.mkdir()
    ins._overlay_delta(delta, dest)  # empty delta — nothing changes

    seeded = dest / "drive_c" / "windows" / "system.dll"
    assert seeded.read_bytes() == b"system"

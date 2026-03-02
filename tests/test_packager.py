"""Tests for cellar/backend/packager.py — delta archive creation."""

from __future__ import annotations

import tarfile
from pathlib import Path
from unittest.mock import patch

import pytest

from cellar.backend import packager as pkg


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_full_archive(
    tmp_path: Path,
    bottle_name: str = "MyBottle",
    extra_files: dict[str, bytes] | None = None,
) -> Path:
    """Create a minimal full-backup .tar.gz containing a fake bottle."""
    src = tmp_path / "_src" / bottle_name
    (src / "drive_c" / "windows" / "system32").mkdir(parents=True)
    (src / "drive_c" / "windows" / "system32" / "ntdll.dll").write_bytes(b"ntdll_v1")
    (src / "bottle.yml").write_text(f"Name: {bottle_name}\nRunner: wine-9.0\nWindows: win10\n")
    if extra_files:
        for rel, data in extra_files.items():
            f = src / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
    archive = tmp_path / f"{bottle_name}.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(src, arcname=bottle_name)
    return archive


def _make_base_dir(
    tmp_path: Path,
    files: dict[str, bytes] | None = None,
) -> Path:
    """Create a fake extracted base directory."""
    base = tmp_path / "base"
    (base / "drive_c" / "windows" / "system32").mkdir(parents=True)
    (base / "drive_c" / "windows" / "system32" / "ntdll.dll").write_bytes(b"ntdll_v1")
    # Deliberately shorter than the app bottle.yml so size differs in the Python fallback.
    (base / "bottle.yml").write_text("Name: base\nWindows: win10\n")
    if files:
        for rel, data in files.items():
            f = base / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
    return base


# ---------------------------------------------------------------------------
# _compute_delta (Python fallback, rsync mocked out)
# ---------------------------------------------------------------------------

@patch("shutil.which", return_value=None)
def test_compute_delta_excludes_same_size_file(_, tmp_path):
    """Files with the same size as base are excluded from the delta."""
    full = tmp_path / "full"
    (full / "drive_c").mkdir(parents=True)
    (full / "drive_c" / "app.exe").write_bytes(b"AAAA")  # 4 bytes

    base = tmp_path / "base"
    (base / "drive_c").mkdir(parents=True)
    (base / "drive_c" / "app.exe").write_bytes(b"BBBB")  # 4 bytes — same size, different content

    out = tmp_path / "delta"
    out.mkdir()
    pkg._compute_delta(full, base, out)

    # Same size → excluded by Python fallback (size-based heuristic)
    assert not (out / "drive_c" / "app.exe").exists()


@patch("shutil.which", return_value=None)
def test_compute_delta_includes_different_size_file(_, tmp_path):
    """Files with a different size from base ARE included in the delta."""
    full = tmp_path / "full"
    (full / "drive_c").mkdir(parents=True)
    (full / "drive_c" / "app.exe").write_bytes(b"updated_and_longer")

    base = tmp_path / "base"
    (base / "drive_c").mkdir(parents=True)
    (base / "drive_c" / "app.exe").write_bytes(b"old")

    out = tmp_path / "delta"
    out.mkdir()
    pkg._compute_delta(full, base, out)

    assert (out / "drive_c" / "app.exe").read_bytes() == b"updated_and_longer"


@patch("shutil.which", return_value=None)
def test_compute_delta_includes_new_files(_, tmp_path):
    """Files absent from the base are always included in the delta."""
    full = tmp_path / "full"
    (full / "drive_c" / "app").mkdir(parents=True)
    (full / "drive_c" / "app" / "game.exe").write_bytes(b"game binary")

    base = tmp_path / "base"
    base.mkdir()

    out = tmp_path / "delta"
    out.mkdir()
    pkg._compute_delta(full, base, out)

    assert (out / "drive_c" / "app" / "game.exe").read_bytes() == b"game binary"


@patch("shutil.which", return_value=None)
def test_compute_delta_preserves_subdirectory_structure(_, tmp_path):
    """Directory hierarchy is recreated in the delta output."""
    full = tmp_path / "full"
    (full / "a" / "b" / "c").mkdir(parents=True)
    (full / "a" / "b" / "c" / "deep.txt").write_bytes(b"deep file content")

    base = tmp_path / "base"
    base.mkdir()

    out = tmp_path / "delta"
    out.mkdir()
    pkg._compute_delta(full, base, out)

    assert (out / "a" / "b" / "c" / "deep.txt").exists()


# ---------------------------------------------------------------------------
# create_delta_archive — end-to-end
# ---------------------------------------------------------------------------

@patch("shutil.which", return_value=None)
def test_create_delta_archive_produces_valid_tarball(_, tmp_path):
    """create_delta_archive writes a valid .tar.gz file."""
    archive = _make_full_archive(tmp_path)
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.gz"

    pkg.create_delta_archive(archive, base, dest)

    assert dest.exists()
    assert tarfile.is_tarfile(dest)


@patch("shutil.which", return_value=None)
def test_create_delta_archive_preserves_bottle_name(_, tmp_path):
    """Top-level directory in the delta matches the original bottle name."""
    archive = _make_full_archive(tmp_path, bottle_name="GameBottle")
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.gz"

    pkg.create_delta_archive(archive, base, dest)

    with tarfile.open(dest, "r:gz") as tf:
        top_dirs = {m.name.split("/")[0] for m in tf.getmembers()}
    assert "GameBottle" in top_dirs


@patch("shutil.which", return_value=None)
def test_create_delta_archive_excludes_base_files(_, tmp_path):
    """Files that are identical to the base (same size) are not in the delta."""
    # ntdll.dll is in both full backup and base with the same content/size
    archive = _make_full_archive(tmp_path, bottle_name="TestBottle")
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.gz"

    pkg.create_delta_archive(archive, base, dest)

    with tarfile.open(dest, "r:gz") as tf:
        names = {m.name for m in tf.getmembers()}
    # ntdll.dll (8 bytes in both) should be excluded
    assert not any("ntdll.dll" in n for n in names)


@patch("shutil.which", return_value=None)
def test_create_delta_archive_includes_app_files(_, tmp_path):
    """Files only in the full backup (not in base) appear in the delta."""
    archive = _make_full_archive(
        tmp_path,
        bottle_name="TestBottle",
        extra_files={"drive_c/Program Files/MyApp/myapp.exe": b"app executable bytes here"},
    )
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.gz"

    pkg.create_delta_archive(archive, base, dest)

    with tarfile.open(dest, "r:gz") as tf:
        names = {m.name for m in tf.getmembers()}
    assert any("myapp.exe" in n for n in names)


@patch("shutil.which", return_value=None)
def test_create_delta_archive_includes_bottle_yml(_, tmp_path):
    """bottle.yml always differs (app-specific) so it is always in the delta."""
    archive = _make_full_archive(tmp_path, bottle_name="TestBottle")
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.gz"

    pkg.create_delta_archive(archive, base, dest)

    with tarfile.open(dest, "r:gz") as tf:
        names = {m.name for m in tf.getmembers()}
    # bottle.yml sizes differ (app bottle.yml != base bottle.yml)
    assert any("bottle.yml" in n for n in names)


@patch("shutil.which", return_value=None)
def test_create_delta_archive_progress_reported(_, tmp_path):
    """progress_cb is called at 0.0, 0.3, 0.7, and 1.0."""
    archive = _make_full_archive(tmp_path)
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.gz"
    calls: list[float] = []

    pkg.create_delta_archive(archive, base, dest, progress_cb=calls.append)

    assert calls[0] == 0.0
    assert calls[-1] == 1.0
    assert all(0.0 <= f <= 1.0 for f in calls)


@patch("shutil.which", return_value=None)
def test_compute_delta_writes_delete_manifest(_, tmp_path):
    """Files in base but absent from full backup appear in .cellar_delete."""
    full = tmp_path / "full"
    (full / "drive_c" / "app").mkdir(parents=True)
    (full / "drive_c" / "app" / "game.exe").write_bytes(b"game")

    base = tmp_path / "base"
    (base / "drive_c" / "app").mkdir(parents=True)
    (base / "drive_c" / "app" / "game.exe").write_bytes(b"game")
    # This base file was deleted before the app backup was taken.
    (base / "drive_c" / "windows" / "temp").mkdir(parents=True)
    (base / "drive_c" / "windows" / "temp" / "setup.tmp").write_bytes(b"tempdata")

    out = tmp_path / "delta"
    out.mkdir()
    pkg._compute_delta(full, base, out)

    manifest = (out / ".cellar_delete").read_text().splitlines()
    assert any("setup.tmp" in p for p in manifest)


@patch("shutil.which", return_value=None)
def test_compute_delta_no_manifest_when_nothing_deleted(_, tmp_path):
    """.cellar_delete is not written when all base files are in the full backup."""
    full = tmp_path / "full"
    (full / "drive_c").mkdir(parents=True)
    (full / "drive_c" / "ntdll.dll").write_bytes(b"ntdll")
    (full / "drive_c" / "app.exe").write_bytes(b"app longer")

    base = tmp_path / "base"
    (base / "drive_c").mkdir(parents=True)
    (base / "drive_c" / "ntdll.dll").write_bytes(b"ntdll")

    out = tmp_path / "delta"
    out.mkdir()
    pkg._compute_delta(full, base, out)

    assert not (out / ".cellar_delete").exists()


@patch("shutil.which", return_value=None)
def test_create_delta_archive_bad_source_raises(_, tmp_path):
    """Passing a corrupt archive raises RuntimeError."""
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tarball at all")
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.gz"

    with pytest.raises(RuntimeError, match="extract"):
        pkg.create_delta_archive(bad, base, dest)

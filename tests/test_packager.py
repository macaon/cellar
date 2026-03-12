"""Tests for cellar/backend/packager.py — delta archive creation."""

from __future__ import annotations

import tarfile
from pathlib import Path

import pytest
import zstandard as zstd

from cellar.backend import packager as pkg


def _open_zst(path: Path) -> tarfile.TarFile:
    """Return a TarFile opened from a .tar.zst path (streaming, read-only)."""
    dctx = zstd.ZstdDecompressor()
    raw = open(path, "rb")  # noqa: SIM115 — kept open for duration of test
    reader = dctx.stream_reader(raw)
    return tarfile.open(fileobj=reader, mode="r|")


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
    # Different content from the app bottle.yml so it is included in the delta.
    (base / "bottle.yml").write_text("Name: base\nWindows: win10\n")
    if files:
        for rel, data in files.items():
            f = base / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(data)
    return base


# ---------------------------------------------------------------------------
# _compute_delta (content-hash comparison)
# ---------------------------------------------------------------------------

def test_compute_delta_excludes_identical_file(tmp_path):
    """Files with identical content to the base are excluded from the delta."""
    full = tmp_path / "full"
    (full / "drive_c").mkdir(parents=True)
    (full / "drive_c" / "ntdll.dll").write_bytes(b"same content as base")

    base = tmp_path / "base"
    (base / "drive_c").mkdir(parents=True)
    (base / "drive_c" / "ntdll.dll").write_bytes(b"same content as base")

    out = tmp_path / "delta"
    out.mkdir()
    pkg._compute_delta(full, base, out)

    assert not (out / "drive_c" / "ntdll.dll").exists()


def test_compute_delta_includes_same_size_different_content(tmp_path):
    """Files with same size but different content ARE included in the delta.

    This is the key correctness guarantee: size alone is not sufficient to
    decide a file is unchanged; content must be compared.
    """
    full = tmp_path / "full"
    (full / "drive_c").mkdir(parents=True)
    (full / "drive_c" / "app.exe").write_bytes(b"AAAA")  # 4 bytes

    base = tmp_path / "base"
    (base / "drive_c").mkdir(parents=True)
    (base / "drive_c" / "app.exe").write_bytes(b"BBBB")  # 4 bytes — same size, different content

    out = tmp_path / "delta"
    out.mkdir()
    pkg._compute_delta(full, base, out)

    assert (out / "drive_c" / "app.exe").read_bytes() == b"AAAA"


def test_compute_delta_includes_different_content_file(tmp_path):
    """Files with different content from base ARE included in the delta."""
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


def test_compute_delta_includes_new_files(tmp_path):
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


def test_compute_delta_preserves_subdirectory_structure(tmp_path):
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

def test_create_delta_archive_produces_valid_tarball(tmp_path):
    """create_delta_archive writes a valid .tar.zst file."""
    archive = _make_full_archive(tmp_path)
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.zst"

    pkg.create_delta_archive(archive, base, dest)

    assert dest.exists()
    assert dest.stat().st_size > 0


def test_create_delta_archive_preserves_bottle_name(tmp_path):
    """Top-level directory in the delta matches the original bottle name."""
    archive = _make_full_archive(tmp_path, bottle_name="GameBottle")
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.zst"

    pkg.create_delta_archive(archive, base, dest)

    with _open_zst(dest) as tf:
        top_dirs = {m.name.split("/")[0] for m in tf.getmembers()}
    assert "GameBottle" in top_dirs


def test_create_delta_archive_excludes_base_files(tmp_path):
    """Files identical to the base (same content) are not in the delta."""
    # ntdll.dll is in both full backup and base with identical bytes
    archive = _make_full_archive(tmp_path, bottle_name="TestBottle")
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.zst"

    pkg.create_delta_archive(archive, base, dest)

    with _open_zst(dest) as tf:
        names = {m.name for m in tf.getmembers()}
    assert not any("ntdll.dll" in n for n in names)


def test_create_delta_archive_includes_app_files(tmp_path):
    """Files only in the full backup (not in base) appear in the delta."""
    archive = _make_full_archive(
        tmp_path,
        bottle_name="TestBottle",
        extra_files={"drive_c/Program Files/MyApp/myapp.exe": b"app executable bytes here"},
    )
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.zst"

    pkg.create_delta_archive(archive, base, dest)

    with _open_zst(dest) as tf:
        names = {m.name for m in tf.getmembers()}
    assert any("myapp.exe" in n for n in names)


def test_create_delta_archive_includes_bottle_yml(tmp_path):
    """bottle.yml has different content from the base so it is in the delta."""
    archive = _make_full_archive(tmp_path, bottle_name="TestBottle")
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.zst"

    pkg.create_delta_archive(archive, base, dest)

    with _open_zst(dest) as tf:
        names = {m.name for m in tf.getmembers()}
    assert any("bottle.yml" in n for n in names)


def test_create_delta_archive_progress_reported(tmp_path):
    """progress_cb emits values in [0, 1] per phase; extraction emits none."""
    archive = _make_full_archive(tmp_path)
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.zst"
    calls: list[float] = []

    pkg.create_delta_archive(archive, base, dest, progress_cb=calls.append)

    assert calls, "expected at least one progress call"
    assert all(0.0 <= f <= 1.0 for f in calls)
    # Each phase resets to 0→1; the final compression phase ends at 1.0.
    assert calls[-1] == 1.0


def test_create_delta_archive_returns_uncompressed_size(tmp_path):
    """Return value is (uncompressed_size, crc32_hex); size is delta-only."""
    extra = {"drive_c/Program Files/MyApp/myapp.exe": b"x" * 4096}
    archive = _make_full_archive(tmp_path, bottle_name="TestBottle", extra_files=extra)
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.zst"

    size, crc32 = pkg.create_delta_archive(archive, base, dest)

    # Must be positive and smaller than the full install size (base excluded).
    assert size > 0
    # ntdll.dll (shared with base) must not be counted.
    assert size < 1024 * 1024  # well under 1 MB — only app-unique files
    assert len(crc32) == 8
    assert all(c in "0123456789abcdef" for c in crc32)


def test_compute_delta_writes_delete_manifest(tmp_path):
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


def test_compute_delta_no_manifest_when_nothing_deleted(tmp_path):
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


def test_create_delta_archive_bad_source_raises(tmp_path):
    """Passing a corrupt archive raises RuntimeError."""
    bad = tmp_path / "bad.tar.gz"
    bad.write_bytes(b"not a tarball at all")
    base = _make_base_dir(tmp_path)
    dest = tmp_path / "delta.tar.gz"

    with pytest.raises(RuntimeError, match="extract"):
        pkg.create_delta_archive(bad, base, dest)

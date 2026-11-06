"""Regression coverage for interrupted app republishes."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cellar.backend import packager
from cellar.backend.publish_queue import PublishJob, PublishQueue
from cellar.models.app_entry import AppEntry


@pytest.mark.parametrize("outcome", ["success", "cancelled", "catalogue error"])
def test_republish_preserves_previous_archive(tmp_path, monkeypatch, outcome):
    entry = AppEntry(
        id="game", name="Game", version="1", category="Games", platform="linux",
        archive="apps/game/game.tar.zst", archive_crc32="old",
        archive_chunks=({"size": 3},),
    )
    old_chunk = tmp_path / f"{entry.archive}.001"
    old_chunk.parent.mkdir(parents=True)
    old_chunk.write_bytes(b"old")
    packager.import_to_repo(tmp_path, entry, "", {}, archive_in_place=True)
    catalogue = tmp_path / "catalogue.json"
    old_catalogue = catalogue.read_bytes()
    job = PublishJob(
        app_id=entry.id,
        app_name=entry.name,
        project=SimpleNamespace(
            selected_steam_urls=[], project_type="linux", source_dir=tmp_path,
        ),
        repo=SimpleNamespace(writable_path=lambda: tmp_path),
        all_repos=[],
        entry=entry,
        images={},
    )

    def compress(_source, dest, **_kwargs):
        assert old_chunk.read_bytes() == b"old"
        assert dest != tmp_path / entry.archive
        dest.with_name(dest.name + ".001").write_bytes(b"new")
        if outcome == "cancelled":
            job.cancel_event.set()
        return 3, "new", ({"size": 3},)

    monkeypatch.setattr(packager, "compress_prefix_zst", compress)
    queue = PublishQueue()
    if outcome == "cancelled":
        with pytest.raises(packager.CancelledError):
            queue._do_publish(job)
        assert list(old_chunk.parent.glob("*.001")) == [old_chunk]
        assert catalogue.read_bytes() == old_catalogue
    elif outcome == "catalogue error":
        replace = Path.replace

        def fail_catalogue_replace(source, target):
            if target == catalogue:
                raise OSError("replace failed")
            return replace(source, target)

        monkeypatch.setattr(Path, "replace", fail_catalogue_replace)
        with pytest.raises(OSError, match="replace failed"):
            queue._do_publish(job)
        assert catalogue.read_bytes() == old_catalogue
        assert not list(tmp_path.glob(".catalogue.json.*.tmp"))
    else:
        queue._do_publish(job)
        assert json.loads(catalogue.read_text())["apps"][0]["archive_crc32"] == "new"

    metadata = json.loads((old_chunk.parent / "metadata.json").read_text())
    current_chunk = tmp_path / f"{metadata['archive']}.001"
    assert current_chunk.read_bytes() == (b"old" if outcome == "cancelled" else b"new")
    assert old_chunk.read_bytes() == b"old"

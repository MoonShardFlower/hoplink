"""Tests for the storage backends and the manifest that indexes them."""

from __future__ import annotations

import json
import os
from typing import Any, Mapping

import pytest

from reddit_extract.storage import (
    FilesystemStorage,
    Manifest,
    ManifestSet,
    MemoryStorage,
)
from reddit_extract.storage import storage as storage_module
from reddit_extract.storage.storage import MANIFEST_NAME, StorageBackend


def entry(url: str, **extra: Any) -> dict[str, Any]:
    """One manifest record."""
    record = {"post_id": "p1", "media_url": url}
    record.update(extra)
    return record


def manifest_data(**files: Mapping[str, Any]) -> dict[str, Any]:
    """A manifest document as it would be read back from storage."""
    return {
        "source": "pics",
        "generated_at": "2026-07-16T00:00:00+00:00",
        "files": dict(files),
    }


class CountingMemoryStorage(MemoryStorage):
    """MemoryStorage that counts manifest writes, so a ManifestSet's flush cadence is observable."""

    def __init__(self) -> None:
        super().__init__()
        self.manifest_writes = 0

    def write_manifest(self, key: str, data: Mapping[str, Any]) -> str:
        self.manifest_writes += 1
        return super().write_manifest(key, data)


# -- MemoryStorage ----------------------------------------------------------


def test_memory_storage_starts_empty():
    storage = MemoryStorage()
    assert storage.list_files("pics") == []
    assert storage.read_manifest("pics") is None
    assert storage.exists("pics", "0001.jpg") is False


def test_memory_storage_round_trips_a_file():
    storage = MemoryStorage()
    assert storage.write("pics", "0001.jpg", b"bytes") == "memory://pics/0001.jpg"
    assert storage.files["pics"]["0001.jpg"] == b"bytes"
    assert storage.exists("pics", "0001.jpg") is True
    assert storage.list_files("pics") == ["0001.jpg"]


def test_memory_storage_prepare_creates_an_empty_namespace():
    storage = MemoryStorage()
    storage.prepare("pics")
    assert storage.files["pics"] == {}


def test_memory_storage_prepare_does_not_wipe_an_existing_namespace():
    storage = MemoryStorage()
    storage.write("pics", "0001.jpg", b"bytes")
    storage.prepare("pics")
    assert storage.list_files("pics") == ["0001.jpg"]


def test_memory_storage_keys_are_independent():
    storage = MemoryStorage()
    storage.write("pics", "0001.jpg", b"a")
    assert storage.exists("art", "0001.jpg") is False


def test_memory_storage_round_trips_a_manifest():
    storage = MemoryStorage()
    assert (
        storage.write_manifest("pics", manifest_data()) == "memory://pics/manifest.json"
    )
    assert storage.read_manifest("pics") == manifest_data()


def test_a_stored_manifest_is_copied_not_aliased():
    # A caller mutating what it wrote (or read) must not corrupt what storage holds.
    storage = MemoryStorage()
    original = manifest_data(**{"0001.jpg": entry("https://i.redd.it/a.jpg")})
    storage.write_manifest("pics", original)
    original["files"].clear()
    assert list(storage.read_manifest("pics")["files"]) == ["0001.jpg"]

    read_back = storage.read_manifest("pics")
    read_back["files"].clear()
    assert list(storage.read_manifest("pics")["files"]) == ["0001.jpg"]


def test_memory_storage_reports_a_location():
    assert MemoryStorage().location("pics") == "memory://pics"


# -- the backend interface's defaults ---------------------------------------


def test_a_minimal_backend_need_not_implement_listing_or_location():
    class Minimal(StorageBackend):
        def prepare(self, key):
            pass

        def exists(self, key, filename):
            return False

        def write(self, key, filename, data):
            return None

        def read_manifest(self, key):
            return None

        def write_manifest(self, key, data):
            return None

    assert list(Minimal().list_files("pics")) == []
    assert Minimal().location("pics") is None


def test_the_backend_interface_cannot_be_instantiated():
    with pytest.raises(TypeError):
        StorageBackend()  # type: ignore[abstract]


# -- FilesystemStorage ------------------------------------------------------


def test_files_land_under_root_key_filename(tmp_path):
    storage = FilesystemStorage(str(tmp_path))
    path = storage.write("pics", "0001.jpg", b"bytes")
    assert path == str(tmp_path / "pics" / "0001.jpg")
    assert (tmp_path / "pics" / "0001.jpg").read_bytes() == b"bytes"


def test_writing_creates_the_directory_on_demand(tmp_path):
    FilesystemStorage(str(tmp_path / "deep")).write("pics", "0001.jpg", b"bytes")
    assert (tmp_path / "deep" / "pics" / "0001.jpg").is_file()


def test_a_write_leaves_no_part_file_behind(tmp_path):
    # Downloads are written to a .part file and renamed, so a crash never leaves a
    # half-written file that a later run would mistake for a complete download.
    storage = FilesystemStorage(str(tmp_path))
    storage.write("pics", "0001.jpg", b"bytes")
    assert os.listdir(tmp_path / "pics") == ["0001.jpg"]


def test_a_write_retries_a_rename_blocked_by_another_process(tmp_path, monkeypatch):
    # Windows raises PermissionError while an antivirus or the indexer still holds the
    # destination open; the file is released a moment later, so the rename must retry.
    real_replace = os.replace
    calls = []

    def flaky_replace(src, dst):
        calls.append(src)
        if len(calls) < 3:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)

    monkeypatch.setattr(storage_module.os, "replace", flaky_replace)
    monkeypatch.setattr(storage_module.time, "sleep", lambda _: None)

    storage = FilesystemStorage(str(tmp_path))
    storage.write_manifest("pics", {"items": []})
    assert len(calls) == 3
    assert json.loads((tmp_path / "pics" / MANIFEST_NAME).read_text()) == {"items": []}


def test_a_rename_that_never_unblocks_raises_and_leaves_no_part_file(
    tmp_path, monkeypatch
):
    def blocked_replace(src, dst):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(storage_module.os, "replace", blocked_replace)
    monkeypatch.setattr(storage_module.time, "sleep", lambda _: None)

    storage = FilesystemStorage(str(tmp_path))
    with pytest.raises(PermissionError):
        storage.write("pics", "0001.jpg", b"bytes")
    assert os.listdir(tmp_path / "pics") == []


def test_a_write_replaces_an_existing_file(tmp_path):
    storage = FilesystemStorage(str(tmp_path))
    storage.write("pics", "0001.jpg", b"old")
    storage.write("pics", "0001.jpg", b"new")
    assert (tmp_path / "pics" / "0001.jpg").read_bytes() == b"new"


def test_prepare_creates_the_source_directory(tmp_path):
    FilesystemStorage(str(tmp_path)).prepare("pics")
    assert (tmp_path / "pics").is_dir()


def test_prepare_is_idempotent(tmp_path):
    storage = FilesystemStorage(str(tmp_path))
    storage.prepare("pics")
    storage.prepare("pics")  # must not raise


def test_exists_tracks_what_is_on_disk(tmp_path):
    storage = FilesystemStorage(str(tmp_path))
    assert storage.exists("pics", "0001.jpg") is False
    storage.write("pics", "0001.jpg", b"bytes")
    assert storage.exists("pics", "0001.jpg") is True


def test_listing_a_directory_that_does_not_exist_yet_is_empty(tmp_path):
    # A first run has nothing on disk; that is not an error.
    assert FilesystemStorage(str(tmp_path)).list_files("pics") == []


def test_listing_reports_stored_files(tmp_path):
    storage = FilesystemStorage(str(tmp_path))
    storage.write("pics", "0001.jpg", b"a")
    storage.write("pics", "0002.jpg", b"b")
    assert sorted(storage.list_files("pics")) == ["0001.jpg", "0002.jpg"]


def test_the_root_is_expanded(tmp_path, monkeypatch):
    # open() has no idea what ~ means, so a literal tilde would create a folder named "~".
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    root = FilesystemStorage("~/downloads").root
    assert not root.startswith("~")
    assert root == os.path.expanduser("~/downloads")


def test_an_expanded_root_really_writes_under_the_home_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    FilesystemStorage("~/downloads").write("pics", "0001.jpg", b"bytes")
    assert (tmp_path / "downloads" / "pics" / "0001.jpg").read_bytes() == b"bytes"


def test_the_location_is_the_sources_directory(tmp_path):
    assert FilesystemStorage(str(tmp_path)).location("pics") == str(tmp_path / "pics")


# -- FilesystemStorage: nested (collection) keys ----------------------------


def test_a_nested_key_becomes_a_subdirectory(tmp_path):
    # A collection's key carries a "/" that has to survive as a real path segment, Windows included.
    storage = FilesystemStorage(str(tmp_path))
    path = storage.write("pics/creator", "0001_creator.mp4", b"bytes")
    assert path == str(tmp_path / "pics" / "creator" / "0001_creator.mp4")
    assert storage.exists("pics/creator", "0001_creator.mp4") is True
    assert storage.location("pics/creator") == str(tmp_path / "pics" / "creator")


def test_a_nested_key_gets_its_own_manifest(tmp_path):
    storage = FilesystemStorage(str(tmp_path))
    storage.write_manifest("pics/creator", {"source": "pics", "collection": "creator"})
    assert (tmp_path / "pics" / "creator" / MANIFEST_NAME).is_file()
    assert storage.read_manifest("pics/creator")["collection"] == "creator"


def test_a_sources_listing_reports_files_not_its_collection_folders(tmp_path):
    # A folder named for a digit-leading uploader would otherwise be read as a stored file's index.
    storage = FilesystemStorage(str(tmp_path))
    storage.write("pics", "0001_a.jpg", b"a")
    storage.write("pics/9lives", "0001_9lives.mp4", b"b")
    assert storage.list_files("pics") == ["0001_a.jpg"]


# -- FilesystemStorage: manifests -------------------------------------------


def test_a_manifest_round_trips_through_disk(tmp_path):
    storage = FilesystemStorage(str(tmp_path))
    data = manifest_data(**{"0001.jpg": entry("https://i.redd.it/a.jpg")})
    path = storage.write_manifest("pics", data)
    assert path == str(tmp_path / "pics" / MANIFEST_NAME)
    assert storage.read_manifest("pics") == data


def test_a_manifest_is_human_readable_utf8(tmp_path):
    # It sits next to the downloads; a person may well open it.
    storage = FilesystemStorage(str(tmp_path))
    storage.write_manifest(
        "pics", manifest_data(**{"0001.jpg": entry("u", title="ünïcode")})
    )
    text = (tmp_path / "pics" / MANIFEST_NAME).read_text(encoding="utf-8")
    assert "ünïcode" in text  # not escaped to ü
    assert "\n  " in text  # indented


def test_an_absent_manifest_reads_as_none(tmp_path):
    assert FilesystemStorage(str(tmp_path)).read_manifest("pics") is None


def test_a_corrupt_manifest_reads_as_none_rather_than_raising(tmp_path):
    # A truncated manifest should cost a run its numbering, not crash it.
    (tmp_path / "pics").mkdir()
    (tmp_path / "pics" / MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    assert FilesystemStorage(str(tmp_path)).read_manifest("pics") is None


def test_a_manifest_that_is_not_an_object_reads_as_none(tmp_path):
    (tmp_path / "pics").mkdir()
    (tmp_path / "pics" / MANIFEST_NAME).write_text("[1, 2, 3]", encoding="utf-8")
    assert FilesystemStorage(str(tmp_path)).read_manifest("pics") is None


def test_writing_a_manifest_creates_its_directory(tmp_path):
    storage = FilesystemStorage(str(tmp_path))
    storage.write_manifest("pics", manifest_data())
    assert json.loads((tmp_path / "pics" / MANIFEST_NAME).read_text(encoding="utf-8"))


# -- Manifest: loading ------------------------------------------------------


def test_a_fresh_manifest_is_empty():
    manifest = Manifest("pics")
    assert len(manifest) == 0
    assert manifest.known_urls() == set()
    assert manifest.known_hashes() == set()
    assert list(manifest.entries()) == []


def test_an_existing_manifest_is_loaded():
    data = manifest_data(**{"0001.jpg": entry("https://i.redd.it/a.jpg")})
    manifest = Manifest("pics", data)
    assert len(manifest) == 1
    assert "0001.jpg" in manifest
    assert "0002.jpg" not in manifest
    assert list(manifest.entries()) == [
        ("0001.jpg", {"post_id": "p1", "media_url": "https://i.redd.it/a.jpg"})
    ]


def test_a_manifest_with_no_files_block_loads_empty():
    assert len(Manifest("pics", {"source": "pics"})) == 0


def test_a_manifest_whose_files_block_is_not_an_object_loads_empty():
    assert len(Manifest("pics", {"files": ["0001.jpg"]})) == 0


def test_records_that_are_not_objects_are_dropped():
    # Hand-edited or half-written manifests must not break the run.
    manifest = Manifest("pics", {"files": {"0001.jpg": "junk", "0002.jpg": entry("u")}})
    assert list(dict(manifest.entries())) == ["0002.jpg"]


def test_a_non_mapping_manifest_is_ignored():
    assert len(Manifest("pics", None)) == 0


# -- Manifest: known urls and hashes ----------------------------------------


def test_known_urls_reports_every_recorded_media_url():
    data = manifest_data(
        **{
            "0001.jpg": entry("https://i.redd.it/a.jpg"),
            "0002.jpg": entry("https://i.redd.it/b.jpg"),
        }
    )
    assert Manifest("pics", data).known_urls() == {
        "https://i.redd.it/a.jpg",
        "https://i.redd.it/b.jpg",
    }


def test_a_record_with_no_media_url_contributes_no_known_url():
    assert (
        Manifest("pics", {"files": {"0001.jpg": {"post_id": "p1"}}}).known_urls()
        == set()
    )


def test_known_hashes_reports_only_records_that_have_one():
    # sha256 is only written when dedupe_by_hash ran, so most manifests have none.
    data = manifest_data(
        **{"0001.jpg": entry("u1", sha256="abc"), "0002.jpg": entry("u2")}
    )
    assert Manifest("pics", data).known_hashes() == {"abc"}


# -- Manifest: numbering ----------------------------------------------------


def test_numbering_starts_at_one_for_a_fresh_folder():
    assert Manifest("pics").allocate_index() == 1


def test_each_allocation_advances():
    manifest = Manifest("pics")
    assert [manifest.allocate_index() for _ in range(3)] == [1, 2, 3]


def test_numbering_continues_past_the_manifests_own_records():
    data = manifest_data(**{"0001.jpg": entry("u1"), "0007.jpg": entry("u2")})
    assert Manifest("pics", data).allocate_index() == 8


def test_numbering_continues_past_files_that_the_manifest_forgot():
    # A lost manifest or an interrupted run leaves orphans; overwriting them would lose data.
    manifest = Manifest("pics", None, existing_files=["0004.jpg", "0009.jpg"])
    assert manifest.allocate_index() == 10


def test_numbering_clears_the_higher_of_the_two_sources():
    data = manifest_data(**{"0012.jpg": entry("u1")})
    assert Manifest("pics", data, existing_files=["0003.jpg"]).allocate_index() == 13


def test_multi_part_names_are_read_by_their_leading_index():
    assert Manifest("pics", None, existing_files=["0005_03.jpg"]).allocate_index() == 6


def test_files_with_no_leading_number_are_ignored_by_numbering():
    manifest = Manifest("pics", None, existing_files=["manifest.json", "notes.txt"])
    assert manifest.allocate_index() == 1


# -- Manifest: writing ------------------------------------------------------


def test_add_records_an_entry():
    manifest = Manifest("pics")
    manifest.add("0001.jpg", entry("https://i.redd.it/a.jpg"))
    assert "0001.jpg" in manifest
    assert manifest.known_urls() == {"https://i.redd.it/a.jpg"}


def test_add_copies_the_entry_it_is_given():
    manifest = Manifest("pics")
    record = entry("https://i.redd.it/a.jpg")
    manifest.add("0001.jpg", record)
    record["media_url"] = "https://i.redd.it/changed.jpg"
    assert manifest.known_urls() == {"https://i.redd.it/a.jpg"}


def test_adding_the_same_name_twice_overwrites():
    manifest = Manifest("pics")
    manifest.add("0001.jpg", entry("https://i.redd.it/a.jpg"))
    manifest.add("0001.jpg", entry("https://i.redd.it/b.jpg"))
    assert len(manifest) == 1
    assert manifest.known_urls() == {"https://i.redd.it/b.jpg"}


def test_to_dict_carries_the_source_a_timestamp_and_the_files():
    manifest = Manifest("pics")
    manifest.add("0001.jpg", entry("https://i.redd.it/a.jpg"))
    data = manifest.to_dict()
    assert data["source"] == "pics"
    assert data["files"] == {
        "0001.jpg": {"post_id": "p1", "media_url": "https://i.redd.it/a.jpg"}
    }
    assert data["generated_at"].endswith("+00:00")  # timezone-aware UTC


def test_to_dict_is_json_serializable():
    manifest = Manifest("pics")
    manifest.add("0001.jpg", entry("https://i.redd.it/a.jpg"))
    assert json.loads(json.dumps(manifest.to_dict()))["source"] == "pics"


def test_to_dict_snapshots_the_entries():
    manifest = Manifest("pics")
    manifest.add("0001.jpg", entry("https://i.redd.it/a.jpg"))
    data = manifest.to_dict()
    manifest.add("0002.jpg", entry("https://i.redd.it/b.jpg"))
    assert list(data["files"]) == ["0001.jpg"]


def test_a_manifest_survives_a_round_trip_through_storage(tmp_path):
    # The whole point: a second run continues the first one's numbering and skips its URLs.
    storage = FilesystemStorage(str(tmp_path))
    first = Manifest("pics", storage.read_manifest("pics"), storage.list_files("pics"))
    first.add("0001.jpg", entry("https://i.redd.it/a.jpg"))
    storage.write_manifest("pics", first.to_dict())

    second = Manifest("pics", storage.read_manifest("pics"), storage.list_files("pics"))
    assert second.known_urls() == {"https://i.redd.it/a.jpg"}
    assert second.allocate_index() == 2


# -- Manifest: membership lookups -------------------------------------------


def test_a_recorded_url_is_known():
    manifest = Manifest("pics", manifest_data(**{"0001.jpg": entry("u1")}))
    assert manifest.knows_url("u1") is True
    assert manifest.knows_url("u2") is False


def test_a_url_becomes_known_as_soon_as_it_is_added():
    # The next candidate of the same post must see it, before any manifest is written.
    manifest = Manifest("pics")
    manifest.add("0001.jpg", entry("u1"))
    assert manifest.knows_url("u1") is True


def test_a_recorded_hash_is_known():
    manifest = Manifest("pics")
    manifest.add("0001.jpg", entry("u1", sha256="abc"))
    assert manifest.knows_hash("abc") is True
    assert manifest.knows_hash("def") is False


def test_overwriting_a_record_forgets_what_it_replaced():
    manifest = Manifest("pics")
    manifest.add("0001.jpg", entry("u1", sha256="abc"))
    manifest.add("0001.jpg", entry("u2", sha256="def"))
    assert (manifest.knows_url("u1"), manifest.knows_hash("abc")) == (False, False)
    assert (manifest.knows_url("u2"), manifest.knows_hash("def")) == (True, True)


def test_the_known_url_set_is_a_copy():
    manifest = Manifest("pics", manifest_data(**{"0001.jpg": entry("u1")}))
    manifest.known_urls().clear()
    assert manifest.knows_url("u1") is True


# -- Manifest: collections --------------------------------------------------


def test_a_fresh_manifest_holds_no_collections():
    assert list(Manifest("pics").collections()) == []


def test_a_collection_manifest_knows_where_it_lives():
    manifest = Manifest("pics", collection="creator")
    assert manifest.storage_key == "pics/creator"
    assert Manifest("pics").storage_key == "pics"


def test_a_collection_manifest_names_both_its_source_and_itself():
    data = Manifest("pics", collection="creator").to_dict()
    assert (data["source"], data["collection"]) == ("pics", "creator")


def test_a_manifest_of_the_source_itself_carries_no_collection_field():
    assert "collection" not in Manifest("pics").to_dict()


def test_a_collection_record_is_kept_at_post_level():
    manifest = Manifest("pics")
    manifest.add_collection("creator", {"post_id": "p1", "files": 214})
    assert dict(manifest.collections()) == {"creator": {"post_id": "p1", "files": 214}}
    assert len(manifest) == 0  # the collection's own files are not this manifest's


def test_adding_a_collection_again_merges_rather_than_replaces():
    # The post's provenance is written once; the file count is restated on every flush.
    manifest = Manifest("pics")
    manifest.add_collection("creator", {"post_id": "p1", "files": 3})
    manifest.add_collection("creator", {"files": 9})
    assert dict(manifest.collections())["creator"] == {"post_id": "p1", "files": 9}


def test_collections_are_left_out_when_there_are_none():
    # Manifests of sources that never met a collection stay byte-for-byte what they always were.
    assert "collections" not in Manifest("pics").to_dict()


def test_collections_round_trip_through_a_manifest_document():
    manifest = Manifest("pics")
    manifest.add_collection("creator", {"post_id": "p1"})
    reloaded = Manifest("pics", manifest.to_dict())
    assert dict(reloaded.collections()) == {"creator": {"post_id": "p1"}}


def test_a_collections_block_that_is_not_an_object_loads_empty():
    assert list(Manifest("pics", {"collections": ["creator"]}).collections()) == []


def test_collection_records_that_are_not_objects_are_dropped():
    data = {"collections": {"a": "junk", "b": {"post_id": "p1"}}}
    assert list(dict(Manifest("pics", data).collections())) == ["b"]


# -- ManifestSet ------------------------------------------------------------


def test_a_set_prepares_and_loads_the_sources_own_manifest():
    storage = MemoryStorage()
    storage.manifests["pics"] = manifest_data(**{"0001.jpg": entry("u1")})
    manifests = ManifestSet(storage, "pics")
    assert manifests.main.knows_url("u1") is True
    assert storage.files["pics"] == {}  # prepare() ran


def test_a_collection_manifest_is_created_on_first_use():
    storage = MemoryStorage()
    manifests = ManifestSet(storage, "pics")
    collection = manifests.collection("creator")
    assert collection.storage_key == "pics/creator"
    assert "pics/creator" in storage.files  # its folder was prepared


def test_the_same_collection_hands_back_the_same_manifest():
    # Two posts naming one uploader must share a numbering sequence, not restart it.
    manifests = ManifestSet(MemoryStorage(), "pics")
    assert manifests.collection("creator") is manifests.collection("creator")


def test_a_collection_continues_its_own_folders_numbering():
    storage = MemoryStorage()
    storage.manifests["pics/creator"] = {
        "source": "pics",
        "collection": "creator",
        "files": {"0007_creator.mp4": entry("u1")},
    }
    manifests = ManifestSet(storage, "pics")
    assert manifests.collection("creator").allocate_index() == 8
    assert manifests.main.allocate_index() == 1  # the source numbers separately


def test_flushing_writes_nothing_until_something_changed():
    storage = CountingMemoryStorage()
    ManifestSet(storage, "pics").flush()
    assert storage.manifest_writes == 0


def test_a_forced_flush_writes_the_sources_manifest_regardless():
    storage = CountingMemoryStorage()
    manifests = ManifestSet(storage, "pics")
    assert manifests.flush(force=True) == "memory://pics/manifest.json"
    assert storage.manifest_writes == 1


def test_a_touched_manifest_is_written_once_per_flush():
    storage = CountingMemoryStorage()
    manifests = ManifestSet(storage, "pics")
    manifests.main.add("0001.jpg", entry("u1"))
    manifests.touch(manifests.main)
    manifests.flush()
    manifests.flush()  # nothing new since
    assert storage.manifest_writes == 1


def test_touching_a_collection_writes_it_and_the_source_together():
    # The source's manifest carries the collection's file count, which just moved.
    storage = CountingMemoryStorage()
    manifests = ManifestSet(storage, "pics")
    collection = manifests.collection("creator")
    collection.add("0001_creator.mp4", entry("u1"))
    manifests.touch(collection)
    manifests.flush()
    assert storage.manifest_writes == 2
    assert list(storage.manifests["pics/creator"]["files"]) == ["0001_creator.mp4"]


def test_a_flush_restates_each_collections_file_count():
    storage = MemoryStorage()
    manifests = ManifestSet(storage, "pics")
    collection = manifests.collection("creator")
    manifests.main.add_collection("creator", {"post_id": "p1"})
    for i in (1, 2, 3):
        collection.add("000{}_creator.mp4".format(i), entry("u{}".format(i)))
    manifests.touch(collection)
    manifests.flush()
    assert storage.manifests["pics"]["collections"]["creator"] == {
        "post_id": "p1",
        "files": 3,
    }


def test_a_set_that_does_not_persist_writes_nothing():
    # A dry run still reads what is stored, so its plan is realistic, but leaves nothing behind.
    storage = CountingMemoryStorage()
    storage.manifests["pics"] = manifest_data(**{"0001.jpg": entry("u1")})
    manifests = ManifestSet(storage, "pics", persist=False)
    manifests.collection("creator")
    manifests.touch(manifests.main)
    assert manifests.flush(force=True) is None
    assert manifests.main.knows_url("u1") is True  # ...but it did read
    assert storage.manifest_writes == 0
    assert storage.files == {}  # no folder was prepared either


def keyed(url: str, key: str) -> dict:
    return {"post_id": "p1", "media_url": url, "media_key": key}


def test_a_records_key_is_an_identity_it_answers_to():
    manifest = Manifest(
        "pics", {"files": {"0001.jpg": keyed("https://x/1?sig=a", "k1")}}
    )
    assert manifest.knows_key("k1") is True


def test_a_keyed_record_still_answers_to_its_url():
    manifest = Manifest(
        "pics", {"files": {"0001.jpg": keyed("https://x/1?sig=a", "k1")}}
    )
    assert manifest.knows_key("https://x/1?sig=a") is True


def test_a_fresh_signature_on_a_keyed_file_is_not_a_new_file():
    manifest = Manifest(
        "pics", {"files": {"0001.jpg": keyed("https://x/1?sig=a", "k1")}}
    )
    assert manifest.knows_key("https://x/1?sig=b") is False
    assert manifest.knows_key("k1") is True


def test_records_written_before_keys_existed_still_match_on_their_url():
    old = {
        "files": {"0001.jpg": {"post_id": "p1", "media_url": "https://i.redd.it/a.jpg"}}
    }
    assert Manifest("pics", old).knows_key("https://i.redd.it/a.jpg") is True


def test_knows_url_is_an_alias_of_knows_key():
    manifest = Manifest("pics", {"files": {"0001.jpg": keyed("https://x/1", "k1")}})
    assert manifest.knows_url("k1") is True


def test_adding_a_keyed_record_registers_both_identities():
    manifest = Manifest("pics")
    manifest.add("0001.jpg", keyed("https://x/1?sig=a", "k1"))
    assert manifest.known_urls() == {"https://x/1?sig=a", "k1"}


def test_overwriting_a_keyed_record_forgets_the_identities_it_carried():
    manifest = Manifest("pics")
    manifest.add("0001.jpg", keyed("https://x/1?sig=a", "k1"))
    manifest.add("0001.jpg", keyed("https://x/2?sig=b", "k2"))
    assert manifest.knows_key("k1") is False
    assert manifest.knows_key("k2") is True

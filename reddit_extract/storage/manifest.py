"""
Manifest: maps everything a source stored back to the Reddit post it came from.

A manifest is written as ``manifest.json`` alongside a source's downloaded files. Its on-disk shape is::

    {
      "source": "EarthPorn",
      "generated_at": "2026-07-15T12:00:00+00:00",
      "files": {
        "0001_Sunset_Over_Lofoten.jpg": {"post_id": "...", "media_url": "...", ...},
        ...
      },
      "collections": {
        "someuploader": {"name": "someUploader", "files": 214, "post_id": "...", ...}
      }
    }

``files`` is one record per file stored beside the manifest. ``collections`` is one record per *post* whose media
went into a subfolder of its own (a handler that answers a single post with a whole profile's worth of files (see
`ManifestSet`). Those files are indexed by the subfolder's own manifest, this one stays post-level).

Keeping the manifest lets a later run continue a folder's numbering and skip media that was already downloaded.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping

from .naming import collection_key
from .storage import StorageBackend


class Manifest:
    """
    In-memory manifest that merges with whatever was already on disk.

    Args:
        source_key: The source's storage key (its output subdirectory name).
        data: A previously loaded manifest dict, or None for a fresh one.
        existing_files: File names already present in storage. Numbering starts past the highest one, so files orphaned
            by a lost manifest or an interrupted run are never overwritten.
        collection: The collection this manifest indexes, or None when it indexes the source's own folder. A
            collection's manifest lives one level down, in ``<source>/<collection>/`` (see `storage_key`).
    """

    def __init__(
        self,
        source_key: str,
        data: Mapping[str, Any] | None = None,
        existing_files: Iterable[str] = (),
        *,
        collection: str | None = None,
    ) -> None:
        self.source_key = source_key
        self.collection = collection
        self._entries = self._records(data, "files")
        self._collections = self._records(data, "collections")
        self._urls: set[str] = set()
        self._hashes: set[str] = set()
        self._reindex()
        self._next_index = (
            max(self._max_index(self._entries), self._max_index(existing_files)) + 1
        )

    @staticmethod
    def _records(
        data: Mapping[str, Any] | None, section: str
    ) -> dict[str, dict[str, Any]]:
        """Read one object-of-objects section out of a loaded manifest, dropping anything malformed."""
        if not isinstance(data, Mapping):
            return {}
        raw = data.get(section)
        if not isinstance(raw, Mapping):
            return {}
        return {
            str(name): dict(entry)
            for name, entry in raw.items()
            if isinstance(entry, Mapping)
        }

    @property
    def storage_key(self) -> str:
        """The storage key of the folder this manifest indexes (the source's, or a collection's inside it)."""
        if self.collection is None:
            return self.source_key
        return collection_key(self.source_key, self.collection)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, filename: str) -> bool:
        return filename in self._entries

    def entries(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """Iterate over ``(filename, record)`` pairs already in the manifest."""
        return iter(self._entries.items())

    def collections(self) -> Iterator[tuple[str, dict[str, Any]]]:
        """Iterate over ``(collection, record)`` pairs already in the manifest."""
        return iter(self._collections.items())

    def known_urls(self) -> set[str]:
        """Return every media URL already recorded in the manifest."""
        return set(self._urls)

    def known_hashes(self) -> set[str]:
        """Return every content hash (``sha256``) recorded in the manifest, if any."""
        return set(self._hashes)

    def knows_url(self, url: str) -> bool:
        """Whether ``url`` was already downloaded into this folder."""
        return url in self._urls

    def knows_hash(self, digest: str) -> bool:
        """Whether a file with this content hash was already saved into this folder."""
        return digest in self._hashes

    def _reindex(self) -> None:
        """Rebuild the URL and hash lookups from the records."""
        self._urls = {
            entry["media_url"]
            for entry in self._entries.values()
            if entry.get("media_url")
        }
        self._hashes = {
            entry["sha256"] for entry in self._entries.values() if entry.get("sha256")
        }

    @staticmethod
    def _max_index(filenames: Iterable[str]) -> int:
        """Return the largest leading integer across ``filenames`` (0 if none)."""
        mx = 0
        for fname in filenames:
            m = re.match(r"(\d+)", fname)
            if m:
                mx = max(mx, int(m.group(1)))
        return mx

    def allocate_index(self) -> int:
        """
        Reserve and return the next index.

        Continues an existing folder's numbering rather than restarting at 1.
        """
        index = self._next_index
        self._next_index += 1
        return index

    def add(self, filename: str, entry: Mapping[str, Any]) -> None:
        """Record ``entry`` under ``filename`` (overwriting any prior record)."""
        overwriting = filename in self._entries
        record = dict(entry)
        self._entries[filename] = record
        if overwriting:
            # The replaced record may have been the only one carrying its URL or hash.
            self._reindex()
        else:
            if record.get("media_url"):
                self._urls.add(record["media_url"])
            if record.get("sha256"):
                self._hashes.add(record["sha256"])

    def add_collection(self, name: str, entry: Mapping[str, Any]) -> None:
        """
        Record what is known about collection ``name``, merging over any record already held.

        Args:
            name: The collection's slug, which is also its subfolder's name.
            entry: Fields to store or restate (the originating post's provenance when the collection is first seen, later just its file count).
        """
        self._collections.setdefault(name, {}).update(entry)

    def to_dict(self) -> dict[str, Any]:
        """Return the full manifest as a JSON-serializable dict."""
        data: dict[str, Any] = {"source": self.source_key}
        if self.collection is not None:
            data["collection"] = self.collection
        data["generated_at"] = datetime.now(timezone.utc).isoformat()
        data["files"] = dict(self._entries)
        if self._collections:
            data["collections"] = {
                name: dict(record) for name, record in self._collections.items()
            }
        return data


class ManifestSet:
    """
    Every manifest one job writes: the source's own, plus one per collection it stored.

    A *collection* is media a handler resolved as a unit (e.g. the uploader's entire profile). Its files go into a
    ``<source>/<collection>/`` folder with its own manifest and de-duplication. The source's manifest stays post-level
    recording one entry naming the collection instead of one entry per file inside it.

    Handlers opt in per candidate, by setting ``MediaCandidate.collection``; nothing here is RedGIFs-specific.

    Args:
        storage: The backend to read from and write to.
        source_key: The source's storage key.
        persist: Whether folders may be created and manifests written. A dry run passes False.
    """

    def __init__(
        self, storage: StorageBackend, source_key: str, *, persist: bool = True
    ) -> None:
        self._storage = storage
        self._persist = persist
        self.source_key = source_key
        if persist:
            storage.prepare(source_key)
        self.main = self._load(source_key)
        self._by_key: dict[str, Manifest] = {source_key: self.main}
        self._collections: dict[str, Manifest] = {}
        self._dirty: set[str] = set()

    def _load(self, key: str, collection: str | None = None) -> Manifest:
        """Build a manifest from what ``key`` already holds in storage."""
        return Manifest(
            self.source_key,
            self._storage.read_manifest(key),
            self._storage.list_files(key),
            collection=collection,
        )

    def collection(self, name: str) -> Manifest:
        """
        The manifest for collection ``name``, loading it (and preparing its folder) on first use.

        Args:
            name: The collection's slug.

        Returns:
            The collection's own manifest. The same object is returned for the rest of the job, so a collection met
            twice keeps one numbering sequence.
        """
        manifest = self._collections.get(name)
        if manifest is None:
            key = collection_key(self.source_key, name)
            if self._persist:
                self._storage.prepare(key)
            manifest = self._load(key, collection=name)
            self._collections[name] = manifest
            self._by_key[key] = manifest
        return manifest

    def touch(self, manifest: Manifest) -> None:
        """
        Mark ``manifest`` as holding changes not yet written.

        A collection's change dirties the source's manifest: the file count it keeps for that collection has just moved.
        """
        self._dirty.add(manifest.storage_key)
        self._dirty.add(self.source_key)

    def flush(self, *, force: bool = False) -> str | None:
        """
        Write out the manifests that changed since the last flush.

        Args:
            force: Also write the source's manifest when nothing changed, so a job that saved nothing new still
                leaves a current manifest behind.

        Returns:
            The source manifest's path/URI when it was written this call, else None. A set that doesn't persist
            (a dry run) writes nothing and returns None.
        """
        if not self._persist:
            return None
        keys = set(self._dirty)
        self._dirty.clear()
        if force:
            keys.add(self.source_key)
        path: str | None = None
        for key in sorted(keys):
            manifest = self._by_key[key]
            if manifest is self.main:
                self._restate_counts()
            written = self._storage.write_manifest(key, manifest.to_dict())
            if manifest is self.main:
                path = written
        return path

    def _restate_counts(self) -> None:
        """Bring each collection record's file count up to date from the collection's own manifest."""
        for name, manifest in self._collections.items():
            self.main.add_collection(name, {"files": len(manifest)})

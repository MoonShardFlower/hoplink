"""Storage backend interface."""

from __future__ import annotations

import abc
import copy
import json
import os
from typing import Any, Iterable, Mapping


class StorageBackend(abc.ABC):
    """
    Where extracted files and manifests go.

    One backend serves many sources. Every method takes the source ``key`` (its output subdirectory / namespace).
    Implementations must be safe to call from multiple asyncio tasks as long as each task uses its own key.
    """

    @abc.abstractmethod
    def prepare(self, key: str) -> None:
        """Create whatever the namespace needs (directories, buckets, ...)."""

    @abc.abstractmethod
    def exists(self, key: str, filename: str) -> bool:
        """Whether ``filename`` is already stored for this source."""

    def list_files(self, key: str) -> Iterable[str]:
        """
        Names of the files already stored for this source (may be empty).

        Used to seed the manifest's numbering past files that exist in storage but are missing from the manifest
        (lost/corrupt manifest, interrupted run), so new downloads never collide with them.
        """
        return ()

    @abc.abstractmethod
    def write(self, key: str, filename: str, data: bytes) -> str | None:
        """Persist a file atomically; return its path/URI (or None)."""

    @abc.abstractmethod
    def read_manifest(self, key: str) -> Mapping[str, Any] | None:
        """Load the stored manifest dict, or None if absent/corrupt."""

    @abc.abstractmethod
    def write_manifest(self, key: str, data: Mapping[str, Any]) -> str | None:
        """Persist the manifest; return its path/URI (or None)."""

    def location(self, key: str) -> str | None:
        """Human-readable location of this source's output (for summaries)."""
        return None


class MemoryStorage(StorageBackend):
    """
    In-memory storage backend for tests, dry experiments, and pipelines.

    Nothing is written to disk. Files and manifests live in dicts keyed by a source key.
    """

    def __init__(self) -> None:
        self.files: dict[str, dict[str, bytes]] = {}
        self.manifests: dict[str, dict] = {}

    def prepare(self, key: str) -> None:
        self.files.setdefault(key, {})

    def exists(self, key: str, filename: str) -> bool:
        return filename in self.files.get(key, {})

    def list_files(self, key: str) -> list[str]:
        return list(self.files.get(key, {}))

    def write(self, key: str, filename: str, data: bytes) -> str:
        self.files.setdefault(key, {})[filename] = data
        return "memory://{}/{}".format(key, filename)

    def read_manifest(self, key: str) -> Mapping[str, Any] | None:
        manifest = self.manifests.get(key)
        return copy.deepcopy(manifest) if manifest is not None else None

    def write_manifest(self, key: str, data: Mapping[str, Any]) -> str:
        self.manifests[key] = copy.deepcopy(dict(data))
        return "memory://{}/manifest.json".format(key)

    def location(self, key: str) -> str:
        return "memory://{}".format(key)


MANIFEST_NAME = "manifest.json"


class FilesystemStorage(StorageBackend):
    """Writes ``<root>/<key>/<filename>`` atomically (tmp file + rename)."""

    def __init__(self, root: str) -> None:
        """
        Store files under ``root``.

        Args:
            root: Base directory. Each source gets a ``<root>/<key>/`` subdirectory with its own ``manifest.json``. ``~`` is expanded.
        """
        self.root = os.path.expanduser(root)

    def _dir(self, key: str) -> str:
        return os.path.join(self.root, key)

    def _path(self, key: str, filename: str) -> str:
        return os.path.join(self._dir(key), filename)

    def prepare(self, key: str) -> None:
        os.makedirs(self._dir(key), exist_ok=True)

    def exists(self, key: str, filename: str) -> bool:
        return os.path.exists(self._path(key, filename))

    def list_files(self, key: str) -> list[str]:
        try:
            return os.listdir(self._dir(key))
        except OSError:  # directory doesn't exist yet
            return []

    def write(self, key: str, filename: str, data: bytes) -> str:
        dest = self._path(key, filename)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dest)
        return os.path.abspath(dest)

    def read_manifest(self, key: str) -> Mapping[str, Any] | None:
        path = self._path(key, MANIFEST_NAME)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (ValueError, OSError):
            return None

    def write_manifest(self, key: str, data: Mapping[str, Any]) -> str:
        payload = json.dumps(data, indent=2, ensure_ascii=False)
        dest = self._path(key, MANIFEST_NAME)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, dest)
        return os.path.abspath(dest)

    def location(self, key: str) -> str:
        return os.path.abspath(self._dir(key))

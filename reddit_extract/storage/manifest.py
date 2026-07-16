"""
Manifest: maps every saved file back to the Reddit post it came from.

A manifest is written as ``manifest.json`` alongside a source's downloaded files. Its on-disk shape is::

    {
      "source": "EarthPorn",
      "generated_at": "2026-07-15T12:00:00+00:00",
      "files": {
        "0001.jpg": {"post_id": "...", "media_url": "...", ...},
        ...
      }
    }

Keeping the manifest lets a later run continue a folder's numbering and skip media that was already downloaded.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping


class Manifest:
    """
    In-memory manifest that merges with whatever was already on disk.

    Args:
        source_key: The source's storage key (its output subdirectory name).
        data: A previously loaded manifest dict, or None for a fresh one.
        existing_files: File names already present in storage. Numbering starts past the highest one, so files orphaned
            by a lost manifest or an interrupted run are never overwritten.
    """

    def __init__(
        self,
        source_key: str,
        data: Mapping[str, Any] | None = None,
        existing_files: Iterable[str] = (),
    ) -> None:
        self.source_key = source_key
        entries: dict[str, dict] = {}
        if isinstance(data, Mapping):
            raw = data.get("files")
            if isinstance(raw, Mapping):
                entries = {
                    str(name): dict(entry)
                    for name, entry in raw.items()
                    if isinstance(entry, Mapping)
                }
        self._entries = entries
        self._next_index = (
            max(self._max_index(self._entries), self._max_index(existing_files)) + 1
        )

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, filename: str) -> bool:
        return filename in self._entries

    def entries(self) -> Iterator[tuple[str, dict]]:
        """Iterate over ``(filename, record)`` pairs already in the manifest."""
        return iter(self._entries.items())

    def known_urls(self) -> set[str]:
        """Return every media URL already recorded in the manifest."""
        return {
            entry["media_url"]
            for entry in self._entries.values()
            if entry.get("media_url")
        }

    def known_hashes(self) -> set[str]:
        """Return every content hash (``sha256``) recorded in the manifest, if any."""
        return {
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
        Reserve and return the next post index.

        Continues an existing folder's numbering rather than restarting at 1.
        """
        index = self._next_index
        self._next_index += 1
        return index

    def add(self, filename: str, entry: Mapping[str, Any]) -> None:
        """Record ``entry`` under ``filename`` (overwriting any prior record)."""
        self._entries[filename] = dict(entry)

    def to_dict(self) -> dict:
        """Return the full manifest as a JSON-serializable dict."""
        return {
            "source": self.source_key,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "files": dict(self._entries),
        }

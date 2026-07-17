"""Media types and the objects that flow through the extraction pipeline."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Iterable, Union

MediaTypeLike = Union["MediaType", str, Iterable[Union["MediaType", str]]]


class MediaType(enum.Flag):
    """
    The kind of media a post carries.

    Flags are combinable with ``|``, e.g. ``MediaType.IMAGE | MediaType.GALLERY``.

    Attributes:
        IMAGE: Single-image posts (i.redd.it).
        GALLERY: Multi-image gallery posts.
        VIDEO: v.redd.it videos and silent "gif" posts.
        TEXT: Self posts; recorded as metadata, never downloaded.
        LINK: External link posts pointing at a direct image.
        ALL: Every media type combined.
    """

    IMAGE = enum.auto()
    GALLERY = enum.auto()
    VIDEO = enum.auto()
    TEXT = enum.auto()
    LINK = enum.auto()
    ALL = IMAGE | GALLERY | VIDEO | TEXT | LINK

    @classmethod
    def coerce(cls, value: MediaTypeLike) -> "MediaType":
        """
        Coerce a flexible value into a single MediaType flag.

        Args:
            value: A MediaType; a case-insensitive type name such as ``"image"`` or ``"all"``; a comma-separated string
                such as ``"image,gallery"``; or an iterable mixing any of these. Strings are split on commas, and every
                part is OR-ed together into one combined flag.

        Returns:
            The resolved MediaType, possibly a combination of flags.

        Raises:
            ValueError: If a part does not name a known media type, or a string names none.
        """
        if isinstance(value, MediaType):
            return value
        if isinstance(value, str):
            names = [name.strip() for name in value.split(",") if name.strip()]
            if not names:
                raise ValueError("no media type given in {!r}".format(value))
            combined = cls(0)
            for name in names:
                combined |= cls._by_name(name)
            return combined
        combined = cls(0)
        for item in value:
            combined |= cls.coerce(item)
        return combined

    @classmethod
    def _by_name(cls, name: str) -> "MediaType":
        """
        Resolve a single case-insensitive type name to its flag.

        Args:
            name: One media type name, e.g. ``"image"`` or ``"all"``.

        Returns:
            The matching MediaType.

        Raises:
            ValueError: If ``name`` is not a known media type.
        """
        try:
            return cls[name.strip().upper()]
        except KeyError:
            # dict.fromkeys dedupes; every single-bit member has a name (mypy: str | None)
            allowed = dict.fromkeys([m.name.lower() for m in cls if m.name] + ["all"])
            raise ValueError(
                "unknown media type {!r} (allowed: {})".format(name, ", ".join(allowed))
            ) from None

    @property
    def label(self) -> str:
        """
        Lower-case name for manifests and logs, e.g. ``"image"``.

        A combined flag renders as its composite name, e.g. ``"image|video"``. Only a flag with no
        name at all -- the empty ``MediaType(0)`` -- falls back to ``"mixed"``. In practice neither
        arises: a labelled media type always comes from a handler's ``media_type``, which is a
        single flag.
        """
        return (self.name or "mixed").lower()


@dataclass
class MediaCandidate:
    """
    A downloadable media URL resolved from a post by a handler.

    Attributes:
        url: The direct media URL to download.
        media_type: Which MediaType this candidate belongs to.
        ext: File extension to save under, or None to infer it from the URL.
        content_prefixes: Acceptable Content-Type prefixes; a download whose response type matches none of these is rejected.
    """

    url: str
    media_type: MediaType
    ext: str | None = None
    content_prefixes: tuple[str, ...] = ("image/",)


@dataclass
class MediaItem:
    """One saved (or, in dry runs, planned) media file plus its provenance.

    Attributes:
        url: The media URL the file came from.
        media_type: Which MediaType the file is.
        filename: The name it is (or would be) stored under.
        path: Absolute path/URI once written, else None.
        downloaded: Whether the bytes were actually written this run.
        size: File size in bytes once downloaded, else None.
        sha256: Hex SHA-256 of the file's bytes, set when content de-duplication is enabled, else None.
        post_id: ID of the originating post.
        post_url: Permalink of the originating post.
        title: Title of the originating post.
        author: Author of the originating post.
        created: ISO-8601 creation time of the post, if known.
        source_key: Storage key of the source this file belongs to.
    """

    url: str
    media_type: MediaType
    filename: str | None = None
    path: str | None = None
    downloaded: bool = False
    size: int | None = None
    sha256: str | None = None
    post_id: str | None = None
    post_url: str | None = None
    title: str | None = None
    author: str | None = None
    created: str | None = None
    source_key: str | None = None

    def to_manifest_entry(self) -> dict[str, Any]:
        """
        Serialize this item to a manifest record.

        Returns:
            A JSON-serializable dict with the post's provenance (id, url, title, author, creation time),
            the downloaded ``media_url``, and the ``media_type`` label.
        """
        entry = {
            "post_id": self.post_id,
            "post_url": self.post_url,
            "title": self.title,
            "author": self.author,
            "created": self.created,
            "media_url": self.url,
            "media_type": self.media_type.label,
        }
        # Only present when content de-duplication ran, so manifests are otherwise unchanged.
        if self.sha256:
            entry["sha256"] = self.sha256
        return entry

    def to_report_dict(self) -> dict[str, Any]:
        """
        Serialize this item for a run report.

        Returns:
            A JSON-serializable dict describing the stored (or, in a dry run, planned) file:
            its name, path, size, content hash, and the originating post's provenance.
        """
        return {
            "filename": self.filename,
            "path": self.path,
            "url": self.url,
            "media_type": self.media_type.label,
            "downloaded": self.downloaded,
            "size": self.size,
            "sha256": self.sha256,
            "post_id": self.post_id,
            "post_url": self.post_url,
            "title": self.title,
            "author": self.author,
            "created": self.created,
        }

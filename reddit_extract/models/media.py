"""Media types and the objects that flow through the extraction pipeline."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Iterable, Union

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
            value: A MediaType, a case-insensitive type name such as `"image"`` or ``"all"``, or an iterable mixing either.
                Iterables are OR-ed together into one combined flag.

        Returns:
            The resolved MediaType, possibly a combination of flags.

        Raises:
            ValueError: If a string does not name a known media type.
        """
        if isinstance(value, MediaType):
            return value
        if isinstance(value, str):
            name = value.strip().upper()
            try:
                return cls[name]
            except KeyError:
                # dict.fromkeys dedupes
                names = dict.fromkeys([m.name.lower() for m in cls] + ["all"])
                raise ValueError(
                    "unknown media type {!r} (allowed: {})".format(
                        value, ", ".join(names)
                    )
                ) from None
        combined = cls(0)
        for item in value:
            combined |= cls.coerce(item)
        return combined

    @property
    def label(self) -> str:
        """
        Lower-case name for manifests and logs, e.g. ``"image"``.

        Combined flags (which have no single name) become ``"mixed"``.
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
    post_id: str | None = None
    post_url: str | None = None
    title: str | None = None
    author: str | None = None
    created: str | None = None
    source_key: str | None = None

    def to_manifest_entry(self) -> dict:
        """
        Serialize this item to a manifest record.

        Returns:
            A JSON-serializable dict with the post's provenance (id, url, title, author, creation time),
            the downloaded ``media_url``, and the ``media_type`` label.
        """
        return {
            "post_id": self.post_id,
            "post_url": self.post_url,
            "title": self.title,
            "author": self.author,
            "created": self.created,
            "media_url": self.url,
            "media_type": self.media_type.label,
        }

"""
Naming scheme for downloaded files and their containing folders.

Files are named as `<index>_<slug>.<ext>` where:
- `index` is zero-padded to preserve arrival order.
- `slug` is derived from the post title (for post media) or collection name (for collection files).
- For posts with multiple files, an additional numeric part is inserted: `<index>_<part>_<slug>.<ext>`.
- If no usable slug exists, the name falls back to `<index>.<ext>`.

Slugs are sanitized for filesystem compatibility:
- Characters outside letters, digits, and `-` are replaced with `_`.
- Windows‑forbidden characters (`<>:"/\\|?*` and control characters) are removed.
- Reserved device names (CON, COM1, etc.) and trailing spaces/dots are removed.
"""

from __future__ import annotations

import mimetypes
import os
import re
import unicodedata
from urllib.parse import urlparse

#: how many characters of a title survive into a file name. Windows caps a whole path at 260 characters unless long
#: paths are enabled, and the slug is only part of one: the rest is the output directory, the source folder, an
#: optional collection folder (itself a slug), the index, the part number, and the extension. 120 leaves room for
#: all of that under a reasonably shallow output directory while keeping most titles intact.
MAX_SLUG_LENGTH = 120

#: how many characters of a collection's name survive into its folder name. A collection's name lands in the path
#: twice — once as the folder, once as the slug of every file inside it — so it gets a tighter cap than a post title.
MAX_COLLECTION_SLUG_LENGTH = 60

#: MS-DOS device names. Windows refuses these as a file or folder name.
_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM{}".format(n) for n in range(1, 10)]
    + ["LPT{}".format(n) for n in range(1, 10)]
)

_UNDERSCORE_RUN = re.compile(r"_+")


def slugify(text: str | None, *, max_length: int = MAX_SLUG_LENGTH) -> str:
    """
    Reduce free text to a filesystem-safe slug: ``"Hello, World!"`` becomes ``"Hello_World"``.

    Args:
        text: The text to slug (a post's title, an uploader's name), or None.
        max_length: How many characters to keep. An over-long slug is cut back to the last word boundary that fits.

    Returns:
        The slug, or ``""`` when ``text`` holds nothing usable (it was empty, or was punctuation and emoji only).
        Case is preserved; every run of unsafe characters collapses into a single underscore.
    """
    # NFKC folds compatibility forms (full-width letters, superscripts) onto their plain equivalents
    normalized = unicodedata.normalize("NFKC", text or "")
    kept = "".join(ch if ch.isalnum() or ch == "-" else "_" for ch in normalized)
    slug = _UNDERSCORE_RUN.sub("_", kept).strip("_-")
    if len(slug) > max_length:
        slug = slug[:max_length]
        head, sep, _tail = slug.rpartition("_")
        slug = (head if sep and head else slug).strip("_-")
    return "_" + slug if slug.upper() in _RESERVED else slug


def media_extension(url: str, ext: str | None = None, *, default: str = "jpg") -> str:
    """
    Decide the extension a downloaded file is saved under.

    Args:
        url: The media URL, whose path supplies the extension when the candidate names none.
        ext: The extension a handler asked for, if any. A leading dot and any casing are accepted.
        default: The extension to use when neither the handler nor the URL offers one (an extension-less CDN path).

    Returns:
        A lower-case extension with no leading dot, e.g. ``"jpg"``. A URL's query string is never mistaken for one.
    """
    resolved = (ext or "").strip().lstrip(".").lower()
    if not resolved:
        resolved = os.path.splitext(urlparse(url).path)[1].lower().lstrip(".")
    return resolved or default


# Content types whose usual extension `mimetypes` either doesn't know or spells unhelpfully (it answers ".jpe" for
# image/jpeg). Anything not listed falls through to `mimetypes`, and types that describe no format in particular
# (application/octet-stream) deliberately resolve to nothing.
_CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/avif": "avif",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "audio/mpeg": "mp3",
    "audio/mp4": "m4a",
    "audio/x-m4a": "m4a",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "application/pdf": "pdf",
    "application/zip": "zip",
}

#: Content types that name no particular format, so they can never correct an extension.
_UNINFORMATIVE_TYPES = frozenset(
    {"application/octet-stream", "binary/octet-stream", ""}
)


def extension_for_content_type(content_type: str | None) -> str | None:
    """
    The extension a response's Content-Type implies, or None when it implies nothing.

    Used to correct a guessed extension once the bytes arrive: some hosts (Imgur most notably) serve whatever
    format they like regardless of the extension the URL asked for, so the response is the only honest source.

    Args:
        content_type: A Content-Type header value, with or without parameters (``"image/png; charset=..."``).

    Returns:
        A lower-case extension with no leading dot, or None when the type is missing, generic
        (``application/octet-stream``), or names a format with no known extension.
    """
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype in _UNINFORMATIVE_TYPES:
        return None
    known = _CONTENT_TYPE_EXTENSIONS.get(ctype)
    if known:
        return known
    guessed = mimetypes.guess_extension(ctype)
    return guessed.lstrip(".").lower() if guessed else None


def media_filename(
    index: int, ext: str, *, slug: str = "", part: int | None = None
) -> str:
    """
    Build the name one media file is stored under.

    Args:
        index: The file's allocated index, zero-padded to four digits (and wider when it outgrows them).
        ext: The file extension, without a leading dot (see `media_extension`).
        slug: An already-slugged descriptor to append (see `slugify`). Empty leaves the index to stand alone.
        part: The 1-based slide number of a post that resolved to several files, or None for a single-file post.

    Returns:
        A name like ``"0007_Sunset_Over_Lofoten.jpg"``, ``"0007_02_Sunset_Over_Lofoten.jpg"``, or ``"0007.jpg"``.
    """
    stem = "{:04d}".format(index)
    if part is not None:
        stem += "_{:02d}".format(part)
    if slug:
        stem += "_" + slug
    return "{}.{}".format(stem, ext)


def collection_key(source_key: str, collection: str) -> str:
    """
    The storage key of a collection's folder, nested inside its source's.

    Args:
        source_key: The source's storage key, e.g. ``"EarthPorn"``.
        collection: The collection's slug, e.g. ``"someuploader"``.

    Returns:
        The nested key, e.g. ``"EarthPorn/someuploader"``. Backends split it on ``/`` into path segments.
    """
    return "{}/{}".format(source_key, collection)

"""Video posts (v.redd.it), including Reddit's silent "gif" videos.

Two-tier resolution strategy:

1. **Packaged media** (preferred): post cards embed a ``shreddit-player``
   element whose ``packaged-media-json`` attribute lists pre-muxed MP4
   renditions (video and audio in one file). We take the highest resolution.
2. **DASH fallback**: not every video has packaged renditions. The public
   ``DASHPlaylist.mpd`` on v.redd.it lists per-resolution MP4 streams. We take
   the tallest **video-only** stream. (DASH keeps audio in a separate file, so
   these fallback downloads have no sound.)
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING, Any, List, Optional, Tuple

from ..models.media import MediaCandidate, MediaType
from ..models.post import Post
from .base import MediaHandler

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import ExtractionContext

log = logging.getLogger(__name__)

VIDEO_CONTENT_PREFIXES = ("video/", "application/octet-stream")


def best_mp4(packaged_media_json: str | None) -> Tuple[str, int] | None:
    """Find the highest-resolution MP4 in a post's packaged-media JSON.

    Args:
        packaged_media_json: The raw ``packaged-media-json`` attribute value, or None.

    Returns:
        A ``(url, height)`` tuple for the tallest MP4 rendition, or None if the JSON is missing, invalid, or contains no MP4.
    """
    if not packaged_media_json:
        return None
    try:
        data = json.loads(packaged_media_json)
    except ValueError:
        return None

    found: List[Tuple[int, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            url = node.get("url")
            if isinstance(url, str) and ".mp4" in url.lower():
                dims = node.get("dimensions")
                height = 0
                if isinstance(dims, dict) and isinstance(dims.get("height"), int):
                    height = dims["height"]
                elif isinstance(node.get("height"), int):
                    height = node["height"]
                found.append((height, url))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    if not found:
        return None
    height, url = max(found, key=lambda pair: pair[0])
    return url, height


def best_dash_video(mpd_xml: str, base_url: str) -> Optional[str]:
    """
    Pick the highest-resolution video stream from a DASH manifest.

    Audio-only representations are ignored, so the returned stream is video-only (silent).

    Args:
        mpd_xml: The ``DASHPlaylist.mpd`` document as text.
        base_url: The v.redd.it base URL the relative stream name hangs off.

    Returns:
        The absolute URL of the tallest video stream, or None if the manifest is unparseable or has no video representation.
    """
    try:
        root = ET.fromstring(mpd_xml)
    except ET.ParseError:
        return None

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    best_name, best_height = None, -1
    for rep in root.iter():
        if local(rep.tag) != "Representation":
            continue
        mime = rep.get("mimeType") or ""
        height = rep.get("height")
        if not mime.startswith("video") and height is None:
            continue  # audio representations
        name = None
        for child in rep:
            if local(child.tag) == "BaseURL":
                name = (child.text or "").strip()
                break
        if not name:
            continue
        h = int(height) if height and height.isdigit() else 0
        if h > best_height:
            best_name, best_height = name, h
    if best_name is None:
        return None
    return base_url.rstrip("/") + "/" + best_name


def vreddit_base(post: Post) -> Optional[str]:
    """
    Derive the canonical ``https://v.redd.it/<id>`` base for a post.

    Args:
        post: The post to inspect (its content href and raw ``player_src``).

    Returns:
        The v.redd.it base URL, or None if the post carries no v.redd.it id.
    """
    candidates = (post.content_href, post.raw.get("player_src"))
    for value in candidates:
        if not value:
            continue
        m = re.search(r"https://v\.redd\.it/([A-Za-z0-9]+)", value)
        if m:
            return "https://v.redd.it/" + m.group(1)
    return None


class VideoHandler(MediaHandler):
    """``video``/``gif`` posts: packaged MP4 when available, else DASH fallback."""

    media_type = MediaType.VIDEO

    def can_handle(self, post: Post) -> bool:
        """Match ``video`` and ``gif`` (silent video) posts."""
        return post.type in ("video", "gif")

    async def resolve(
        self, post: Post, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """
        Resolve the best MP4: packaged rendition first, DASH fallback second.

        The DASH fallback is video-only (no audio). Posts that are neither packaged nor a resolvable v.redd.it video are skipped.
        """
        best = best_mp4(post.packaged_media_json)
        if best is not None:
            url, height = best
            log.debug("video %s: packaged %dp mp4", post.id, height)
            return [self._candidate(url)]

        base = vreddit_base(post)
        if base is None:
            await ctx.skip(
                post.url or post.id, "not a v.redd.it video (no packaged media)"
            )
            return []
        manifest = await ctx.fetch(base + "/DASHPlaylist.mpd")
        if not manifest.ok or manifest.body is None:
            await ctx.skip(
                base,
                "DASH manifest unavailable ({})".format(
                    manifest.error or manifest.status
                ),
            )
            return []
        video_url = best_dash_video(manifest.body.decode("utf-8", "replace"), base)
        if video_url is None:
            await ctx.skip(base, "no video rendition in DASH manifest")
            return []
        log.debug("video %s: DASH fallback (video-only) %s", post.id, video_url)
        return [self._candidate(video_url)]

    def _candidate(self, url: str) -> MediaCandidate:
        """Wrap an MP4 URL as a video MediaCandidate."""
        return MediaCandidate(
            url=url,
            media_type=self.media_type,
            ext="mp4",
            content_prefixes=VIDEO_CONTENT_PREFIXES,
        )

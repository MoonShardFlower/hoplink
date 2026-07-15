"""Single-image posts (full resolution straight from i.redd.it)."""

from __future__ import annotations

import logging
from typing import List

from ..models.media import MediaCandidate, MediaType
from ..models.post import Post
from .base import MediaHandler

log = logging.getLogger(__name__)


class ImageHandler(MediaHandler):
    """``image`` posts: the card's ``content-href`` already is the original file."""

    media_type = MediaType.IMAGE

    def can_handle(self, post: Post) -> bool:
        """Match ``image`` posts that carry a content href."""
        return post.type == "image" and bool(post.content_href)

    async def resolve(self, post: Post, ctx) -> List[MediaCandidate]:
        """Return the image URL as a candidate if its extension is allowed."""
        url = post.content_href or ""
        ext = ctx.extension_of(url)
        if ext not in ctx.formats:
            log.debug("image %s skipped: extension %r not in formats", post.id, ext)
            return []
        return [MediaCandidate(url=url, media_type=self.media_type, ext=ext)]

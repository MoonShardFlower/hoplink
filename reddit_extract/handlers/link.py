"""Link posts whose target is a direct image on an external host."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List

from ..models.media import MediaCandidate, MediaType
from ..models.post import Post
from .base import MediaHandler

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import ExtractionContext

log = logging.getLogger(__name__)


class LinkImageHandler(MediaHandler):
    """``link`` posts kept only when the target is itself an image file."""

    media_type = MediaType.LINK

    def can_handle(self, post: Post) -> bool:
        """Match ``link`` posts that carry a content href."""
        return post.type == "link" and bool(post.content_href)

    async def resolve(
        self, post: Post, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """Keep the link only when it points directly at an allowed image file."""
        url = post.content_href or ""
        ext = ctx.extension_of(url)
        if ext not in ctx.formats:
            log.debug("link %s skipped: not a direct image (%r)", post.id, ext)
            return []
        return [MediaCandidate(url=url, media_type=self.media_type, ext=ext)]

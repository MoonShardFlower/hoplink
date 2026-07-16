"""Self (text) posts: recorded as metadata, never downloaded."""

from __future__ import annotations

from typing import TYPE_CHECKING, List

from ..models.media import MediaCandidate, MediaType
from ..models.post import Post
from .base import MediaHandler

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import ExtractionContext


class TextHandler(MediaHandler):
    """``text`` (self) posts: matched for the record, produce no candidates."""

    media_type = MediaType.TEXT
    metadata_only = True

    def can_handle(self, post: Post) -> bool:
        """Match ``text`` (self) posts."""
        return post.type == "text"

    async def resolve(
        self, post: Post, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """Produce no candidates; text posts are recorded as metadata only."""
        return []

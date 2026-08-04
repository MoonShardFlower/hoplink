"""Poll posts: archived as a Markdown document of the post's metadata."""

from __future__ import annotations

from typing import TYPE_CHECKING, List

from ..models.media import MediaCandidate, MediaType
from ..models.post import Post
from .base import MediaHandler
from .markdown import post_to_markdown

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import ExtractionContext


class PollHandler(MediaHandler):
    """``poll`` posts: saved as ``NNNN.md`` with the post's metadata (the poll question is the title)."""

    media_type = MediaType.POLL

    def can_handle(self, post: Post) -> bool:
        """Match ``poll`` posts."""
        return post.type == "poll"

    async def resolve(
        self, post: Post, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """
        Produce the poll's metadata document.

        A poll carries no downloadable file, and its options aren't on the listing card, so this records what the
        harvest already knows (title/question, author, date, score, ...) without a further page visit.
        """
        document = post_to_markdown(post)
        return [
            MediaCandidate(
                url=post.url or post.id,
                media_type=self.media_type,
                ext="md",
                body=document.encode("utf-8"),
            )
        ]

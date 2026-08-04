"""Media handler interface.

A handler answers two questions: *does this post belong to me?* (`can_handle`) and
*which downloadable URLs does it carry?* (`resolve`).

Example:
    A minimal custom handler::

        class StickerHandler(MediaHandler):
            media_type = MediaType.LINK

            def can_handle(self, post):
                return post.type == "link" and "stickers.example" in (post.content_href or "")

            async def resolve(self, post, ctx):
                return [MediaCandidate(post.content_href, self.media_type, ext="png")]

        extractor.register_handler(StickerHandler())
"""

from __future__ import annotations

import abc
from typing import TYPE_CHECKING, ClassVar, List

from ..models.media import MediaCandidate, MediaType
from ..models.post import Post

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import ExtractionContext


class MediaHandler(abc.ABC):
    """Resolves one kind of post into downloadable media candidates.

    Attributes:
        media_type: The MediaType this handler serves. Used for selection: the handler is considered only when the
            job asked for something it serves. It may be a combination (``IMAGE | GALLERY``) for a handler that
            resolves several kinds, and an instance may set its own in ``__init__`` when what it can produce depends on
            how it was configured.
        metadata_only: True for handlers that record a matched post but never produce files. No built-in handler
            sets this (text and poll posts are saved as Markdown documents). It remains for custom handlers.
        fallback: True for a catch-all that should only be consulted once every other handler has passed on the post.
    """

    media_type: MediaType
    metadata_only: ClassVar[bool] = False
    fallback: ClassVar[bool] = False

    @abc.abstractmethod
    def can_handle(self, post: Post) -> bool:
        """
        Return whether this handler applies to ``post``.

        This should be an inexpensive check against the already-harvested post-attributes (no network access).

        Args:
            post: The harvested post.

        Returns:
            True if `resolve` should be called for this post.
        """

    @abc.abstractmethod
    async def resolve(
        self, post: Post, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """
        Return the post's downloadable media candidates.

        May use ``ctx`` to visit pages or fetch resources. Use ``await ctx.skip(url, reason)`` to surface
        *why* something was passed over.

        Args:
            post: The post to resolve.
            ctx: The active extraction context.

        Returns:
            The resolved candidates, or ``[]`` when nothing qualifies.
        """

    @property
    def name(self) -> str:
        """The handler's class name (used in logs and events)."""
        return type(self).__name__

    def __repr__(self) -> str:  # pragma: no cover
        return "<{}>".format(self.name)

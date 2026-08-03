"""Posts whose media lives on an external host, routed to that host's `LinkResolver`."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List, Sequence, Any

from ..models.media import MediaCandidate
from ..models.post import Post
from .base import MediaHandler
from .resolver import LinkResolver, ResolverRegistry, default_resolvers

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import ExtractionContext

log = logging.getLogger(__name__)


class ExternalLinkHandler(MediaHandler):
    """
    Any post pointing at a host the registry knows, whatever its post-type.

    Reddit files the same external clip as a ``link`` card, a ``video`` card with an embedded player, or a crosspost,
    so this has to match on the URLs a post carries. The per-host knowledge lives in the resolvers. This handler only
    decides which of a post's URLs to offer them.

    Args:
        resolvers: The hosts to cover. Defaults to `default_resolvers`.
    """

    def __init__(self, resolvers: Sequence[LinkResolver] | None = None) -> None:
        self.registry = ResolverRegistry(
            list(resolvers) if resolvers is not None else default_resolvers()
        )
        self.media_type = self.registry.media_type

    def can_handle(self, post: Post) -> bool:
        """Whether any registered host claims one of the post's URLs."""
        return any(self.registry.claims(url) for url in self._post_urls(post))

    async def resolve(
        self, post: Post, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """
        Hand the post's first recognized URL to its host's resolver.

        Candidates of a kind this job didn't ask for are dropped: a host serving both images and video answers an
        images-only run with images alone.
        """
        ref = post.url or post.content_href or post.id
        for url in self._post_urls(post):
            resolver = self.registry.resolver_for(url, ctx.wanted)
            if resolver is None:
                continue
            candidates = await resolver.resolve(url, ctx, ref=ref)
            return [c for c in candidates if c.media_type & ctx.wanted]
        return []

    @staticmethod
    def _post_urls(post: Post) -> list[str | None | Any]:
        """The post's outward-pointing URLs: the card's target, then any embedded player's source."""
        player_src = post.raw.get("player_src")
        return [
            url
            for url in (post.content_href, player_src)
            if isinstance(url, str) and url
        ]

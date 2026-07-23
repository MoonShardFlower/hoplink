"""Self (text) posts: archived as a Markdown document (metadata + body)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, List

from ..models.media import MediaCandidate, MediaType
from ..models.post import Post
from .base import MediaHandler
from .markdown import post_to_markdown

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import ExtractionContext

log = logging.getLogger(__name__)

# JS run on a self post's comment page: return the body text, scoped to the main post so sidebar/related
# posts are ignored. Reddit renders the self-text inside a `[slot="text-body"]` element; innerText gives a
# clean, readable rendering (paragraph breaks preserved) without pulling in the surrounding chrome.
JS_TEXT_BODY = """
(pid) => {
  const post = document.getElementById(pid) || document.querySelector('shreddit-post');
  if (!post) return null;
  const body = post.querySelector('[slot="text-body"]');
  return body ? body.innerText.trim() : "";
}
"""


class TextHandler(MediaHandler):
    """``text`` (self) posts: saved as ``NNNN.md`` with a YAML metadata header and the post body."""

    media_type = MediaType.TEXT

    def can_handle(self, post: Post) -> bool:
        """Match ``text`` (self) posts."""
        return post.type == "text"

    async def resolve(
        self, post: Post, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """
        Build the post's Markdown document and hand it over as a ready-to-write candidate.

        The full body lives on the post's own page so the page is visited to read it. If that visit fails, the document
        is still produced with an empty body (metadata only).
        """
        body = await self._body(post, ctx)
        document = post_to_markdown(post, body)
        return [
            MediaCandidate(
                # The permalink is the stable per-post key that lets a re-run skip an already-saved post.
                url=post.url or post.id,
                media_type=self.media_type,
                ext="md",
                body=document.encode("utf-8"),
            )
        ]

    @staticmethod
    async def _body(post: Post, ctx: "ExtractionContext") -> str:
        """Read the self-text from the post page, returning "" when it is empty or the page can't be read."""
        if not post.url:
            return ""
        try:
            text = await ctx.evaluate_on(post.url, JS_TEXT_BODY, arg=post.id)
        except Exception as exc:
            log.debug("text %s: body unavailable (%s)", post.id, exc)
            return ""
        await ctx.sleep()  # politeness pause after loading a full post-page
        return text or ""

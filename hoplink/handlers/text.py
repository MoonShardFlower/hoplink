"""Self (text) posts: archived as a Markdown document (metadata + body), optionally following the links inside."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, List, Sequence, Tuple
from urllib.parse import parse_qs, urlparse

from ..models.media import MediaCandidate, MediaType
from ..models.post import Post
from .base import MediaHandler
from .markdown import post_to_markdown
from .resolver import LinkResolver, ResolverRegistry, default_resolvers

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import ExtractionContext

log = logging.getLogger(__name__)

# JS run on a self post's comment page: return the body text*and its links, scoped to the main post. Reddit renders the
# self-text inside a `[slot="text-body"]` element; innerText gives a clean rendering (paragraph breaks preserved)
# without pulling in the surrounding chrome. The links have to be collected separately: innerText renders a Markdown
# link as its label, so the destination of `[my album](https://...)` is nowhere in the text.
# `a.href` (the property, not the attribute) resolves relative URLs against the page for us.
JS_TEXT_BODY = """
(pid) => {
  const post = document.getElementById(pid) || document.querySelector('shreddit-post');
  if (!post) return null;
  const body = post.querySelector('[slot="text-body"]');
  if (!body) return {text: "", links: []};
  return {
    text: body.innerText.trim(),
    links: Array.from(body.querySelectorAll('a[href]')).map(a => a.href),
  };
}
"""


def unwrap_redirect(url: str) -> str:
    """
    Follow Reddit's outbound link wrapper to the URL it points at.

    Reddit sometimes rewrites an off-site link in a post body to ``out.reddit.com/...?url=<target>``. Resolvers
    match on the destination host, so the wrapper has to come off first.

    Args:
        url: A URL from a post body.

    Returns:
        The unwrapped target when ``url`` is such a wrapper, else ``url`` unchanged.
    """
    parsed = urlparse(url)
    if parsed.netloc.lower() != "out.reddit.com":
        return url
    target = parse_qs(parsed.query).get("url", [])
    return target[0] if target and target[0] else url


def body_links(raw: Any) -> List[str]:
    """
    The usable links out of what the body-reading JS returned.

    Args:
        raw: The JS result. A mapping with a ``links`` list is the current shape; anything else (a bare string from
            an older shape, None from a page that wouldn't load) simply carries no links.

    Returns:
        Unwrapped ``http(s)`` URLs in document order, and de-duplicated. Other schemes are dropped: only something
        fetchable is worth handing to a resolver.
    """
    links = raw.get("links") if isinstance(raw, dict) else None
    if not isinstance(links, list):
        return []
    kept: List[str] = []
    seen = set()
    for link in links:
        if not isinstance(link, str) or not link:
            continue
        url = unwrap_redirect(link)
        if urlparse(url).scheme not in ("http", "https") or url in seen:
            continue
        seen.add(url)
        kept.append(url)
    return kept


class TextHandler(MediaHandler):
    """
    ``text`` (self) posts: saved as ``NNNN.md`` with a YAML metadata header and the post body.

    With ``follow_links`` the body's links are resolved too, through the same per-host resolvers an external link
    post goes through, and the post yields that media alongside its document.

    Following widens the handler's ``media_type`` to cover what the resolvers produce, so asking for just those
    types still selects text posts.

    Args:
        follow_links: Resolve the links found in post bodies. Off by default.
        resolvers: The hosts to follow links out to. Default: `default_resolvers`. Ignored unless ``follow_links`` is set.
    """

    def __init__(
        self,
        *,
        follow_links: bool = False,
        resolvers: Sequence[LinkResolver] | None = None,
    ) -> None:
        self.media_type = MediaType.TEXT
        self.registry: ResolverRegistry | None = None
        if follow_links:
            self.registry = ResolverRegistry(
                list(resolvers) if resolvers is not None else default_resolvers()
            )
            self.media_type |= self.registry.media_type

    def can_handle(self, post: Post) -> bool:
        """Match ``text`` (self) posts."""
        return post.type == "text"

    async def resolve(
        self, post: Post, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """
        Build the post's Markdown document and, when following links, whatever its body points at.

        The full body lives on the post's own page so the page is visited to read it. If that visit fails, the
        document is still produced with an empty body (metadata only) and no links are followed.
        """
        body, links = await self._read(post, ctx)
        document = MediaCandidate(
            # The permalink is the stable per-post key that lets a re-run skip an already-saved post.
            url=post.url or post.id,
            media_type=MediaType.TEXT,
            ext="md",
            body=post_to_markdown(post, body).encode("utf-8"),
        )
        if self.registry is None:
            return [document]
        candidates = [document] + await self._follow(links, post, ctx)
        return [c for c in candidates if c.media_type & ctx.wanted]

    async def _follow(
        self, links: List[str], post: Post, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """
        Resolve each body link whose host is registered, in the order they appear.

        Unregistered hosts are ignored rather than fetched: a post body is free text written by anyone, so the
        registry is the allowlist deciding what this ever connects to.
        """
        if self.registry is None:  # pragma: no cover (guarded by the caller)
            return []
        ref = post.url or post.id
        resolved: List[MediaCandidate] = []
        for url in links:
            resolver = self.registry.resolver_for(url, ctx.wanted)
            if resolver is None:
                continue
            log.debug("text %s: following %s to %s", post.id, url, resolver.name)
            resolved.extend(await resolver.resolve(url, ctx, ref=ref))
        return resolved

    @staticmethod
    async def _read(post: Post, ctx: "ExtractionContext") -> Tuple[str, List[str]]:
        """Read the self-text and its links, returning empties when the page can't be read."""
        if not post.url:
            return "", []
        try:
            raw = await ctx.evaluate_on(post.url, JS_TEXT_BODY, arg=post.id)
        except Exception as exc:
            log.debug("text %s: body unavailable (%s)", post.id, exc)
            return "", []
        await ctx.sleep()  # politeness pause after loading a full post-page
        if isinstance(raw, str):
            return raw, []
        text = raw.get("text") if isinstance(raw, dict) else None
        return (text if isinstance(text, str) else ""), body_links(raw)

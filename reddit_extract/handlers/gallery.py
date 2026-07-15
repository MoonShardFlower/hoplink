"""Gallery posts: visit the post-page, read the carousel, reconstruct originals."""

from __future__ import annotations

import logging
import os
import re
from typing import Iterable, List

from ..models.media import MediaCandidate, MediaType
from ..models.post import Post
from .base import MediaHandler

log = logging.getLogger(__name__)

# JS run on a gallery's comment page: return the gallery carousel's raw HTML,
# scoped to the main post only (so we don't pick up sidebar/related thumbnails).
JS_GALLERY = """
(pid) => {
  const post = document.getElementById(pid) || document.querySelector('shreddit-post');
  if (!post) return "";
  const car = post.querySelector('gallery-carousel');
  return car ? car.outerHTML : "";
}
"""


def gallery_image_urls(carousel_html: str, allowed_formats: Iterable[str]) -> List[str]:
    """
    Reconstruct full-resolution i.redd.it URLs from a gallery carousel.

    Reddit renders gallery slides as ``preview.redd.it`` URLs whose final path token is the i.redd.it media id, e.g.
    ``https://preview.redd.it/some-slug-v0-3ps5r77zi1ah1.jpg?width=640&...``
    becomes ``https://i.redd.it/3ps5r77zi1ah1.jpg``.

    Args:
        carousel_html: Outer HTML of the gallery carousel element.
        allowed_formats: Extensions to keep (others, including GIFs, are dropped).

    Returns:
        Full-resolution i.redd.it URLs in slide order, de-duplicated.
    """
    allowed = set(allowed_formats)
    urls: List[str] = []
    seen = set()
    for m in re.finditer(r"https://preview\.redd\.it/([^\s\"'<>?]+)", carousel_html):
        base, ext = os.path.splitext(m.group(1))
        ext = ext.lower().lstrip(".")
        if ext not in allowed:  # excludes gifs / odd formats
            continue
        token = base.split("-")[-1]  # trailing token = media id
        if not re.fullmatch(r"[A-Za-z0-9]{6,}", token):
            continue
        if token in seen:
            continue
        seen.add(token)
        urls.append("https://i.redd.it/{}.{}".format(token, ext))
    return urls


class GalleryHandler(MediaHandler):
    """``gallery`` posts: visits the post-page and yields every slide, full-res."""

    media_type = MediaType.GALLERY

    def can_handle(self, post: Post) -> bool:
        """Match ``gallery`` posts that have a permalink to visit."""
        return post.type == "gallery" and bool(post.permalink)

    async def resolve(self, post: Post, ctx) -> List[MediaCandidate]:
        """
        Load the post page, read the carousel, and rebuild each slide's URL.

        A failure to load the gallery is reported via ``ctx.skip`` and yields no candidates rather than raising.
        """
        try:
            html = await ctx.evaluate_on(
                post.url, JS_GALLERY, arg=post.id, wait_ms=ctx.config.gallery_wait_ms
            )
        except Exception as exc:
            log.debug("gallery %s failed", post.permalink, exc_info=True)
            await ctx.skip(post.url or post.id, "gallery failed: {}".format(exc))
            return []
        urls = gallery_image_urls(html or "", ctx.formats)
        # politeness pause after loading a full post-page
        await ctx.sleep()
        return [
            MediaCandidate(url=u, media_type=self.media_type, ext=ctx.extension_of(u))
            for u in urls
        ]

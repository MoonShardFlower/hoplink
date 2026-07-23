"""Gallery posts: visit the post-page, read the carousel, reconstruct originals."""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Iterable, List

from ..models.media import MediaCandidate, MediaType
from ..models.post import Post
from .base import MediaHandler

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import ExtractionContext

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


# Reddit's preview host, optionally behind a CDN subdomain (e.g. ``cf.preview.redd.it``). ``external-preview.redd.it``
# -- used for previews of *off-site* links -- deliberately does not match: those are not i.redd.it uploads.
_PREVIEW_RE = re.compile(r"https://(?:[a-z0-9]+\.)?preview\.redd\.it/([^\s\"'<>?]+)")


def full_res_from_preview(
    preview_url: str, allowed_formats: Iterable[str]
) -> str | None:
    """
    Reconstruct the full-resolution i.redd.it URL behind one Reddit preview URL.

    Reddit renders a slide (or a crosspost's shared image) as a ``preview.redd.it`` URL whose final path token is the
    i.redd.it media id, e.g. ``https://preview.redd.it/some-slug-v0-3ps5r77zi1ah1.jpg?width=640&...`` becomes
    ``https://i.redd.it/3ps5r77zi1ah1.jpg``.

    Args:
        preview_url: A single preview URL (query string and all).
        allowed_formats: Extensions to keep (others, including GIFs, are dropped).

    Returns:
        The full-resolution i.redd.it URL, or None if ``preview_url`` isn't a Reddit preview, its extension isn't
        allowed, or its trailing token doesn't look like a media id.
    """
    m = _PREVIEW_RE.search(preview_url)
    if not m:
        return None
    base, ext = os.path.splitext(m.group(1))
    ext = ext.lower().lstrip(".")
    if ext not in set(allowed_formats):  # excludes gifs / odd formats
        return None
    token = base.split("-")[-1]  # trailing token = media id
    if not re.fullmatch(r"[A-Za-z0-9]{6,}", token):
        return None
    return "https://i.redd.it/{}.{}".format(token, ext)


def gallery_image_urls(carousel_html: str, allowed_formats: Iterable[str]) -> List[str]:
    """
    Reconstruct full-resolution i.redd.it URLs from a gallery carousel.

    Each slide's ``preview.redd.it`` URL is rebuilt into its original i.redd.it upload (see `full_res_from_preview`).

    Args:
        carousel_html: Outer HTML of the gallery carousel element.
        allowed_formats: Extensions to keep (others, including GIFs, are dropped).

    Returns:
        Full-resolution i.redd.it URLs in slide order, de-duplicated.
    """
    allowed = set(allowed_formats)
    urls: List[str] = []
    seen = set()
    for m in _PREVIEW_RE.finditer(carousel_html):
        full = full_res_from_preview(m.group(0), allowed)
        if full is None or full in seen:
            continue
        seen.add(full)
        urls.append(full)
    return urls


class GalleryHandler(MediaHandler):
    """``gallery`` posts: visits the post-page and yields every slide, full-res."""

    media_type = MediaType.GALLERY

    def can_handle(self, post: Post) -> bool:
        """Match ``gallery`` posts that have a permalink to visit."""
        return post.type == "gallery" and bool(post.permalink)

    async def resolve(
        self, post: Post, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """
        Load the post page, read the carousel, and rebuild each slide's URL.

        A failure to load the gallery is reported via ``ctx.skip`` and yields no candidates rather than raising.
        """
        try:
            html = await ctx.evaluate_on(
                post.url or "",
                JS_GALLERY,
                arg=post.id,
                wait_ms=ctx.config.gallery_wait_ms,
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

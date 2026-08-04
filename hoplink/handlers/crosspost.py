"""
Crossposts: posts that re-share another post, resolved to the shared media.

A crosspost carries no media of its own -- its ``content-href`` points at the *original* post, not a file. But its
own page renders the shared media inline (an image, a gallery carousel, or a video player), exactly like a normal
post. This handler reads that rendered media and reuses the image/gallery/video logic to rebuild the download URLs,
so a profile full of crossposts (the common shape for reposter accounts) no longer comes back empty.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, List, Optional

from ..models.media import MediaCandidate, MediaType
from ..models.post import Post
from .base import MediaHandler
from .gallery import full_res_from_preview, gallery_image_urls
from .video import VIDEO_CONTENT_PREFIXES, best_dash_video, best_mp4

if TYPE_CHECKING:  # pragma: no cover
    from ..core.context import ExtractionContext

log = logging.getLogger(__name__)

# JS run on a crosspost's own page: describe the shared media rendered inside the main post. Scoped to the post id
# (like the gallery handler) so sidebar/related content is ignored.
JS_CROSSPOST = """
(pid) => {
  const post = document.getElementById(pid) || document.querySelector('shreddit-post');
  if (!post) return null;
  const carousel = post.querySelector('gallery-carousel');
  const player = post.querySelector('shreddit-player, shreddit-player-2, [packaged-media-json]');
  const img = post.querySelector('img[src*="i.redd.it"], img[src*="preview.redd.it"]');
  return {
    gallery: carousel ? carousel.outerHTML : null,
    packaged: player ? player.getAttribute('packaged-media-json') : null,
    player_src: player ? player.getAttribute('src') : null,
    content_href: post.getAttribute('content-href'),
    img_src: img ? (img.getAttribute('src') || img.src) : null,
  };
}
"""


def _vreddit_base(*values: Optional[str]) -> Optional[str]:
    """Return the ``https://v.redd.it/<id>`` base found in any of ``values`` (None if none carry one)."""
    for value in values:
        if not value:
            continue
        m = re.search(r"https://v\.redd\.it/([A-Za-z0-9]+)", value)
        if m:
            return "https://v.redd.it/" + m.group(1)
    return None


class CrosspostHandler(MediaHandler):
    """``crosspost`` posts: resolve the shared image, gallery, or video from the crosspost's page."""

    media_type = MediaType.CROSSPOST

    def can_handle(self, post: Post) -> bool:
        """Match ``crosspost`` posts that have a page to read."""
        return post.type == "crosspost" and bool(post.permalink)

    async def resolve(
        self, post: Post, ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """
        Load the crosspost's page, identify the shared media, and rebuild its download URL(s).

        Galleries, videos, and images are each rebuilt with the same logic the dedicated handlers use. A page that
        can't be read, or that shares media of no recognized kind, is reported via ``ctx.skip`` and yields nothing.
        """
        try:
            data = await ctx.evaluate_on(post.url or "", JS_CROSSPOST, arg=post.id)
        except Exception as exc:
            log.debug("crosspost %s failed", post.permalink, exc_info=True)
            await ctx.skip(post.url or post.id, "crosspost failed: {}".format(exc))
            return []
        await ctx.sleep()  # politeness pause after loading a full post-page
        if not data:
            return []

        candidates = self._images(data, ctx) or await self._video(data, ctx)
        if not candidates:
            await ctx.skip(post.url or post.id, "crosspost: no recognized media")
        return candidates

    @staticmethod
    def _images(data: dict[str, Any], ctx: "ExtractionContext") -> List[MediaCandidate]:
        """Rebuild gallery slides or a single shared image; empty if the crosspost shares neither."""
        gallery = data.get("gallery")
        if gallery:
            return [
                MediaCandidate(
                    url=u, media_type=MediaType.GALLERY, ext=ctx.extension_of(u)
                )
                for u in gallery_image_urls(gallery, ctx.formats)
            ]
        img_src = data.get("img_src") or ""
        full = full_res_from_preview(img_src, ctx.formats)
        if (
            full is None
            and ctx.extension_of(img_src) in ctx.formats
            and "i.redd.it" in img_src
        ):
            full = img_src.split("?")[0]  # already a direct, allowed i.redd.it upload
        if full:
            return [
                MediaCandidate(
                    url=full, media_type=MediaType.IMAGE, ext=ctx.extension_of(full)
                )
            ]
        return []

    @staticmethod
    async def _video(
        data: dict[str, Any], ctx: "ExtractionContext"
    ) -> List[MediaCandidate]:
        """Rebuild the shared video: packaged MP4 when present, else the DASH fallback (video-only)."""
        best = best_mp4(data.get("packaged"))
        if best is not None:
            return [CrosspostHandler._mp4(best[0])]
        base = _vreddit_base(data.get("player_src"), data.get("content_href"))
        if base is None:
            return []
        manifest = await ctx.fetch(base + "/DASHPlaylist.mpd")
        if not manifest.ok or manifest.body is None:
            return []
        video_url = best_dash_video(manifest.body.decode("utf-8", "replace"), base)
        return [CrosspostHandler._mp4(video_url)] if video_url else []

    @staticmethod
    def _mp4(url: str) -> MediaCandidate:
        """Wrap an MP4 URL as a video MediaCandidate."""
        return MediaCandidate(
            url=url,
            media_type=MediaType.VIDEO,
            ext="mp4",
            content_prefixes=VIDEO_CONTENT_PREFIXES,
        )

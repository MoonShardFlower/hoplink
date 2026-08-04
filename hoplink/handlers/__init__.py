from typing import List, Optional

from ..models.config import ExtractorConfig
from .base import MediaHandler
from .crosspost import CrosspostHandler
from .external import ExternalLinkHandler
from .gallery import GalleryHandler, full_res_from_preview, gallery_image_urls
from .image import ImageHandler
from .link import LinkImageHandler
from .markdown import post_to_markdown
from .poll import PollHandler
from .redgifs import RedGifsResolver
from .resolver import LinkResolver, ResolverRegistry, default_resolvers
from .text import TextHandler
from .video import VideoHandler


def default_handlers(config: Optional[ExtractorConfig] = None) -> List[MediaHandler]:
    """
    Fresh instances of the built-in handlers, in selection order.

    Args:
        config: The configuration the chain is built for. Only the options that change what a handler *is* are read
            here. Everything else is read per job from the context. None builds the default chain.

    Returns:
        The handlers, first match winning, except that fallbacks (`LinkImageHandler`) are consulted only once every
        other handler has passed on the post. `ExternalLinkHandler` covers every host in `default_resolvers`.
    """
    follow_links = bool(config is not None and config.follow_text_links)
    return [
        ImageHandler(),
        GalleryHandler(),
        ExternalLinkHandler(),
        VideoHandler(),
        CrosspostHandler(),
        TextHandler(follow_links=follow_links),
        PollHandler(),
        LinkImageHandler(),
    ]


__all__ = [
    "MediaHandler",
    "ImageHandler",
    "GalleryHandler",
    "ExternalLinkHandler",
    "RedGifsResolver",
    "LinkResolver",
    "ResolverRegistry",
    "VideoHandler",
    "LinkImageHandler",
    "CrosspostHandler",
    "TextHandler",
    "PollHandler",
    "default_handlers",
    "default_resolvers",
    "gallery_image_urls",
    "full_res_from_preview",
    "post_to_markdown",
]

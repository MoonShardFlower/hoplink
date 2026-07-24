from typing import List

from .base import MediaHandler
from .crosspost import CrosspostHandler
from .gallery import GalleryHandler, full_res_from_preview, gallery_image_urls
from .image import ImageHandler
from .link import LinkImageHandler
from .markdown import post_to_markdown
from .poll import PollHandler
from .redgifs import RedGifsHandler
from .text import TextHandler
from .video import VideoHandler


def default_handlers() -> List[MediaHandler]:
    """Fresh instances of the built-in handlers, in selection order."""
    return [
        ImageHandler(),
        GalleryHandler(),
        RedGifsHandler(),
        VideoHandler(),
        LinkImageHandler(),
        CrosspostHandler(),
        TextHandler(),
        PollHandler(),
    ]


__all__ = [
    "MediaHandler",
    "ImageHandler",
    "GalleryHandler",
    "RedGifsHandler",
    "VideoHandler",
    "LinkImageHandler",
    "CrosspostHandler",
    "TextHandler",
    "PollHandler",
    "default_handlers",
    "gallery_image_urls",
    "full_res_from_preview",
    "post_to_markdown",
]

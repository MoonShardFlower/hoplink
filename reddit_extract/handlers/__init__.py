from typing import List

from .base import MediaHandler
from .gallery import GalleryHandler, gallery_image_urls
from .image import ImageHandler
from .link import LinkImageHandler
from .text import TextHandler
from .video import VideoHandler


def default_handlers() -> List[MediaHandler]:
    """Fresh instances of the built-in handlers, in selection order."""
    return [
        ImageHandler(),
        GalleryHandler(),
        VideoHandler(),
        LinkImageHandler(),
        TextHandler(),
    ]


__all__ = [
    "MediaHandler",
    "ImageHandler",
    "GalleryHandler",
    "VideoHandler",
    "LinkImageHandler",
    "TextHandler",
    "default_handlers",
    "gallery_image_urls",
]

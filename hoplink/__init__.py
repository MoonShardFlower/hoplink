"""
hoplink: composable, browser-driven Reddit media extraction that hops outbound links to their real host.

Reddit blocks plain HTTP access to its JSON/OAuth API from most IPs, so this library drives a real browser
(Playwright and Chromium) to render listings, then downloads the underlying media from Reddit's CDN.

Quick start::

    from hoplink import HoplinkExtractor, Subreddit, MediaType

    with HoplinkExtractor(headless=True) as hle:
        result = hle.extract(
            Subreddit("EarthPorn", limit=50, sort="top", time_filter="month"),
            media_types=MediaType.IMAGE | MediaType.GALLERY,
            output_dir="./downloads",
        )
        print(result.summary())
"""

import logging

from hoplink.storage.manifest import Manifest, ManifestSet

from .core.browser import BrowserManager, FetchResult
from .core.context import ExtractionContext
from .core.extractor import AsyncHoplinkExtractor
from .core.sync import HoplinkExtractor
from .core.timing import Timings
from .events import Events
from .exceptions import BrowserError, HoplinkExtractError, NoPostsFoundError
from .handlers import (
    ExternalLinkHandler,
    GalleryHandler,
    ImageHandler,
    LinkImageHandler,
    LinkResolver,
    MediaHandler,
    RedGifsResolver,
    ResolverRegistry,
    TextHandler,
    VideoHandler,
    default_handlers,
    default_resolvers,
)
from .models import (
    ExtractionResult,
    ExtractorConfig,
    MediaCandidate,
    MediaItem,
    MediaType,
    MultiReddit,
    Post,
    PostFilter,
    Source,
    Subreddit,
    UserProfile,
    parse_source,
)
from .storage import (
    FilesystemStorage,
    MemoryStorage,
    StorageBackend,
    media_filename,
    slugify,
)

# A library shouldn't configure logging for its host; this keeps the stdlib's "no handlers could be found" warning away
# while leaving the choice to the caller. The CLI attaches a real handler for --verbose / --log-level.
logging.getLogger(__name__).addHandler(logging.NullHandler())

__version__ = "0.1.0"

__all__ = [
    "AsyncHoplinkExtractor",
    "BrowserError",
    "BrowserManager",
    "Events",
    "ExternalLinkHandler",
    "ExtractionContext",
    "ExtractionResult",
    "ExtractorConfig",
    "FetchResult",
    "FilesystemStorage",
    "GalleryHandler",
    "HoplinkExtractError",
    "HoplinkExtractor",
    "ImageHandler",
    "LinkImageHandler",
    "LinkResolver",
    "Manifest",
    "ManifestSet",
    "MediaCandidate",
    "MediaHandler",
    "MediaItem",
    "MediaType",
    "MemoryStorage",
    "MultiReddit",
    "NoPostsFoundError",
    "Post",
    "PostFilter",
    "RedGifsResolver",
    "ResolverRegistry",
    "Source",
    "StorageBackend",
    "Subreddit",
    "TextHandler",
    "Timings",
    "UserProfile",
    "VideoHandler",
    "default_handlers",
    "default_resolvers",
    "media_filename",
    "parse_source",
    "slugify",
    "__version__",
]

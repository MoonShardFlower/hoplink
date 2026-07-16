"""
reddit_extract: composable, browser-driven Reddit media extraction.

Reddit blocks plain HTTP access to its JSON/OAuth API from most IPs, so this library drives a real browser
(Playwright and Chromium) to render listings, then downloads the underlying media from Reddit's CDN.

Quick start::

    from reddit_extract import RedditExtractor, Subreddit, MediaType

    with RedditExtractor(headless=True) as rex:
        result = rex.extract(
            Subreddit("EarthPorn", limit=50, sort="top", time_filter="month"),
            media_types=MediaType.IMAGE | MediaType.GALLERY,
            output_dir="./downloads",
        )
        print(result.summary())
"""

from reddit_extract.storage.manifest import Manifest

from .core.browser import BrowserManager, FetchResult
from .core.context import ExtractionContext
from .core.extractor import AsyncRedditExtractor
from .core.sync import RedditExtractor
from .events import Events
from .exceptions import BrowserError, NoPostsFoundError, RedditExtractError
from .handlers import (
    GalleryHandler,
    ImageHandler,
    LinkImageHandler,
    MediaHandler,
    TextHandler,
    VideoHandler,
    default_handlers,
)
from .models import (
    ExtractionResult,
    ExtractorConfig,
    MediaCandidate,
    MediaItem,
    MediaType,
    MultiReddit,
    Post,
    Source,
    Subreddit,
    UserProfile,
    parse_source,
)
from .storage import FilesystemStorage, MemoryStorage, StorageBackend

__version__ = "0.1.0"

__all__ = [
    "AsyncRedditExtractor",
    "BrowserError",
    "BrowserManager",
    "Events",
    "ExtractionContext",
    "ExtractionResult",
    "ExtractorConfig",
    "FetchResult",
    "FilesystemStorage",
    "GalleryHandler",
    "ImageHandler",
    "LinkImageHandler",
    "Manifest",
    "MediaCandidate",
    "MediaHandler",
    "MediaItem",
    "MediaType",
    "MemoryStorage",
    "MultiReddit",
    "NoPostsFoundError",
    "Post",
    "RedditExtractError",
    "RedditExtractor",
    "Source",
    "StorageBackend",
    "Subreddit",
    "TextHandler",
    "UserProfile",
    "VideoHandler",
    "default_handlers",
    "parse_source",
    "__version__",
]

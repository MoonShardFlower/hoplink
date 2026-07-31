"""Extractor-wide configuration."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from typing import Any, Iterable, Union

from .media import MediaType

DEFAULT_FORMATS = ("jpg", "jpeg", "png", "webp")
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class ExtractorConfig:
    """
    Configuration for how the extractor drives the browser and downloads.

    All fields have safe defaults. Pacing is relaxed because Reddit throttles aggressive clients.
    """

    # -- media selection ------------------------------------------------
    #: file extensions accepted for image-like media (comma string or iterable)
    formats: tuple[str, ...] = DEFAULT_FORMATS
    #: media types extracted when a call doesn't specify any
    default_media_types: MediaType = MediaType.IMAGE | MediaType.GALLERY
    #: when a RedGIFs post is found, scrape the uploader's whole RedGIFs profile instead of just the linked clip.
    # Each profile is scraped at most once per run (a repeat uploader in the same listing is skipped, not re-scraped).
    redgifs_scrape_all: bool = False
    #: RedGIFs usernames whose clips are never downloaded (comma string or iterable, case-insensitive).
    # Posts from these RedGIFs users are skipped in both single-clip and scrape-all mode.
    # Anonymous uploads carry no username and are not blocked by this list.
    redgifs_blacklist: tuple[str, ...] = ()

    # -- browser --------------------------------------------------------
    headless: bool = True
    #: persistent browser profile directory (keeps you logged in for NSFW)
    profile_dir: str | None = None
    user_agent: str = DEFAULT_UA
    viewport: tuple[int, int] = (1366, 900)
    locale: str = "en-US"

    # -- output ---------------------------------------------------------
    #: base output directory (each source gets a subdirectory)
    output_dir: str = "downloads"
    #: write the manifest to storage after every N saved files
    manifest_flush_every: int = 1
    #: skip writing a downloaded file whose content hash matches one already saved for this source
    dedupe_by_hash: bool = False

    # -- pacing ---------------------------------------------------------
    scroll_pause: float = 2.0
    #: seconds a downloader waits after finishing one file before taking the next
    delay: float = 0.5
    #: seconds between successive pages of a third-party API (e.g. RedGIFs profile paging). Separate from
    # ``scroll_pause`` which waits for a listing to render more cards, while this one only paces JSON requests.
    api_pause: float = 0.5
    max_stale_scrolls: int = 10
    scroll_px: int = 18000
    #: files fetched at once within a single post's media (a gallery's images, a scraped RedGIFs profile's clips).
    # 1 keeps downloads strictly sequential. Raising it speeds up posts that resolve to many files.
    download_concurrency: int = 1
    #: fetch media bytes directly instead of through the browser, carrying its cookies and User-Agent.
    # Much faster for large files. A host that refuses a direct request falls back to the browser automatically.
    direct_download: bool = True

    # -- download retries -----------------------------------------------
    #: extra attempts for a download that fails transiently (0 disables retrying)
    max_retries: int = 2
    #: base seconds for exponential retry backoff; the wait never dips below ``delay``
    retry_backoff: float = 1.0

    # -- timeouts (milliseconds) ----------------------------------------
    nav_timeout_ms: int = 60000
    post_wait_timeout_ms: int = 30000
    gallery_wait_ms: int = 2000
    request_timeout_ms: int = 60000

    def __post_init__(self) -> None:
        """
        Normalize ``formats``/``default_media_types`` and validate numbers.

        Raises:
            ValueError: If any pacing, timeout, or count field is out of range.
        """
        formats: Union[str, Iterable[str]] = self.formats
        if isinstance(formats, str):
            formats = formats.split(",")
        normalized = tuple(
            f.strip().lower().lstrip(".") for f in formats if f and f.strip()
        )
        object.__setattr__(self, "formats", normalized)
        blacklist: Union[str, Iterable[str], None] = self.redgifs_blacklist
        if blacklist is None:
            blacklist = ()
        elif isinstance(blacklist, str):
            blacklist = blacklist.split(",")
        object.__setattr__(
            self,
            "redgifs_blacklist",
            tuple(name.strip().lower() for name in blacklist if name and name.strip()),
        )
        object.__setattr__(
            self, "default_media_types", MediaType.coerce(self.default_media_types)
        )
        self._require_non_negative(
            "scroll_pause", "delay", "api_pause", "max_retries", "retry_backoff"
        )
        self._require_positive(
            "max_stale_scrolls",
            "scroll_px",
            "manifest_flush_every",
            "download_concurrency",
            "nav_timeout_ms",
            "post_wait_timeout_ms",
            "request_timeout_ms",
        )
        if self.gallery_wait_ms < 0:
            raise ValueError("gallery_wait_ms must be >= 0")

    def _require_non_negative(self, *names: str) -> None:
        """Raise ValueError if any named field is negative."""
        for name in names:
            if getattr(self, name) < 0:
                raise ValueError("{} must be >= 0".format(name))

    def _require_positive(self, *names: str) -> None:
        """Raise ValueError if any named field is less than 1."""
        for name in names:
            if getattr(self, name) < 1:
                raise ValueError("{} must be >= 1".format(name))

    def replace(self, **changes: Any) -> "ExtractorConfig":
        """
        Return a copy of this config with the given fields changed.

        Args:
            **changes: Field values to override.

        Returns:
            A new, validated ExtractorConfig.
        """
        return dataclasses.replace(self, **changes)

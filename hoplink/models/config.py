"""Extractor-wide configuration."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Union

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

    # -- external hosts -------------------------------------------------
    #: hosts whose posts are followed out to the uploader's whole profile rather than just the linked item
    # (comma string or iterable, e.g. ``"redgifs"``). Each profile is scraped at most once per run: a repeat
    # uploader in the same listing is skipped.
    scrape_all_hosts: tuple[str, ...] = ()
    #: per-host settings, keyed by the host a handler serves::
    #
    #     host_options={"imgur": {"client_id": "..."}, "redgifs": {"blacklist": "alice,bob"}}
    #
    # A ``blacklist`` entry names uploaders whose media is never downloaded, in both single-item and scrape-all
    # mode (anonymous uploads carry no name and are not blocked). Other keys are the host handler's own business;
    # unknown hosts and unknown keys are ignored.
    host_options: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: resolve links found in the body of a text post, so a self post pointing at an external host yields that
    # host's media alongside the post's Markdown document.
    follow_text_links: bool = False

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

    # -- rate limits ----------------------------------------------------
    # A host answering 429 is held for a cooldown, doubling with each consecutive refusal. Every request to that
    # host waits it out, whichever handler or job makes it.
    #: seconds a host is left alone after it first answers 429 (a ``Retry-After`` asking for longer wins)
    rate_limit_backoff: float = 15.0
    #: ceiling for that wait however many refusals pile up, so a run cannot stall indefinitely
    rate_limit_max_backoff: float = 300.0
    #: extra attempts for a rate-limited request, when more than ``max_retries``
    rate_limit_retries: int = 4

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
        object.__setattr__(self, "scrape_all_hosts", self._names(self.scrape_all_hosts))
        self._normalize_host_options()
        object.__setattr__(
            self, "default_media_types", MediaType.coerce(self.default_media_types)
        )
        self._require_non_negative(
            "scroll_pause",
            "delay",
            "api_pause",
            "max_retries",
            "retry_backoff",
            "rate_limit_backoff",
            "rate_limit_max_backoff",
            "rate_limit_retries",
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

    @staticmethod
    def _names(value: Union[str, Iterable[str], None]) -> tuple[str, ...]:
        """Normalize a comma string or iterable of names into a trimmed, lower-cased tuple."""
        if value is None:
            return ()
        parts: Iterable[str] = value.split(",") if isinstance(value, str) else value
        return tuple(name.strip().lower() for name in parts if name and name.strip())

    def _normalize_host_options(self) -> None:
        """
        Freeze ``host_options`` into a read-only mapping of read-only mappings.

        Host names are lower-cased so ``"RedGifs"`` == ``"redgifs"``. Entries that aren't mappings are dropped.
        """
        source: Mapping[str, Any] = self.host_options or {}
        options: dict[str, dict[str, Any]] = {
            str(host).strip().lower(): dict(values)
            for host, values in source.items()
            if isinstance(values, Mapping)
        }
        object.__setattr__(
            self,
            "host_options",
            MappingProxyType(
                {host: MappingProxyType(values) for host, values in options.items()}
            ),
        )

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

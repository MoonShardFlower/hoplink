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

    # -- pacing ---------------------------------------------------------
    scroll_pause: float = 2.0
    img_delay: float = 0.5
    max_stale_scrolls: int = 10
    scroll_px: int = 18000

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
        object.__setattr__(
            self, "default_media_types", MediaType.coerce(self.default_media_types)
        )
        self._require_non_negative("scroll_pause", "img_delay")
        self._require_positive(
            "max_stale_scrolls", "scroll_px", "manifest_flush_every",
            "nav_timeout_ms", "post_wait_timeout_ms", "request_timeout_ms",
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

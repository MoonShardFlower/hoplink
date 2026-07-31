"""Per-job context handed to media handlers."""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from ..events import Events, emit
from ..models.config import ExtractorConfig
from ..models.source import Source

if TYPE_CHECKING:  # pragma: no cover
    from .browser import BrowserManager, FetchResult


class ExtractionContext:
    """
    The interface a handler may use while resolving a post.

    Handlers should stick to `evaluate_on`, `fetch`, `skip`, `sleep`, and the ``config``/``formats`` attributes.
    ``page`` and ``browser_context`` are escape hatches for advanced handlers.

    Attributes:
        source: The source currently being extracted.
        config: The active ExtractorConfig.
        events: Progress callbacks for this job.
        formats: Accepted image extensions, as a frozen set.
        page: The already-navigated listing page.
    """

    def __init__(
        self,
        *,
        source: Source,
        config: ExtractorConfig,
        events: Events,
        browser: "BrowserManager",
        page: Any,
    ) -> None:
        """Build a context for one extraction job.

        Args:
            source: The source being extracted.
            config: The active ExtractorConfig.
            events: Progress callbacks for this job.
            browser: The shared browser manager.
            page: The listing page, already navigated to the source's URL.
        """
        self.source = source
        self.config = config
        self.events = events
        self.formats = frozenset(config.formats)
        self.page = page  #: the listing page (already navigated)
        self._browser = browser
        self._util_page: Any = None

    @staticmethod
    def extension_of(url: str) -> str:
        """Lower-case extension of a URL's path ('' if none)."""
        return os.path.splitext(urlparse(url).path)[1].lower().lstrip(".")

    async def evaluate_on(
        self, url: str, js: str, arg: Any = None, wait_ms: int = 0
    ) -> Any:
        """
        Navigate a utility page to ``url`` and evaluate JavaScript on it.

        The utility page is separate from the listing page and is reused across posts within the job
        (so the listing's scroll position is preserved).

        Args:
            url: The page to navigate to.
            js: A JavaScript function expression to evaluate.
            arg: Optional argument passed to the JS function.
            wait_ms: Milliseconds to wait after load before evaluating.

        Returns:
            Whatever the evaluated JavaScript returns.
        """
        page = await self._ensure_util_page()
        await page.goto(
            url, wait_until="domcontentloaded", timeout=self.config.nav_timeout_ms
        )
        if wait_ms:
            await page.wait_for_timeout(wait_ms)
        return await page.evaluate(js, arg)

    async def fetch(
        self, url: str, headers: dict[str, str] | None = None
    ) -> "FetchResult":
        """
        GET a URL through the browser context (cookies, UA, and all).

        Args:
            url: The URL to fetch.
            headers: Extra request headers (e.g. an ``Authorization`` bearer for a third-party API).

        Returns:
            The fetch outcome, including body and content type.
        """
        return await self._browser.fetch(
            url, self.config.request_timeout_ms, headers=headers
        )

    async def download(self, url: str) -> "FetchResult":
        """
        GET media bytes by whichever route is faster and works.

        Prefers a direct request (carrying the browser's cookies and User-Agent) and falls back to the browser per host
        when one refuses. Handlers should use `fetch` for API calls, where a token may be bound to the browser's
        identity. This is for the media bytes themselves, where the browser round-trip is expensive.

        Args:
            url: The media URL to fetch.

        Returns:
            The fetch outcome, whichever route produced it.
        """
        return await self._browser.download(url, self.config.request_timeout_ms)

    async def skip(self, url: str, reason: str) -> None:
        """
        Report that ``url`` was passed over, firing the ``on_skip`` event.

        Args:
            url: The URL (or post reference) being skipped.
            reason: A short human-readable explanation.
        """
        await emit(self.events.on_skip, self.source, url, reason)

    async def sleep(self, seconds: float | None = None) -> None:
        """
        Pause politely, by default for the configured ``scroll_pause``.

        Args:
            seconds: An explicit pause, for waits that are not scroll pacing (a handler walking pages of a
                third-party API passes ``config.api_pause``). Zero or less does not sleep at all.
        """
        pause = self.config.scroll_pause if seconds is None else seconds
        if pause > 0:
            await asyncio.sleep(pause)

    @property
    def browser_context(self) -> Any:
        """Underlying Playwright BrowserContext (None with fake browsers)."""
        return self._browser.playwright_context

    async def new_page(self) -> Any:
        """A fresh page owned by the caller."""
        return await self._browser.new_page()

    async def _ensure_util_page(self) -> Any:
        """Return the reusable utility page, creating it on first use."""
        if self._util_page is None:
            self._util_page = await self._browser.new_page()
        return self._util_page

    async def aclose(self) -> None:
        """Close the utility page if one was created."""
        if self._util_page is not None:
            try:
                await self._util_page.close()
            except Exception:  # pragma: no cover
                pass
            self._util_page = None

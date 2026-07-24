"""
Playwright lifecycle: one browser, many pages.

This is the only module that touches Playwright, and it imports it lazily, so the rest of the library
(models, handlers, storage) works without browsers installed (handy for tests).
"""

from __future__ import annotations

import logging
import re
from typing import Any, NamedTuple

from ..exceptions import BrowserError
from ..models.config import ExtractorConfig

log = logging.getLogger(__name__)

GATE_BUTTON_NAMES = (
    "Accept all",
    "Yes, I'm over 18",
    "I'm over 18",
    "Continue",
    "View NSFW content",
    "Yes",
)


class FetchResult(NamedTuple):
    """
    Outcome of an HTTP fetch through the browser context.

    Attributes:
        ok: Whether the request succeeded (HTTP 2xx and no transport error).
        status: HTTP status code, or 0 if the request never completed.
        content_type: Lower-cased Content-Type without parameters.
        body: Response bytes on success, else None.
        error: Short error description on failure, else None.
    """

    ok: bool
    status: int = 0
    content_type: str = ""
    body: bytes | None = None
    error: str | None = None


class BrowserManager:
    """Owns one Chromium instance (or persistent context) for many jobs."""

    def __init__(self, config: ExtractorConfig) -> None:
        """
        Store config; 'start' launches the browser lazily.

        Args:
            config: Configuration controlling launch options and timeouts.
        """
        self._config = config
        # Playwright objects are untyped. Hold them as Any until start() launches them.
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._timeout_error: type[Exception] = TimeoutError

    @property
    def started(self) -> bool:
        """Whether the browser context is currently launched."""
        return self._context is not None

    @property
    def playwright_context(self) -> Any:
        """The underlying Playwright BrowserContext (escape hatch)."""
        return self._context

    async def start(self) -> None:
        """
        Launch Chromium (idempotent).

        Uses a persistent context when ``config.profile_dir`` is set, otherwise a fresh incognito context.
        Playwright is imported lazily here.

        Raises:
            BrowserError: If Playwright is not installed or Chromium cannot be launched.
        """
        if self.started:
            return
        try:
            from playwright.async_api import TimeoutError as PWTimeoutError
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover
            raise BrowserError(
                "playwright is not installed. Run: pip install playwright && python -m playwright install chromium."
            ) from exc
        self._timeout_error = PWTimeoutError

        cfg = self._config
        viewport = {"width": cfg.viewport[0], "height": cfg.viewport[1]}
        try:
            self._pw = await async_playwright().start()
            if cfg.profile_dir:
                import os

                profile_dir = os.path.expanduser(cfg.profile_dir)
                os.makedirs(profile_dir, exist_ok=True)
                self._context = await self._pw.chromium.launch_persistent_context(
                    profile_dir,
                    headless=cfg.headless,
                    user_agent=cfg.user_agent,
                    viewport=viewport,
                    locale=cfg.locale,
                )
            else:
                self._browser = await self._pw.chromium.launch(headless=cfg.headless)
                self._context = await self._browser.new_context(
                    user_agent=cfg.user_agent, viewport=viewport, locale=cfg.locale
                )
        except Exception as exc:
            await self.close()
            if isinstance(exc, BrowserError):
                raise
            raise BrowserError("could not launch Chromium: {}".format(exc)) from exc
        log.debug("browser started (profile=%s)", cfg.profile_dir)

    async def close(self) -> None:
        """Close the context, browser, and Playwright driver."""
        for closeable in (self._context, self._browser):
            if closeable is not None:
                try:
                    await closeable.close()
                except Exception:  # pragma: no cover
                    pass
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:  # pragma: no cover
                pass
        self._pw = self._browser = self._context = None

    async def new_page(self) -> Any:
        """
        Open and return a new page in the browser context.

        Raises:
            BrowserError: If the browser has not been started.
        """
        if not self.started:
            raise BrowserError("browser is not started")
        return await self._context.new_page()

    async def wait_for_posts(self, page: Any, timeout_ms: int) -> bool:
        """True once post cards are rendered (modern or legacy UI), else False."""
        try:
            await page.wait_for_selector(
                "shreddit-post, #siteTable .thing", timeout=timeout_ms
            )
            return True
        except self._timeout_error:
            return False

    async def dismiss_gates(self, page: Any) -> None:
        """Best-effort click-through of cookie / over-18 interstitials."""
        for name in GATE_BUTTON_NAMES:
            try:
                btn = page.get_by_role("button", name=re.compile(name, re.I))
                if await btn.count():
                    await btn.first.click(timeout=1500)
                    await page.wait_for_timeout(400)
            except Exception:
                pass

    async def fetch(
        self,
        url: str,
        timeout_ms: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResult:
        """
        GET a URL through the browser context (cookies, UA, and all).

        Transport errors and non-2xx responses are returned as an unsuccessful FetchResult rather than raised.
        The response is always disposed so Playwright does not hold the body in memory.

        Args:
            url: The URL to fetch.
            timeout_ms: Request timeout; defaults to ``config.request_timeout_ms``.
            headers: Extra request headers (e.g. an ``Authorization`` bearer), merged over the context's own.

        Returns:
            The fetch outcome.

        Raises:
            BrowserError: If the browser has not been started.
        """
        if not self.started:
            raise BrowserError("browser is not started")
        timeout = (
            timeout_ms if timeout_ms is not None else self._config.request_timeout_ms
        )
        # Only pass headers when given, so the common call stays get(url, timeout=...).
        kwargs: dict[str, Any] = {"timeout": timeout}
        if headers:
            kwargs["headers"] = headers
        try:
            resp = await self._context.request.get(url, **kwargs)
        except Exception as exc:
            return FetchResult(ok=False, error="error: {}".format(exc))
        try:
            ctype = (
                (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            )
            body = await resp.body() if resp.ok else None
            return FetchResult(
                ok=resp.ok,
                status=resp.status,
                content_type=ctype,
                body=body,
                error=None if resp.ok else "HTTP {}".format(resp.status),
            )
        except Exception as exc:
            return FetchResult(ok=False, error="error: {}".format(exc))
        finally:
            # Playwright keeps response bodies in memory until the context closes unless they are disposed.
            try:
                await resp.dispose()
            except Exception:
                pass

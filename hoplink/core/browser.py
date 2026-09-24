"""
Playwright lifecycle: one browser, many pages.

This is the only module that touches Playwright, and it imports it lazily, so the rest of the library
(models, handlers, storage) works without browsers installed (handy for tests).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlparse

from ..exceptions import BrowserError
from ..models.config import ExtractorConfig
from .http import (
    FetchResult,
    fetch_direct,
    is_rate_limited,
    looks_blocked,
    parse_retry_after,
)
from .timing import human_bytes

# FetchResult lives with the HTTP layer, since both routes produce one, but it is re-exported here for convenience
__all__ = ["GATE_BUTTON_NAMES", "BrowserManager", "FetchResult", "HostPacer"]

log = logging.getLogger(__name__)


def _log_fetch(
    url: str, status: int, body: bytes | None, wait: float, transfer: float
) -> None:
    """Log one fetch's size and split timing, the per-file view of a slow run (debug level)."""
    if not log.isEnabledFor(logging.DEBUG):
        return
    size = len(body) if body else 0
    rate = " at {:.1f} MB/s".format(size / transfer / 1_000_000) if transfer > 0 else ""
    log.debug(
        "fetch %s %s in %.2fs (wait %.2fs, transfer %.2fs%s) %s",
        status,
        human_bytes(size),
        wait + transfer,
        wait,
        transfer,
        rate,
        url,
    )


GATE_BUTTON_NAMES = (
    "Accept all",
    "Yes, I'm over 18",
    "I'm over 18",
    "Continue",
    "View NSFW content",
    "Yes",
)


@dataclass
class _HostPace:
    """What `HostPacer` knows about one host."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: monotonic start of the latest request
    last: float = float("-inf")
    #: monotonic time before which the host must not be asked at all, after a 429
    until: float = float("-inf")
    #: consecutive 429s, the exponent of the cooldown
    strikes: int = 0


class HostPacer:
    """
    Per-host request spacing, and the one place a host's 429 is waited out.

    Because the cooldown a refusal earns (`penalize`) lives here rather than in a retry loop, it holds every request
    aimed at that host: the retry of the refused call, whatever else is queued behind it, and other jobs' calls.
    """

    def __init__(self) -> None:
        self._hosts: dict[str, _HostPace] = {}

    def reset(self) -> None:
        """Drop all pacing state, including its loop-bound locks (called when the browser (re)starts)."""
        self._hosts.clear()

    def _pace(self, host: str) -> _HostPace:
        pace = self._hosts.get(host)
        if pace is None:
            pace = self._hosts[host] = _HostPace()
        return pace

    def penalize(
        self,
        host: str,
        *,
        base: float,
        cap: float,
        retry_after: float | None = None,
    ) -> float | None:
        """
        Record that ``host`` answered 429, and hold it until it has had its pause.

        The cooldown doubles with each consecutive refusal, and a longer ``Retry-After`` wins. A refusal arriving
        while a cooldown is already running came from a request that was in flight when it began: that is the same
        offense, not a further one, so it changes nothing.

        Args:
            host: The host that answered 429.
            base: Seconds to leave it alone after a first refusal (``config.rate_limit_backoff``).
            cap: Ceiling for that wait (``config.rate_limit_max_backoff``).
            retry_after: Seconds the response's ``Retry-After`` named.

        Returns:
            The cooldown this refusal started, or None when one was already running.
        """
        pace = self._pace(host)
        now = time.monotonic()
        if now < pace.until:
            return None
        pace.strikes += 1
        cooldown = min(cap, max(base * 2.0 ** (pace.strikes - 1), retry_after or 0.0))
        pace.until = now + cooldown
        return cooldown

    def succeeded(self, host: str) -> None:
        """Record that ``host`` answered, so its next refusal starts the cooldown over at ``base``."""
        pace = self._hosts.get(host)
        if pace is not None:
            pace.strikes = 0

    async def wait(self, host: str, interval: float) -> float:
        """
        Sleep until ``host`` may be asked again, and record this request's start.

        Args:
            host: The host about to be requested.
            interval: Minimum seconds between successive requests to it. Zero or less disables spacing, though not
                a cooldown `penalize` imposed.

        Returns:
            The seconds actually slept (0.0 when the host was already due).
        """
        pace = self._pace(host)
        async with pace.lock:
            now = time.monotonic()
            slept = max(0.0, max(pace.last + interval, pace.until) - now)
            if slept:
                await asyncio.sleep(slept)
                now = time.monotonic()
            pace.last = now
            return slept


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
        #: hosts that refused a direct request, so the browser is used for them from then on
        self._browser_only: set[str] = set()
        #: cookie header per host, so each one costs a single query to the Playwright context
        self._cookies: dict[str, str] = {}
        #: spaces out the API calls handlers make, per host
        self._pacer = HostPacer()

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
        self._pacer.reset()
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

    async def download(
        self,
        url: str,
        timeout_ms: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        """
        GET media bytes, preferring a direct request over the browser.

        The browser is slow for large files (buffering each response, then copying it out through Playwright's driver).
        A direct request skips that, while still carrying this context's cookies and the User-Agent.

        A host returning a transport error or a "not through this route" status is retried through the browser, and
        is blacklisted, so the wasted attempt happens only once. A 404 is not a routing problem and is reported as is.

        Downloads are spaced by the engine (``config.delay``), not here, but a host's 429 cooldown holds them like
        any other request to it (see `HostPacer`).

        Args:
            url: The media URL to fetch.
            timeout_ms: Request timeout; defaults to ``config.request_timeout_ms``.
            headers: Extra request headers this download needs (some CDNs answer 403 without a ``Referer``).
                Sent on whichever route is taken, layered over the User-Agent and cookies.

        Returns:
            The fetch outcome, whichever route produced it.
        """
        timeout = (
            timeout_ms if timeout_ms is not None else self._config.request_timeout_ms
        )
        host = urlparse(url).netloc.lower()
        await self._pacer.wait(host, 0.0)
        result = await self._download(url, host, timeout, headers)
        self._record_pace(host, result)
        return result

    async def _download(
        self,
        url: str,
        host: str,
        timeout: int,
        headers: Mapping[str, str] | None,
    ) -> FetchResult:
        """The route choice behind `download`: direct first, the browser for a host that refused it."""
        if not self._config.direct_download or host in self._browser_only:
            return await self._fetch(url, timeout, headers)
        result = await fetch_direct(
            url,
            headers=await self._direct_headers(url, host, headers),
            timeout_ms=timeout,
        )
        if not looks_blocked(result):
            return result
        self._browser_only.add(host)
        log.info(
            "%s refused a direct request (%s); using the browser for it from here on",
            host,
            result.error,
        )
        return await self._fetch(url, timeout, headers)

    async def _direct_headers(
        self, url: str, host: str, extra: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """Headers for a direct request: the configured User-Agent, the context's cookies, then ``extra`` on top."""
        headers = {"User-Agent": self._config.user_agent, "Accept": "*/*"}
        if host not in self._cookies:
            self._cookies[host] = await self.cookie_header(url)
        if self._cookies[host]:
            headers["Cookie"] = self._cookies[host]
        if extra:
            headers.update(extra)
        return headers

    async def cookies(self, url: str) -> list[dict[str, Any]]:
        """
        The context's cookies for ``url``, as Playwright's raw records.

        Args:
            url: The URL whose cookies are wanted.

        Returns:
            One mapping per cookie (``name``, ``value``, ``domain``, ...), or an empty list if the browser is not
            started or Playwright refuses the query.
        """
        if not self.started:
            return []
        try:
            return list(await self._context.cookies(url))
        except Exception:  # pragma: no cover
            return []

    async def cookie_header(self, url: str) -> str:
        """
        The context's cookies for ``url``, as a ready-to-send ``Cookie`` header value.

        Lets a download leave the browser while keeping the session (over-18 interstitial or logged-in NSFW accounts).

        Args:
            url: The URL whose cookies are wanted.

        Returns:
            ``"name=value; other=value"``,
            or "" if the browser is not started, the context has no cookies for the URL, or Playwright refuses the query
        """
        cookies = await self.cookies(url)
        return "; ".join(
            "{}={}".format(c["name"], c["value"])
            for c in cookies
            if c.get("name") and c.get("value") is not None
        )

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
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        """
        GET a URL through the browser context (cookies, UA, and all), no faster than the host's pace allows.

        This is the route for API calls, so it is where per-host politeness is enforced: successive requests to one
        host are held ``config.api_pause`` apart, whichever handler makes them. Media bytes go through `download`,
        which is spaced by the engine instead. Both routes wait out a host's 429 cooldown (see `HostPacer`).

        Args:
            url: The URL to fetch.
            timeout_ms: Request timeout; defaults to ``config.request_timeout_ms``.
            headers: Extra request headers (e.g. an ``Authorization`` bearer), merged over the context's own.

        Returns:
            The fetch outcome.

        Raises:
            BrowserError: If the browser has not been started.
        """
        host = urlparse(url).netloc.lower()
        await self._pacer.wait(host, self._config.api_pause)
        result = await self._fetch(url, timeout_ms, headers)
        self._record_pace(host, result)
        return result

    def _record_pace(self, host: str, result: FetchResult) -> None:
        """Feed one request's outcome back into the pacer, so a refusal holds every later request to that host."""
        cfg = self._config
        if is_rate_limited(result):
            cooldown = self._pacer.penalize(
                host,
                base=cfg.rate_limit_backoff,
                cap=cfg.rate_limit_max_backoff,
                retry_after=result.retry_after,
            )
            if cooldown is not None:
                log.info(
                    "%s is rate-limiting us (HTTP 429); holding every request to it for %.1fs",
                    host,
                    cooldown,
                )
        elif result.ok:
            self._pacer.succeeded(host)

    async def _fetch(
        self,
        url: str,
        timeout_ms: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> FetchResult:
        """
        GET a URL through the browser context without host pacing (see `fetch`).

        Transport errors and non-2xx responses are returned as an unsuccessful FetchResult rather than raised.
        The response is always disposed so Playwright does not hold the body in memory.

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
            kwargs["headers"] = dict(headers)
        started = time.perf_counter()
        try:
            resp = await self._context.request.get(url, **kwargs)
        except Exception as exc:
            return FetchResult(
                ok=False,
                error="error: {}".format(exc),
                wait=time.perf_counter() - started,
            )
        wait = time.perf_counter() - started
        try:
            ctype = (
                (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
            )
            body = await resp.body() if resp.ok else None
            transfer = time.perf_counter() - started - wait
            _log_fetch(url, resp.status, body, wait, transfer)
            return FetchResult(
                ok=resp.ok,
                status=resp.status,
                content_type=ctype,
                body=body,
                error=None if resp.ok else "HTTP {}".format(resp.status),
                wait=wait,
                transfer=transfer,
                retry_after=parse_retry_after(resp.headers.get("retry-after")),
            )
        except Exception as exc:
            return FetchResult(
                ok=False,
                error="error: {}".format(exc),
                wait=wait,
                transfer=time.perf_counter() - started - wait,
            )
        finally:
            # Playwright keeps response bodies in memory until the context closes unless they are disposed.
            try:
                await resp.dispose()
            except Exception:
                pass

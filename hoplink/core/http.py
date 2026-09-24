"""
Direct HTTP downloads from the Python process, bypassing Playwright's browser.

This module provides `fetch_direct` to perform HTTP GET requests using the standard library's `urllib.request`. It
runs in a worker thread to avoid blocking the event loop. The request carries cookies and User-Agent from the browser
session to maintain identity. Direct fetching eliminates the overhead of buffering responses in the browser process and
copying them back through the driver. If a direct request fails with a status indicating the server rejects this route
(e.g., 401, 403),the caller may retry through the browser. Transport errors and non-2xx responses are returned as
`FetchResult` with `ok=False`.

Timings are wall-clock and split into `wait` (time to first byte) and `transfer` (time to read the body).

`fetch_with_retries` is the one retry loop both routes share.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, NamedTuple, Optional

log = logging.getLogger(__name__)


class FetchResult(NamedTuple):
    """
    Outcome of a single HTTP fetch attempt.

    Attributes:
        ok: True if the request succeeded (HTTP 2xx and no transport error).
        status: HTTP status code, or 0 if the request never completed.
        content_type: Lowercased Content-Type without parameters.
        body: Response bytes on success, else None.
        error: Short error description on failure, else None.
        wait: Seconds until the response headers were received (time to first byte).
        transfer: Seconds spent reading the response body after headers arrived.
        retry_after: Seconds the host asked us to wait, read from its ``Retry-After`` header (None when it named none).
    """

    ok: bool
    status: int = 0
    content_type: str = ""
    body: bytes | None = None
    error: str | None = None
    wait: float = 0.0
    transfer: float = 0.0
    retry_after: float | None = None


#: Statuses that mean "not through this route": worth one browser retry.
BLOCKED_STATUSES = frozenset({0, 401, 403, 405, 406, 451})

#: "You are asking too often." Retryable like the rest, but only after a wait of a wholly different order.
RATE_LIMITED = 429

#: Non-2xx statuses a retry could plausibly clear, alongside anything >= 500. Status 0 is how a transport error
#: (timeout, connection reset, DNS failure) is reported by both fetch routes.
RETRY_STATUSES = frozenset({0, 408, 425, RATE_LIMITED})


def looks_blocked(result: FetchResult) -> bool:
    """Whether a failed direct fetch is worth retrying through the browser."""
    return not result.ok and result.status in BLOCKED_STATUSES


def is_transient(result: FetchResult) -> bool:
    """
    Whether a failed fetch is worth retrying.

    Args:
        result: The unsuccessful FetchResult to classify.

    Returns:
        True for faults a later attempt could plausibly survive: transport errors, rate limiting, and server-side
        errors. A 404 or 403 is the host's settled answer, so it is not retried.
    """
    return result.status in RETRY_STATUSES or result.status >= 500


def is_rate_limited(result: FetchResult) -> bool:
    """
    Whether a failed fetch was refused for asking too often.

    Args:
        result: The unsuccessful FetchResult to classify.

    Returns:
        True for HTTP 429. Such a failure is transient like a 5xx, but the pause it earns is the host's cooldown
        (see `HostPacer.penalize`), not the seconds-scale backoff a 5xx gets.
    """
    return result.status == RATE_LIMITED


def parse_retry_after(value: Optional[str]) -> Optional[float]:
    """
    Read a ``Retry-After`` header as seconds from now.

    Args:
        value: The raw header value, in either documented form: delta-seconds (``"30"``) or an HTTP date
            (``"Wed, 21 Oct 2015 07:28:00 GMT"``). None or blank yields None.

    Returns:
        Non-negative seconds to wait, or None when the header is absent or unparseable. A date already in the past
        yields 0.0, which is the host saying "now", not "never".
    """
    if not value or not value.strip():
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if when is None:  # pragma: no cover (only older Pythons return None here)
        return None
    # A date without a zone is UTC by the HTTP spec, and comparing naive to aware would raise.
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


async def fetch_with_retries(
    attempt: Callable[[], Awaitable[FetchResult]],
    *,
    retries: int,
    rate_limit_retries: int,
    backoff: float,
    floor: float,
    label: str,
) -> FetchResult:
    """
    Run ``attempt`` until it succeeds, fails for good, or runs out of retries.

    A transient failure (see `is_transient`) is retried after an exponential backoff. A rate limit is retried
    straight away, because the route itself holds the host until its cooldown is over (see `HostPacer`).

    Args:
        attempt: Makes one request.
        retries: Extra attempts after a transient failure.
        rate_limit_retries: Extra attempts after a 429, when that is more than ``retries``.
        backoff: Seconds before the first retry of a transient failure, doubling per retry.
        floor: The shortest wait between two attempts (the route's politeness delay).
        label: What is being fetched, for the log line.

    Returns:
        The last attempt's result, its ``wait`` and ``transfer`` summed over every attempt.
    """
    waited = transferred = 0.0
    tries = 0
    while True:
        tries += 1
        result = await attempt()
        waited += result.wait
        transferred += result.transfer
        result = result._replace(wait=waited, transfer=transferred)
        if result.ok or not is_transient(result):
            return result
        limited = is_rate_limited(result)
        allowed = max(retries, rate_limit_retries) if limited else retries
        if tries > allowed:
            return result
        if limited:
            log.info(
                "attempt %d/%d for %s was rate-limited; retrying once the host's cooldown is over",
                tries,
                allowed + 1,
                label,
            )
            continue
        delay = max(floor, backoff * (2 ** (tries - 1)))
        log.info(
            "attempt %d/%d for %s failed (%s); retrying in %.1fs",
            tries,
            allowed + 1,
            label,
            result.error,
            delay,
        )
        await asyncio.sleep(delay)


def _read(url: str, headers: Dict[str, str], timeout: float) -> FetchResult:
    """
    Fetch ``url`` with the standard library, timing the response and the body separately.

    Blocking; call it through `fetch_direct`. Unlike the browser path, ``wait`` here really is time to first byte,
    because ``urlopen`` returns as soon as the response head has arrived.
    """
    request = urllib.request.Request(url, headers=headers, method="GET")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            wait = time.perf_counter() - started
            ctype = (
                (response.headers.get("content-type") or "")
                .split(";")[0]
                .strip()
                .lower()
            )
            body = response.read()
            return FetchResult(
                ok=True,
                status=response.status,
                content_type=ctype,
                body=body,
                wait=wait,
                transfer=time.perf_counter() - started - wait,
            )
    except urllib.error.HTTPError as exc:
        # A non-2xx is an answer, not a failure to reach the host: keep its status so the caller can tell "forbidden here, try the browser" from "gone".
        return FetchResult(
            ok=False,
            status=exc.code,
            error="HTTP {}".format(exc.code),
            wait=time.perf_counter() - started,
            retry_after=parse_retry_after(exc.headers.get("Retry-After")),
        )
    except Exception as exc:
        # Status stays 0, which both the retry logic and `looks_blocked` read as a transport failure.
        return FetchResult(
            ok=False,
            error="error: {}".format(exc),
            wait=time.perf_counter() - started,
        )


async def fetch_direct(
    url: str, *, headers: Optional[Dict[str, str]] = None, timeout_ms: int
) -> FetchResult:
    """
    GET a URL from this process, off the event loop.

    Args:
        url: The URL to fetch.
        headers: Request headers to send (User-Agent, Cookie, ...).
        timeout_ms: Per-request timeout in milliseconds.

    Returns:
        The fetch outcome. Transport errors and non-2xx responses come back as an unsuccessful FetchResult rather than raised.
    """
    result = await asyncio.to_thread(
        _read, url, dict(headers or {}), timeout_ms / 1000.0
    )
    if log.isEnabledFor(logging.DEBUG):
        size = len(result.body) if result.body else 0
        log.debug(
            "direct %s %d B in %.2fs (wait %.2fs, transfer %.2fs) %s",
            result.status,
            size,
            result.wait + result.transfer,
            result.wait,
            result.transfer,
            url,
        )
    return result

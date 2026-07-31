"""
Direct HTTP downloads from the Python process, bypassing Playwright's browser.

This module provides `fetch_direct` to perform HTTP GET requests using the standard library's `urllib.request`. It
runs in a worker thread to avoid blocking the event loop. The request carries cookies and User-Agent from the browser
session to maintain identity. Direct fetching eliminates the overhead of buffering responses in the browser process and
copying them back through the driver. If a direct request fails with a status indicating the server rejects this route
(e.g., 401, 403),the caller may retry through the browser. Transport errors and non-2xx responses are returned as
`FetchResult` with `ok=False`.

Timings are wall-clock and split into `wait` (time to first byte) and `transfer` (time to read the body).
"""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.error
import urllib.request
from typing import Dict, NamedTuple, Optional

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
    """

    ok: bool
    status: int = 0
    content_type: str = ""
    body: bytes | None = None
    error: str | None = None
    wait: float = 0.0
    transfer: float = 0.0


#: Statuses that mean "not through this route": worth one browser retry.
BLOCKED_STATUSES = frozenset({0, 401, 403, 405, 406, 451})


def looks_blocked(result: FetchResult) -> bool:
    """Whether a failed direct fetch is worth retrying through the browser."""
    return not result.ok and result.status in BLOCKED_STATUSES


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
